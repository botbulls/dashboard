"""Estrategia del bot: chequeos por par con la regla de forager (spec bot-estrategia § 3.2).

Repite, sobre la API pública del **mismo endpoint que opera** (Demo con ``BINANCE_TESTNET``), los filtros con los que
forager descarta un par en silencio, más la config que elegiría (manifiesto de passivbot, ``futuresboard.manifiesto``):

==================  =====================================================================================  ==========
Chequeo (clave)     Qué mira                                                                               Si falla
==================  =====================================================================================  ==========
``formato``         ``^[A-Z0-9]{1,20}USDT$`` (la spec decía {2,20}: TUSDT y SUSDT existen)                  bloquea
``mercado``         existe en ``exchangeInfo``, ``PERPETUAL``, cotiza en USDT, ``TRADING``                 bloquea
``config``          forager elegiría ``<SYM>.json`` y es clock (colisiones: TUSDT → ANTUSDT.json)          bloquea
``edad``            primera vela **1M** anterior a ``market_age_threshold`` (``onboardDate`` de Demo es    bloquea
                    falso), comparada con ``<`` como forager
``minimo``          ``max(minNotional, minQty × precio)`` ≤ ``max_min_cost``; en Demo informa además el    bloquea
                    de Binance real
``velas``           500 velas de 15 m (forager descarta los que tienen menos, ``forager.py:518``)         bloquea
``lista_negra``     no está en ``symbols_to_ignore``                                                       bloquea
``medido``          es uno de los 4 del backtest (BNB, BTC, ETH, XRP)                                      aviso
``config_medida``   md5 de la config igual al medido en el backtest                                        aviso
``primera_compra``  ``balance × lw × qty_pct_entry`` contra el mínimo (Demo y real)                        aviso
==================  =====================================================================================  ==========

Estados de un chequeo: ``ok``, ``info``, ``aviso``, ``bloquea`` y ``sin_datos`` (Binance no respondió: para agregar un
par cuenta como bloqueo, "sin verificar"). Los textos van en castellano y con números es-AR: el navegador los muestra
con ``textContent``.

Herramienta, no asesor: los chequeos dicen qué haría forager, no qué conviene operar.
"""
from __future__ import annotations

import calendar
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from futuresboard import binance_publico
from futuresboard import bot_control
from futuresboard import formato
from futuresboard import manifiesto

# Los 4 pares del backtest multianual y el md5 de su config clock medida (botbulls-lab/passivbot @520d3614a, los que
# están en la imagen de passivbot).
MD5_MEDIDOS: Dict[str, str] = {
    "BNBUSDT": "dbefc8b810b3f88ba4bde470f120e4f9",
    "BTCUSDT": "e78895e6dea72b4f12b7432fbbe1f482",
    "ETHUSDT": "60a54e29cedf0f69c2820efc622810de",
    "XRPUSDT": "45570c293fdcf563cbbc7c833a47afb6",
}
PARES_MEDIDOS = tuple(sorted(MD5_MEDIDOS))
FORMATO = re.compile(r"^[A-Z0-9]{1,20}USDT$")
MAX_PARES = 8
VELAS_MINIMAS = binance_publico.VELAS_FORAGER
# Tope para chequear varios pares en un pedido web (gunicorn corta a los 60 s y es el único worker): lo que no llega
# a tiempo queda "sin verificar" y se sigue pidiendo en segundo plano (la próxima vez sale de la caché).
PRESUPUESTO_S = 12.0
HILOS = 4
# Chequeos que bloquean agregar un par (los mismos con los que forager lo descartaría).
BLOQUEANTES = ("formato", "mercado", "config", "edad", "minimo", "velas", "lista_negra")
# Los que corren en cada ciclo sobre los aprobados (Bot → Pares ahora): no necesitan el manifiesto.
DE_CICLO = ("mercado", "minimo", "edad", "velas", "lista_negra")

OK, INFO, AVISO, BLOQUEA, SIN_DATOS = "ok", "info", "aviso", "bloquea", "sin_datos"


def _n(valor: Any, dec: Optional[int] = None) -> str:
    return formato.num(valor, dec)


def corto(symbol: str) -> str:
    return symbol[:-4] if isinstance(symbol, str) and symbol.endswith("USDT") else str(symbol)


# --------------------------------------------------------------------------------------
# Edad: market_age_threshold como lo lee forager (dateutil.parser.parse)
# --------------------------------------------------------------------------------------

_FECHA = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2})(?:[T ](\d{1,2})(?::(\d{2})(?::(\d{2}))?)?)?)?)?Z?$")


def umbral_edad_ms(valor: Any, hoy: dt.date) -> Optional[int]:
    """``market_age_threshold`` en ms, o None si forager no filtra por edad (0, vacío o inválido).

    forager usa ``dateutil.parser.parse``: lo que falta lo completa con la fecha de hoy ("2023-01" un 25 de octubre
    es 2023-01-25; "2023" es 2023-10-25) y un día que no existe en el mes queda en el último."""
    if valor in (None, 0, 0.0, "0", "") or isinstance(valor, bool):
        return None
    m = _FECHA.match(str(valor).strip())
    if not m:
        return None
    anio = int(m.group(1))
    mes = int(m.group(2)) if m.group(2) else hoy.month
    if not 1 <= mes <= 12:
        return None
    dia = int(m.group(3)) if m.group(3) else min(hoy.day, calendar.monthrange(anio, mes)[1])
    hora, minuto, seg = (int(m.group(i)) if m.group(i) else 0 for i in (4, 5, 6))
    try:
        momento = dt.datetime(anio, mes, dia, hora, minuto, seg, tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return int(momento.timestamp() * 1000)


def edad_ok(primera_ms: Optional[int], umbral: Any, hoy: dt.date) -> bool:
    limite = umbral_edad_ms(umbral, hoy)
    if limite is None:
        return True
    return primera_ms is not None and primera_ms < limite


def _mes(ms: int) -> str:
    momento = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
    return f"{momento:%m/%Y}"


# --------------------------------------------------------------------------------------
# Contexto de los chequeos
# --------------------------------------------------------------------------------------


@dataclass
class Contexto:
    cfg: Dict[str, Any]
    operativo: binance_publico.Mercado
    real: binance_publico.Mercado
    manifiesto: Dict[str, Any]
    balance: Optional[float] = None  # balance para dimensionar: max(0, billetera − préstamo del rescate)
    hoy: dt.date = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).date())

    @classmethod
    def armar(cls, cfg: Dict[str, Any], forager_config: Optional[pathlib.Path], testnet: bool,
              base_operativa: Optional[str], balance: Optional[float] = None,
              hoy: Optional[dt.date] = None, manif: Optional[Dict[str, Any]] = None) -> "Contexto":
        operativo, real = binance_publico.mercados(testnet, base_operativa)
        return cls(cfg=cfg, operativo=operativo, real=real,
                   manifiesto=manif if manif is not None else manifiesto.leer(forager_config),
                   balance=balance, hoy=hoy or dt.datetime.now(dt.timezone.utc).date())

    @property
    def en_demo(self) -> bool:
        return self.operativo is not self.real

    @property
    def max_min_cost(self) -> Optional[float]:
        v = self.cfg.get("max_min_cost")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        return int(v) if float(v).is_integer() else float(v)  # 150, no "150,0"


