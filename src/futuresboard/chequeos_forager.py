"""Chequeos de forager en cada ciclo sobre los pares aprobados (spec bot-estrategia § 2.3 y § 2.5).

forager descarta en silencio un par aprobado que deja de pasar sus filtros: el mínimo de Binance sube por encima de
``max_min_cost`` (BTC en real: ``minQty`` 0,001 × precio), el mercado deja de estar en TRADING, le faltan velas o es
más nuevo que ``market_age_threshold``. Si tenía posición queda en parada gradual y no reabre. Este módulo repite esos
filtros (``estrategia.DE_CICLO``, API pública de Binance con la caché de ``binance_publico``) cada
``update_interval_minutes`` de forager y:

* guarda el último estado por par en ``chequeos_forager.json`` (directorio de datos), que Bot muestra en "Pares ahora"
  sin llamar a Binance en el pedido web;
* avisa por Telegram **solo cuando cambia el estado** de un par (ok → descarta y descarta → ok), como el notifier: un
  estado malo que sigue no se repite en cada ciclo. Binance caído es ``sin_datos``: no cambia el estado conocido ni avisa.

Corre en un hilo del worker de gunicorn (``gunicorn_conf.post_worker_init``), nunca en los tests ni en un pedido web.
``FUTURESBOARD_CHEQUEOS_FORAGER=0`` lo apaga. No usa ``current_app``: el hilo recibe lo que necesita en ``Entorno``.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

from futuresboard import bot_control
from futuresboard import estrategia
from futuresboard import telegram_notify
from futuresboard.telegram_notify import esc

log = logging.getLogger("futuresboard.chequeos_forager")

ESTADO_FILE = "chequeos_forager.json"
ENV_FLAG = "FUTURESBOARD_CHEQUEOS_FORAGER"
INTERVALO_DEFECTO_MIN = 15
INTERVALO_MIN_MIN, INTERVALO_MAX_MIN = 5, 60
# Primera vuelta un rato después de arrancar el worker (que el panel responda primero).
ESPERA_INICIAL_S = 30
OK, DESCARTA, SIN_DATOS = "ok", "descarta", "sin_datos"

_hilo: Optional[threading.Thread] = None
_hilo_lock = threading.Lock()


@dataclass
class Entorno:
    forager_config: Optional[pathlib.Path]
    data_dir: pathlib.Path
    testnet: bool
    base_operativa: Optional[str]
    hoy: Callable[[], dt.date] = lambda: dt.datetime.now(dt.timezone.utc).date()


def intervalo_s(cfg: Dict[str, Any]) -> int:
    v = cfg.get("update_interval_minutes")
    minutos = v if isinstance(v, (int, float)) and not isinstance(v, bool) else INTERVALO_DEFECTO_MIN
    return int(max(INTERVALO_MIN_MIN, min(INTERVALO_MAX_MIN, minutos)) * 60)


def leer_estado(data_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((pathlib.Path(data_dir) / ESTADO_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("pares"), dict) else None


def _guardar(data_dir: pathlib.Path, estado: Dict[str, Any]) -> None:
    path = pathlib.Path(data_dir) / ESTADO_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def texto_aviso(symbol: str, estado: str, motivos: List[str]) -> str:
    if estado == DESCARTA:
        lineas = [f"⚠️ <b>forager descartaría {esc(symbol)}</b> (par aprobado en Estrategia)"]
        lineas += [f"• {esc(m)}" for m in motivos[:5]]
        lineas.append("Si tiene posición, queda en parada gradual y no reabre. Revisalo en Bot → Pares ahora.")
        return "\n".join(lineas)
    return f"✅ <b>{esc(symbol)} vuelve a pasar los filtros de forager</b>"


def _enviar(texto: str) -> bool:
    return telegram_notify.send(texto)


def _evaluar(symbol: str, ctx: estrategia.Contexto) -> Dict[str, Any]:
    res = estrategia.chequear_par(symbol, ctx, claves=estrategia.DE_CICLO)
    malos = [c for c in res["chequeos"] if c["estado"] == estrategia.BLOQUEA]
    sin_datos = [c for c in res["chequeos"] if c["estado"] == estrategia.SIN_DATOS]
    if malos:
        estado = DESCARTA
    elif sin_datos:
        estado = SIN_DATOS
    else:
        estado = OK
    return {"estado": estado, "motivos": [c["texto"] for c in (malos or sin_datos)],
            "chequeos": [{k: c[k] for k in ("clave", "estado", "texto")} for c in res["chequeos"]]}


def ciclo(entorno: Entorno) -> Dict[str, Any]:
    """Una vuelta: chequea los aprobados, guarda el estado y avisa los cambios. Nunca lanza."""
    ahora = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    previo = leer_estado(entorno.data_dir) or {"pares": {}}
    salida: Dict[str, Any] = {"ts": ahora, "pares": {}, "error": None, "endpoint": "Demo" if entorno.testnet else "real"}
    try:
        if entorno.forager_config is None:
            raise bot_control.DisabledError(f"Falta {bot_control.ENV_FORAGER_CONFIG}.")
        cfg = bot_control.read_forager_config(entorno.forager_config)
    except bot_control.BotControlError as exc:
        salida["error"] = str(exc)
        _guardar_seguro(entorno, salida)
        return salida
    salida["intervalo_s"] = intervalo_s(cfg)
    ctx = estrategia.Contexto.armar(cfg, entorno.forager_config, entorno.testnet, entorno.base_operativa,
                                    hoy=entorno.hoy(), manif={"usable": False})
    aprobados = [s for s in (cfg.get("approved_symbols_long") or []) if isinstance(s, str)]
    for symbol in aprobados:
        try:
            r = _evaluar(symbol, ctx)
        except Exception as exc:  # noqa: BLE001 - un par raro no corta el ciclo
            log.warning("Chequeo de forager de %s falló (%s)", symbol, exc.__class__.__name__)
            r = {"estado": SIN_DATOS, "motivos": [f"Error inesperado ({exc.__class__.__name__})."], "chequeos": []}
        prev_par = previo["pares"].get(symbol) or {}
        antes = prev_par.get("conocido")
        conocido = antes if r["estado"] == SIN_DATOS else r["estado"]
        r["conocido"] = conocido
        r["desde"] = prev_par.get("desde") if conocido == antes else ahora
        pendiente = prev_par.get("aviso_pendiente") if isinstance(prev_par.get("aviso_pendiente"), dict) else None
        cambio = r["estado"] != SIN_DATOS and conocido != antes and (antes is not None or conocido == DESCARTA)
        if cambio and pendiente is not None:
            # Volvió antes de que saliera el aviso del cambio anterior: no se avisa ninguno de los dos.
            pass
        elif cambio:
            _avisar(r, symbol, conocido, r["motivos"])
        elif pendiente is not None and pendiente.get("estado") == conocido:
            motivos = r["motivos"] if r["estado"] == conocido else list(pendiente.get("motivos") or [])
            _avisar(r, symbol, conocido, motivos)
        salida["pares"][symbol] = r
    _guardar_seguro(entorno, salida)
    return salida


def _avisar(r: Dict[str, Any], symbol: str, estado: str, motivos: List[str]) -> None:
    """Manda el aviso del cambio. Si no sale, queda ``aviso_pendiente`` en el par y se reintenta en el ciclo siguiente
    mientras siga en ese estado: el estado conocido avanza igual, porque Bot y el texto de "sin datos" lo usan
    (revisión, hallazgo 15). Sin Telegram configurado no queda pendiente: nunca saldría."""
    try:
        r["avisado"] = bool(_enviar(texto_aviso(symbol, estado, motivos)))
    except Exception:  # noqa: BLE001 - notificar nunca corta el ciclo
        r["avisado"] = False
    if not r["avisado"] and telegram_notify.TelegramConfig.from_env().enabled:
        r["aviso_pendiente"] = {"estado": estado, "motivos": list(motivos)}


def _guardar_seguro(entorno: Entorno, estado: Dict[str, Any]) -> None:
    try:
        _guardar(entorno.data_dir, estado)
    except OSError:
        log.exception("No se pudo guardar %s", ESTADO_FILE)


def _loop(entorno: Entorno) -> None:
    time.sleep(ESPERA_INICIAL_S)
    while True:
        espera = INTERVALO_DEFECTO_MIN * 60
        try:
            estado = ciclo(entorno)
            espera = int(estado.get("intervalo_s") or espera)
        except Exception:  # pragma: no cover - defensivo: el hilo no se muere
            log.exception("Error en el ciclo de chequeos de forager")
        time.sleep(espera)


def arrancar(app: Any) -> Optional[threading.Thread]:
    """Arranca el hilo (uno por proceso). None si está apagado por ``FUTURESBOARD_CHEQUEOS_FORAGER=0``, si el panel
    no tiene la config de forager o si no se puede leer la config de la app."""
    global _hilo
    if os.environ.get(ENV_FLAG, "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    settings = bot_control.Settings()
    if settings.forager_config is None:
        return None
    try:
        from futuresboard.config import state_dir

        cfg = app.config
        entorno = Entorno(forager_config=settings.forager_config, data_dir=state_dir(cfg),
                          testnet=bool(cfg.get("BINANCE_TESTNET")), base_operativa=cfg.get("API_BASE_URL"))
    except Exception:  # noqa: BLE001
        log.exception("No se pudieron arrancar los chequeos de forager")
        return None
    with _hilo_lock:
        if _hilo is not None and _hilo.is_alive():
            return _hilo
        _hilo = threading.Thread(target=_loop, args=(entorno,), name="chequeos-forager", daemon=True)
        _hilo.start()
        log.info("Chequeos de forager por ciclo: cada %s min, %s.", INTERVALO_DEFECTO_MIN,
                 "Demo" if entorno.testnet else "real")
        return _hilo
