"""Health checks y metricas del dashboard y de passivbot.

Endpoints (blueprint ``ops``):

* ``GET /health``         liveness del proceso web. Sin login y sin datos: solo ``{"status": "ok"}``.
* ``GET /api/bot/health`` estado detallado (requiere login): contenedor de passivbot, ultima
  orden/trade, posiciones abiertas, UPNL y edad del ultimo scrape, con status ok/warn/critical
  segun umbrales configurables por env.
* ``GET /metrics``        lo mismo en formato Prometheus text. Requiere login o
  ``Authorization: Bearer <FUTURESBOARD_METRICS_TOKEN>``.

Posiciones y UPNL salen de la DB que llena el scraper (no de Binance en vivo): no gastan peso
de API en cada consulta y ya respetan BINANCE_TESTNET porque el scraper usa ese endpoint. La
frescura de esos datos la mide ``scrape_age_seconds``.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import math
import os
import time
from typing import Any
from typing import Dict
from typing import Optional

from flask import Blueprint
from flask import current_app
from flask import request
from flask import Response

from futuresboard import bot_control
from futuresboard import db
from futuresboard import scraper

log = logging.getLogger(__name__)

ops = Blueprint("ops", __name__)

ENV_METRICS_TOKEN = "FUTURESBOARD_METRICS_TOKEN"

OK, WARN, CRITICAL, SKIPPED = "ok", "warn", "critical", "skipped"
_LEVEL = {OK: 0, WARN: 1, CRITICAL: 2}

# Fills: cierre (REALIZED_PNL), comision de cada ejecucion y liquidaciones/ADL.
TRADE_INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "ADLTRADE", "BUSTTRADE")

# (env, default). 0 o vacio deshabilita el umbral. Edades en segundos.
THRESHOLD_DEFAULTS: Dict[str, Optional[float]] = {
    "FUTURESBOARD_HEALTH_SCRAPE_WARN_SECONDS": 900,
    "FUTURESBOARD_HEALTH_SCRAPE_CRIT_SECONDS": 3600,
    "FUTURESBOARD_HEALTH_TRADE_WARN_SECONDS": 6 * 3600,
    "FUTURESBOARD_HEALTH_TRADE_CRIT_SECONDS": 24 * 3600,
    # UPNL total en la moneda de la cuenta (USDT). Negativos: warn si upnl <= valor.
    "FUTURESBOARD_HEALTH_UPNL_WARN": None,
    "FUTURESBOARD_HEALTH_UPNL_CRIT": None,
    # Cantidad maxima de posiciones abiertas.
    "FUTURESBOARD_HEALTH_POSITIONS_WARN": None,
    "FUTURESBOARD_HEALTH_POSITIONS_CRIT": None,
}


def load_thresholds() -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for name, default in THRESHOLD_DEFAULTS.items():
        raw = os.environ.get(name, "").strip()
        if not raw:
            out[name] = default
            continue
        try:
            value = float(raw)
        except ValueError:
            log.warning("%s=%r no es numerico; se usa el default %r", name, raw, default)
            out[name] = default
            continue
        # En umbrales de edad/cantidad 0 deshabilita; en UPNL 0 es un valor valido.
        out[name] = None if (value == 0 and "UPNL" not in name) else value
    return out


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat(timespec="seconds")


def _age(now: float, ts: Optional[float]) -> Optional[float]:
    if ts is None:
        return None
    return round(max(0.0, now - ts), 3)


def _check_max(value: Optional[float], warn: Optional[float], crit: Optional[float],
               unknown_detail: str) -> Dict[str, Any]:
    """Status para valores donde mas es peor (edades, cantidad de posiciones)."""
    if warn is None and crit is None:
        return {"status": SKIPPED, "detail": "sin umbral configurado"}
    if value is None:
        return {"status": WARN, "detail": unknown_detail}
    if crit is not None and value >= crit:
        return {"status": CRITICAL, "detail": f"{value:g} >= {crit:g}"}
    if warn is not None and value >= warn:
        return {"status": WARN, "detail": f"{value:g} >= {warn:g}"}
    return {"status": OK, "detail": ""}


def _check_min(value: Optional[float], warn: Optional[float], crit: Optional[float]) -> Dict[str, Any]:
    """Status para valores donde menos es peor (UPNL)."""
    if warn is None and crit is None:
        return {"status": SKIPPED, "detail": "sin umbral configurado"}
    if value is None:
        return {"status": WARN, "detail": "sin datos"}
    if crit is not None and value <= crit:
        return {"status": CRITICAL, "detail": f"{value:g} <= {crit:g}"}
    if warn is not None and value <= warn:
        return {"status": WARN, "detail": f"{value:g} <= {warn:g}"}
    return {"status": OK, "detail": ""}


def _bot_info(now: float, docker: Optional[bot_control.DockerClient]) -> Dict[str, Any]:
    settings = bot_control.Settings()
    info: Dict[str, Any] = {
        "enabled": settings.enabled,
        "container": settings.container,
        "running": None,
        "status": None,
        "started_at": None,
        "uptime_seconds": None,
        "error": None,
    }
    if not settings.enabled:
        info["check"] = {"status": SKIPPED, "detail": f"panel deshabilitado (falta {bot_control.ENV_DOCKER_URL})"}
        return info
    try:
        settings.require_enabled()
        client = docker or bot_control.DockerClient(settings.docker_url)
        state = client.inspect(settings.container)
    except bot_control.BotControlError as exc:
        info["error"] = str(exc)
        info["check"] = {"status": WARN, "detail": "no se pudo consultar el contenedor"}
        return info

    info.update(running=state["running"], status=state["status"], started_at=state.get("started_at"))
    started = bot_control.parse_docker_time(state.get("started_at"))
    if state["running"] and started is not None:
        info["uptime_seconds"] = _age(now, started.timestamp())
    if state["status"] == "not_found":
        info["check"] = {"status": CRITICAL, "detail": "el contenedor no existe"}
    elif not state["running"]:
        info["check"] = {"status": CRITICAL, "detail": f"contenedor {state['status']}"}
    else:
        info["check"] = {"status": OK, "detail": ""}
    return info


def _db_info(now: float) -> Dict[str, Any]:
    placeholders = ",".join("?" for _ in TRADE_INCOME_TYPES)
    trade = db.query(
        f"SELECT MAX(time) FROM income WHERE incomeType IN ({placeholders})",
        TRADE_INCOME_TYPES,
        one=True,
    )
    order = db.query("SELECT MAX(time) FROM orders", one=True)
    pos = db.query(
        "SELECT COUNT(*), COALESCE(SUM(unrealizedProfit), 0) FROM positions WHERE positionAmt != 0",
        one=True,
    )
    trade_ts = trade[0] / 1000.0 if trade and trade[0] is not None else None
    order_ts = order[0] / 1000.0 if order and order[0] is not None else None
    return {
        "last_trade_at": _iso(trade_ts),
        "last_trade_age_seconds": _age(now, trade_ts),
        "last_order_at": _iso(order_ts),
        "last_order_age_seconds": _age(now, order_ts),
        "positions_open": int(pos[0]) if pos else 0,
        "upnl_total": round(float(pos[1]), 8) if pos else 0.0,
    }


def _scrape_info(now: float) -> Dict[str, Any]:
    state = scraper.read_scrape_state(current_app.config["DATABASE"])

    def _ts(key: str) -> Optional[float]:
        value = state.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    success = _ts("last_success_at")
    return {
        "auto_scrape": not current_app.config.get("DISABLE_AUTO_SCRAPE", False),
        "interval_seconds": current_app.config.get("AUTO_SCRAPE_INTERVAL"),
        "last_success_at": _iso(success),
        "age_seconds": _age(now, success),
        "last_started_at": _iso(_ts("last_started_at")),
        "last_error_at": _iso(_ts("last_error_at")),
        "last_error": state.get("last_error") if _ts("last_error_at") else None,
    }


def collect_health(now: Optional[float] = None,
                   docker: Optional[bot_control.DockerClient] = None) -> Dict[str, Any]:
    """Arma el estado completo. Requiere app context. Nunca lanza por fallas de Docker."""
    now = time.time() if now is None else now
    th = load_thresholds()
    bot = _bot_info(now, docker)
    dbi = _db_info(now)
    scr = _scrape_info(now)

    checks = {
        "bot": bot.pop("check"),
        "scrape": _check_max(
            scr["age_seconds"],
            th["FUTURESBOARD_HEALTH_SCRAPE_WARN_SECONDS"],
            th["FUTURESBOARD_HEALTH_SCRAPE_CRIT_SECONDS"],
            "sin scrape exitoso registrado",
        ),
        "last_trade": _check_max(
            dbi["last_trade_age_seconds"],
            th["FUTURESBOARD_HEALTH_TRADE_WARN_SECONDS"],
            th["FUTURESBOARD_HEALTH_TRADE_CRIT_SECONDS"],
            "sin trades en la DB",
        ),
        "positions": _check_max(
            float(dbi["positions_open"]),
            th["FUTURESBOARD_HEALTH_POSITIONS_WARN"],
            th["FUTURESBOARD_HEALTH_POSITIONS_CRIT"],
            "sin datos",
        ),
        "upnl": _check_min(
            dbi["upnl_total"], th["FUTURESBOARD_HEALTH_UPNL_WARN"], th["FUTURESBOARD_HEALTH_UPNL_CRIT"]
        ),
    }
    levels = [_LEVEL[c["status"]] for c in checks.values() if c["status"] in _LEVEL]
    worst = max(levels, default=0)
    status = {v: k for k, v in _LEVEL.items()}[worst]

    return {
        "status": status,
        "checked_at": _iso(now),
        "checks": checks,
        "bot": bot,
        "db": dbi,
        "scrape": scr,
        "binance_testnet": bool(current_app.config.get("BINANCE_TESTNET")),
        "thresholds": {k.replace("FUTURESBOARD_HEALTH_", "").lower(): v for k, v in th.items()},
    }


# --------------------------------------------------------------------------------------
# Prometheus
# --------------------------------------------------------------------------------------

METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "NaN"
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if value.is_integer():
        return str(int(value))
    return repr(value)


def render_metrics(health: Dict[str, Any]) -> str:
    bot = health["bot"]
    bot_up: Optional[float]
    if bot["running"] is None:
        bot_up = None
    else:
        bot_up = 1.0 if bot["running"] else 0.0
    metrics = [
        ("futuresboard_health_status", "Estado global: 0=ok, 1=warn, 2=critical.",
         _LEVEL[health["status"]]),
        ("futuresboard_bot_up", "1 si el contenedor de passivbot esta corriendo, 0 si no, NaN si no se sabe.",
         bot_up),
        ("futuresboard_positions_open", "Posiciones abiertas segun la DB (positionAmt != 0).",
         health["db"]["positions_open"]),
        ("futuresboard_upnl_total", "Suma del PnL no realizado de las posiciones abiertas (DB).",
         health["db"]["upnl_total"]),
        ("futuresboard_last_trade_age_seconds", "Segundos desde el ultimo fill registrado en la DB.",
         health["db"]["last_trade_age_seconds"]),
        ("futuresboard_scrape_age_seconds", "Segundos desde el ultimo scrape exitoso.",
         health["scrape"]["age_seconds"]),
    ]
    lines = []
    for name, help_text, value in metrics:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {_fmt(value)}")
    return "\n".join(lines) + "\n"


def bearer_token_response() -> Optional[Response]:
    """Valida ``Authorization: Bearer`` contra FUTURESBOARD_METRICS_TOKEN.

    La llama ``auth._require_login`` solo cuando el request trae header Authorization.
    Devuelve None si el token es valido, o un 401. El token nunca se acepta por query string
    (quedaria en los access logs).
    """
    expected = os.environ.get(ENV_METRICS_TOKEN, "").strip()
    header = request.headers.get("Authorization", "")
    scheme, _, sent = header.partition(" ")
    if expected and scheme.lower() == "bearer" and hmac.compare_digest(expected.encode(), sent.strip().encode()):
        return None
    return Response(
        json.dumps({"error": "unauthorized"}),
        status=401,
        mimetype="application/json",
        headers={"WWW-Authenticate": 'Bearer realm="metrics"'},
    )


# --------------------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------------------


@ops.route("/health", methods=["GET"])
def liveness():
    # Liveness pura: no toca DB, Docker ni el exchange, y no expone datos.
    return Response(json.dumps({"status": "ok"}), status=200, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


@ops.route("/api/bot/health", methods=["GET"])
def bot_health():
    # Siempre 200: el estado va en "status". El probe de vida es /health.
    return Response(json.dumps(collect_health()), status=200, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


@ops.route("/metrics", methods=["GET"])
def metrics():
    return Response(render_metrics(collect_health()), status=200, mimetype=None,
                    content_type=METRICS_CONTENT_TYPE, headers={"Cache-Control": "no-store"})
