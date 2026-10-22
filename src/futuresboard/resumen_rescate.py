"""Resumen del rescate para las tarjetas del dashboard principal (``/`` y ``/dashboard/<a>/<b>``).

Solo lee lo que ya persiste el servicio rescate (``rescate_config.json`` y ``rescate_state.json``): **nunca**
consulta Binance desde la request de la página. Si algo no se puede leer, devuelve un resumen con
``error`` y la página se dibuja igual ("sin datos").

Semáforo del ratio margen / mantenimiento (Binance liquida en 1):

* rojo: debajo del umbral de inyección (el rescate inyecta ahí);
* amarillo: entre el umbral y ``umbral × 1,5`` (cerca de la inyección);
* verde: desde ``umbral × 1,5`` (o sin posiciones abiertas).
"""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
import time
from decimal import Decimal
from typing import Any
from typing import Dict
from typing import Optional

from futuresboard import c3
from futuresboard import rescate

log = logging.getLogger("futuresboard.resumen_rescate")

# Banda amarilla del semáforo: hasta umbral × este factor.
FACTOR_AMARILLO = Decimal("1.5")

COMO_ACTIVAR = ("Para ver el saldo de la billetera de rescate, levantá el servicio rescate con "
                "FUTURESBOARD_RESCATE_MODE=dry_run (no transfiere nada) o real. Ver docs/rescate.md.")