def _item(clave: str, estado: str, texto: str, **datos: Any) -> Dict[str, Any]:
    out = {"clave": clave, "estado": estado, "texto": texto}
    if datos:
        out["datos"] = datos
    return out


def _minimo(m: binance_publico.Mercado, symbol: str) -> Optional[Dict[str, float]]:
    """``max(minNotional, minQty × precio)`` como forager (``forager.py:412``). None si Binance no tiene el par."""
    info = m.simbolos().get(symbol)
    precio = m.precios().get(symbol)
    if info is None or precio is None:
        return None
    return {"minimo": max(info["min_notional"], info["min_qty"] * precio), "min_qty": info["min_qty"],
            "min_notional": info["min_notional"], "precio": precio}


def _chequeo_mercado(symbol: str, ctx: Contexto) -> Dict[str, Any]:
    nombre = ctx.operativo.nombre
    info = ctx.operativo.simbolos().get(symbol)
    if info is None:
        return _item("mercado", BLOQUEA, f"No existe en Binance ({nombre})")
    if info.get("contractType") != "PERPETUAL" or info.get("quoteAsset") != "USDT":
        return _item("mercado", BLOQUEA, f"No es un perpetuo USDT en Binance ({nombre}): tipo {info.get('contractType')}, "
                                         f"cotiza en {info.get('quoteAsset')}")
    if info.get("status") != "TRADING":
        return _item("mercado", BLOQUEA, f"Binance ({nombre}) lo tiene en {info.get('status')}: no se opera")
    return _item("mercado", OK, f"Perpetuo USDT en Binance ({nombre}), en TRADING")


def _chequeo_config(symbol: str, ctx: Contexto) -> Dict[str, Any]:
    m = ctx.manifiesto
    if not m.get("usable"):
        return _item("config", BLOQUEA, f"Sin verificar la config: {m.get('motivo') or 'sin manifiesto de configs.'}")
    info = manifiesto.config_de(m, symbol)
    if info is None:
        return _item("config", BLOQUEA, f"forager no conoce {symbol}: no está en los mercados que cargó")
    archivo = info.get("file") or "?"
    if info.get("source") == "default":
        nombre = info.get("config_name") or "?"
        return _item("config", BLOQUEA, f"Sin config clock propia: forager usaría {archivo} (grilla {nombre})"
                     if not info.get("is_clock") else f"Sin config propia: forager usaría {archivo} ({nombre})")
    if not info.get("own_file"):
        return _item("config", BLOQUEA, f"forager usaría {archivo} (coincide por el nombre): no es la config de {symbol}")
    if not info.get("is_clock"):
        return _item("config", BLOQUEA, f"{archivo} no es clock (modo {info.get('passivbot_mode') or '?'})")
    if info.get("error"):
        return _item("config", BLOQUEA, f"{archivo}: passivbot no la pudo leer ({info.get('error')})")
    return _item("config", OK, f"Config clock propia: {archivo} (forager elige esa, no otra por el nombre)")


def _chequeos_medido(symbol: str, ctx: Contexto) -> List[Dict[str, Any]]:
    out = []
    if symbol in MD5_MEDIDOS:
        out.append(_item("medido", OK, "Medido en el backtest"))
    else:
        out.append(_item("medido", AVISO, "Sin backtest de BotBulls: los números de los perfiles no aplican"))
        return out
    info = manifiesto.config_de(ctx.manifiesto, symbol)
    if info is None:
        out.append(_item("config_medida", INFO, "Config sin comparar con la medida (sin manifiesto de configs)"))
    elif info.get("md5") == MD5_MEDIDOS[symbol] and manifiesto.config_propia_clock(info):
        out.append(_item("config_medida", OK, "Config clock = la medida"))
    else:
        out.append(_item("config_medida", AVISO, "Config distinta de la medida en el backtest (md5): sin backtest"))
    return out


def _chequeo_edad(symbol: str, ctx: Contexto) -> Dict[str, Any]:
    umbral = ctx.cfg.get("market_age_threshold")
    limite = umbral_edad_ms(umbral, ctx.hoy)
    primera = ctx.operativo.primera_vela_1m(symbol)
    if limite is None:
        return _item("edad", OK, f"forager no filtra por antigüedad{f' (en el mercado desde {_mes(primera)})' if primera else ''}")
    if primera is None:
        return _item("edad", BLOQUEA, f"Sin velas mensuales en Binance ({ctx.operativo.nombre}): forager lo descarta")
    if primera < limite:
        return _item("edad", OK, f"En el mercado desde {_mes(primera)} (forager pide antes de {umbral})")
    return _item("edad", BLOQUEA, f"En el mercado desde {_mes(primera)}: forager lo descarta (umbral {umbral})")


def _chequeo_minimo(symbol: str, ctx: Contexto) -> tuple:
    tope = ctx.max_min_cost
    op = _minimo(ctx.operativo, symbol)
    if op is None:
        return _item("minimo", BLOQUEA, f"Sin precio en Binance ({ctx.operativo.nombre})"), None
    real = _minimo(ctx.real, symbol) if ctx.en_demo else op
    vals = {"operativo": op["minimo"], "real": real["minimo"] if real else None}
    if tope is None:
        return _item("minimo", INFO, f"Mínimo {_n(op['minimo'], 2)} (max_min_cost sin definir)"), vals
    texto = f"Mínimo {_n(op['minimo'], 2)} de {_n(tope)}"
    if op["minimo"] > tope:
        return _item("minimo", BLOQUEA, f"{texto}: forager lo descarta (max_min_cost)"), vals
    extra = ""
    umbral_info = real if real is not None else op
    if umbral_info["min_qty"] > 0:
        precio_tope = tope / umbral_info["min_qty"]
        extra = f"forager lo saca si {corto(symbol)} pasa de {_n(round(precio_tope))}"
    if ctx.en_demo and real is not None and real["minimo"] > tope:
        return _item("minimo", AVISO, f"{texto} (real {_n(real['minimo'], 2)}: en Binance real forager lo descartaría)"), vals
    # Demo y real con mínimos distintos de verdad (BTC: minQty 0,0001 contra 0,001), no por la diferencia de precio.
    if ctx.en_demo and real is not None and abs(real["minimo"] - op["minimo"]) > 0.1 * max(op["minimo"], 1e-9):
        return _item("minimo", INFO, f"{texto} (real {_n(real['minimo'], 2)}: {extra})"), vals
    if max(op["minimo"], (real or op)["minimo"]) > tope / 2:
        return _item("minimo", INFO, f"{texto} ({extra})"), vals
    return _item("minimo", OK, texto), vals


def _chequeo_velas(symbol: str, ctx: Contexto) -> Dict[str, Any]:
    n = len(ctx.operativo.velas_15m(symbol))
    if n < VELAS_MINIMAS:
        return _item("velas", BLOQUEA, f"Solo {n} velas de 15 m: forager lo descarta (pide {VELAS_MINIMAS})")
    return _item("velas", OK, f"Historial completo: {VELAS_MINIMAS} velas de 15 m (forager descarta en silencio los "
                              "que tienen menos)")


