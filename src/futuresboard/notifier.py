"""Proceso opcional de alertas de salud y resumen diario por Telegram.

Uso: ``python -m futuresboard.notifier [-c CONFIG_DIR] [--once]``

Cada ``NOTIFIER_INTERVAL`` segundos:
- chequea el contenedor de passivbot via docker-socket-proxy (si FUTURESBOARD_DOCKER_URL está configurado),
- la edad del último scrape (mtime del archivo de la DB) y, opcionalmente, la del último trade/income,
- avisa al pasar a caído (tras ``NOTIFIER_FAIL_THRESHOLD`` chequeos fallidos seguidos) y al recuperarse.
  Solo avisa en las transiciones: un estado caído persistente no se repite cada ciclo.
- una vez por día, a ``NOTIFIER_DAILY_SUMMARY`` (HH:MM, hora de Buenos Aires), manda el resumen de PnL.

El estado (caído/ok por chequeo y fecha del último resumen) se guarda en ``notifier_state.json`` en el
directorio de la DB, para que un reinicio del notifier no repita alertas ni el resumen del día.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sqlite3
import time
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

from futuresboard import bot_control
from futuresboard import telegram_notify
from futuresboard.telegram_notify import esc

log = logging.getLogger("futuresboard.notifier")

TZ_NAME = "America/Argentina/Buenos_Aires"
try:  # Python 3.9+; sin base de zonas horarias se usa UTC-3 fijo (Argentina no tiene horario de verano)
    from zoneinfo import ZoneInfo

    TZ: dt.tzinfo = ZoneInfo(TZ_NAME)
except Exception:  # pragma: no cover - depende del entorno
    TZ = dt.timezone(dt.timedelta(hours=-3), "ART")

STATE_FILE_NAME = "notifier_state.json"
# Mismo criterio que blueprint.py para el ingreso realizado.
EXCLUDED_INCOME_TYPES = ("TRANSFER", "COIN_SWAP_DEPOSIT", "COIN_SWAP_WITHDRAW")

CHECK_LABELS = {
    "passivbot": "Contenedor passivbot",
    "scrape": "Scrape de la DB",
    "trade": "Último trade",
}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log.warning("Valor inválido en %s=%r, se usa %s.", name, raw, default)
        return default


def _parse_hhmm(raw: str) -> Optional[dt.time]:
    raw = raw.strip().lower()
    if raw in ("", "off", "no", "0", "false"):
        return None
    try:
        hh, mm = raw.split(":")
        return dt.time(int(hh), int(mm))
    except ValueError:
        log.warning("NOTIFIER_DAILY_SUMMARY inválido (%r): resumen diario deshabilitado.", raw)
        return None


class NotifierSettings:
    def __init__(self) -> None:
        self.interval = _env_int("NOTIFIER_INTERVAL", 60, minimum=10)
        self.fail_threshold = _env_int("NOTIFIER_FAIL_THRESHOLD", 2, minimum=1)
        # 0 deshabilita el chequeo.
        self.scrape_max_age = _env_int("NOTIFIER_SCRAPE_MAX_AGE", 900)
        self.trade_max_age = _env_int("NOTIFIER_TRADE_MAX_AGE", 0)
        self.daily_summary = _parse_hhmm(os.environ.get("NOTIFIER_DAILY_SUMMARY", "21:00"))


def _fmt_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


def _connect_ro(db_path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------------------
# Chequeos: cada uno devuelve (ok, detalle)
# --------------------------------------------------------------------------------------


def check_passivbot(docker: bot_control.DockerClient, container: str) -> Tuple[bool, str]:
    try:
        info = docker.inspect(container)
    except bot_control.BotControlError as exc:
        return False, f"no se pudo consultar Docker: {exc}"
    except Exception as exc:
        return False, f"no se pudo consultar Docker ({exc.__class__.__name__})"
    if info.get("status") == "not_found":
        return False, f"el contenedor {container} no existe"
    if not info.get("running"):
        return False, f"{container} en estado {info.get('status')}"
    return True, f"{container} corriendo"


def check_scrape(db_path: pathlib.Path, max_age: int, now: float) -> Tuple[bool, str]:
    """Proxy del último scrape: mtime de la DB (el scraper escribe positions/account en cada ciclo)."""
    try:
        age = now - db_path.stat().st_mtime
    except OSError:
        return False, f"no se encuentra la DB ({db_path.name})"
    if age > max_age:
        return False, f"sin escrituras en la DB hace {_fmt_age(age)} (máx {_fmt_age(max_age)})"
    return True, f"última escritura hace {_fmt_age(age)}"


def check_trade(db_path: pathlib.Path, max_age: int, now: float) -> Tuple[bool, str]:
    try:
        with _connect_ro(db_path) as conn:
            row = conn.execute("SELECT MAX(time) AS t FROM income").fetchone()
    except sqlite3.Error as exc:
        return False, f"no se pudo leer la DB ({exc.__class__.__name__})"
    last = row["t"] if row else None
    if not last:
        return False, "no hay trades en la DB"
    age = now - last / 1000.0
    if age > max_age:
        return False, f"último trade hace {_fmt_age(age)} (máx {_fmt_age(max_age)})"
    return True, f"último trade hace {_fmt_age(age)}"


# --------------------------------------------------------------------------------------
# Resumen diario
# --------------------------------------------------------------------------------------


def day_bounds_ms(day: dt.date) -> Tuple[int, int]:
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=TZ)
    end = start + dt.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


def daily_summary_data(db_path: pathlib.Path, day: dt.date) -> Dict[str, Any]:
    start, end = day_bounds_ms(day)
    placeholders = ", ".join("?" for _ in EXCLUDED_INCOME_TYPES)
    out: Dict[str, Any] = {"fecha": day.isoformat(), "ingreso": 0.0, "por_tipo": {}, "posiciones": [],
                           "upnl": None, "balance": None}
    with _connect_ro(db_path) as conn:
        rows = conn.execute(
            "SELECT incomeType, SUM(income) AS s FROM income WHERE asset <> 'BNB' "
            f"AND incomeType NOT IN ({placeholders}) AND time >= ? AND time <= ? GROUP BY incomeType",
            (*EXCLUDED_INCOME_TYPES, start, end),
        ).fetchall()
        out["por_tipo"] = {r["incomeType"]: float(r["s"] or 0) for r in rows}
        out["ingreso"] = sum(out["por_tipo"].values())
        try:
            pos = conn.execute(
                "SELECT symbol, positionSide, positionAmt, unrealizedProfit FROM positions "
                "WHERE ABS(positionAmt) > 0 ORDER BY symbol"
            ).fetchall()
            out["posiciones"] = [dict(p) for p in pos]
            if pos:
                out["upnl"] = sum(float(p["unrealizedProfit"] or 0) for p in pos)
        except sqlite3.Error:
            pass
        try:
            acc = conn.execute("SELECT totalWalletBalance, totalUnrealizedProfit FROM account LIMIT 1").fetchone()
            if acc is not None:
                out["balance"] = acc["totalWalletBalance"]
                if acc["totalUnrealizedProfit"] is not None:
                    out["upnl"] = float(acc["totalUnrealizedProfit"])
        except sqlite3.Error:
            pass
    return out


def format_daily_summary(data: Dict[str, Any]) -> str:
    lines = [f"📊 <b>Resumen diario {esc(data['fecha'])}</b>",
             f"Ingreso realizado del día: {data['ingreso']:+.2f} USDT"]
    for tipo, val in sorted(data["por_tipo"].items()):
        lines.append(f"  • {esc(tipo)}: {val:+.2f}")
    if data.get("balance") is not None:
        lines.append(f"Balance wallet: {float(data['balance']):.2f} USDT")
    pos: List[Dict[str, Any]] = data.get("posiciones") or []
    lines.append(f"Posiciones abiertas: {len(pos)}")
    for p in pos[:15]:
        lines.append(f"  • {esc(p.get('symbol'))} {esc(p.get('positionSide'))} {esc(p.get('positionAmt'))} "
                     f"(UPNL {float(p.get('unrealizedProfit') or 0):+.2f})")
    if len(pos) > 15:
        lines.append(f"  • … y {len(pos) - 15} más")
    if data.get("upnl") is not None:
        lines.append(f"UPNL total: {float(data['upnl']):+.2f} USDT")
    lines.append("<i>Datos según el último scrape de la DB.</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Monitor con deduplicación
# --------------------------------------------------------------------------------------


class Monitor:
    def __init__(self, db_path: pathlib.Path, settings: Optional[NotifierSettings] = None,
                 bot_settings: Optional[bot_control.Settings] = None,
                 docker: Optional[bot_control.DockerClient] = None,
                 send: Optional[Callable[[str], bool]] = None,
                 now: Optional[Callable[[], dt.datetime]] = None) -> None:
        self.db_path = pathlib.Path(db_path)
        self.settings = settings or NotifierSettings()
        self.bot_settings = bot_settings or bot_control.Settings()
        self.docker = docker
        if self.docker is None and self.bot_settings.enabled:
            self.docker = bot_control.DockerClient(self.bot_settings.docker_url)
        self.send = send or telegram_notify.send
        self.now = now or (lambda: dt.datetime.now(TZ))
        self.state_path = self.db_path.parent / STATE_FILE_NAME
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("checks", {})
                return data
        except (OSError, ValueError):
            pass
        return {"checks": {}, "last_summary_date": None}

    def _save_state(self) -> None:
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("No se pudo guardar el estado del notifier (%s).", exc.__class__.__name__)

    def run_checks(self) -> Dict[str, Tuple[bool, str]]:
        ts = self.now().timestamp()
        results: Dict[str, Tuple[bool, str]] = {}
        if self.docker is not None:
            results["passivbot"] = check_passivbot(self.docker, self.bot_settings.container)
        if self.settings.scrape_max_age:
            results["scrape"] = check_scrape(self.db_path, self.settings.scrape_max_age, ts)
        if self.settings.trade_max_age:
            results["trade"] = check_trade(self.db_path, self.settings.trade_max_age, ts)
        return results

    def process(self, results: Dict[str, Tuple[bool, str]]) -> List[str]:
        """Aplica la deduplicación y envía solo en transiciones. Devuelve los mensajes enviados."""
        sent: List[str] = []
        checks = self.state["checks"]
        for name, (ok, detail) in results.items():
            st = checks.setdefault(name, {"status": "ok", "fails": 0})
            label = CHECK_LABELS.get(name, name)
            if ok:
                if st["status"] == "down":
                    text = f"✅ <b>{esc(label)}</b> recuperado: {esc(detail)}"
                    if self.send(text):
                        st["status"] = "ok"
                        sent.append(text)
                    # si el envío falla queda "down" y se reintenta el aviso en el próximo ciclo
                else:
                    st["status"] = "ok"
                st["fails"] = 0
            else:
                st["fails"] = int(st.get("fails", 0)) + 1
                if st["status"] != "down" and st["fails"] >= self.settings.fail_threshold:
                    text = f"🔴 <b>{esc(label)}</b> caído: {esc(detail)}"
                    if self.send(text):
                        st["status"] = "down"
                        sent.append(text)
        return sent

    def maybe_daily_summary(self) -> Optional[str]:
        target = self.settings.daily_summary
        if target is None:
            return None
        now = self.now()
        today = now.date().isoformat()
        if now.time() < target or self.state.get("last_summary_date") == today:
            return None
        try:
            text = format_daily_summary(daily_summary_data(self.db_path, now.date()))
        except sqlite3.Error as exc:
            text = (f"📊 <b>Resumen diario {esc(today)}</b>\n"
                    f"No se pudo leer la DB ({esc(exc.__class__.__name__)}).")
        if self.send(text):
            self.state["last_summary_date"] = today
            return text
        return None

    def tick(self) -> List[str]:
        sent = self.process(self.run_checks())
        summary = self.maybe_daily_summary()
        if summary:
            sent.append(summary)
        self._save_state()
        return sent


def _db_path_from_config(config_dir: pathlib.Path) -> pathlib.Path:
    from futuresboard.config import Config

    return pathlib.Path(Config.from_config_dir(config_dir).DATABASE)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="futuresboard.notifier")
    parser.add_argument("-c", "--config-dir", type=pathlib.Path, default=None,
                        help="Directorio de config (default: ./config), igual que futuresboard.")
    parser.add_argument("--once", action="store_true", help="Un solo ciclo y salir.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not telegram_notify.log_startup_state(log):
        if args.once:
            return 0
        # Sin Telegram no hay nada que hacer; se queda inactivo para no entrar en un loop de reinicios.
        while True:
            time.sleep(3600)

    config_dir = (args.config_dir or pathlib.Path.cwd() / "config").resolve()
    monitor = Monitor(_db_path_from_config(config_dir))
    log.info("Notifier iniciado: intervalo %ss, DB %s, passivbot %s.", monitor.settings.interval,
             monitor.db_path, "sí" if monitor.docker is not None else "no (sin FUTURESBOARD_DOCKER_URL)")
    while True:
        try:
            monitor.tick()
        except Exception as exc:  # el loop no se cae por un ciclo con error
            log.warning("Ciclo del notifier con error (%s).", exc.__class__.__name__)
        if args.once:
            return 0
        time.sleep(monitor.settings.interval)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