def _parse_ts(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


def antiguedad(ts: Any, now: float) -> Optional[str]:
    """'hace 40 s', 'hace 12 min', 'hace 3 h', 'hace 2 d' (None si el ts no sirve)."""
    moment = _parse_ts(ts)
    if moment is None:
        return None
    segundos = max(0, int(now - moment))
    if segundos < 90:
        return f"hace {segundos} s"
    if segundos < 90 * 60:
        return f"hace {segundos // 60} min"
    if segundos < 48 * 3600:
        return f"hace {segundos // 3600} h"
    return f"hace {segundos // 86400} d"


def _dec(value: Any) -> Optional[Decimal]:
    try:
        return rescate.dec(value)
    except ValueError:
        return None


def semaforo(ratio: Optional[Decimal], umbral: Optional[Decimal], posiciones: Any) -> str:
    """verde / amarillo / rojo / gris (sin datos)."""
    if ratio is None:
        return "verde" if posiciones == 0 else "gris"
    if umbral is None:
        return "gris"
    if ratio < umbral:
        return "rojo"
    if ratio < umbral * FACTOR_AMARILLO:
        return "amarillo"
    return "verde"


def _pct(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def tarjetas(data_dir: pathlib.Path, now: Optional[float] = None) -> Dict[str, Any]:
    """Resumen para las tarjetas. Nunca lanza: ante cualquier error devuelve ``{"error": ...}``."""
    now = time.time() if now is None else now
    try:
        return _tarjetas(pathlib.Path(data_dir), now)
    except Exception:  # defensivo: la página principal no se rompe por el rescate
        log.exception("No se pudo armar el resumen del rescate para el dashboard")
        return {"error": "No se pudo leer el estado del rescate.", "como_activar": COMO_ACTIVAR}


def _tarjetas(data_dir: pathlib.Path, now: float) -> Dict[str, Any]:
    status = rescate.panel_status(data_dir, now=now)
    state, state_err = rescate.load_state(data_dir)
    cfg = status["config"]
    servicio = status["servicio"]
    modo = servicio.get("modo")
    vivo = bool(servicio.get("vivo"))
    ultimo = status.get("ultimo") if isinstance(status.get("ultimo"), dict) else {}
    lectura_ok = bool(ultimo) and not ultimo.get("error") and ultimo.get("capital") is not None
    simulado = modo == rescate.MODE_DRY_RUN

    out: Dict[str, Any] = {
        "error": None,
        "errores": status.get("errores") or [],
        "modo": modo,
        "simulado": simulado,
        "habilitado": bool(cfg.get("habilitado")),
        "servicio_vivo": vivo,
        "latido": servicio.get("heartbeat"),
        "latido_hace": antiguedad(servicio.get("heartbeat"), now),
        "testnet": bool(servicio.get("testnet")),
        "como_activar": COMO_ACTIVAR,
        "lectura_error": ultimo.get("error") if ultimo.get("error") else None,
    }

    # Billetera de rescate: USDT libre en spot (real). Sin lectura OK en el último ciclo, el último saldo leído.
    spot: Dict[str, Any] = {"usdt": None, "ts": None, "hace": None, "fuente": None, "efectivo": None}
    if lectura_ok and ultimo.get("spot_usdt_real") is not None:
        spot.update(usdt=ultimo["spot_usdt_real"], ts=ultimo.get("ts"), fuente="ultima_lectura")
        if simulado and ultimo.get("spot_usdt") != ultimo.get("spot_usdt_real"):
            spot["efectivo"] = ultimo.get("spot_usdt")
    else:
        ref = state.get("spot_ref") if not state_err else None
        if isinstance(ref, dict) and ref.get("usdt") is not None:
            spot.update(usdt=ref["usdt"], ts=ref.get("ts"), fuente="ultimo_saldo_leido")
    spot["hace"] = antiguedad(spot["ts"], now)
    out["spot"] = spot

    out["prestamo"] = {"usdt": status.get("prestamo"), "simulado": status.get("prestamo_modo") == rescate.MODE_DRY_RUN}

    # Capital total (futuros + spot) y su variación contra la base (solo con el tope armado).
    capital: Dict[str, Any] = {"usdt": None, "ts": None, "hace": None, "base": None, "variacion_usdt": None,
                               "variacion_pct": None, "limite": status.get("limite_tope")}
    if lectura_ok:
        capital.update(usdt=ultimo["capital"], ts=ultimo.get("ts"), hace=antiguedad(ultimo.get("ts"), now))
        base = status.get("base")
        cap = _dec(ultimo["capital"])
        base_cap = _dec(base.get("capital")) if isinstance(base, dict) else None
        if cap is not None and base_cap is not None and base_cap > 0:
            diff = cap - base_cap
            signo = "+" if diff >= 0 else ""
            capital.update(base=rescate.money(base_cap), variacion_usdt=signo + rescate.money(diff),
                           variacion_pct=signo + _pct(diff / base_cap * 100))
    out["capital"] = capital

    # Ratio margen / mantenimiento con semáforo.
    umbral = _dec(cfg.get("umbral_ratio")) if cfg.get("umbral_ratio") is not None else None
    ratio = _dec(ultimo.get("ratio")) if lectura_ok and ultimo.get("ratio") is not None else None
    posiciones = ultimo.get("posiciones") if lectura_ok else None
    out["ratio"] = {
        "valor": None if ratio is None else format(ratio.quantize(Decimal("0.01")), "f"),
        "posiciones": posiciones,
        "umbral": None if umbral is None else format(umbral.normalize(), "f"),
        "amarillo_hasta": None if umbral is None else format((umbral * FACTOR_AMARILLO).normalize(), "f"),
        "semaforo": semaforo(ratio, umbral, posiciones) if lectura_ok else "gris",
        "simulado": bool(ultimo.get("simulado")) if lectura_ok else False,
    }

    # Estado del tope de pérdida total.
    if status.get("tope"):
        tope_estado = "disparado"
    elif status.get("tope_confirmacion"):
        tope_estado = "en_confirmacion"
    elif status.get("limite_tope"):
        tope_estado = "armado"
    else:
        tope_estado = "sin_armar"
    out["tope"] = {"estado": tope_estado, "limite": status.get("limite_tope"), "pct": status.get("tope_pct"),
                   "start_bloqueado": status.get("start_bloqueado")}
    out["sin_datos"] = spot["usdt"] is None and not lectura_ok
    out["c3"] = _c3(data_dir, now)
    return out


def _c3(data_dir: pathlib.Path, now: float) -> Optional[Dict[str, Any]]:
    """Tarjeta de C3 (stop con reinicio): solo si está habilitado o quedó parado."""
    st = c3.panel_status(data_dir, now=now)
    if not st["config"].get("habilitado") and not st["c3_parado"]:
        return None
    ultimo = st.get("ultimo") or {}
    evento = None
    for h in st.get("historial") or []:
        if h.get("tipo") in ("corte", "reinicio", "reinicio_manual"):
            evento = h
            break
    return {
        "estado": st["estado"],
        "parado": st["c3_parado"],
        "simulado": st.get("modo") == c3.MODE_DRY_RUN,
        "piso": st.get("piso"),
        "distancia_usdt": st.get("distancia_usdt"),
        "distancia_pct": st.get("distancia_pct"),
        "capital_ts": ultimo.get("ts"),
        "capital_hace": antiguedad(ultimo.get("ts"), now),
        "proximo_reinicio": st.get("proximo_reinicio"),
        "ultimo_evento": None if evento is None else {"tipo": evento.get("tipo"), "ts": evento.get("ts"),
                                                      "hace": antiguedad(evento.get("ts"), now)},
        "errores": st.get("errores") or [],
    }