def _chequeo_lista_negra(symbol: str, ctx: Contexto) -> Dict[str, Any]:
    ignorar = ctx.cfg.get("symbols_to_ignore")
    if isinstance(ignorar, list) and symbol in ignorar:
        return _item("lista_negra", BLOQUEA, "Está en la lista negra (symbols_to_ignore): forager no lo opera y una "
                                             "posición suya queda sin bot")
    return _item("lista_negra", OK, "No está en la lista negra")


def primera_compra(balance: Optional[float], lw: Optional[float], qty_pct: Optional[float],
                   minimo: Optional[float], minimo_real: Optional[float], en_demo: bool) -> Dict[str, Any]:
    """1.ª compra en clock = balance × lw × qty_pct_entry (``njit_funcs.calc_clock_qty`` con exposición 0); si queda
    debajo del mínimo de Binance, passivbot compra el mínimo (``calc_min_entry_qty``)."""
    datos = {"qty_pct": qty_pct, "min": minimo, "min_real": minimo_real, "balance": balance, "en_demo": en_demo}
    if not balance or not lw or not qty_pct or minimo is None:
        return _item("primera_compra", INFO, "1.ª compra sin calcular (falta el balance, la config o el mínimo)", **datos)
    compra = balance * lw * qty_pct
    texto = f"1.ª compra {_n(compra, 1)} USDT"
    if compra < minimo:
        extra = f" / {_n(minimo_real, 2)} real" if en_demo and minimo_real is not None else ""
        return _item("primera_compra", AVISO, f"{texto}, menor que el mínimo ({_n(minimo, 2)}{' Demo' if en_demo else ''}"
                                              f"{extra}): compra el mínimo", **datos)
    if en_demo and minimo_real is not None and compra < minimo_real:
        return _item("primera_compra", AVISO, f"{texto}: en real compraría el mínimo, {_n(minimo_real, 2)}", **datos)
    return _item("primera_compra", OK, f"{texto} (mín. {_n(minimo, 2)})", **datos)


def _seguro(clave: str, fn, *args) -> Any:
    try:
        return fn(*args)
    except binance_publico.SinRespuesta as exc:
        return _item(clave, SIN_DATOS, f"No se pudo consultar Binance: {exc}. Reintentá en un rato.")


def _estado(chequeos: List[Dict[str, Any]]) -> str:
    estados = {c["estado"] for c in chequeos}
    for e in (BLOQUEA, SIN_DATOS, AVISO):
        if e in estados:
            return e
    return OK


def chequear_par(symbol: Any, ctx: Contexto, lw: Optional[float] = None,
                 claves: Optional[tuple] = None) -> Dict[str, Any]:
    """Chequeos de ``symbol`` (todos, o solo ``claves``). ``lw``: la del borrador, para la 1.ª compra.

    ``bloquea``: algún chequeo bloqueante falló o no se pudo verificar (``sin_datos``): así no se puede agregar."""
    sym = symbol if isinstance(symbol, str) else str(symbol)
    quiere = (lambda c: True) if claves is None else (lambda c: c in claves)
    chequeos: List[Dict[str, Any]] = []
    out: Dict[str, Any] = {"symbol": sym, "chequeos": chequeos, "minimo": None, "sin_backtest": sym not in MD5_MEDIDOS}
    if not FORMATO.match(sym) or sym != sym.strip():
        chequeos.append(_item("formato", BLOQUEA, "Formato inválido: tiene que ser como BTCUSDT (A-Z y 0-9, termina "
                                                  "en USDT)"))
        out.update(estado=BLOQUEA, bloquea=True, motivos=[chequeos[0]["texto"]])
        return out
    if quiere("mercado"):
        chequeos.append(_seguro("mercado", _chequeo_mercado, sym, ctx))
    if quiere("config"):
        chequeos.append(_chequeo_config(sym, ctx))
    if claves is None:
        medidos = _chequeos_medido(sym, ctx)
        chequeos.extend(medidos)
        out["sin_backtest"] = any(c["estado"] == AVISO for c in medidos)
    if quiere("edad"):
        chequeos.append(_seguro("edad", _chequeo_edad, sym, ctx))
    if quiere("minimo"):
        try:
            item, vals = _chequeo_minimo(sym, ctx)
        except binance_publico.SinRespuesta as exc:
            item, vals = _item("minimo", SIN_DATOS, f"No se pudo consultar Binance: {exc}. Reintentá en un rato."), None
        chequeos.append(item)
        out["minimo"] = vals
    if quiere("velas"):
        chequeos.append(_seguro("velas", _chequeo_velas, sym, ctx))
    if quiere("lista_negra"):
        chequeos.append(_chequeo_lista_negra(sym, ctx))
    if claves is None:
        info = manifiesto.config_de(ctx.manifiesto, sym)
        info = info if manifiesto.config_propia_clock(info) else None
        vals = out["minimo"] or {}
        chequeos.append(primera_compra(ctx.balance, lw, (info or {}).get("qty_pct_entry_long"), vals.get("operativo"),
                                       vals.get("real"), ctx.en_demo))
    bloqueantes = [c for c in chequeos if c["clave"] in BLOQUEANTES and c["estado"] in (BLOQUEA, SIN_DATOS)]
    out["estado"] = _estado(chequeos)
    out["bloquea"] = bool(bloqueantes)
    out["motivos"] = [c["texto"] for c in bloqueantes]
    return out


def _sin_verificar(symbol: str) -> Dict[str, Any]:
    texto = "Binance tarda en responder: se sigue consultando. Reintentá en unos segundos."
    return {"symbol": symbol, "chequeos": [_item("mercado", SIN_DATOS, texto)], "minimo": None,
            "sin_backtest": symbol not in MD5_MEDIDOS, "estado": SIN_DATOS, "bloquea": True, "motivos": [texto]}


def chequear_varios(symbols: List[str], ctx: Contexto, lw: Optional[float] = None, claves: Optional[tuple] = None,
                    presupuesto_s: float = PRESUPUESTO_S) -> List[Dict[str, Any]]:
    """``chequear_par`` de varios en paralelo, con un tope de tiempo total. Lo que no terminó a tiempo vuelve
    ``sin_datos`` (sigue en segundo plano y llena la caché). Mismo orden que ``symbols``."""
    if not symbols:
        return []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=HILOS, thread_name_prefix="estrategia")
    try:
        futuros = [pool.submit(chequear_par, s, ctx, lw, claves) for s in symbols]
        concurrent.futures.wait(futuros, timeout=presupuesto_s)
        out = []
        for s, f in zip(symbols, futuros):
            out.append(f.result() if f.done() else _sin_verificar(s))
        return out
    finally:
        pool.shutdown(wait=False)


# --------------------------------------------------------------------------------------
# Exposición: pares con posición (P) y con órdenes abiertas, según la base del scraper
# --------------------------------------------------------------------------------------


