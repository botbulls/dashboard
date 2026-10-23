"""C3: stop con reinicio. Piso sobre la cuenta de futuros, corte con Apagar y reinicio automático.

Corre dentro del servicio rescate (``python -m futuresboard.rescate``): un paso por ciclo, con el mismo lock de
estado, y su estado vive en ``rescate_state.json`` bajo la clave ``c3``. La config la escribe solo el panel
(``c3_config.json``). Ver ``docs/c3.md``.

* **Capital de futuros** = ``totalMarginBalance - préstamo real pendiente del rescate`` (más el monto de una
  transferencia real en vuelo: se toma la hipótesis que da más capital, así una transferencia a medio camino
  nunca provoca un corte). Sin restar el préstamo, una inyección del rescate taparía la pérdida.
* **Piso** = ``base × (1 - tope%)``; la base es el capital de futuros al habilitar (o al reiniciar).
* **Corte**: al cruzar el piso queda *en confirmación*; si una lectura separada al menos
  ``FUTURESBOARD_C3_CONFIRM_SECONDS`` (60 s) sigue en o debajo, se ejecuta la rutina de Apagar del panel (mismo
  lock) y el estado queda ``c3_parado``. Si rebota, se cancela.
* **Reinicio**: rebote del índice equiponderado de los pares (precio normalizado al corte, muestreado al cierre de
  cada vela de 1 h) ``>= mínimo posterior × (1 + X%)``, o cooldown de N días, o lo primero de los dos. Al reiniciar
  se re-ancla la base y se ejecuta START con la config que quedó (la rutina del panel). Límite de reinicios
  automáticos en una ventana; superado, queda parado esperando acción manual.
* **Dead-man**: ``watcher_heartbeat.json`` (atómico) para passivbot. En ``real`` se escribe solo mientras C3
  protege: con lectura de futuros reciente, sin un Apagar pendiente (propio o del tope del rescate) y sin
  bloqueo ni config inválida. Si no, se deja el latido viejo y passivbot deja de abrir entradas.

Es una herramienta, no un asesor: cortar realiza la pérdida y reiniciar puede volver a perder.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import math
import os
import pathlib
import re
import secrets
import time
from decimal import Decimal
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

from futuresboard import bot_control
from futuresboard import jobs
from futuresboard import rescate
from futuresboard import telegram_notify
from futuresboard.rescate import dec
from futuresboard.rescate import money
from futuresboard.rescate import now_iso
from futuresboard.telegram_notify import esc

log = logging.getLogger("futuresboard.c3")

# --------------------------------------------------------------------------------------
# Entorno
# --------------------------------------------------------------------------------------

ENV_MODE = "FUTURESBOARD_C3_MODE"
ENV_HEARTBEAT = "FUTURESBOARD_C3_HEARTBEAT"
ENV_CONFIRM = "FUTURESBOARD_C3_CONFIRM_SECONDS"
ENV_PARES = "FUTURESBOARD_C3_PARES"

MODE_OFF = rescate.MODE_OFF
MODE_DRY_RUN = rescate.MODE_DRY_RUN
MODE_REAL = rescate.MODE_REAL

DEFAULT_HEARTBEAT = "/rescate/watcher_heartbeat.json"
HEARTBEAT_VERSION = 1
DEFAULT_CONFIRM_S = 60
MAX_CONFIRM_S = 600
DEFAULT_PARES = ("BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT")
MAX_PARES = 10
_PAR_RE = re.compile(r"^[A-Z0-9]{2,30}$")

# --------------------------------------------------------------------------------------
# Config del panel (c3_config.json)
# --------------------------------------------------------------------------------------

CONFIG_FILE_NAME = "c3_config.json"
CONFIG_LOCK_NAME = ".c3_config.lock"

POLITICA_REBOTE = "rebote"
POLITICA_COOLDOWN = "cooldown"
POLITICA_AMBOS = "ambos"
POLITICAS = (POLITICA_REBOTE, POLITICA_COOLDOWN, POLITICA_AMBOS)

# (mínimo, máximo) inclusivos.
LIMITES: Dict[str, Tuple[Decimal, Decimal]] = {
    "tope_pct": (Decimal("5"), Decimal("80")),
    "rebote_pct": (Decimal("1"), Decimal("100")),
    "cooldown_dias": (Decimal("0.5"), Decimal("90")),
}
LIMITES_ENTEROS: Dict[str, Tuple[int, int]] = {
    "max_reinicios": (0, 20),
    "ventana_dias": (1, 365),
}
CAMPOS = ("habilitado", "tope_pct", "politica", "rebote_pct", "cooldown_dias", "max_reinicios", "ventana_dias")

# --------------------------------------------------------------------------------------
# Reglas
# --------------------------------------------------------------------------------------

MS_HORA = 3_600_000
MS_DIA = 86_400_000
MS_MINUTO = 60_000
VELA_MARGEN_MS = 10_000          # una vela de 1 h se usa recién 10 s después de su cierre
MAX_VELAS_POR_PEDIDO = 500
HUECO_INTERVALOS = rescate.TOPE_HUECO_INTERVALOS
AVISO_SILENCIO_MS = rescate.TOPE_AVISO_SILENCIO_MS
MAX_INTENTOS = rescate.APAGAR_MAX_INTENTOS
REINTENTO_MS = rescate.APAGAR_REINTENTO_MS
MAX_HISTORIAL = 50

USUARIO_APAGAR = "C3 (stop con reinicio)"
USUARIO_START = "C3 (reinicio automático)"

AVISO = ("C3 corta (Apagar: cierra todo a mercado) cuando el capital de futuros cruza el piso y reinicia solo según "
         "la política elegida: cortar realiza la pérdida y reiniciar puede volver a perder. Es una herramienta, "
         "no un asesor.")


class C3Error(Exception):
    pass


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


def default_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "habilitado": False,
        "generacion": 0,
        "tope_pct": "30",
        "politica": POLITICA_REBOTE,
        "rebote_pct": "10",
        "cooldown_dias": "7",
        "max_reinicios": 3,
        "ventana_dias": 30,
        "actualizado_en": None,
        "actualizado_por": None,
    }


def _check_range(name: str, value: Decimal) -> None:
    lo, hi = LIMITES[name]
    if value < lo or value > hi:
        raise ValueError(f"{name} fuera de rango: tiene que estar entre {lo} y {hi}.")


def _check_int(name: str, value: Any) -> int:
    lo, hi = LIMITES_ENTEROS[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} tiene que ser un número entero.")
    if value < lo or value > hi:
        raise ValueError(f"{name} fuera de rango: tiene que estar entre {lo} y {hi}.")
    return value


def load_config(data_dir: pathlib.Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """Config del panel. Si el archivo es inválido se devuelve deshabilitada + el error (fail-closed)."""
    cfg, err, _transitorio = _load_config(data_dir)
    return cfg, err


def _load_config(data_dir: pathlib.Path) -> Tuple[Dict[str, Any], Optional[str], bool]:
    """``load_config`` + si el error es de lectura (``OSError``, puede ser pasajero) y no de contenido."""
    cfg, err = _load_config_contenido(data_dir)
    return cfg, err, bool(err) and err.startswith(_ERROR_LECTURA)


_ERROR_LECTURA = f"No se pudo leer {CONFIG_FILE_NAME}"


def _load_config_contenido(data_dir: pathlib.Path) -> Tuple[Dict[str, Any], Optional[str]]:
    cfg = default_config()
    path = pathlib.Path(data_dir) / CONFIG_FILE_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return cfg, None
    except OSError as exc:
        return cfg, f"{_ERROR_LECTURA} ({exc.__class__.__name__}): C3 deshabilitado."
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return cfg, f"{CONFIG_FILE_NAME} inválido (JSON: {exc.__class__.__name__}): C3 deshabilitado."
    try:
        if not isinstance(raw, dict):
            raise ValueError("no es un objeto")
        cfg.update({k: v for k, v in raw.items() if k in cfg})
        if not isinstance(cfg["habilitado"], bool):
            raise ValueError("habilitado no es booleano")
        if isinstance(cfg["generacion"], bool) or not isinstance(cfg["generacion"], int) or cfg["generacion"] < 0:
            raise ValueError("generacion inválida")
        if cfg["politica"] not in POLITICAS:
            raise ValueError("politica inválida")
        for name in LIMITES:
            _check_range(name, dec(cfg[name]))
        for name in LIMITES_ENTEROS:
            _check_int(name, cfg[name])
    except ValueError as exc:
        return default_config(), f"{CONFIG_FILE_NAME} inválido ({exc}): C3 deshabilitado."
    return cfg, None


def validate_config_body(body: Any) -> Dict[str, Any]:
    """Validación estricta del POST del panel: todos los campos, ninguno desconocido, tipos y rangos."""
    if not isinstance(body, dict):
        raise ValueError("Body inválido: se esperaba un objeto JSON.")
    unknown = sorted(set(body) - set(CAMPOS))
    if unknown:
        raise ValueError(f"Campos desconocidos: {', '.join(unknown)}.")
    missing = sorted(set(CAMPOS) - set(body))
    if missing:
        raise ValueError(f"Faltan campos: {', '.join(missing)}.")
    if not isinstance(body["habilitado"], bool):
        raise ValueError("habilitado tiene que ser true o false.")
    if not isinstance(body["politica"], str) or body["politica"] not in POLITICAS:
        raise ValueError(f"politica inválida: usar {', '.join(POLITICAS)}.")
    out: Dict[str, Any] = {"habilitado": body["habilitado"], "politica": body["politica"]}
    for name in LIMITES:
        value = body[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} tiene que ser un número.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} tiene que ser un número finito.")
        parsed = dec(value)
        _check_range(name, parsed)
        out[name] = parsed
    for name in LIMITES_ENTEROS:
        out[name] = _check_int(name, body[name])
    return out


def corte_al_guardar(data_dir: pathlib.Path, parsed: Dict[str, Any]) -> Optional[str]:
    """Aviso si guardar ``parsed`` deja el último capital leído en o debajo del piso nuevo (y arriba del vigente):
    C3 cortaría a mercado en cuanto se confirme. Habilitar (base nueva) o un C3 parado no cortan por esto."""
    if not parsed.get("habilitado"):
        return None
    prev, err = load_config(data_dir)
    if err or not prev.get("habilitado"):
        return None
    state, state_err = rescate.load_state(pathlib.Path(data_dir))
    if state_err:
        return None
    c3, _err = get_state(dict(state))
    base = c3.get("base") if isinstance(c3.get("base"), dict) else None
    ultimo = c3.get("ultimo") if isinstance(c3.get("ultimo"), dict) else None
    if c3.get("c3_parado") or c3.get("generacion") != prev.get("generacion") or not base or not ultimo:
        return None
    try:
        base_capital, capital = dec(base["capital"]), dec(ultimo["capital"])
        piso_vigente, piso_nuevo = piso_de(base_capital, dec(prev["tope_pct"])), piso_de(base_capital,
                                                                                         dec(parsed["tope_pct"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not piso_vigente < capital <= piso_nuevo:
        return None
    return (f"Con tope {_pct_txt(dec(parsed['tope_pct']))}% el piso pasa a {money(piso_nuevo)} USDT y el último "
            f"capital de futuros leído es {money(capital)} USDT ({ultimo.get('ts')}): C3 va a cortar en cuanto se "
            "confirme (FUTURESBOARD_C3_CONFIRM_SECONDS, 60 s por defecto). Apagar detiene passivbot y cierra todo a "
            "mercado. "
            "¿Guardar igual?")


def save_config(data_dir: pathlib.Path, parsed: Dict[str, Any], user: str) -> Dict[str, Any]:
    """Guarda la config. Habilitar (false -> true) abre una generación nueva => base nueva."""
    data_dir = pathlib.Path(data_dir)
    with rescate.file_lock(data_dir / CONFIG_LOCK_NAME):
        prev, _err = load_config(data_dir)
        cfg = dict(prev)
        if parsed["habilitado"] and not prev.get("habilitado"):
            cfg["generacion"] = int(prev.get("generacion") or 0) + 1
        cfg["habilitado"] = parsed["habilitado"]
        cfg["politica"] = parsed["politica"]
        for name in LIMITES:
            cfg[name] = format(parsed[name].normalize(), "f")
        for name in LIMITES_ENTEROS:
            cfg[name] = parsed[name]
        cfg["actualizado_en"] = now_iso()
        cfg["actualizado_por"] = user
        rescate.write_json_atomic(data_dir / CONFIG_FILE_NAME, cfg, mode=0o600)
        return cfg


# --------------------------------------------------------------------------------------
# Estado (rescate_state.json["c3"], lo escribe solo el servicio)
# --------------------------------------------------------------------------------------


def default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "generacion": None,
        "modo": None,
        "base": None,
        "confirmacion": None,
        "cancelada_ms": None,
        "c3_parado": False,
        "corte": None,
        "reinicios": [],
        "historial": [],
        "ultimo": None,
        "bloqueo": None,
        "latido": None,
        "sin_observacion": None,
    }


def get_state(state: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """La sección ``c3`` del estado del rescate (creada si falta). Una sección inválida se reinicia con error."""
    c3 = state.get("c3")
    if c3 is None:
        c3 = state["c3"] = default_state()
        return c3, None
    if not isinstance(c3, dict):
        c3 = state["c3"] = default_state()
        return c3, "El estado de C3 en rescate_state.json era inválido: se reinició (sin base ni corte)."
    for key, value in default_state().items():
        c3.setdefault(key, value)
    for key in ("reinicios", "historial"):
        if not isinstance(c3.get(key), list):
            c3[key] = []
    if not isinstance(c3.get("c3_parado"), bool):
        c3["c3_parado"] = bool(c3.get("c3_parado"))
    return c3, None


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


def parse_pares(raw: str) -> Tuple[Tuple[str, ...], Optional[str]]:
    text = (raw or "").strip()
    if not text:
        return DEFAULT_PARES, None
    pares = tuple(dict.fromkeys(p.strip().upper() for p in text.split(",") if p.strip()))
    if not pares or len(pares) > MAX_PARES or not all(_PAR_RE.match(p) for p in pares):
        return DEFAULT_PARES, (f"{ENV_PARES}={raw!r} inválido (1 a {MAX_PARES} símbolos separados por coma): se usan "
                               f"{','.join(DEFAULT_PARES)}.")
    return pares, None


class C3Settings:
    """Variables de entorno de C3. Un modo inválido es ``off`` (fail-closed)."""

    def __init__(self, environ: Optional[Dict[str, str]] = None) -> None:
        env = os.environ if environ is None else environ
        raw = env.get(ENV_MODE, "").strip().lower()
        self.mode_error: Optional[str] = None
        if raw in ("", MODE_OFF):
            self.mode = MODE_OFF
        elif raw in (MODE_DRY_RUN, MODE_REAL):
            self.mode = raw
        else:
            self.mode = MODE_OFF
            self.mode_error = f"{ENV_MODE}={raw!r} inválido: se usa off (valores: off, dry_run, real)."
        self.heartbeat_path = pathlib.Path(env.get(ENV_HEARTBEAT, "").strip() or DEFAULT_HEARTBEAT)
        self.confirm_seconds = rescate._env_int(env, ENV_CONFIRM, DEFAULT_CONFIRM_S, 0, MAX_CONFIRM_S)
        self.pares, self.pares_error = parse_pares(env.get(ENV_PARES, ""))


# --------------------------------------------------------------------------------------
# Dead-man: watcher_heartbeat.json (contrato con passivbot)
# --------------------------------------------------------------------------------------


def heartbeat_payload(c3_mode: str, parado: bool, ts: float) -> Dict[str, Any]:
    if c3_mode not in (MODE_OFF, MODE_DRY_RUN, MODE_REAL):
        raise ValueError(f"c3_mode inválido: {c3_mode!r}")
    return {"version": HEARTBEAT_VERSION, "ts": now_iso(ts), "c3_mode": c3_mode, "c3_parado": bool(parado)}


def write_heartbeat(path: pathlib.Path, c3_mode: str, parado: bool, ts: float) -> Dict[str, Any]:
    """Escritura atómica (tmp + fsync + os.replace), legible por otro uid (passivbot lo monta read-only)."""
    payload = heartbeat_payload(c3_mode, parado, ts)
    rescate.write_json_atomic(pathlib.Path(path), payload, mode=0o644)
    return payload


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------


def capital_futuros(prestamo: Dict[str, Any], fut: Dict[str, Any]) -> Tuple[Decimal, Decimal, Decimal]:
    """(capital, préstamo real, monto en vuelo) que mira C3.

    ``totalMarginBalance - préstamo real + monto de una transferencia real en vuelo``. ``prestamo`` es
    ``rescate.prestamo_snapshot`` tomado junto con la lectura de futuros (no al final del ciclo). Con una
    transferencia en vuelo no se sabe si ya llegó: se toma la hipótesis con más capital (inyección no llegada /
    devolución ya salida), que nunca corta de más; a lo sumo protege ``monto`` menos mientras dura.
    """
    loan = prestamo["prestamo_real"]
    en_vuelo = prestamo.get("en_vuelo")
    extra = Decimal(0)
    if isinstance(en_vuelo, dict) and en_vuelo.get("modo") == MODE_REAL:
        try:
            extra = dec(en_vuelo.get("monto"))
        except ValueError:
            extra = Decimal(0)
    return fut["margin_balance"] - loan + extra, loan, extra


def piso_de(base_capital: Decimal, pct: Decimal) -> Decimal:
    return base_capital * (1 - pct / 100)


def ajustar_base_por_retiro(state: Dict[str, Any], monto: Decimal, ts: float,
                            capital_previo: Optional[Decimal] = None) -> Optional[str]:
    """Un retiro periódico REAL confirmado (futuros -> spot, ver ``rescate._retiro``) baja la base de C3 en proporción.

    C3 mira ``totalMarginBalance - préstamo``: el retiro baja el margen para siempre sin ser una pérdida. Sin bajar la
    base, C3 lo vería como una caída igual al retiro y podría cortar (por ejemplo, base 20.000 con tope 30 % y un
    retiro de 10.000 dejaría el capital en 10.000, debajo del piso 14.000).

    ``base' = base × (capital_previo - monto) / capital_previo`` (en centavos hacia abajo), con ``capital_previo`` el
    capital de futuros de C3 cuando se decidió el retiro: capital / base no cambia, así que el retiro solo nunca acerca
    el corte (el drawdown % sigue igual) ni abre una confirmación. Restar el monto no alcanza: el piso es un % de la
    base y baja menos que el capital (con tope 30 %, cada retiro se come el 30 % de lo retirado del margen sobre el
    piso), así que un retiro con la cuenta en drawdown podía cortar. Con la cuenta plana (capital = base) da lo mismo
    que restar. Sin ``capital_previo`` válido (una transferencia en vuelo de una versión anterior) se resta el monto.

    Lo llama el servicio rescate dentro de su ciclo (mismo lock de estado). Devuelve el texto del ajuste, o None si C3
    no tiene base."""
    c3 = state.get("c3")
    if not isinstance(c3, dict):
        return None
    base = c3.get("base")
    if not isinstance(base, dict) or base.get("capital") is None:
        return None
    try:
        capital = dec(base["capital"])
        previo = dec(base.get("retirado") or "0")
    except ValueError:
        return None
    try:
        cap_previo = None if capital_previo is None else dec(capital_previo)
    except ValueError:
        cap_previo = None
    if cap_previo is not None and cap_previo > monto:
        nueva = rescate.floor_cent(capital * (cap_previo - monto) / cap_previo)
        regla = (f"proporcional al capital de futuros {money(cap_previo)} -> {money(cap_previo - monto)} USDT: el "
                 "retiro no es una pérdida ni acerca el corte")
    else:
        nueva = max(Decimal(0), capital - monto)
        regla = "el retiro no es una pérdida"
    base["capital"] = money(nueva)
    base["retirado"] = money(previo + monto)
    texto = (f"Base de C3 ajustada por un retiro de ganancias de {money(monto)} USDT: {money(capital)} -> "
             f"{money(nueva)} USDT ({regla}).")
    historial = c3.get("historial") if isinstance(c3.get("historial"), list) else []
    historial.append({"ts": now_iso(ts), "tipo": "base_retiro", "modo": base.get("modo") or MODE_REAL, "texto": texto,
                      "capital": money(nueva), "retiro": money(monto),
                      "capital_previo": None if cap_previo is None else money(cap_previo)})
    c3["historial"] = historial[-MAX_HISTORIAL:]
    return texto


def _pct_txt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _ratio_txt(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001"), rounding=decimal.ROUND_DOWN), "f")


def _start_automatico_intentado(corte: Dict[str, Any]) -> bool:
    """True si C3 ya intentó un START automático para este corte (con error o sin resultado conocido)."""
    rein = corte.get("reinicio") if isinstance(corte.get("reinicio"), dict) else {}
    try:
        intentos = int(rein.get("intentos") or 0)
    except (TypeError, ValueError):
        intentos = 1
    return rein.get("resultado") in ("pendiente", "error") or intentos > 0


def aviso_max_age(interval: int, max_age: int = 120) -> Optional[str]:
    """Error de arranque si el intervalo del servicio no entra en el MAX_AGE del dead-man de passivbot (default 120):
    con un intervalo largo passivbot cancelaría entradas de forma intermitente con C3 sano."""
    necesario = 3 * int(interval) + 30
    if necesario <= max_age:
        return None
    return (f"C3 en real con FUTURESBOARD_RESCATE_INTERVAL={interval} s: passivbot tiene que tener "
            f"PASSIVBOT_WATCHER_MAX_AGE_SECONDS >= {necesario} (3 × intervalo + 30; el default es {max_age}). Si no, "
            "el dead-man frena entradas de forma intermitente con C3 sano.")


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _ms_iso(ms: int) -> str:
    return now_iso(ms / 1000)


# --------------------------------------------------------------------------------------
# Servicio
# --------------------------------------------------------------------------------------


class C3Watcher:
    """Un paso de C3 por ciclo del servicio rescate. Todo lo externo es inyectable (los tests usan dobles).

    * ``apagar()`` -> (outcome, payload): la rutina Apagar del panel con el lock de acciones (``rescate.make_apagar``).
    * ``start(plan)`` -> (outcome, payload): START del panel con el lock (``make_start``). outcome ``ok``,
      ``error``, ``bloqueado`` o ``ya_corriendo``.
    * ``contenedor()`` -> True / False / None (corriendo, detenido, no se sabe).
    * ``forager()`` -> dict con la config HJSON de forager (o lanza).
    * ``klines(symbol, interval, start_ms, limit)`` -> filas de ``GET /fapi/v1/klines``.
    """

    def __init__(self, data_dir: pathlib.Path, settings: C3Settings, *, interval: int = rescate.DEFAULT_INTERVAL,
                 exchange: str = "binance", testnet: bool = False,
                 apagar: Optional[Callable[[], Tuple[str, Dict[str, Any]]]] = None,
                 start: Optional[Callable[[Dict[str, Any]], Tuple[str, Dict[str, Any]]]] = None,
                 contenedor: Optional[Callable[[], Optional[bool]]] = None,
                 forager: Optional[Callable[[], Dict[str, Any]]] = None,
                 klines: Optional[Callable[[str, str, int, int], List[List[Any]]]] = None,
                 send: Optional[Callable[[str], bool]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.settings = settings
        self.mode = settings.mode
        self.interval = int(interval)
        self.exchange = (exchange or "").lower()
        self.testnet = bool(testnet)
        self._apagar = apagar
        self._start = start
        self._contenedor = contenedor
        self._forager = forager
        self._klines = klines
        self.send = send or telegram_notify.send
        self.clock = clock
        self._alertas: Dict[str, str] = {}
        self._cfg_valida: Optional[Dict[str, Any]] = None  # última c3_config.json válida (ante un OSError pasajero)
        self._tope: Optional[Dict[str, Any]] = None  # tope activo del rescate en este ciclo

    # -- utilidades -------------------------------------------------------------------
    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _notify(self, text: str, simulado: bool = False) -> None:
        prefix = "🧪 <b>[SIMULACIÓN]</b> " if simulado else ""
        try:
            self.send(prefix + text)
        except Exception:  # pragma: no cover - telegram_notify.send no lanza
            log.warning("No se pudo notificar a Telegram.")

    def _alert(self, key: str, text: str, simulado: bool = False) -> None:
        if self._alertas.get(key) != text:
            log.warning("%s", text)
            self._alertas[key] = text
            self._notify(f"⚠️ <b>C3</b>: {esc(text)}", simulado)

    def _clear(self, key: str, recovery: Optional[str] = None) -> None:
        if self._alertas.pop(key, None) is not None and recovery:
            self._notify(f"✅ <b>C3</b>: {esc(recovery)}")

    def _event(self, state: Dict[str, Any], texto: str, modo: Optional[str] = None) -> None:
        log.info("[c3] %s", texto)
        rescate.add_event(state, "c3", texto, modo or self.mode, self.clock())

    def _historial(self, c3: Dict[str, Any], tipo: str, texto: str, modo: str, **extra: Any) -> None:
        c3["historial"].append({"ts": now_iso(self.clock()), "tipo": tipo, "modo": modo, "texto": texto, **extra})
        c3["historial"] = c3["historial"][-MAX_HISTORIAL:]

    def _guardar(self, state: Dict[str, Any]) -> None:
        rescate.save_state(self.data_dir, state)

    def max_hueco_ms(self) -> int:
        """Cuánto puede pasar sin observar el capital: confirmación + 2 intervalos (90 s por defecto)."""
        return (self.settings.confirm_seconds + HUECO_INTERVALOS * self.interval) * 1000

    def _sin_observacion(self, c3: Dict[str, Any], now_ms: int) -> Optional[Dict[str, Any]]:
        """None si la última lectura OK de futuros es reciente; si no, desde cuándo C3 está ciego."""
        ultimo = c3.get("ultimo") if isinstance(c3.get("ultimo"), dict) else {}
        ts_ok = ultimo.get("ts") if ultimo.get("capital") is not None else None
        try:
            ok_ms = int(dt.datetime.fromisoformat(ts_ok).timestamp() * 1000) if ts_ok else None
        except (TypeError, ValueError):
            ok_ms = None
        if ok_ms is None:
            return {"desde": None, "texto": "todavía no hubo una lectura de la cuenta de futuros."}
        if now_ms - ok_ms <= self.max_hueco_ms():
            return None
        return {"desde": ts_ok, "texto": f"sin lectura de la cuenta de futuros desde {ts_ok}."}

    def _motivo_sin_latido(self, state: Dict[str, Any], c3: Dict[str, Any]) -> Optional[str]:
        """Por qué C3 en real no está protegiendo (y no tiene que latir), o None."""
        corte = c3.get("corte") if isinstance(c3.get("corte"), dict) else None
        if c3.get("c3_parado") and corte and corte.get("modo") == MODE_REAL and corte.get("origen") != "tope_rescate":
            if (corte.get("apagar") or {}).get("resultado") != "ok":
                return (f"el Apagar del corte del {corte.get('en')} todavía no se completó: passivbot puede seguir "
                        "corriendo.")
        tope = self._tope
        if tope is not None and tope.get("modo") == MODE_REAL and (tope.get("apagar") or {}).get("resultado") != "ok":
            return (f"el tope de pérdida del rescate ({tope.get('disparado_en')}) no completó su Apagar: passivbot "
                    "puede seguir corriendo.")
        ciego = c3.get("sin_observacion")
        if isinstance(ciego, dict):
            return f"C3 no observa el capital: {ciego.get('texto')}"
        return None

    def _latir(self, state: Dict[str, Any], c3: Dict[str, Any], efectivo: str) -> None:
        """Escribe el latido para passivbot. Si falla, passivbot ve un latido viejo (fail-safe) y se avisa.

        En ``real`` solo se escribe si C3 está protegiendo (``_motivo_sin_latido``): si no, queda el latido viejo y
        passivbot, con el dead-man, deja de abrir entradas nuevas.
        """
        if efectivo == MODE_REAL:
            motivo = self._motivo_sin_latido(state, c3)
            if motivo:
                self._no_latir(c3, motivo)
                return
        ts = self.clock()
        try:
            write_heartbeat(self.settings.heartbeat_path, efectivo, c3.get("c3_parado") is True, ts)
        except (OSError, ValueError) as exc:
            c3["latido"] = {"ts": None, "c3_mode": efectivo, "error": f"{exc.__class__.__name__}", "suspendido": None}
            self._alert("latido", f"No se pudo escribir {self.settings.heartbeat_path} ({exc.__class__.__name__}): "
                                  "passivbot va a ver el latido viejo.")
            return
        c3["latido"] = {"ts": now_iso(ts), "c3_mode": efectivo, "error": None, "suspendido": None}
        self._clear("latido", "Latido de C3 escrito otra vez.")
        self._clear("latido_suspendido", "C3 escribe el latido otra vez.")

    def _no_latir(self, c3: Dict[str, Any], motivo: str) -> None:
        """C3 en real que no protege: no se escribe el latido (passivbot ve el viejo) y se avisa una vez."""
        prev = c3.get("latido") if isinstance(c3.get("latido"), dict) else {}
        desde = prev.get("suspendido_desde") if prev.get("suspendido") else None
        c3["latido"] = {"ts": prev.get("ts"), "c3_mode": prev.get("c3_mode"), "error": prev.get("error"),
                        "suspendido": motivo, "suspendido_desde": desde or now_iso(self.clock())}
        self._alert("latido_suspendido", f"C3 no escribe el latido: {motivo} Con el dead-man, passivbot deja de "
                                         "abrir entradas nuevas cuando el latido supera "
                                         "PASSIVBOT_WATCHER_MAX_AGE_SECONDS.")

    def _cargar_config(self) -> Tuple[Dict[str, Any], Optional[str]]:
        """``c3_config.json``. Un error de lectura (``OSError``) usa la última config válida: un tropiezo del
        disco no cancela una confirmación ni deja de proteger. Un contenido inválido sigue siendo fail-closed."""
        cfg, err, transitorio = _load_config(self.data_dir)
        if err and transitorio and self._cfg_valida is not None:
            self._alert("config", f"{err.split(':')[0]}: se usa la última config válida leída.")
            return dict(self._cfg_valida), None
        if err:
            self._alert("config", err)
            if not transitorio:
                self._cfg_valida = None  # un contenido inválido (fail-closed) no se tapa con una config anterior
        else:
            self._clear("config")
            self._cfg_valida = dict(cfg)
        return cfg, err

    # -- ciclo ------------------------------------------------------------------------
    def step(self, state: Dict[str, Any], fut: Optional[Dict[str, Any]], lectura_error: Optional[str],
             rescate_cfg: Dict[str, Any], prestamo: Optional[Dict[str, Any]] = None) -> None:
        """Un paso. ``fut`` es la lectura de futuros del ciclo (None si falló, con ``lectura_error``) y
        ``prestamo`` el préstamo del rescate tomado junto con esa lectura (``rescate.prestamo_snapshot``)."""
        now_ms = self._now_ms()
        self._tope = None
        c3, err = get_state(state)
        if err:
            self._event(state, err)
            self._alert("estado", err)
        cfg, cfg_err = self._cargar_config()
        self._cambio_de_modo(state, c3, now_ms)

        if self.mode == MODE_OFF:
            if c3.get("confirmacion"):
                self._cancelar(state, c3, "C3 está en modo off.", now_ms)
            c3.update(bloqueo=None, sin_observacion=None)
            self._latir(state, c3, MODE_OFF)
            return

        habilitado = bool(cfg.get("habilitado")) and not cfg_err
        if habilitado and c3.get("generacion") != cfg.get("generacion"):
            self._nueva_generacion(state, c3, cfg)
        if not habilitado:
            if c3.get("confirmacion"):
                self._cancelar(state, c3, "C3 deshabilitado.", now_ms)
            c3.update(bloqueo=None, sin_observacion=None)
            if cfg_err and self.mode == MODE_REAL:
                # No es un "deshabilitado" explícito: en real no se anuncia off, se deja el latido que había.
                self._no_latir(c3, cfg_err)
            else:
                self._latir(state, c3, MODE_OFF)
            return

        bloqueo = self._bloqueo()
        if bloqueo != c3.get("bloqueo"):
            self._event(state, bloqueo or "C3 desbloqueado.")
        c3["bloqueo"] = bloqueo
        if bloqueo:
            self._alert("bloqueo", bloqueo)
            if c3.get("confirmacion"):
                self._cancelar(state, c3, bloqueo, now_ms)
            c3["sin_observacion"] = None
            if self.mode == MODE_REAL:
                self._no_latir(c3, bloqueo)  # no puede proteger: en real no se anuncia off
            else:
                self._latir(state, c3, MODE_OFF)
            return
        self._clear("bloqueo")

        capital: Optional[Decimal] = None
        if fut is not None:
            capital, loan, extra = capital_futuros(prestamo or rescate.prestamo_snapshot(state), fut)
            c3["ultimo"] = {"ts": now_iso(self.clock()), "error": None, "capital": money(capital),
                            "margin_balance": money(fut["margin_balance"]), "prestamo_real": money(loan),
                            "en_vuelo": money(extra), "posiciones": fut.get("posiciones")}
        else:
            prev = c3.get("ultimo") if isinstance(c3.get("ultimo"), dict) else {}
            c3["ultimo"] = {**prev, "ts_error": now_iso(self.clock()),
                            "error": lectura_error or "Sin lectura de la cuenta de futuros."}
        c3["sin_observacion"] = self._sin_observacion(c3, now_ms)

        tope = rescate.tope_activo(state, rescate_cfg)
        self._tope = tope
        tope_real = tope is not None and tope.get("modo") == MODE_REAL
        if c3.get("c3_parado"):
            self._parado(state, c3, cfg, capital, now_ms, tope_real)
        elif tope_real:
            self._parado_por_tope(state, c3, tope, now_ms)
        elif c3.get("base") is None:
            if capital is not None:
                self._anclar(state, c3, cfg, capital, "habilitado", f"C3 habilitado: base {money(capital)} USDT "
                                                                    "(capital de futuros)")
        else:
            self._evaluar_piso(state, c3, cfg, capital, now_ms)
        self._latir(state, c3, self.mode)

    def _bloqueo(self) -> Optional[str]:
        if self.exchange != "binance":
            return "C3 solo funciona con Binance Futures (EXCHANGE)."
        if self.mode == MODE_REAL and self._apagar is None:
            return ("C3 en real bloqueado: falta FUTURESBOARD_DOCKER_URL (o las credenciales de futuros), sin eso no "
                    "puede ejecutar Apagar.")
        return None

    def _cambio_de_modo(self, state: Dict[str, Any], c3: Dict[str, Any], now_ms: int) -> None:
        prev = c3.get("modo")
        if prev == self.mode:
            return
        if prev is not None:
            self._event(state, f"Modo de C3: {prev} -> {self.mode}.")
        if c3.get("confirmacion"):
            self._cancelar(state, c3, f"cambió el modo de C3 ({prev} -> {self.mode}).", now_ms)
        corte = c3.get("corte") if isinstance(c3.get("corte"), dict) else None
        if c3.get("c3_parado") and corte and corte.get("modo") == MODE_DRY_RUN and self.mode != MODE_DRY_RUN:
            # Un corte simulado nunca pasa a real: passivbot nunca se detuvo.
            c3["c3_parado"] = False
            c3["corte"] = None
            texto = (f"Corte simulado del {corte.get('en')} descartado al pasar a {self.mode}: passivbot no se había "
                     "detenido. Se sigue con la base vigente.")
            self._historial(c3, "corte_descartado", texto, MODE_DRY_RUN)
            self._event(state, texto)
        c3["modo"] = self.mode

    def _nueva_generacion(self, state: Dict[str, Any], c3: Dict[str, Any], cfg: Dict[str, Any]) -> None:
        if c3.get("c3_parado") and isinstance(c3.get("corte"), dict):
            corte = c3["corte"]
            texto = (f"C3 habilitado otra vez: se descarta el corte del {corte.get('en')}. passivbot queda como esté "
                     "(si está detenido, START a mano desde el panel).")
            self._historial(c3, "corte_descartado", texto, corte.get("modo") or self.mode)
            self._event(state, texto)
        c3.update(generacion=cfg.get("generacion"), base=None, confirmacion=None, c3_parado=False, corte=None,
                  reinicios=[])

    # -- base ---------------------------------------------------------------------------
    def _anclar(self, state: Dict[str, Any], c3: Dict[str, Any], cfg: Dict[str, Any], capital: Decimal,
                motivo: str, titulo: str) -> None:
        pct = dec(cfg["tope_pct"])
        piso = piso_de(capital, pct)
        c3["base"] = {"capital": money(capital), "fijada_en": now_iso(self.clock()), "motivo": motivo,
                      "modo": self.mode}
        c3["confirmacion"] = None
        texto = f"{titulo}. Piso {money(piso)} USDT (tope {_pct_txt(pct)}%)."
        self._historial(c3, "base", texto, self.mode, capital=money(capital), piso=money(piso))
        self._event(state, texto)
        self._notify(f"🧭 <b>C3</b>: {esc(texto)}", self.mode == MODE_DRY_RUN)

    # -- piso y corte -----------------------------------------------------------------
    def _evaluar_piso(self, state: Dict[str, Any], c3: Dict[str, Any], cfg: Dict[str, Any],
                      capital: Optional[Decimal], now_ms: int) -> None:
        conf = c3.get("confirmacion") if isinstance(c3.get("confirmacion"), dict) else None
        if capital is None:
            return  # sin observación: la confirmación ni se confirma ni se cancela (decide la próxima lectura)
        base_capital = dec(c3["base"]["capital"])
        pct = dec(cfg["tope_pct"])
        piso = piso_de(base_capital, pct)
        if capital > piso:
            if conf is not None:
                self._cancelar(state, c3, f"el capital de futuros volvió a {money(capital)} USDT, arriba del piso "
                                          f"{money(piso)} USDT.", now_ms)
            return
        reinicio = ""
        if conf is not None:
            try:
                ultimo_ms = int(conf["ultimo_ms"])
                desde_ms = int(conf["desde_ms"])
            except (KeyError, TypeError, ValueError):
                conf, reinicio = None, " La confirmación guardada era inválida: arrancó de cero."
            else:
                if now_ms - ultimo_ms > self.max_hueco_ms():
                    reinicio = (f" La confirmación abierta el {conf.get('desde_en')} arrancó de cero: pasaron "
                                f"{(now_ms - ultimo_ms) // 1000} s sin observar el capital.")
                    conf = None
                elif now_ms < desde_ms:  # reloj hacia atrás
                    conf["desde_ms"] = desde_ms = now_ms
        if conf is None:
            if self.settings.confirm_seconds > 0:
                self._abrir_confirmacion(state, c3, base_capital, pct, piso, capital, now_ms, reinicio)
                return
            detalle = reinicio
        else:
            conf.update(ultimo_ms=now_ms, ultimo_en=_ms_iso(now_ms), ultimo_capital=money(capital))
            transcurrido = now_ms - desde_ms
            if transcurrido < self.settings.confirm_seconds * 1000:
                return
            detalle = (f" Confirmado: {transcurrido // 1000} s después del primer cruce ({conf.get('desde_en')}, "
                       f"capital {conf.get('capital')} USDT) sigue en o debajo del piso.")
        self._cortar(state, c3, base_capital, pct, piso, capital, now_ms, detalle)

    def _abrir_confirmacion(self, state: Dict[str, Any], c3: Dict[str, Any], base_capital: Decimal, pct: Decimal,
                            piso: Decimal, capital: Decimal, now_ms: int, nota: str) -> None:
        cancelada = _int(c3.get("cancelada_ms"))
        silenciosa = not nota and cancelada is not None and 0 <= now_ms - cancelada < AVISO_SILENCIO_MS
        confirm_s = self.settings.confirm_seconds
        c3["confirmacion"] = {
            "id": secrets.token_hex(8), "modo": self.mode, "desde_ms": now_ms, "desde_en": _ms_iso(now_ms),
            "capital": money(capital), "base": money(base_capital), "piso": money(piso), "pct": _pct_txt(pct),
            "ejecuta_desde_en": _ms_iso(now_ms + confirm_s * 1000), "ultimo_ms": now_ms,
            "ultimo_en": _ms_iso(now_ms), "ultimo_capital": money(capital), "silenciosa": silenciosa,
        }
        texto = (f"Piso EN CONFIRMACIÓN: capital de futuros {money(capital)} USDT <= piso {money(piso)} (base "
                 f"{money(base_capital)}, tope {_pct_txt(pct)}%). Se ejecuta Apagar si una lectura desde "
                 f"{c3['confirmacion']['ejecuta_desde_en']} ({confirm_s} s) sigue en o debajo; si vuelve arriba, se "
                 f"cancela.{nota}")
        self._event(state, texto)
        self._guardar(state)
        if not silenciosa:
            self._notify(f"⏳ <b>C3</b>: {esc(texto)}", self.mode == MODE_DRY_RUN)

    def _cancelar(self, state: Dict[str, Any], c3: Dict[str, Any], motivo: str, now_ms: int) -> None:
        conf = c3.get("confirmacion") if isinstance(c3.get("confirmacion"), dict) else {}
        c3["confirmacion"] = None
        c3["cancelada_ms"] = now_ms
        modo = conf.get("modo") if conf.get("modo") in (MODE_REAL, MODE_DRY_RUN) else self.mode
        texto = f"Confirmación del piso cancelada (primer cruce {conf.get('desde_en')}): {motivo} No se cortó."
        self._event(state, texto, modo)
        if conf.get("silenciosa") is not True:
            self._notify(f"✅ <b>C3</b>: {esc(texto)}", modo == MODE_DRY_RUN)

    def _cortar(self, state: Dict[str, Any], c3: Dict[str, Any], base_capital: Decimal, pct: Decimal, piso: Decimal,
                capital: Decimal, now_ms: int, nota: str) -> None:
        real = self.mode == MODE_REAL
        config_al_corte, contenedor = None, None
        auto, motivo_manual = True, None
        if real:
            config_al_corte = self._leer_forager()
            contenedor = self._estado_contenedor()
            # Sin cualquiera de las dos lecturas no se puede comprobar lo que el reinicio automático necesita.
            if contenedor is None:
                auto, motivo_manual = False, "no se pudo saber si passivbot estaba corriendo al momento del corte."
            elif contenedor is False:
                auto, motivo_manual = False, "passivbot ya estaba detenido al momento del corte."
            elif config_al_corte is None:
                auto = False
                motivo_manual = ("no se pudo leer la config de forager al momento del corte (no se sabe si estaba en "
                                 "graceful_stop ni con qué twe reiniciar).")
            elif config_al_corte.get("long_mode") not in (None, bot_control.MODE_NORMAL):
                auto = False
                motivo_manual = (f"al corte forager estaba con long_mode {config_al_corte.get('long_mode')}: START "
                                 "lo pasaría a normal.")
        corte = {
            "id": secrets.token_hex(8), "modo": self.mode, "origen": "c3", "desde_ms": now_ms, "en": _ms_iso(now_ms),
            "capital": money(capital), "base": money(base_capital), "piso": money(piso), "pct": _pct_txt(pct),
            "apagar": {"resultado": "pendiente", "intentos": 0, "ultimo_ms": None, "detalle": ""},
            "config_al_corte": config_al_corte, "contenedor_al_corte": contenedor,
            "auto": auto, "motivo_manual": motivo_manual,
            "indice": self._indice_nuevo(now_ms),
            "reinicio": {"resultado": None, "intentos": 0, "ultimo_ms": None, "detalle": "", "aviso": None},
        }
        c3.update(confirmacion=None, c3_parado=True, corte=corte)
        texto = (f"CORTE: capital de futuros {money(capital)} USDT <= piso {money(piso)} (base {money(base_capital)}, "
                 f"tope {_pct_txt(pct)}%).{nota}")
        if not auto:
            texto += f" Sin reinicio automático: {motivo_manual}"
        self._historial(c3, "corte", texto, self.mode, capital=money(capital), piso=money(piso), id=corte["id"])
        self._event(state, texto)
        # El corte queda persistido antes de Apagar. En real no se late hasta que Apagar termine bien: mientras tanto
        # passivbot ve el latido de antes del corte (si Apagar falla, el dead-man deja de abrir entradas).
        self._guardar(state)
        self._notify(f"🛑 <b>C3</b>: {esc(texto)}", not real)
        self._ejecutar_apagar(state, c3, corte, now_ms)

    def _parado_por_tope(self, state: Dict[str, Any], c3: Dict[str, Any], tope: Dict[str, Any], now_ms: int) -> None:
        """El tope del rescate (real) ya ejecutó Apagar: C3 queda parado sin Apagar propio y sin reinicio
        automático (el tope exige reconocerlo y un START a mano, que re-ancla la base)."""
        if c3.get("confirmacion"):
            self._cancelar(state, c3, "se disparó el tope de pérdida del rescate.", now_ms)
        corte = {
            "id": secrets.token_hex(8), "modo": self.mode, "origen": "tope_rescate", "desde_ms": now_ms,
            "en": _ms_iso(now_ms), "capital": None, "base": (c3.get("base") or {}).get("capital"), "piso": None,
            "pct": None, "apagar": {"resultado": "tope_rescate", "intentos": 0, "ultimo_ms": None,
                                    "detalle": f"Apagar del tope del rescate ({tope.get('disparado_en')})."},
            "config_al_corte": None, "contenedor_al_corte": None, "auto": False,
            "motivo_manual": "se disparó el tope de pérdida del rescate: reconocelo y hacé START a mano.",
            "indice": self._indice_nuevo(now_ms),
            "reinicio": {"resultado": None, "intentos": 0, "ultimo_ms": None, "detalle": "", "aviso": None},
        }
        c3.update(c3_parado=True, corte=corte)
        texto = ("C3 parado por el tope de pérdida del rescate (ya ejecutó Apagar). Sin reinicio automático: al "
                 "reconocer el tope y hacer START a mano, C3 re-ancla la base.")
        self._historial(c3, "corte", texto, self.mode, id=corte["id"], origen="tope_rescate")
        self._event(state, texto)

    def _ejecutar_apagar(self, state: Dict[str, Any], c3: Dict[str, Any], corte: Dict[str, Any], now_ms: int) -> None:
        ap = corte["apagar"]
        if corte.get("modo") != MODE_REAL:
            ap.update(resultado="simulado", detalle="Simulación: no se detuvo passivbot ni se cerró nada en Binance.")
            self._event(state, "HARÍA Apagar (detener passivbot, cancelar órdenes y cerrar posiciones). No se "
                               "ejecutó: modo dry_run.", MODE_DRY_RUN)
            return
        ap["intentos"] = int(ap.get("intentos") or 0) + 1
        ap.update(ultimo_ms=now_ms, resultado="pendiente", detalle="Apagar en curso.")
        try:
            self._guardar(state)  # si el proceso muere en medio, el ciclo siguiente lo ve cortado
        except OSError:
            log.exception("No se pudo persistir el intento de Apagar de C3: se ejecuta igual.")
        if self._apagar is None:
            outcome, payload = "error", {"ok": False, "error": "Apagar no disponible (falta FUTURESBOARD_DOCKER_URL)."}
        else:
            try:
                outcome, payload = self._apagar()
            except Exception as exc:  # defensivo: el corte nunca debe tirar el servicio
                log.exception("Apagar de C3: error inesperado")
                outcome, payload = "error", {"ok": False, "error": f"error inesperado ({exc.__class__.__name__})"}
        ap["resultado"] = "ok" if outcome == "ok" else "error"
        ap["detalle"] = str(payload.get("error") or payload.get("warning") or "Apagar completo.")[:500]
        self._event(state, f"Apagar por C3 (intento {ap['intentos']}): {ap['resultado']}. {ap['detalle']}")
        text = telegram_notify.format_bot_action("stop", {"modo": "apagar"}, outcome, payload, USUARIO_APAGAR)
        if text:
            self._notify(text)

    def _reintentar_apagar(self, state: Dict[str, Any], c3: Dict[str, Any], corte: Dict[str, Any],
                           now_ms: int) -> bool:
        """True si el Apagar del corte está completo (o simulado / hecho por el tope del rescate)."""
        ap = corte.setdefault("apagar", {"resultado": "pendiente", "intentos": 0, "ultimo_ms": None, "detalle": ""})
        resultado = ap.get("resultado")
        if resultado in ("ok", "simulado", "tope_rescate"):
            return True
        if corte.get("modo") != MODE_REAL or self.mode != MODE_REAL:
            return False  # un Apagar real pendiente solo lo reintenta el servicio en real
        intentos = int(ap.get("intentos") or 0)
        if resultado == "pendiente":
            if intentos == 0:
                self._ejecutar_apagar(state, c3, corte, now_ms)  # se cortó antes de arrancar
                return corte["apagar"].get("resultado") == "ok"
            ap.update(resultado="error", ultimo_ms=None,
                      detalle="Apagar cortado por un reinicio del servicio: no se sabe hasta dónde llegó.")
            texto = (f"El Apagar de C3 (intento {intentos}) quedó cortado por un reinicio del servicio. "
                     + ("Se reintenta ahora." if intentos < MAX_INTENTOS else ""))
            self._event(state, texto.strip())
            self._notify(f"⚠️ <b>C3</b>: {esc(texto.strip())}")
        if intentos >= MAX_INTENTOS:
            self._alert("apagar", f"Apagar de C3 falló {intentos} veces: requiere intervención manual (último error: "
                                  f"{ap.get('detalle')}). Para volver a empezar, deshabilitá y habilitá C3.")
            return False
        if now_ms - int(ap.get("ultimo_ms") or 0) < REINTENTO_MS:
            return False
        self._ejecutar_apagar(state, c3, corte, now_ms)
        return corte["apagar"].get("resultado") == "ok"

    # -- parado -------------------------------------------------------------------------
    def _parado(self, state: Dict[str, Any], c3: Dict[str, Any], cfg: Dict[str, Any], capital: Optional[Decimal],
                now_ms: int, tope_real: bool) -> None:
        corte = c3.get("corte")
        if not isinstance(corte, dict):
            c3["c3_parado"] = False  # defensivo: parado sin corte no puede reiniciar nunca
            self._event(state, "C3 estaba parado sin un corte registrado: se reanuda la vigilancia.")
            return
        if tope_real and corte.get("auto"):
            corte.update(auto=False, motivo_manual="se disparó el tope de pérdida del rescate: reconocelo y hacé "
                                                   "START a mano.")
            self._event(state, "Tope del rescate disparado con C3 parado: el reinicio automático queda en manual.")
        if not self._reintentar_apagar(state, c3, corte, now_ms):
            return
        stop_real = corte.get("origen") == "tope_rescate" or (corte.get("modo") == MODE_REAL
                                                              and self.mode == MODE_REAL)
        if stop_real and self._start_manual(state, c3, cfg, capital):
            return
        self._muestrear_indice(state, corte, cfg, now_ms)
        if not corte.get("auto"):
            return
        debido, motivo = self._reinicio_debido(corte, cfg, now_ms)
        if not debido:
            return
        if self._limite_alcanzado(state, c3, corte, cfg, now_ms):
            return
        bloqueo = rescate.start_bloqueado(self.data_dir)
        if bloqueo:
            self._aviso_reinicio(state, corte, f"reinicio esperando: {bloqueo}")
            return
        if capital is None:
            self._aviso_reinicio(state, corte, "reinicio esperando una lectura de futuros para re-anclar la base.")
            return
        if corte.get("modo") == MODE_DRY_RUN:
            plan = self._plan_start(corte, simulado=True)
            detalle = plan.get("error") or f"START con {plan.get('descripcion')}"
            self._event(state, f"HARÍA {detalle}. No se ejecutó: modo dry_run.", MODE_DRY_RUN)
            self._reanudar(state, c3, cfg, capital, "reinicio", f"Reinicio automático simulado ({motivo})", now_ms)
            return
        if self.mode != MODE_REAL:
            self._aviso_reinicio(state, corte, "reinicio debido, pero C3 está en dry_run: con un corte real no se "
                                               "reinicia hasta volver a real (o START a mano).")
            return
        self._reiniciar(state, c3, cfg, corte, capital, motivo, now_ms)

    def _aviso_reinicio(self, state: Dict[str, Any], corte: Dict[str, Any], texto: str) -> None:
        reinicio = corte.setdefault("reinicio", {})
        if reinicio.get("aviso") != texto:
            reinicio["aviso"] = texto
            self._event(state, texto)

    def _start_manual(self, state: Dict[str, Any], c3: Dict[str, Any], cfg: Dict[str, Any],
                      capital: Optional[Decimal]) -> bool:
        """passivbot corriendo con C3 parado (START a mano desde el panel): re-ancla y vuelve a vigilar."""
        if rescate.start_bloqueado(self.data_dir):
            return False  # con START bloqueado, un contenedor corriendo no es un START del panel
        if self._estado_contenedor() is not True:
            return False
        if capital is None:
            self._aviso_reinicio(state, c3["corte"], "passivbot está corriendo otra vez: C3 espera una lectura de "
                                                     "futuros para re-anclar la base.")
            return True
        if _start_automatico_intentado(c3["corte"]):
            # El START automático dio error (timeout incierto del proxy, verify_running) pero dejó passivbot
            # corriendo: es el reinicio de C3 y cuenta para el límite. Un START a mano después de un intento
            # automático fallido también cuenta (no se pueden distinguir): queda del lado seguro.
            self._reanudar(state, c3, cfg, capital, "reinicio", "passivbot en ejecución después de un START automático "
                           "que había dado error (cuenta como reinicio automático)", self._now_ms())
            return True
        self._reanudar(state, c3, cfg, capital, "reinicio_manual", "passivbot en ejecución otra vez (START a mano)",
                       self._now_ms(), contar=False)
        return True

    def _limite_alcanzado(self, state: Dict[str, Any], c3: Dict[str, Any], corte: Dict[str, Any],
                          cfg: Dict[str, Any], now_ms: int) -> bool:
        maximo = int(cfg["max_reinicios"])
        ventana_ms = int(cfg["ventana_dias"]) * MS_DIA
        recientes = [r for r in c3["reinicios"] if isinstance(r, dict) and r.get("modo") == corte.get("modo")
                     and _int(r.get("ms")) is not None and now_ms - r["ms"] < ventana_ms]
        if len(recientes) < maximo:
            return False
        motivo = (f"límite de reinicios automáticos alcanzado ({len(recientes)} en {cfg['ventana_dias']} días, "
                  f"máximo {maximo}): queda parado esperando acción manual (START desde el panel).")
        corte.update(auto=False, motivo_manual=motivo)
        self._historial(c3, "limite", motivo, corte.get("modo") or self.mode)
        self._event(state, motivo)
        self._notify(f"⛔ <b>C3</b>: {esc(motivo)}", corte.get("modo") == MODE_DRY_RUN)
        return True

    def _reinicio_debido(self, corte: Dict[str, Any], cfg: Dict[str, Any], now_ms: int) -> Tuple[bool, str]:
        politica = cfg.get("politica")
        desde_ms = int(corte["desde_ms"])
        dias = dec(cfg["cooldown_dias"])
        cooldown_ok = Decimal(now_ms - desde_ms) >= dias * MS_DIA
        rebote_en = (corte.get("indice") or {}).get("rebote_en")
        if politica in (POLITICA_REBOTE, POLITICA_AMBOS) and rebote_en:
            idx = corte["indice"]
            return True, (f"rebote: índice {idx.get('rebote_indice')} >= mínimo {idx.get('min')} × "
                          f"(1 + {cfg['rebote_pct']}%) al {rebote_en}")
        if politica in (POLITICA_COOLDOWN, POLITICA_AMBOS) and cooldown_ok:
            return True, f"cooldown de {_pct_txt(dias)} días desde el corte ({corte.get('en')})"
        return False, ""

    # -- reinicio -----------------------------------------------------------------------
    def _plan_start(self, corte: Dict[str, Any], simulado: bool = False) -> Dict[str, Any]:
        """Con qué START se reinicia: el preset que quedó en la config de forager (Apagar no la toca).

        Preset conocido (bajo/medio/alto) => START con ese preset. "personalizado" => START sin tocar twe_long /
        twe_short, solo si siguen iguales a los del corte (si cambiaron, no se adivina: reinicio manual).
        """
        al_corte = corte.get("config_al_corte") if isinstance(corte.get("config_al_corte"), dict) else None
        if al_corte is None and not simulado:
            return {"error": "no se leyó la config de forager al corte: no se puede comprobar que no cambió."}
        try:
            actual = self._resumen_forager(self._forager()) if self._forager else None
        except Exception as exc:  # noqa: BLE001 - un error de lectura puede ser pasajero: se reintenta
            actual = None
            if not simulado:
                return {"error": f"no se pudo leer la config de forager ({exc.__class__.__name__}).",
                        "transitorio": True}
        if actual is None:
            if simulado:
                return {"descripcion": "la config de forager tal como quedó (sin leerla: modo dry_run)"}
            return {"error": "no se pudo leer la config de forager (falta FUTURESBOARD_FORAGER_CONFIG)."}
        if al_corte and (str(al_corte.get("twe_long")) != str(actual.get("twe_long"))
                         or str(al_corte.get("twe_short")) != str(actual.get("twe_short"))):
            return {"error": (f"la config de forager cambió desde el corte (twe {al_corte.get('twe_long')}/"
                              f"{al_corte.get('twe_short')} -> {actual.get('twe_long')}/{actual.get('twe_short')}).")}
        preset = actual.get("preset")
        if preset in bot_control.RISK_PRESETS:
            return {"riesgo": preset, "mantener_twe": False, "descripcion": f"el preset {preset}"}
        if preset == "personalizado":
            return {"riesgo": None, "mantener_twe": True,
                    "descripcion": f"la config personalizada (twe {actual.get('twe_long')}/{actual.get('twe_short')})"}
        return {"error": f"preset de forager {preset!r}: no se puede reiniciar solo."}

    def _reiniciar(self, state: Dict[str, Any], c3: Dict[str, Any], cfg: Dict[str, Any], corte: Dict[str, Any],
                   capital: Decimal, motivo: str, now_ms: int) -> None:
        rein = corte.setdefault("reinicio", {"resultado": None, "intentos": 0, "ultimo_ms": None, "detalle": ""})
        intentos = int(rein.get("intentos") or 0)
        if rein.get("resultado") in ("error", "pendiente"):
            if intentos >= MAX_INTENTOS:
                self._alert("reinicio", f"El reinicio automático de C3 falló {intentos} veces: queda parado esperando "
                                        f"acción manual (último error: {rein.get('detalle')}).")
                return
            if now_ms - int(rein.get("ultimo_ms") or 0) < REINTENTO_MS:
                return
        plan = self._plan_start(corte)
        if plan.get("error") and not plan.get("transitorio"):
            texto = f"Sin reinicio automático: {plan['error']} Queda parado esperando acción manual."
            corte.update(auto=False, motivo_manual=plan["error"])
            self._historial(c3, "reinicio_fallido", texto, MODE_REAL)
            self._event(state, texto)
            self._notify(f"⚠️ <b>C3</b>: {esc(texto)}")
            return
        rein.update(intentos=intentos + 1, ultimo_ms=now_ms, resultado="pendiente",
                    detalle=f"START con {plan.get('descripcion')} en curso.")
        self._guardar(state)
        self._latir(state, c3, self.mode)  # el lock puede tardar: que passivbot no vea el latido viejo
        if plan.get("transitorio"):
            outcome, payload = "error", {"ok": False, "error": plan["error"]}
        elif self._start is None:
            outcome, payload = "error", {"ok": False, "error": "START no disponible (falta FUTURESBOARD_DOCKER_URL)."}
        else:
            try:
                outcome, payload = self._start(plan)
            except Exception as exc:  # defensivo
                log.exception("START de C3: error inesperado")
                outcome, payload = "error", {"ok": False, "error": f"error inesperado ({exc.__class__.__name__})"}
        if outcome == "ok":
            rein.update(resultado="ok",
                        detalle=f"START con {plan['descripcion']}: {payload.get('docker_action', 'ok')}.")
            self._reanudar(state, c3, cfg, capital, "reinicio",
                           f"Reinicio automático ({motivo}): START con {plan['descripcion']}", now_ms)
            return
        if outcome == "ya_corriendo":
            if intentos > 0:  # un intento anterior con error pudo haberlo arrancado: cuenta (lado seguro)
                rein.update(resultado="ok", detalle="passivbot ya estaba corriendo después de un START automático con "
                                                    "error: cuenta como reinicio automático.")
                self._reanudar(state, c3, cfg, capital, "reinicio", "passivbot ya estaba en ejecución después de un "
                               "START automático que había dado error", now_ms)
                return
            rein.update(resultado="ok", detalle="passivbot ya estaba corriendo (START a mano): no se reinició.")
            self._reanudar(state, c3, cfg, capital, "reinicio_manual", "passivbot ya estaba en ejecución (START a "
                                                                      "mano)", now_ms, contar=False)
            return
        if outcome == "bloqueado":
            rein.update(resultado=None, intentos=intentos, detalle=str(payload.get("error") or ""))
            self._aviso_reinicio(state, corte, f"reinicio esperando: {payload.get('error')}")
            return
        rein.update(resultado="error", detalle=str(payload.get("error") or "error")[:500])
        texto = (f"Reinicio automático de C3 (intento {rein['intentos']}) falló: {rein['detalle']} "
                 + ("Se reintenta en 2 min." if rein["intentos"] < MAX_INTENTOS else "Requiere acción manual."))
        self._event(state, texto)
        self._notify(f"❌ <b>C3</b>: {esc(texto)}")

    def _reanudar(self, state: Dict[str, Any], c3: Dict[str, Any], cfg: Dict[str, Any], capital: Decimal,
                  tipo: str, titulo: str, now_ms: int, contar: bool = True) -> None:
        corte = c3.get("corte") or {}
        modo = corte.get("modo") or self.mode
        if contar:
            c3["reinicios"].append({"ms": now_ms, "en": _ms_iso(now_ms), "modo": modo, "corte": corte.get("id")})
            c3["reinicios"] = c3["reinicios"][-MAX_HISTORIAL:]
        c3.update(c3_parado=False, corte=None, confirmacion=None)
        pct = dec(cfg["tope_pct"])
        piso = piso_de(capital, pct)
        c3["base"] = {"capital": money(capital), "fijada_en": now_iso(self.clock()), "motivo": tipo, "modo": modo}
        texto = (f"{titulo}. Base re-anclada en {money(capital)} USDT (capital de futuros), piso {money(piso)} USDT "
                 f"(tope {_pct_txt(pct)}%).")
        self._historial(c3, tipo, texto, modo, capital=money(capital), piso=money(piso), corte=corte.get("id"))
        self._event(state, texto, modo)
        self._notify(f"▶️ <b>C3</b>: {esc(texto)}", modo == MODE_DRY_RUN)

    # -- índice de rebote ---------------------------------------------------------------
    def _indice_nuevo(self, now_ms: int) -> Dict[str, Any]:
        return {"pares": list(self.settings.pares), "ref_ms": (now_ms // MS_MINUTO) * MS_MINUTO, "ref": None,
                "ultimo_open_ms": None, "min": "1", "actual": None, "actual_en": None, "muestras": 0,
                "rebote_en": None, "rebote_indice": None, "error": None}

    def _muestrear_indice(self, state: Dict[str, Any], corte: Dict[str, Any], cfg: Dict[str, Any],
                          now_ms: int) -> None:
        """Índice equiponderado: promedio de precio / precio al corte, al cierre de cada vela de 1 h posterior.

        El mínimo arranca en 1 (el corte). Por cada muestra, en orden: si ``índice >= mínimo × (1 + X%)`` hay rebote;
        si no y ``índice < mínimo``, baja el mínimo. Igual que el backtest (``c3_stop_reinicio``). Después de un
        corte del servicio se procesan en orden las velas que faltan.
        """
        idx = corte.get("indice")
        if not isinstance(idx, dict) or idx.get("rebote_en") or self._klines is None:
            return
        pares = [p for p in idx.get("pares") or [] if isinstance(p, str)]
        if not pares:
            return
        try:
            if idx.get("ref") is None:
                ref_ms = int(idx["ref_ms"])
                if now_ms < ref_ms + MS_MINUTO + VELA_MARGEN_MS:
                    return  # la vela de 1 min del corte todavía no cerró
                ref = {}
                for sym in pares:
                    rows = self._klines(sym, "1m", ref_ms, 1)
                    row = next((r for r in rows if int(r[0]) == ref_ms), None)
                    if row is None:
                        raise C3Error(f"sin vela de 1 min de {sym} al corte")
                    ref[sym] = format(dec(row[4]), "f")
                idx["ref"] = ref
            ref_dec = {s: dec(v) for s, v in idx["ref"].items()}
            corte_ms = int(corte["desde_ms"])
            proxima = (int(idx["ultimo_open_ms"]) + MS_HORA if idx.get("ultimo_open_ms") is not None
                       else (corte_ms // MS_HORA) * MS_HORA)
            if now_ms < proxima + MS_HORA + VELA_MARGEN_MS:
                return  # la próxima vela de 1 h todavía no cerró
            cerradas = (now_ms - VELA_MARGEN_MS - proxima) // MS_HORA
            limite = int(min(MAX_VELAS_POR_PEDIDO, max(1, cerradas)))
            closes: Dict[str, Dict[int, Decimal]] = {}
            for sym in pares:
                rows = self._klines(sym, "1h", proxima, limite)
                closes[sym] = {int(r[0]): dec(r[4]) for r in rows
                               if int(r[0]) >= proxima and int(r[0]) + MS_HORA + VELA_MARGEN_MS <= now_ms}
        except (bot_control.BotControlError, C3Error, KeyError, TypeError, ValueError, IndexError) as exc:
            texto = f"No se pudo actualizar el índice de rebote: {exc}"
            idx["error"] = texto[:300]
            self._alert("indice", texto, corte.get("modo") == MODE_DRY_RUN)
            return
        idx["error"] = None
        self._clear("indice", "Índice de rebote actualizado otra vez.")
        rebote = dec(cfg["rebote_pct"]) / 100
        minimo = dec(idx.get("min") or "1")
        open_ms = proxima
        ultimas = [max(closes[s]) for s in pares if closes[s]]
        faltan = sorted(s for s in pares if proxima not in closes[s])
        if faltan and ultimas and max(ultimas) > proxima:
            self._alert("indice_hueco", f"Falta la vela de 1 h de {_ms_iso(proxima)} de {', '.join(faltan)} (otros "
                                        "pares ya tienen velas posteriores): el índice no avanza hasta que aparezca.",
                        corte.get("modo") == MODE_DRY_RUN)
        elif not faltan:
            self._clear("indice_hueco")
        while all(open_ms in closes[s] for s in pares):
            valor = sum((closes[s][open_ms] / ref_dec[s] for s in pares), Decimal(0)) / len(pares)
            idx.update(ultimo_open_ms=open_ms, actual=_ratio_txt(valor), actual_en=_ms_iso(open_ms + MS_HORA),
                       muestras=int(idx.get("muestras") or 0) + 1)
            if valor >= minimo * (1 + rebote):
                idx.update(rebote_en=_ms_iso(open_ms + MS_HORA), rebote_indice=_ratio_txt(valor))
                self._event(state, f"Rebote del índice: {_ratio_txt(valor)} >= mínimo {_ratio_txt(minimo)} × "
                                   f"(1 + {cfg['rebote_pct']}%) al cierre de la vela de {_ms_iso(open_ms)}.",
                            corte.get("modo"))
                break
            if valor < minimo:
                minimo = valor
            open_ms += MS_HORA
        idx["min"] = format(minimo.quantize(Decimal("1e-10"), rounding=decimal.ROUND_DOWN).normalize(), "f")

    # -- Docker / forager ---------------------------------------------------------------
    def _estado_contenedor(self) -> Optional[bool]:
        if self._contenedor is None:
            return None
        try:
            return self._contenedor()
        except Exception:  # noqa: BLE001 - sin dato
            log.warning("C3: no se pudo consultar el contenedor de passivbot.")
            return None

    @staticmethod
    def _resumen_forager(cfg: Dict[str, Any]) -> Dict[str, Any]:
        try:
            long_mode = bot_control.normalize_mode(cfg.get("long_mode"), "long_mode")
        except bot_control.InvalidModeError:
            long_mode = str(cfg.get("long_mode"))
        return {"preset": bot_control.detect_preset(cfg), "twe_long": cfg.get("twe_long"),
                "twe_short": cfg.get("twe_short"), "long_mode": long_mode}

    def _leer_forager(self) -> Optional[Dict[str, Any]]:
        if self._forager is None:
            return None
        try:
            return self._resumen_forager(self._forager())
        except Exception:  # noqa: BLE001 - sin dato
            log.warning("C3: no se pudo leer la config de forager al cortar.")
            return None


# --------------------------------------------------------------------------------------
# START del panel con el lock de acciones (reinicio automático)
# --------------------------------------------------------------------------------------


def make_start(data_dir: pathlib.Path, bot_settings: bot_control.Settings,
               docker: Optional[bot_control.DockerClient] = None,
               lock_timeout: float = rescate.APAGAR_LOCK_TIMEOUT_S) -> Callable[[Dict[str, Any]],
                                                                               Tuple[str, Dict[str, Any]]]:
    """START del panel (``bot_control.start_bot`` + ``verify_running``) bajo el mismo lock de acciones.

    Dentro del lock vuelve a mirar el bloqueo de START del rescate y si passivbot ya corre (un START a mano
    ganó la carrera: no se reinicia de nuevo).
    """
    store = bot_control.Store(pathlib.Path(data_dir))

    def start(plan: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        deadline = time.monotonic() + lock_timeout
        handle = store.try_lock()
        while handle is None and time.monotonic() < deadline:
            time.sleep(rescate.LOCK_POLL_S)
            handle = store.try_lock()
        params = {"riesgo": plan.get("riesgo"), "mantener_twe": bool(plan.get("mantener_twe")),
                  "origen": "c3_reinicio"}
        if handle is None:
            return "error", {"ok": False, "error": "No se pudo tomar el lock de acciones del panel (hay una acción en "
                                                   "curso). Se reintenta."}
        try:
            bloqueo = rescate.start_bloqueado(pathlib.Path(data_dir))
            if bloqueo:
                return "bloqueado", {"ok": False, "error": bloqueo}
            client = docker or bot_control.DockerClient(bot_settings.docker_url)
            try:
                if client.inspect(bot_settings.container).get("running"):
                    return "ya_corriendo", {"ok": True, "docker_action": "ninguna"}
                payload = bot_control.start_bot(bot_settings, store, plan.get("riesgo"), docker=client,
                                                mantener_twe=bool(plan.get("mantener_twe")))
                bot_control.verify_running(bot_settings, docker=client)
                outcome = "ok"
            except bot_control.BotControlError as exc:
                payload = {**exc.payload, "ok": False, "error": str(exc)}
                outcome = "error"
        finally:
            handle.release()
        try:
            store.audit(USUARIO_START, "start", params, outcome, jobs.audit_detail(payload), None)
        except OSError:
            log.exception("No se pudo escribir el log de auditoría")
        return outcome, payload

    return start


# --------------------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------------------


def panel_status(data_dir: pathlib.Path, now: Optional[float] = None) -> Dict[str, Any]:
    """Estado de C3 para la UI y las tarjetas (lee archivos; no consulta Binance)."""
    now = time.time() if now is None else now
    data_dir = pathlib.Path(data_dir)
    cfg, cfg_err = load_config(data_dir)
    state, state_err = rescate.load_state(data_dir)
    c3, c3_err = get_state(dict(state))
    servicio = state.get("servicio") if isinstance(state.get("servicio"), dict) else {}
    vivo = False
    if servicio.get("heartbeat"):
        try:
            age = now - dt.datetime.fromisoformat(servicio["heartbeat"]).timestamp()
            vivo = age <= 3 * int(servicio.get("intervalo") or rescate.DEFAULT_INTERVAL) + 30
        except (TypeError, ValueError):
            vivo = False
    base = c3.get("base") if isinstance(c3.get("base"), dict) else None
    ultimo = c3.get("ultimo") if isinstance(c3.get("ultimo"), dict) else None
    # Lo decide el servicio (conoce la confirmación y el intervalo): el panel no recalcula umbrales.
    ciego = c3.get("sin_observacion") if isinstance(c3.get("sin_observacion"), dict) else None
    out: Dict[str, Any] = {
        "config": {k: cfg.get(k) for k in (*CAMPOS, "generacion", "actualizado_en", "actualizado_por")},
        "limites": {**{k: [str(lo), str(hi)] for k, (lo, hi) in LIMITES.items()},
                    **{k: [str(lo), str(hi)] for k, (lo, hi) in LIMITES_ENTEROS.items()}},
        "politicas": list(POLITICAS),
        "errores": [e for e in (cfg_err, state_err, c3_err) if e],
        "modo": c3.get("modo"),
        "servicio_vivo": vivo,
        "latido": c3.get("latido"),
        "bloqueo": c3.get("bloqueo"),
        "c3_parado": c3.get("c3_parado") is True,
        "base": base,
        "piso": None,
        "capital": ultimo.get("capital") if ultimo else None,
        "ultimo": ultimo,
        "sin_observacion": ciego,
        "distancia_usdt": None,
        "distancia_pct": None,
        "confirmacion": c3.get("confirmacion"),
        "corte": c3.get("corte"),
        "proximo_reinicio": None,
        "reinicios_recientes": 0,
        "historial": list(reversed(c3.get("historial") or []))[:20],
        "ultimo_corte": next((h for h in reversed(c3.get("historial") or []) if h.get("tipo") == "corte"), None),
        "ultimo_reinicio": next((h for h in reversed(c3.get("historial") or [])
                                 if h.get("tipo") in ("reinicio", "reinicio_manual")), None),
        "aviso": AVISO,
    }
    try:
        if base and cfg.get("tope_pct") is not None:
            piso = piso_de(dec(base["capital"]), dec(cfg["tope_pct"]))
            out["piso"] = money(piso)
            if out["capital"] is not None and ciego is None:  # sin distancia sobre un capital viejo
                distancia = dec(out["capital"]) - piso
                out["distancia_usdt"] = money(distancia)
                cap = dec(out["capital"])
                if cap > 0:
                    out["distancia_pct"] = format((distancia / cap * 100).quantize(Decimal("0.01")), "f")
        ventana_ms = int(cfg["ventana_dias"]) * MS_DIA
        now_ms = int(now * 1000)
        out["reinicios_recientes"] = len([r for r in c3.get("reinicios") or [] if isinstance(r, dict)
                                          and _int(r.get("ms")) is not None and now_ms - r["ms"] < ventana_ms])
        out["proximo_reinicio"] = _proximo_reinicio(c3, cfg)
    except (KeyError, TypeError, ValueError):
        out["errores"].append("Estado de C3 con valores inválidos: revisar rescate_state.json.")
    if not cfg.get("habilitado") or cfg_err:
        out["estado"] = "deshabilitado"
    elif c3.get("modo") in (None, MODE_OFF):
        out["estado"] = "off"
    elif c3.get("bloqueo"):
        out["estado"] = "bloqueado"
    elif c3.get("c3_parado"):
        out["estado"] = "parado"
    elif ciego is not None:
        out["estado"] = "sin_observacion"
    elif c3.get("confirmacion"):
        out["estado"] = "en_confirmacion"
    elif base is None:
        out["estado"] = "base_pendiente"
    else:
        out["estado"] = "operando"
    return out


def _proximo_reinicio(c3: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[str]:
    """Texto con el próximo reinicio estimado (None si no está parado)."""
    corte = c3.get("corte") if isinstance(c3.get("corte"), dict) else None
    if not c3.get("c3_parado") or corte is None:
        return None
    if not corte.get("auto"):
        return f"manual: {corte.get('motivo_manual')}"
    partes: List[str] = []
    politica = cfg.get("politica")
    idx = corte.get("indice") if isinstance(corte.get("indice"), dict) else {}
    if politica in (POLITICA_REBOTE, POLITICA_AMBOS):
        if idx.get("rebote_en"):
            partes.append(f"rebote alcanzado el {idx['rebote_en']}")
        else:
            minimo = dec(idx.get("min") or "1")
            objetivo = minimo * (1 + dec(cfg["rebote_pct"]) / 100)
            actual = idx.get("actual")
            partes.append(f"cuando el índice llegue a {_ratio_txt(objetivo)} (mínimo {_ratio_txt(minimo)} + "
                          f"{cfg['rebote_pct']}%; ahora {actual if actual else 'sin muestras'})")
    if politica in (POLITICA_COOLDOWN, POLITICA_AMBOS):
        fin = int(corte["desde_ms"]) + int(dec(cfg["cooldown_dias"]) * MS_DIA)
        partes.append(f"cooldown hasta {_ms_iso(fin)}")
    return " o ".join(partes)