def leer_exposicion(db_path: Any) -> Dict[str, Any]:
    """Posiciones long y órdenes abiertas por par, de la base del scraper (lo que forager ve como
    ``current_positions_long`` y ``current_open_orders_long``: una orden con ``positionSide`` LONG o BOTH cuenta como
    long, ``forager.get_current_symbols``). Abre la base en solo lectura: sirve también en el hilo de un job.

    ``error``: no se pudo leer (sin posiciones no se puede correr la regla de exposición: el plan bloquea)."""
    from futuresboard import db

    out: Dict[str, Any] = {"posiciones": {}, "nocional": {}, "ordenes": {}, "error": None}
    try:
        conn = db.connect(db_path, readonly=True)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"No se pudo abrir la base del scraper ({exc.__class__.__name__})."
        return out
    try:
        for symbol, lado, cantidad, entrada in conn.execute(
                "SELECT symbol, positionSide, positionAmt, entryPrice FROM positions WHERE ABS(positionAmt) > 0"):
            lado = str(lado or "BOTH").upper()
            qty = float(cantidad or 0)
            if lado == "LONG" or (lado == "BOTH" and qty > 0):
                out["posiciones"][symbol] = out["posiciones"].get(symbol, 0.0) + abs(qty)
                out["nocional"][symbol] = out["nocional"].get(symbol, 0.0) + abs(qty * float(entrada or 0))
        for symbol, lado in conn.execute("SELECT symbol, positionSide FROM orders"):
            if str(lado or "BOTH").upper() != "SHORT":
                out["ordenes"][symbol] = out["ordenes"].get(symbol, 0) + 1
    except Exception as exc:  # noqa: BLE001 - tabla faltante, base bloqueada, etc.
        out["error"] = f"No se pudo leer posiciones y órdenes de la base del scraper ({exc.__class__.__name__})."
    finally:
        conn.close()
    return out


def lugares(pares_nuevos: List[str], expo: Dict[str, Any], ignorar: Optional[List[str]] = None) -> Dict[str, Any]:
    """La regla de exposición (spec § 3.2): ``n_longs`` entre ``|P ∪ O|`` y ``pares + salen con posición``.

    P = pares con posición, estén o no en la lista nueva (forager los sigue en parada gradual con la misma lw: si
    ``n_longs`` bajara, la lw subiría también para ellos). O = pares de la lista nueva con órdenes abiertas (una orden
    ocupa lugar, ``forager.py:130``). Los que salen con solo órdenes no cuentan: sin posición, en parada gradual no
    compran y la orden se cancela. Los de ``symbols_to_ignore`` no cuentan: forager los saca de sus posiciones."""
    ign = set(ignorar or [])
    nuevos = [p for p in pares_nuevos]
    P = sorted(s for s in expo.get("posiciones", {}) if s not in ign)
    O = sorted(s for s in expo.get("ordenes", {}) if s in nuevos and s not in ign)
    ocupados = sorted(set(P) | set(O))
    salen = sorted(s for s in P if s not in nuevos)
    return {"con_posicion": P, "con_ordenes": O, "ocupados": ocupados, "salen_con_posicion": salen,
            "minimo": max(1, len(ocupados)), "maximo": len(nuevos) + len(salen)}


# --------------------------------------------------------------------------------------
# Vista de Estrategia: derivados, avanzado (solo lectura) y pares con chequeos
# --------------------------------------------------------------------------------------

# Mantenimiento aproximado que se resta a 1/TWE en "caída que liquida" (≈ 24 % con TWE 4): es una cifra para
# dimensionar, no la fórmula de Binance (depende del par y del tramo de nocional).
MANTENIMIENTO_APROX = 0.01


def numero(valor: Any) -> Optional[float]:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return float(valor)


def derivados(twe: Any, n_longs: Any, balance: Optional[float], leverage: Any,
              nocional: Optional[float] = None) -> Dict[str, Any]:
    """Consecuencias de TWE y pares a la vez (spec § 2.4, paso 2). ``balance``: el de dimensionar."""
    t, n, lev = numero(twe), numero(n_longs), numero(leverage)
    out: Dict[str, Any] = {"lw": None, "max_por_par": None, "max_total": None, "caida_liquida_pct": None,
                           "sin_margen_upnl_pct": None, "margen_inicial_pct": None, "en_uso": nocional,
                           "en_uso_pct_balance": None, "en_uso_pct_max": None}
    if t and n:
        out["lw"] = t / n
    if t:
        out["caida_liquida_pct"] = max(0.0, (1 / t - MANTENIMIENTO_APROX) * 100)
        if lev:
            out["margen_inicial_pct"] = min(100.0, t / lev * 100)
            out["sin_margen_upnl_pct"] = -max(0.0, 100 - out["margen_inicial_pct"])
    if balance is not None and t:
        out["max_total"] = balance * t
        if out["lw"] is not None:
            out["max_por_par"] = balance * out["lw"]
    if balance and nocional is not None:
        out["en_uso_pct_balance"] = nocional / balance * 100
        if out["max_total"]:
            out["en_uso_pct_max"] = nocional / out["max_total"] * 100
    return out


def _valor(v: Any) -> str:
    if isinstance(v, list):
        return f"{len(v)} pares" if v else "(vacía)"
    if isinstance(v, dict):
        return f"{len(v)} entradas" if v else "(vacío)"
    if isinstance(v, bool):
        return "sí" if v else "no"
    if isinstance(v, (int, float)):
        return _n(v)
    return str(v) if v not in (None, "") else "—"


def avanzado(cfg: Dict[str, Any], manif: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Lo que también decide qué opera forager, en solo lectura (spec § 2.4 paso 5 y § 3.1)."""
    datos = manif.get("datos") or {}
    default = datos.get("default_config") or {}
    n_clock = sum(1 for f in (datos.get("files") or {}).values() if isinstance(f, dict) and f.get("is_clock"))
    filas = [
        ("leverage", "Apalancamiento", cfg.get("leverage"),
         f"Fija el margen inicial: con todo al máximo, TWE ÷ {_valor(cfg.get('leverage'))} del balance. No cambia la "
         "exposición ni la caída que liquida: las fija la TWE (margen cruzado)."),
        ("max_min_cost", "Mínimo más caro aceptado", f"{_valor(cfg.get('max_min_cost'))} USDT",
         "forager descarta los pares cuyo mínimo de Binance, max(minNotional, minQty × precio), es mayor."),
        ("market_age_threshold", "Antigüedad del mercado", cfg.get("market_age_threshold"),
         "Descarta los pares listados después, por su primera vela mensual (ej.: SUIUSDT, 06/2023, queda afuera)."),
        ("volume_clip_threshold / unilateralness_clip_threshold / noisiness_clip_threshold",
         "Filtros de volumen / unilateralidad / ruido",
         " / ".join(_valor(cfg.get(k)) for k in ("volume_clip_threshold", "unilateralness_clip_threshold",
                                                   "noisiness_clip_threshold")),
         "Con más pares que lugares se queda con esa fracción (nunca menos que los lugares). Con tantos pares como "
         "lugares no descartan ninguno."),
        ("ohlcv_interval / update_interval_minutes", "Velas y ciclo",
         f"{_valor(cfg.get('ohlcv_interval'))} / {_valor(cfg.get('update_interval_minutes'))} min",
         "Cada ciclo forager vuelve a elegir los pares y relanza los bots."),
        ("price_distance_threshold", "Distancia de precio", cfg.get("price_distance_threshold"),
         "No pone órdenes más lejos que esa fracción del precio."),
        ("live_configs_dir_long", "Configs por par", cfg.get("live_configs_dir_long"),
         (f"{n_clock} configs clock adentro de la imagen de passivbot (manifiesto); las de BNB, BTC, ETH y XRP se "
          "comparan por md5 con las del backtest.") if manif.get("usable") else
         "Están adentro de la imagen de passivbot: el panel las ve por el manifiesto de configs, que todavía no está."),
        ("default_config_path", "Config por defecto", cfg.get("default_config_path"),
         (f"{default.get('config_name') or '?'}: {'clock' if default.get('is_clock') else 'no es clock'}. Por eso no se "
          "aceptan pares sin config propia.") if default else
         "La que usaría un par sin config propia: por eso no se aceptan pares sin config clock propia."),
        ("symbols_to_ignore", "Lista negra", cfg.get("symbols_to_ignore"),
         ", ".join(corto(s) for s in cfg.get("symbols_to_ignore") or [] if isinstance(s, str))
         + ". Un par con posición en esta lista queda sin gestión."),
        ("no_long / no_short", "no_long / no_short", cfg.get("no_long"),
         "forager no las lee: no tienen efecto. Se muestran para que no confundan."),
    ]
    return [{"clave": c, "titulo": t, "valor": _valor(v), "efecto": e} for c, t, v, e in filas]


def balance_dimensionar(db_path: Any, data_dir: pathlib.Path) -> Dict[str, Any]:
    """Balance para dimensionar = max(0, billetera − préstamo del rescate) (``passivbot.py:404-410``). La billetera
    sale de la base del scraper (solo lectura) y el préstamo del estado del rescate. Nunca lanza."""
    from futuresboard import db
    from futuresboard import rescate

    billetera = None
    try:
        conn = db.connect(db_path, readonly=True)
        try:
            fila = conn.execute("SELECT totalWalletBalance FROM account WHERE AID = 1").fetchone()
        finally:
            conn.close()
        billetera = float(fila[0]) if fila is not None and fila[0] is not None else None
    except Exception:  # noqa: BLE001
        billetera = None
    try:
        prestamo = float(rescate.dec(rescate.panel_status(data_dir).get("prestamo") or "0"))
    except Exception:  # noqa: BLE001
        prestamo = 0.0
    dim = max(0.0, billetera - prestamo) if billetera is not None else None
    return {"billetera": billetera, "prestamo": prestamo, "para_dimensionar": dim}


# --------------------------------------------------------------------------------------
# Plan y aplicación (spec § 3.3): el mismo camino para Estrategia, un perfil y Volver a una versión
# --------------------------------------------------------------------------------------

VERSIONES_FILE = "estrategia_versiones.jsonl"
MAX_VERSIONES = 200
ESTRATEGIA_STEPS: List[tuple] = [
    ("validar", "Validar estrategia, pares y lugares"),
    ("backup", "Backup de la config"),
    ("escribir", "Escribir config"),
    ("docker", "Reiniciar passivbot"),
    ("verificar", "Verificar contenedor en ejecución"),
    ("forager", "Esperando a forager"),
]
_CAMPOS = ("twe_long", "n_longs", "pares", "base_hash", "confirmar", "plan_hash")
_HASH = re.compile(r"^[0-9a-f]{64}$")
BASE_VIEJA = ("La estrategia cambió desde que se armó el borrador (otra pestaña, un perfil, una versión o un cambio a "
              "mano): recargá Estrategia y revisá de nuevo.")


class EstrategiaError(bot_control.BotControlError):
    """No se puede aplicar (409): ``bloqueos`` dice por qué, uno por renglón. Es un ``BotControlError``: en el job
    termina como error 409 con su texto."""

    status_code = 409

    def __init__(self, bloqueos: List[str], plan: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(" ".join(bloqueos), payload={"bloqueos": list(bloqueos)})
        self.bloqueos = list(bloqueos)
        self.plan = plan


def validar_objetivo(body: Any, extra: tuple = ()) -> Dict[str, Any]:
    """``{twe_long, n_longs, pares, base_hash, confirmar?, plan_hash?}`` con sus tipos (400 si no). Lo que depende de
    la cuenta (formato de cada par, duplicados, cuántos, lugares) lo decide el plan (409 con motivo)."""
    if not isinstance(body, dict):
        raise ValueError("Body inválido: se esperaba un objeto JSON.")
    desconocidos = sorted(set(body) - set(_CAMPOS) - set(extra))
    if desconocidos:
        raise ValueError(f"Campos desconocidos: {', '.join(desconocidos)}.")
    twe = bot_control.validate_twe_manual(body.get("twe_long"))
    n = body.get("n_longs")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 2 * MAX_PARES:
        raise ValueError(f"n_longs tiene que ser un entero entre 1 y {2 * MAX_PARES}.")
    pares = body.get("pares")
    if not isinstance(pares, list) or not all(isinstance(p, str) and len(p) <= 32 for p in pares) or len(pares) > 32:
        raise ValueError("pares tiene que ser una lista de símbolos (texto).")
    base = body.get("base_hash")
    if not isinstance(base, str) or not _HASH.match(base):
        raise ValueError("base_hash inválido: recargá Estrategia.")
    confirmar = body.get("confirmar", False)
    if not isinstance(confirmar, bool):
        raise ValueError("confirmar tiene que ser true o false.")
    plan_hash = body.get("plan_hash")
    if plan_hash is not None and (not isinstance(plan_hash, str) or not _HASH.match(plan_hash)):
        raise ValueError("plan_hash inválido.")
    return {"twe_long": twe, "n_longs": n, "pares": list(pares), "base_hash": base, "confirmar": confirmar,
            "plan_hash": plan_hash}


def params_audit(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Lo que va a ``bot_actions.log`` (el plan y el job con los mismos: el historial los empareja)."""
    return {"twe_long": bot_control.twe_valor(obj["twe_long"]), "n_longs": obj["n_longs"], "pares": list(obj["pares"])}


@dataclass
class Entrada:
    """Lo que necesita el plan, leído en el momento (en el pedido web o, con el lock, en el job)."""

    cfg: Dict[str, Any]
    archivo_sha: str
    objetivo: Dict[str, Any]
    ctx: Contexto
    expo: Dict[str, Any]
    rescate_cfg: Dict[str, Any]
    running: bool
    container: str
    detectar: Any  # cfg -> nombre de perfil o None (perfiles.detectar con el rescate y el manifiesto)
    nombres_perfil: Dict[str, str]
    warmup_s: int = 90
    bloqueo_rescate: Optional[str] = None
    origen: str = "estrategia"
    titulo: str = "Revisar y aplicar la estrategia"
    todos_entran: bool = False  # Volver: cada par de la versión se chequea como si entrara


def _pares_texto(pares: List[str]) -> str:
    cortos = [corto(p) for p in pares]
    return ", ".join(cortos) if cortos else "ninguno"


def _num_txt(v: Optional[float], dec: int = 2) -> str:
    return _n(v, dec) if v is not None else "—"


def _usdt(v: Optional[float]) -> str:
    return f"{_n(v, 2)} USDT" if v is not None else "—"


def _fila(clave: str, titulo: str, sub: str, antes: Any, despues: Any, texto_antes: str, texto_despues: str,
          nota: str = "", cambia: Optional[bool] = None) -> Dict[str, Any]:
    return {"clave": clave, "titulo": titulo, "sub": sub, "antes": antes, "despues": despues, "texto_antes": texto_antes,
            "texto_despues": texto_despues, "nota": nota,
            "cambia": (texto_antes != texto_despues) if cambia is None else cambia}


def _aviso(clave: str, titulo: str, texto: str, casilla: bool = False, tipo: Optional[str] = None,
           frase: str = "") -> Dict[str, Any]:
    return {"clave": clave, "tipo": tipo or ("aviso" if casilla else "info"), "titulo": titulo, "texto": texto,
            "casilla": casilla, "frase": frase}


def armar_plan(e: Entrada) -> Dict[str, Any]:
    """Plan de un cambio de estrategia (spec § 3.2-3.3): valida todo, arma el diff, los avisos y ``plan_hash``. No
    escribe nada. ``EstrategiaError`` (409) con los motivos si algo bloquea."""
    cfg, obj, ctx = e.cfg, e.objetivo, e.ctx
    bloqueos: List[str] = []
    if e.bloqueo_rescate:
        bloqueos.append(e.bloqueo_rescate)
    _n_longs_cfg, n_shorts = bot_control.n_pares(cfg)
    if n_shorts != 0:
        bloqueos.append(f"La config de forager tiene n_shorts={cfg.get('n_shorts')!r}: Estrategia escribe solo con los "
                        "shorts apagados (n_shorts 0), como los perfiles.")
    for side in ("long", "short"):
        try:
            bot_control._mode(cfg, side)
        except bot_control.InvalidModeError as exc:
            bloqueos.append(str(exc).replace(" No se modifico la config.", ""))
    if obj["base_hash"] != bot_control.estrategia_hash(cfg):
        bloqueos.append(BASE_VIEJA)
    vigente = bot_control.resumen_estrategia(cfg)
    pares: List[str] = list(obj["pares"])
    twe = obj["twe_long"]
    n = obj["n_longs"]
    if not 1 <= len(pares) <= MAX_PARES:
        bloqueos.append(f"Tiene que haber entre 1 y {MAX_PARES} pares (hay {len(pares)}).")
    duplicados = sorted({p for p in pares if pares.count(p) > 1})
    if duplicados:
        bloqueos.append(f"Pares repetidos: {', '.join(duplicados)}.")
    malos = [p for p in pares if not FORMATO.match(p)]
    if malos:
        bloqueos.append(f"Formato inválido ({', '.join(malos)}): tiene que ser como BTCUSDT.")
    validos = [p for p in dict.fromkeys(pares) if FORMATO.match(p)]
    entran = [p for p in validos if e.todos_entran or p not in vigente["pares_long"]]
    salen = [p for p in vigente["pares_long"] if p not in pares]
    lw = float(twe) / n
    resultados = {r["symbol"]: r for r in chequear_varios(validos, ctx, lw=lw)}
    avisos: List[Dict[str, Any]] = []
    for p in validos:
        r = resultados[p]
        if p in entran:
            if r["bloquea"]:
                bloqueos.append(f"{p}: {'; '.join(r['motivos'])}.")
            continue
        malos_forager = [c["texto"] for c in r["chequeos"] if c["estado"] == BLOQUEA and c["clave"] in BLOQUEANTES
                         and not (c["clave"] == "config" and not ctx.manifiesto.get("usable"))]
        if malos_forager:
            avisos.append(_aviso(f"descarta:{p}", f"forager descartaría {p}, que ya está en la lista.",
                                 "; ".join(malos_forager) + ". Si tiene posición, queda en parada gradual y no reabre.",
                                 casilla=True, frase=f"forager descartaría {p}"))
    if e.expo.get("error"):
        bloqueos.append(f"{e.expo['error']} Sin posiciones no se puede saber cuántos lugares están ocupados.")
    lug = lugares(validos, e.expo, cfg.get("symbols_to_ignore"))
    ocupados = lug["ocupados"]
    if not e.expo.get("error"):
        if n < lug["minimo"]:
            lw_n = float(twe) / n
            bloqueos.append(
                f"Pares a la vez {n} con {len(ocupados)} lugares ocupados ({_pares_texto(ocupados)}): forager le daría lw "
                f"{_n(lw_n, 2)} a todos, también a los que salen con posición: hasta {_n(lw_n * len(ocupados), 2)} veces el "
                f"balance. Mínimo: {lug['minimo']}.")
        if n > lug["maximo"]:
            extra = f" + {len(lug['salen_con_posicion'])} que salen con posición" if lug["salen_con_posicion"] else ""
            bloqueos.append(f"Pares a la vez {n} es más que los pares ({len(validos)}{extra}): la TWE no se alcanzaría. "
                            f"Máximo: {lug['maximo']}.")
    updates = {"twe_long": bot_control.twe_valor(twe), "twe_short": bot_control.TWE_SHORT_SOLO_LONG, "n_longs": n,
               "approved_symbols_long": list(pares), "approved_symbols_short": list(pares)}
    nuevo = {**cfg, **updates}
    if not bloqueos and all(cfg.get(k) == v for k, v in updates.items()):
        bloqueos.append("No hay cambios: la estrategia ya es esa.")

    balance = ctx.balance
    antes_d = derivados(cfg.get("twe_long"), vigente["n_longs"], balance, cfg.get("leverage"))
    despues_d = derivados(updates["twe_long"], n, balance, cfg.get("leverage"))
    perfil_antes = e.detectar(cfg)
    perfil_despues = e.detectar(nuevo)
    nombre = lambda p: e.nombres_perfil.get(p, "Personalizado") if p else "Personalizado"  # noqa: E731
    filas = [
        _fila("pares_long", "Pares", "approved_symbols_long", vigente["pares_long"], pares,
              _pares_texto(vigente["pares_long"]), _pares_texto(pares)),
        _fila("pares_short", "Pares short", "approved_symbols_short: copia de la long (sin efecto con n_shorts 0; así "
              "forager no mezcla listas)", vigente["pares_short"], pares, _pares_texto(vigente["pares_short"]),
              _pares_texto(pares)),
        _fila("n_longs", "Pares a la vez", "n_longs", vigente["n_longs"], n, str(vigente["n_longs"]), str(n),
              nota=(f"{len(validos)} pares + {_pares_texto(lug['salen_con_posicion'])} mientras tenga posición"
                    if lug["salen_con_posicion"] and n > len(validos) else "")),
        _fila("lw", "Límite por par", "lw = TWE ÷ pares a la vez", antes_d["lw"], despues_d["lw"],
              _num_txt(antes_d["lw"]), _num_txt(despues_d["lw"]),
              nota="también para los que salen con posición" if lug["salen_con_posicion"] else ""),
        _fila("max_por_par", "Máximo por par", "balance para dimensionar × lw", antes_d["max_por_par"],
              despues_d["max_por_par"], _usdt(antes_d["max_por_par"]), _usdt(despues_d["max_por_par"])),
        _fila("twe_long", "TWE long", "twe_long", cfg.get("twe_long"), updates["twe_long"],
              bot_control._fmt_num(cfg.get("twe_long")), bot_control._fmt_num(twe),
              nota=f"máximo total {_usdt(despues_d['max_total'])}"),
    ]
    if numero(cfg.get("twe_short")) != float(bot_control.TWE_SHORT_SOLO_LONG):
        filas.append(_fila("twe_short", "TWE short", "sin efecto con n_shorts 0", cfg.get("twe_short"),
                           updates["twe_short"], _num_txt(numero(cfg.get("twe_short"))),
                           _num_txt(float(bot_control.TWE_SHORT_SOLO_LONG))))
    if lug["salen_con_posicion"] and n > len(validos):
        efectiva = float(twe) / n * len(validos)
        filas.append(_fila("twe_efectiva", "TWE efectiva", "lw × bots con lugar", None, None, _num_txt(float(twe)),
                           f"{_num_txt(float(twe))} mientras {_pares_texto(lug['salen_con_posicion'])} siga abierta · "
                           f"{_num_txt(efectiva)} cuando cierre", cambia=True))
    filas.append(_fila("perfil", "Perfil", "", perfil_antes, perfil_despues, nombre(perfil_antes), nombre(perfil_despues),
                       nota=("rescate, disparo por uPnL y retiro sin cambios" if not perfil_despues else "")))

    # Avisos con casilla (spec § 3.2, columna del medio) e información.
    sin_backtest = [p for p in validos if resultados[p].get("sin_backtest")]
    if sin_backtest:
        base_txt = nombre(perfil_antes) if perfil_antes else "los perfiles"
        avisos.insert(0, _aviso("sin_backtest", f"{', '.join(sin_backtest)} {'no tiene' if len(sin_backtest) == 1 else 'no tienen'} "
                                "backtest de BotBulls.",
                                f"Los números de {base_txt} se midieron con BNB, BTC, ETH y XRP y sus configs clock, 4 a "
                                "la vez (lw 1,00). Con este cambio no aplican.", casilla=True,
                                frase=f"{', '.join(sin_backtest)} no {'tiene' if len(sin_backtest) == 1 else 'tienen'} backtest"))
    if twe > bot_control.TWE_ADVERTENCIA:
        avisos.append(_aviso("twe_alta", f"TWE {bot_control._fmt_num(twe)} por encima de {bot_control._fmt_num(bot_control.TWE_ADVERTENCIA)}.",
                             "En el backtest 2021-2026 aparecen liquidaciones desde TWE 6, y los perfiles medidos son TWE 3 "
                             "y TWE 4.", casilla=True, frase=f"TWE {bot_control._fmt_num(twe)} está por encima de lo medido"))
    for p in lug["salen_con_posicion"]:
        qty = e.expo.get("posiciones", {}).get(p, 0.0)
        noc = e.expo.get("nocional", {}).get(p)
        pct = f" ({_n(noc / balance * 100, 0)} % del balance)" if noc and balance else ""
        avisos.append(_aviso(f"sale:{p}", f"{p} tiene una posición abierta: {_n(qty)} {corto(p)}, ≈ {_n(noc or 0, 0)} USDT"
                             f"{pct}.",
                             f"Sigue en parada gradual hasta cerrarse: puede promediar hasta {_usdt(despues_d['max_por_par'])} "
                             f"(lw {_num_txt(despues_d['lw'])}) y ocupa 1 de los {n} lugares mientras tanto. Su venta abierta "
                             "sigue.", casilla=True, frase=f"{p} sigue operando en parada gradual hasta cerrar"))
    if lug["salen_con_posicion"] and n > len(validos):
        lw_bajo = float(twe) / len(validos) if validos else None
        if lw_bajo:
            avisos.append(_aviso("por_que_no_baja", f"Por qué no baja a {len(validos)} a la vez ahora.",
                                 f"forager le da la misma lw a todos, también al que sale. Con {len(validos)} a la vez sería "
                                 f"lw {_n(lw_bajo, 2)} para los {len(ocupados)} bots: hasta {_n(lw_bajo * len(ocupados), 2)} "
                                 f"veces el balance mientras {_pares_texto(lug['salen_con_posicion'])} siga abierta. El panel "
                                 "no lo permite. Cuando cierre, Bot ofrece bajar los lugares."))
    solo_ordenes = [p for p in salen if p not in lug["salen_con_posicion"] and e.expo.get("ordenes", {}).get(p)]
    for p in solo_ordenes:
        avisos.append(_aviso(f"sale_orden:{p}", f"{p} solo tiene órdenes abiertas.",
                             "Pasa a parada gradual sin posición: no compra, la orden se cancela al reiniciar y el lugar se "
                             "libera en el ciclo siguiente."))
    if perfil_antes != perfil_despues and not perfil_despues:
        avisos.append(_aviso("personalizado", "El perfil pasa a Personalizado.",
                             f"TWE, pares o pares a la vez distintos de los medidos: los números de {nombre(perfil_antes)} "
                             "no aplican.", casilla=True, frase="los números del perfil dejan de aplicar"))
    if not perfil_despues and e.rescate_cfg.get("habilitado"):
        upnl = e.rescate_cfg.get("upnl_x_pct")
        tope = e.rescate_cfg.get("tope_perdida_pct")
        avisos.append(_aviso("rescate_personalizado", "Personalizado con rescate: umbrales medidos para otra cartera.",
                             f"El rescate sigue armado y sus umbrales se midieron con Con rescate (TWE 4 y los 4 pares): "
                             f"disparo por uPnL al {upnl} % del balance{f' y tope de pérdida {tope} %' if tope else ''}. Con "
                             "esta cartera no están medidos: revisalos en Rescate.", casilla=True,
                             frase="los umbrales del rescate dejan de estar medidos"))
    if len(validos) > n:
        avisos.append(_aviso("rotacion", f"Rotando: {n} de {len(validos)} a la vez.",
                             f"Cada {cfg.get('update_interval_minutes') or 15} min forager elige {n} pares por volumen, "
                             "unilateralidad y ruido; el que queda afuera pasa a parada gradual y no abre nada nuevo."))
    libres = max(0, n - len(ocupados))
    for p in [x for x in validos if x not in vigente["pares_long"]]:
        avisos.append(_aviso(f"entra:{p}", f"{p} entra {'apenas arranque forager' if libres else 'cuando se libere un lugar'}.",
                             f"{len(ocupados)} de {n} lugares ocupados ahora (posiciones y órdenes abiertas)."))
        libres = max(0, libres - 1)
    if not ctx.manifiesto.get("usable"):
        avisos.append(_aviso("manifiesto", "Configs sin verificar.",
                             f"{ctx.manifiesto.get('motivo')} No se pueden agregar pares; sí cambiar TWE, pares a la vez y "
                             "quitar."))
    if e.running:
        como = (f"passivbot se reinicia (docker restart) y forager tarda ~{e.warmup_s} s en volver a operar los pares. Las "
                "posiciones y órdenes abiertas siguen en Binance. No cambia los modos (long_mode "
                f"{cfg.get('long_mode') or 'normal'}).")
    else:
        como = ("passivbot está detenido: se escribe la config y no se arranca nada. Se aplica al iniciar (Iniciar… muestra "
                "este cambio).")
    avisos.append(_aviso("como", "Cómo se aplica.", como))

    con_casilla = [a for a in avisos if a["casilla"]]
    frases = [a["frase"] for a in con_casilla if a["frase"]]
    casilla_texto = ("Entiendo que " + (", ".join(frases[:-1]) + " y " + frases[-1] if len(frases) > 1 else frases[0])
                     + (" y que passivbot se reinicia." if e.running else ".")) if frases else ""
    cambios: List[str] = [f"+{p}" for p in validos if p not in vigente["pares_long"]]
    cambios += [f"−{p}" for p in salen]
    if vigente["n_longs"] != n:
        cambios.append(f"pares a la vez {vigente['n_longs']} → {n}")
    if numero(cfg.get("twe_long")) != float(twe):
        cambios.append(f"TWE {bot_control._fmt_num(cfg.get('twe_long'))} → {bot_control._fmt_num(twe)}")
    if perfil_antes != perfil_despues:
        cambios.append(f"perfil {nombre(perfil_antes)} → {nombre(perfil_despues)}")
    resumen = " · ".join(cambios) or "sin cambios de pares, lugares ni TWE"
    plan = {
        "titulo": e.titulo,
        "origen": e.origen,
        "running": e.running,
        "como": "reinicia" if e.running else "escribe",
        "vigente": vigente,
        "objetivo": {"twe_long": updates["twe_long"], "n_longs": n, "pares": list(pares)},
        "updates": updates,
        "diff": filas,
        "avisos": avisos,
        "lugares": lug,
        "pares": [resultados[p] for p in validos],
        "perfil": {"antes": perfil_antes, "despues": perfil_despues},
        "requiere_casilla": bool(con_casilla),
        "casilla_texto": casilla_texto,
        "resumen": resumen,
    }
    plan["texto"] = "\n".join([f"{e.titulo}: {resumen}."] + [f"{f['titulo']}: {f['texto_antes']} -> {f['texto_despues']}"
                                                            for f in filas if f["cambia"]]
                              + [f"{a['titulo']} {a['texto']}" for a in avisos])
    if bloqueos:
        raise EstrategiaError(bloqueos, plan)
    plan["plan_hash"] = hashlib.sha256(json.dumps(
        {"archivo": e.archivo_sha, "updates": updates, "casilla": sorted(a["clave"] for a in con_casilla),
         "como": plan["como"], "origen": e.origen}, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return plan


def registrar_version(data_dir: pathlib.Path, entry: Dict[str, Any]) -> None:
    """Un renglón en ``estrategia_versiones.jsonl``: quién, qué y con qué backup (los backups solos no lo dicen).
    Nunca lanza: el cambio ya quedó escrito."""
    path = pathlib.Path(data_dir) / VERSIONES_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        lineas = path.read_text(encoding="utf-8").splitlines()
        if len(lineas) > 2 * MAX_VERSIONES:
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text("\n".join(lineas[-MAX_VERSIONES:]) + "\n", encoding="utf-8")
            os.replace(tmp, path)
    except (OSError, ValueError):
        pass


def aplicar(settings: Any, store: Any, armar: Any, plan_hash: str, usuario: str, origen: str = "estrategia",
            accion: str = "estrategia", docker: Any = None, reporter: Any = None,
            extra_version: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Aplica un plan (corre en el job, con el lock de acciones). Vuelve a armar el plan con lo de ahora: si cambió
    (``plan_hash``), 409 y nada escrito. Backup y escritura atómica; si passivbot corre (operando o en parada gradual)
    lo reinicia (los modos no se tocan), y si Docker rechaza el reinicio restaura el backup. Detenido: escribe y no
    arranca (los pasos de Docker quedan "omitido: se aplica al iniciar")."""
    rep = reporter or bot_control.NULL_REPORTER
    rep.step("validar", bot_control.PASO_EN_CURSO)
    plan = armar()
    if plan["plan_hash"] != plan_hash:
        raise EstrategiaError(["El plan cambió desde que lo revisaste (estrategia, posiciones o estado del bot): no se "
                               "escribió nada. Revisalo de nuevo."])
    path = settings.require_config()
    docker = docker or bot_control.DockerClient(settings.docker_url)
    rep.step("validar", bot_control.PASO_OK, f"{plan['resumen']}; passivbot {'corriendo' if plan['running'] else 'detenido'}")
    cfg_antes = bot_control.read_forager_config(path)
    updates = plan["updates"]
    backup = bot_control.write_forager_config(path, updates, rep)
    cfg_despues = {**cfg_antes, **updates}
    version = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "usuario": usuario,
               "accion": accion, "backup": backup.name, "hash_antes": bot_control.estrategia_hash(cfg_antes),
               "hash_despues": bot_control.estrategia_hash(cfg_despues), "resumen": plan["resumen"],
               "cambios": bot_control.cambios_estrategia(bot_control.resumen_estrategia(cfg_antes),
                                                         bot_control.resumen_estrategia(cfg_despues)),
               **(extra_version or {})}
    out: Dict[str, Any] = {"ok": True, "docker_action": None, "config": updates, "backup": backup.name,
                           "cambios_texto": plan["resumen"]}
    if plan["running"]:
        rep.step("docker", bot_control.PASO_EN_CURSO, f"Reiniciando {settings.container}")
        try:
            docker.restart(settings.container)
        except bot_control.DockerError as exc:
            if exc.uncertain:
                registrar_version(store.data_dir, {**version, "resultado": "incierto"})
                err = bot_control.DockerError(f"{exc} Resultado incierto: la estrategia quedó escrita (backup "
                                              f"{backup.name}); revisar el estado del bot antes de reintentar.")
                err.uncertain = True
                raise err from exc
            try:
                bot_control.restore_forager_config(path, backup)
            except (bot_control.BotControlError, OSError) as restore_exc:
                registrar_version(store.data_dir, {**version, "resultado": "sin_restaurar"})
                raise bot_control.DockerError(f"{exc} ATENCION: la config de forager quedó con la estrategia nueva y no "
                                              f"se pudo restaurar ({restore_exc}); backup: {backup.name}.") from exc
            raise bot_control.DockerError(f"{exc} Se restauró la config de forager; no se aplicó ningún cambio.") from exc
        rep.step("docker", bot_control.PASO_OK, "docker restart")
        out["docker_action"] = "restart"
        arranque = bot_control.registrar_arranque(store, cfg_despues, usuario, origen, "restart")
        if arranque:
            out["arranque"] = arranque["id"]
    else:
        rep.step("docker", bot_control.PASO_OMITIDO, "passivbot está detenido: no se arranca. Se aplica al iniciar.")
        rep.step("verificar", bot_control.PASO_OMITIDO, "omitido: passivbot detenido.")
        rep.step("forager", bot_control.PASO_OMITIDO, "omitido: se aplica al iniciar.")
        out["warning"] = "passivbot está detenido: la estrategia queda guardada y se aplica al iniciar."
    registrar_version(store.data_dir, {**version, "resultado": "ok"})
    return out
