"""Rescate como colchón: reserva en spot que se presta a futuros solo cerca de la liquidación.

Uso::

    python -m futuresboard.rescate                       # loop cada FUTURESBOARD_RESCATE_INTERVAL s
    python -m futuresboard.rescate --once                # un ciclo y salir
    python -m futuresboard.rescate simular-inyeccion 500 # solo en dry_run
    python -m futuresboard.rescate simular-devolucion [monto]

Cómo funciona (ver ``docs/rescate.md``):

* passivbot dimensiona sobre el capital propio: ``wallet - préstamo pendiente``. El préstamo lo publica
  este servicio en ``loan_state.json`` (contrato compartido con passivbot, escritura atómica).
* **Inyección** solo cerca de la liquidación: si ``totalMarginBalance / totalMaintMargin < umbral`` y hay
  posiciones, transfiere de spot a futuros un paso, sin superar la reserva máxima ni el USDT libre en
  spot. Una transferencia en vuelo a la vez.
* **Devolución automática**: sin posiciones y con préstamo pendiente, transfiere de futuros a spot
  ``min(préstamo, maxWithdrawAmount)``; si no alcanza, en partes en los ciclos siguientes.
* **Tope de pérdida total**: ``capital_total = equity de futuros + USDT libre en spot``. Al habilitar se
  fija una base; si ``capital_total <= base × (1 - tope%)`` se ejecuta la rutina de Apagar del panel (con
  el mismo lock), START queda bloqueado hasta reconocerlo en el panel y se avisa por Telegram.
* Binance no tiene idempotencia en ``/sapi/v1/asset/transfer``: ante un resultado ambiguo no se reintenta;
  se concilia con el historial (``GET /sapi/v1/asset/transfer``) y con la variación del saldo spot.

Archivos (en el directorio de datos del dashboard, el mismo que ``bot_actions.log``):

* ``rescate_config.json``: lo escribe solo el panel (habilitado, reserva, paso, umbral, tope, reconocimiento).
* ``rescate_state.json``: lo escribe solo este servicio (y la CLI de simulación), con el ledger.

Es una herramienta, no un asesor: la reserva sirve para aguantar margen y no garantiza evitar pérdidas.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import decimal
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import pathlib
import secrets
import tempfile
import time
from decimal import Decimal
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple
from urllib.parse import urlencode

import requests

from futuresboard import bot_control
from futuresboard import jobs
from futuresboard import telegram_notify
from futuresboard.telegram_notify import esc

log = logging.getLogger("futuresboard.rescate")

# --------------------------------------------------------------------------------------
# Configuración por entorno (fail-closed)
# --------------------------------------------------------------------------------------

ENV_MODE = "FUTURESBOARD_RESCATE_MODE"
ENV_INTERVAL = "FUTURESBOARD_RESCATE_INTERVAL"
ENV_LOAN_STATE = "FUTURESBOARD_RESCATE_LOAN_STATE"
ENV_SPOT_URL = "FUTURESBOARD_RESCATE_SPOT_URL"

MODE_OFF = "off"
MODE_DRY_RUN = "dry_run"
MODE_REAL = "real"
MODES = (MODE_OFF, MODE_DRY_RUN, MODE_REAL)

DEFAULT_INTERVAL = 15
MIN_INTERVAL = 5
MAX_INTERVAL = 300
DEFAULT_LOAN_STATE = "/rescate/loan_state.json"

SPOT_MAINNET_URL = "https://api.binance.com"
# Binance Demo Trading (spot). /sapi no existe en Demo: el modo real queda bloqueado ahí.
SPOT_DEMO_URL = "https://demo-api.binance.com"

CONFIG_FILE_NAME = "rescate_config.json"
STATE_FILE_NAME = "rescate_state.json"
STATE_LOCK_NAME = ".rescate_state.lock"
CONFIG_LOCK_NAME = ".rescate_config.lock"
SERVICE_LOCK_NAME = ".rescate_service.lock"

LOAN_STATE_VERSION = 1

# --------------------------------------------------------------------------------------
# Parámetros del panel y reglas
# --------------------------------------------------------------------------------------

CENT = Decimal("0.01")
DEFAULT_PASO = Decimal("1000")
DEFAULT_UMBRAL = Decimal("3")
# (mínimo, máximo) inclusivos. El umbral es la relación margen / mantenimiento: Binance liquida en 1.
LIMITES: Dict[str, Tuple[Decimal, Decimal]] = {
    "reserva_max_usdt": (Decimal("10"), Decimal("1000000")),
    "paso_usdt": (Decimal("10"), Decimal("1000000")),
    "umbral_ratio": (Decimal("1.2"), Decimal("10")),
    "tope_perdida_pct": (Decimal("1"), Decimal("80")),
}
CAMPOS_NUMERICOS = tuple(LIMITES)

TRANSFER_ASSET = "USDT"
TIPO_INYECCION = "inyeccion"
TIPO_DEVOLUCION = "devolucion"
TIPO_API = {TIPO_INYECCION: "MAIN_UMFUTURE", TIPO_DEVOLUCION: "UMFUTURE_MAIN"}

MIN_INYECCION = Decimal("1")      # no se inyectan montos menores (USDT)
MIN_DEVOLUCION = CENT
TOLERANCIA_SALDO = CENT           # para conciliar por variación del saldo spot

RECONCILE_GRACE_MS = 120_000      # sin rastro en el historial durante esto => se mira el saldo spot
RECONCILE_MIN_CHECKS = 2
RECONCILE_ALERTA_MS = 600_000     # con tranId y sin fila en el historial: avisar para conciliar a mano
CLOCK_SKEW_MS = 60_000
COOLDOWN_RECHAZO_MS = 300_000     # tras un rechazo claro de Binance, no reintentar ese tipo por 5 min
COOLDOWN_NO_EJECUTADA_MS = 60_000

APAGAR_MAX_INTENTOS = 3
APAGAR_REINTENTO_MS = 120_000
APAGAR_LOCK_TIMEOUT_S = 180.0
LOCK_POLL_S = 0.5
APAGAR_USUARIO = "rescate (tope de pérdida)"

MAX_LEDGER = 200
MAX_EVENTOS = 100
HISTORY_PAGE_SIZE = 100

# Permisos exigidos a la API key para operar en real (GET /sapi/v1/account/apiRestrictions).
REQUIRED_RESTRICTIONS: Dict[str, bool] = {
    "ipRestrict": True,
    "permitsUniversalTransfer": True,
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
}

AVISO = ("La reserva sirve para aguantar margen cerca de la liquidación: no garantiza evitar pérdidas. "
         "Es una herramienta, no un asesor.")

HTTP_TIMEOUT_SECONDS = 10
RECV_WINDOW = 5000
# Códigos de Binance cuyo resultado es desconocido (la orden pudo ejecutarse).
AMBIGUOUS_CODES = (-1006, -1007)


class RescateError(Exception):
    """Error con mensaje apto para el usuario (sin URL firmada ni credenciales)."""


class ReadError(RescateError):
    """No se pudo leer Binance (o la respuesta no es la esperada)."""


class TransferRejected(RescateError):
    """Binance rechazó la transferencia con una respuesta clara: no se ejecutó."""


class TransferAmbiguous(RescateError):
    """Sin respuesta o respuesta ambigua: la transferencia pudo ejecutarse. No reintentar a ciegas."""


class ConflictError(RescateError):
    status_code = 409


class StateError(RescateError):
    """El estado persistido es ilegible: no se opera (fail-closed) y no se pisa."""


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------


def now_iso(ts: Optional[float] = None) -> str:
    moment = dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts is not None else dt.datetime.now(dt.timezone.utc)
    return moment.isoformat(timespec="seconds")


def dec(value: Any) -> Decimal:
    """Decimal estricto: rechaza bool, None, texto inválido, NaN e infinitos (ValueError)."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"valor numérico inválido: {value!r}")
    try:
        out = Decimal(str(value).strip())
    except (decimal.InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"valor numérico inválido: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"valor numérico inválido: {value!r}")
    return out


def money(value: Decimal) -> str:
    """Monto con 2 decimales (texto exacto, para JSON, API y mensajes)."""
    return format(value.quantize(CENT, rounding=decimal.ROUND_DOWN), "f")


def floor_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=decimal.ROUND_DOWN)


def write_json_atomic(path: pathlib.Path, data: Any, mode: int = 0o644) -> None:
    """tmp en el mismo directorio + fsync + chmod + os.replace (el lector nunca ve un archivo a medias)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


@contextlib.contextmanager
def file_lock(path: pathlib.Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------------------
# Contrato con passivbot: loan_state.json
# --------------------------------------------------------------------------------------


def loan_state_payload(loan: Decimal, mode: str, ts: Optional[float] = None) -> Dict[str, Any]:
    if mode not in (MODE_REAL, MODE_DRY_RUN):
        raise ValueError(f"modo inválido para loan_state: {mode!r}")
    if loan < 0:
        raise ValueError("el préstamo no puede ser negativo")
    return {
        "version": LOAN_STATE_VERSION,
        "loan_outstanding_usdt": float(floor_cent(loan)),
        "updated_at": now_iso(ts),
        "mode": mode,
    }


def write_loan_state(path: pathlib.Path, loan: Decimal, mode: str, ts: Optional[float] = None) -> Dict[str, Any]:
    """Escribe el archivo que lee passivbot (``PASSIVBOT_LOAN_STATE``). Atómico y legible por otro uid."""
    payload = loan_state_payload(loan, mode, ts)
    write_json_atomic(path, payload, mode=0o644)
    return payload


def read_loan_state(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------------------
# Config del panel (rescate_config.json)
# --------------------------------------------------------------------------------------


def default_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "habilitado": False,
        "generacion": 0,
        "reserva_max_usdt": None,
        "paso_usdt": money(DEFAULT_PASO),
        "umbral_ratio": str(DEFAULT_UMBRAL),
        "tope_perdida_pct": None,
        "tope_reconocido": None,
        "actualizado_en": None,
        "actualizado_por": None,
    }


def _check_range(name: str, value: Decimal) -> None:
    lo, hi = LIMITES[name]
    if value < lo or value > hi:
        raise ValueError(f"{name} fuera de rango: tiene que estar entre {lo} y {hi}.")


def load_config(data_dir: pathlib.Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """Config del panel. Si el archivo es inválido se devuelve deshabilitado + el error (fail-closed)."""
    cfg = default_config()
    path = data_dir / CONFIG_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg, None
    except (OSError, ValueError) as exc:
        return cfg, f"No se pudo leer {CONFIG_FILE_NAME} ({exc.__class__.__name__}): rescate deshabilitado."
    try:
        if not isinstance(raw, dict):
            raise ValueError("no es un objeto")
        cfg.update({k: v for k, v in raw.items() if k in cfg})
        if not isinstance(cfg["habilitado"], bool):
            raise ValueError("habilitado no es booleano")
        if isinstance(cfg["generacion"], bool) or not isinstance(cfg["generacion"], int) or cfg["generacion"] < 0:
            raise ValueError("generacion inválida")
        for name in CAMPOS_NUMERICOS:
            if cfg[name] is not None:
                _check_range(name, dec(cfg[name]))
        if cfg["habilitado"] and (cfg["reserva_max_usdt"] is None or cfg["tope_perdida_pct"] is None):
            raise ValueError("habilitado sin reserva o sin tope")
        if cfg["paso_usdt"] is None or cfg["umbral_ratio"] is None:
            raise ValueError("falta paso o umbral")
    except ValueError as exc:
        return default_config(), f"{CONFIG_FILE_NAME} inválido ({exc}): rescate deshabilitado."
    return cfg, None


def cfg_dec(cfg: Dict[str, Any], name: str) -> Optional[Decimal]:
    value = cfg.get(name)
    return None if value is None else dec(value)


def validate_config_body(body: Any) -> Dict[str, Any]:
    """Validación estricta del POST del panel. Devuelve los valores como Decimal (o None).

    Campos: ``habilitado`` (bool), ``reserva_max_usdt``, ``paso_usdt``, ``umbral_ratio``,
    ``tope_perdida_pct`` (números JSON finitos, no bool). ``reserva`` y ``tope`` pueden ser null solo
    con el rescate deshabilitado. Campos desconocidos: error.
    """
    if not isinstance(body, dict):
        raise ValueError("Body inválido: se esperaba un objeto JSON.")
    allowed = {"habilitado", *CAMPOS_NUMERICOS}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValueError(f"Campos desconocidos: {', '.join(unknown)}.")
    missing = sorted(allowed - set(body))
    if missing:
        raise ValueError(f"Faltan campos: {', '.join(missing)}.")
    habilitado = body["habilitado"]
    if not isinstance(habilitado, bool):
        raise ValueError("habilitado tiene que ser true o false.")
    out: Dict[str, Any] = {"habilitado": habilitado}
    for name in CAMPOS_NUMERICOS:
        value = body[name]
        if value is None:
            if name in ("paso_usdt", "umbral_ratio"):
                raise ValueError(f"{name} es obligatorio.")
            out[name] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} tiene que ser un número.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} tiene que ser un número finito.")
        parsed = dec(value)
        _check_range(name, parsed)
        out[name] = parsed
    if habilitado and out["reserva_max_usdt"] is None:
        raise ValueError("Para habilitar el rescate hay que fijar la reserva máxima.")
    if habilitado and out["tope_perdida_pct"] is None:
        raise ValueError("Para habilitar el rescate hay que fijar el tope de pérdida total (obligatorio).")
    if out["reserva_max_usdt"] is not None and out["paso_usdt"] > out["reserva_max_usdt"]:
        raise ValueError("El paso no puede ser mayor que la reserva máxima.")
    return out


def _num_to_cfg(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value.normalize(), "f")


def save_config(data_dir: pathlib.Path, parsed: Dict[str, Any], user: str) -> Dict[str, Any]:
    """Guarda la config del panel. Habilitar (false -> true) abre una generación nueva => base nueva."""
    with file_lock(data_dir / CONFIG_LOCK_NAME):
        prev, _err = load_config(data_dir)
        if parsed["habilitado"]:
            bloqueo = start_bloqueado(data_dir)
            if bloqueo:
                raise ConflictError(bloqueo)
        cfg = dict(prev)
        if parsed["habilitado"] and not prev.get("habilitado"):
            cfg["generacion"] = int(prev.get("generacion") or 0) + 1
        cfg["habilitado"] = parsed["habilitado"]
        for name in CAMPOS_NUMERICOS:
            cfg[name] = _num_to_cfg(parsed[name])
        cfg["actualizado_en"] = now_iso()
        cfg["actualizado_por"] = user
        write_json_atomic(data_dir / CONFIG_FILE_NAME, cfg, mode=0o600)
        return cfg


def reconocer_tope(data_dir: pathlib.Path, tope_id: Any, user: str) -> Dict[str, Any]:
    """Reconoce un tope disparado: desbloquea START y deja el rescate deshabilitado (habilitar = base nueva)."""
    if not isinstance(tope_id, str) or not tope_id:
        raise ValueError("id del tope inválido.")
    state, err = load_state(data_dir)
    if err:
        raise ConflictError(err)
    tope = state.get("tope")
    if not isinstance(tope, dict) or tope.get("id") != tope_id:
        raise ConflictError("No hay un tope disparado con ese id (¿ya se reconoció?).")
    with file_lock(data_dir / CONFIG_LOCK_NAME):
        cfg, _err = load_config(data_dir)
        cfg["tope_reconocido"] = tope_id
        cfg["habilitado"] = False
        cfg["actualizado_en"] = now_iso()
        cfg["actualizado_por"] = user
        write_json_atomic(data_dir / CONFIG_FILE_NAME, cfg, mode=0o600)
        return cfg


# --------------------------------------------------------------------------------------
# Estado del servicio (rescate_state.json)
# --------------------------------------------------------------------------------------


def default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "base": None,
        "prestamo": {MODE_REAL: "0.00", MODE_DRY_RUN: "0.00"},
        "en_vuelo": None,
        "ledger": [],
        "eventos": [],
        "ultimo": None,
        "tope": None,
        "bloqueo": None,
        "servicio": None,
        "cooldown_ms": {},
    }


def load_state(data_dir: pathlib.Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """Estado del servicio. Si el archivo existe pero es ilegible, error (no se opera ni se pisa)."""
    path = data_dir / STATE_FILE_NAME
    state = default_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return state, None
    except (OSError, ValueError) as exc:
        return state, f"Estado del rescate ilegible ({STATE_FILE_NAME}: {exc.__class__.__name__}). Revisar a mano."
    if not isinstance(raw, dict) or not isinstance(raw.get("prestamo"), dict):
        return state, f"Estado del rescate inválido ({STATE_FILE_NAME}). Revisar a mano."
    state.update(raw)
    try:
        for mode in (MODE_REAL, MODE_DRY_RUN):
            if dec(state["prestamo"].get(mode, "0")) < 0:
                raise ValueError("préstamo negativo")
            state["prestamo"].setdefault(mode, "0.00")
    except ValueError as exc:
        return default_state(), f"Estado del rescate inválido ({exc}). Revisar a mano."
    for key in ("ledger", "eventos"):
        if not isinstance(state.get(key), list):
            state[key] = []
    if not isinstance(state.get("cooldown_ms"), dict):
        state["cooldown_ms"] = {}
    return state, None


def save_state(data_dir: pathlib.Path, state: Dict[str, Any]) -> None:
    state["ledger"] = state["ledger"][-MAX_LEDGER:]
    state["eventos"] = state["eventos"][-MAX_EVENTOS:]
    write_json_atomic(data_dir / STATE_FILE_NAME, state, mode=0o600)


def get_loan(state: Dict[str, Any], mode: str) -> Decimal:
    return dec(state["prestamo"].get(mode, "0"))


def set_loan(state: Dict[str, Any], mode: str, value: Decimal) -> None:
    state["prestamo"][mode] = money(max(Decimal(0), value))


def real_pendiente(state: Dict[str, Any]) -> bool:
    """Hay plata real prestada (o una transferencia real en vuelo)."""
    en_vuelo = state.get("en_vuelo")
    return get_loan(state, MODE_REAL) > 0 or (isinstance(en_vuelo, dict) and en_vuelo.get("modo") == MODE_REAL)


def loan_publicado(state: Dict[str, Any], mode: Optional[str]) -> Tuple[Decimal, str]:
    """(préstamo, modo) que va a loan_state.json.

    Es el préstamo del modo activo, salvo en dry_run con un préstamo real pendiente: ahí se sigue
    publicando el real (passivbot nunca debe dimensionar sobre plata prestada de verdad).
    """
    if mode == MODE_DRY_RUN and not real_pendiente(state):
        return get_loan(state, MODE_DRY_RUN), MODE_DRY_RUN
    return get_loan(state, MODE_REAL), MODE_REAL


def tope_activo(state: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tope = state.get("tope")
    if isinstance(tope, dict) and tope.get("id") and tope.get("id") != cfg.get("tope_reconocido"):
        return tope
    return None


def start_bloqueado(data_dir: pathlib.Path) -> Optional[str]:
    """Mensaje si START está bloqueado por un tope de pérdida sin reconocer (None si no)."""
    path = data_dir / STATE_FILE_NAME
    if not path.exists():
        return None
    state, err = load_state(data_dir)
    if err:
        return f"START bloqueado: {err}"
    cfg, _err = load_config(data_dir)
    tope = tope_activo(state, cfg)
    if tope is None:
        return None
    simulado = " (simulado)" if tope.get("modo") == MODE_DRY_RUN else ""
    return (f"START bloqueado: se disparó el tope de pérdida total del rescate{simulado} el "
            f"{tope.get('disparado_en')}. Reconocelo en la sección Rescate antes de volver a iniciar.")


def add_event(state: Dict[str, Any], tipo: str, texto: str, mode: Optional[str], ts: Optional[float] = None) -> None:
    state["eventos"].append({"ts": now_iso(ts), "tipo": tipo, "modo": mode, "texto": texto})
    state["eventos"] = state["eventos"][-MAX_EVENTOS:]


# --------------------------------------------------------------------------------------
# Binance: cuenta de futuros, spot y transferencias universales
# --------------------------------------------------------------------------------------


def spot_base_url(testnet: bool, override: str = "") -> str:
    if override:
        return override.rstrip("/")
    return SPOT_DEMO_URL if testnet else SPOT_MAINNET_URL


def _field(data: Dict[str, Any], key: str, path: str) -> Decimal:
    try:
        return dec(data[key])
    except (KeyError, ValueError) as exc:
        raise ReadError(f"Respuesta inesperada de Binance en {path}: falta o es inválido {key}.") from exc


class RescateBinance:
    """Cliente REST mínimo. Firma igual que ``bot_control.BinanceFuturesClient`` (HMAC-SHA256 del query)."""

    def __init__(self, futures_url: str, spot_url: str, api_key: str, api_secret: str,
                 session: Optional[requests.Session] = None) -> None:
        if not futures_url or not spot_url or not api_key or not api_secret:
            raise RescateError("Faltan API_KEY, API_SECRET o las URLs de Binance.")
        self.futures_url = futures_url.rstrip("/")
        self.spot_url = spot_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.http = session or requests.Session()

    def _request(self, method: str, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        query_params = dict(params or {})
        query_params["recvWindow"] = RECV_WINDOW
        query_params["timestamp"] = int(time.time() * 1000)
        query = urlencode(query_params)
        signature = hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{base}{path}?{query}&signature={signature}"
        try:
            resp = self.http.request(method, url, headers={"X-MBX-APIKEY": self._api_key},
                                     timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise bot_control.BinanceError(f"Sin respuesta de Binance en {method} {path} ({exc.__class__.__name__}).") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        code = data.get("code") if isinstance(data, dict) else None
        if resp.status_code != 200 or (code is not None and code not in (0, 200)):
            msg = str(data.get("msg", "")) if isinstance(data, dict) else ""
            raise bot_control.BinanceError(
                f"Binance respondió {resp.status_code} en {method} {path}: {code} {msg[:200]}".rstrip(),
                http_status=resp.status_code, code=code)
        if data is None:
            raise bot_control.BinanceError(f"Binance respondió {resp.status_code} sin JSON válido en {method} {path}.",
                                           http_status=resp.status_code)
        return data

    def _read(self, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return self._request("GET", base, path, params)
        except bot_control.BinanceError as exc:
            raise ReadError(str(exc)) from exc

    def futures_account(self) -> Dict[str, Any]:
        path = "/fapi/v2/account"
        data = self._read(self.futures_url, path)
        if not isinstance(data, dict):
            raise ReadError(f"Respuesta inesperada de Binance en {path} (no es un objeto).")
        positions = data.get("positions")
        if not isinstance(positions, list):
            raise ReadError(f"Respuesta inesperada de Binance en {path}: falta positions.")
        abiertas = 0
        for row in positions:
            if not isinstance(row, dict):
                raise ReadError(f"Respuesta inesperada de Binance en {path}: posición inválida.")
            if _field(row, "positionAmt", path) != 0:
                abiertas += 1
        return {
            "margin_balance": _field(data, "totalMarginBalance", path),
            "maint_margin": _field(data, "totalMaintMargin", path),
            "max_withdraw": _field(data, "maxWithdrawAmount", path),
            "available": _field(data, "availableBalance", path),
            "posiciones": abiertas,
        }

    def spot_usdt_free(self) -> Decimal:
        """USDT libre en SPOT vía /api/v3/account (no ccxt binanceusdm: su fetch_balance spot lee futuros)."""
        path = "/api/v3/account"
        data = self._read(self.spot_url, path)
        balances = data.get("balances") if isinstance(data, dict) else None
        if not isinstance(balances, list):
            raise ReadError(f"Respuesta inesperada de Binance en {path}: falta balances.")
        for row in balances:
            if isinstance(row, dict) and row.get("asset") == TRANSFER_ASSET:
                return _field(row, "free", path)
        return Decimal(0)

    def api_restrictions(self) -> Dict[str, Any]:
        path = "/sapi/v1/account/apiRestrictions"
        data = self._read(self.spot_url, path)
        if not isinstance(data, dict):
            raise ReadError(f"Respuesta inesperada de Binance en {path}.")
        return data

    def transfer(self, tipo_api: str, amount: Decimal) -> int:
        """POST /sapi/v1/asset/transfer. Devuelve tranId. TransferRejected / TransferAmbiguous."""
        path = "/sapi/v1/asset/transfer"
        params = {"type": tipo_api, "asset": TRANSFER_ASSET, "amount": money(amount)}
        try:
            data = self._request("POST", self.spot_url, path, params)
        except bot_control.BinanceError as exc:
            status = exc.http_status
            if status is None or status >= 500 or status in (200, 408) or exc.code in AMBIGUOUS_CODES:
                raise TransferAmbiguous(str(exc)) from exc
            raise TransferRejected(str(exc)) from exc
        tran_id = data.get("tranId") if isinstance(data, dict) else None
        if isinstance(tran_id, bool) or not isinstance(tran_id, int):
            raise TransferAmbiguous(f"Binance respondió 200 sin tranId en POST {path}.")
        return tran_id

    def transfer_history(self, tipo_api: str, start_ms: int) -> List[Dict[str, Any]]:
        path = "/sapi/v1/asset/transfer"
        data = self._read(self.spot_url, path, {"type": tipo_api, "startTime": int(start_ms),
                                                "size": HISTORY_PAGE_SIZE})
        if not isinstance(data, dict):
            raise ReadError(f"Respuesta inesperada de Binance en GET {path}.")
        rows = data.get("rows")
        if rows is None and data.get("total") in (0, "0"):
            return []
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ReadError(f"Respuesta inesperada de Binance en GET {path}: falta rows.")
        return rows


# --------------------------------------------------------------------------------------
# Servicio
# --------------------------------------------------------------------------------------


class RescateSettings:
    """Variables de entorno del servicio. Un modo inválido es ``off`` (fail-closed)."""

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
        raw_interval = env.get(ENV_INTERVAL, "").strip()
        try:
            interval = int(raw_interval) if raw_interval else DEFAULT_INTERVAL
        except ValueError:
            log.warning("%s=%r no es entero: se usa %s.", ENV_INTERVAL, raw_interval, DEFAULT_INTERVAL)
            interval = DEFAULT_INTERVAL
        self.interval = max(MIN_INTERVAL, min(MAX_INTERVAL, interval))
        self.loan_state_path = pathlib.Path(env.get(ENV_LOAN_STATE, "").strip() or DEFAULT_LOAN_STATE)
        self.spot_url_override = env.get(ENV_SPOT_URL, "").strip().rstrip("/")


def check_restrictions(data: Dict[str, Any]) -> List[str]:
    """Lista de problemas de permisos de la API key (vacía si está todo bien)."""
    problemas = []
    for key, expected in REQUIRED_RESTRICTIONS.items():
        if data.get(key) is not expected:
            problemas.append(f"{key} debería ser {str(expected).lower()}")
    return problemas


class RescateService:
    """Un ciclo por ``tick()``. Todo lo externo es inyectable (los tests usan dobles de Binance/Docker)."""

    def __init__(self, data_dir: pathlib.Path, settings: RescateSettings, binance: RescateBinance, *,
                 testnet: bool = False, exchange: str = "binance",
                 apagar: Optional[Callable[[], Tuple[str, Dict[str, Any]]]] = None,
                 apagar_disponible: Optional[bool] = None,
                 send: Optional[Callable[[str], bool]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.settings = settings
        self.mode = settings.mode
        self.binance = binance
        self.testnet = bool(testnet)
        self.exchange = (exchange or "").lower()
        self._apagar = apagar
        self.apagar_disponible = (apagar is not None) if apagar_disponible is None else apagar_disponible
        self.send = send or telegram_notify.send
        self.clock = clock
        self._restricciones: Optional[List[str]] = None  # None = sin verificar todavía
        self._alertas: Dict[str, str] = {}
        self._primer_tick = True

    # -- utilidades -------------------------------------------------------------------
    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _notify(self, text: str) -> None:
        prefix = "🧪 <b>[SIMULACIÓN]</b> " if self.mode == MODE_DRY_RUN else ""
        try:
            self.send(prefix + text)
        except Exception:  # pragma: no cover - telegram_notify.send no lanza
            log.warning("No se pudo notificar a Telegram.")

    def _alert(self, key: str, text: str) -> None:
        """Avisa solo si el mensaje de esa clave cambió (sin spam cada ciclo, ni en Telegram ni en el log)."""
        if self._alertas.get(key) != text:
            log.warning("%s", text)
            self._alertas[key] = text
            self._notify(f"⚠️ <b>Rescate</b>: {esc(text)}")

    def _clear(self, key: str, recovery: Optional[str] = None) -> None:
        if self._alertas.pop(key, None) is not None and recovery:
            self._notify(f"✅ <b>Rescate</b>: {esc(recovery)}")

    def _event(self, state: Dict[str, Any], tipo: str, texto: str) -> None:
        log.info("[%s] %s", tipo, texto)
        add_event(state, tipo, texto, self.mode, self.clock())

    def _sync_loan_file(self, state: Dict[str, Any]) -> None:
        """loan_state.json = préstamo del modo activo (ver ``loan_publicado``).

        Un préstamo simulado nunca pasa a real, y uno real nunca se deja de publicar por estar en dry_run.
        """
        loan, mode = loan_publicado(state, self.mode)
        write_loan_state(self.settings.loan_state_path, loan, mode, self.clock())

    # -- preflight --------------------------------------------------------------------
    def preflight(self) -> Optional[str]:
        """Motivo por el que NO se hacen transferencias (None = se puede operar)."""
        if self.exchange != "binance":
            return "El rescate solo funciona con Binance Futures (EXCHANGE)."
        if self.mode != MODE_REAL:
            return None
        if self.testnet:
            return ("Modo real bloqueado en testnet/Demo: Binance Demo no expone /sapi (no hay transferencias). "
                    "Usar dry_run.")
        if not self.apagar_disponible:
            return ("Modo real bloqueado: falta FUTURESBOARD_DOCKER_URL (o las credenciales de futuros), sin eso "
                    "el tope de pérdida no puede ejecutar Apagar.")
        if self._restricciones is None:
            try:
                self._restricciones = check_restrictions(self.binance.api_restrictions())
            except ReadError as exc:
                return f"No se pudieron verificar los permisos de la API key (se reintenta): {exc}"
            if self._restricciones:
                log.error("API key sin los permisos exigidos: %s", "; ".join(self._restricciones))
        if self._restricciones:
            return ("La API key no cumple los permisos exigidos para operar en real: "
                    + "; ".join(self._restricciones) + ". Corregirlos y reiniciar el servicio.")
        return None

    # -- lectura ----------------------------------------------------------------------
    def read_snapshot(self, state: Dict[str, Any]) -> Dict[str, Any]:
        fut = self.binance.futures_account()
        spot = self.binance.spot_usdt_free()
        snap: Dict[str, Any] = dict(fut)
        snap["spot_real"] = spot
        snap["capital"] = fut["margin_balance"] + spot
        margin, max_withdraw, spot_eff = fut["margin_balance"], fut["max_withdraw"], spot
        if self.mode == MODE_DRY_RUN:
            # Como si lo simulado se hubiera movido de verdad: el margen y lo retirable suben y el spot baja.
            loan = get_loan(state, MODE_DRY_RUN)
            margin, max_withdraw, spot_eff = margin + loan, max_withdraw + loan, max(Decimal(0), spot - loan)
        snap["margin_eff"] = margin
        snap["max_withdraw_eff"] = max_withdraw
        snap["spot_eff"] = spot_eff
        maint = fut["maint_margin"]
        snap["ratio"] = (margin / maint) if maint > 0 else None
        return snap

    @staticmethod
    def snapshot_view(snap: Dict[str, Any], mode: str, ts: float) -> Dict[str, Any]:
        ratio = snap.get("ratio")
        return {
            "ts": now_iso(ts),
            "error": None,
            "simulado": mode == MODE_DRY_RUN,
            "ratio": None if ratio is None else format(ratio.quantize(Decimal("0.0001"), decimal.ROUND_DOWN), "f"),
            "margin_balance": money(snap["margin_balance"]),
            "maint_margin": money(snap["maint_margin"]),
            "max_withdraw": money(snap["max_withdraw_eff"]),
            "spot_usdt": money(snap["spot_eff"]),
            "spot_usdt_real": money(snap["spot_real"]),
            "capital": money(snap["capital"]),
            "posiciones": snap["posiciones"],
        }

    # -- ciclo ------------------------------------------------------------------------
    def tick(self) -> Dict[str, Any]:
        with file_lock(self.data_dir / STATE_LOCK_NAME):
            state, err = load_state(self.data_dir)
            if err:
                self._alert("estado", err)
                return {"ok": False, "error": err}
            self._clear("estado", "Estado del rescate legible otra vez.")
            cfg, cfg_err = load_config(self.data_dir)
            if cfg_err:
                self._alert("config", cfg_err)
            else:
                self._clear("config")
            try:
                self._tick(state, cfg)
            finally:
                state["servicio"] = {"modo": self.mode, "heartbeat": now_iso(self.clock()),
                                     "intervalo": self.settings.interval, "testnet": self.testnet}
                save_state(self.data_dir, state)
            return {"ok": True}

    def _tick(self, state: Dict[str, Any], cfg: Dict[str, Any]) -> None:
        now_ms = self._now_ms()
        prev_mode = (state.get("servicio") or {}).get("modo")
        if self.mode == MODE_OFF:
            self._tick_off(state)
            return
        if self._primer_tick or prev_mode != self.mode:
            if prev_mode and prev_mode != self.mode:
                self._event(state, "modo", f"Modo del servicio: {prev_mode} -> {self.mode}.")
            self._sync_loan_file(state)
            self._primer_tick = False

        bloqueo = self.preflight()
        if not bloqueo and self.mode == MODE_DRY_RUN and real_pendiente(state):
            bloqueo = (f"Hay un préstamo REAL pendiente ({money(get_loan(state, MODE_REAL))} USDT): en dry_run no se "
                       "simulan transferencias y loan_state.json sigue publicando el préstamo real. Volver a "
                       f"{ENV_MODE}=real para devolverlo.")
        if bloqueo != state.get("bloqueo"):
            if bloqueo:
                self._event(state, "bloqueo", bloqueo)
            elif state.get("bloqueo"):
                self._event(state, "bloqueo", "Transferencias habilitadas otra vez.")
        state["bloqueo"] = bloqueo
        if bloqueo:
            self._alert("bloqueo", bloqueo)
        else:
            self._clear("bloqueo")

        self._procesar_reconocimiento(state, cfg)

        try:
            snap = self.read_snapshot(state)
        except (ReadError, RescateError) as exc:
            msg = f"No se pudo leer la cuenta en Binance: {exc}"
            state["ultimo"] = {"ts": now_iso(self.clock()), "error": msg}
            self._alert("lectura", msg)
            return
        self._clear("lectura", "Lectura de la cuenta en Binance recuperada.")
        state["ultimo"] = self.snapshot_view(snap, self.mode, self.clock())

        if state.get("en_vuelo"):
            self._conciliar(state, snap, now_ms)

        self._fijar_base(state, cfg, snap)
        self._tope(state, cfg, snap, now_ms)

        if state.get("en_vuelo") or bloqueo:
            return
        if snap["posiciones"] == 0:
            self._devolver(state, snap, now_ms)
        else:
            self._inyectar(state, cfg, snap, now_ms)

    def _tick_off(self, state: Dict[str, Any]) -> None:
        """off: no opera. Solo evita que un préstamo SIMULADO quede achicando a passivbot para siempre."""
        if not self._primer_tick:
            return
        self._primer_tick = False
        current = read_loan_state(self.settings.loan_state_path)
        if current and current.get("mode") == MODE_DRY_RUN:
            write_loan_state(self.settings.loan_state_path, get_loan(state, MODE_REAL), MODE_REAL, self.clock())
            self._event(state, "modo", "Modo off: loan_state.json tenía un préstamo simulado; se reescribió con el "
                                       "préstamo real.")

    # -- reconocimiento del tope --------------------------------------------------------
    def _procesar_reconocimiento(self, state: Dict[str, Any], cfg: Dict[str, Any]) -> None:
        tope = state.get("tope")
        if isinstance(tope, dict) and tope.get("id") and tope.get("id") == cfg.get("tope_reconocido"):
            self._event(state, "tope", f"Tope {tope['id']} reconocido en el panel: START desbloqueado.")
            state["tope"] = None

    # -- base y tope --------------------------------------------------------------------
    def _fijar_base(self, state: Dict[str, Any], cfg: Dict[str, Any], snap: Dict[str, Any]) -> None:
        if not cfg.get("habilitado") or tope_activo(state, cfg):
            return
        base = state.get("base")
        if isinstance(base, dict) and base.get("generacion") == cfg.get("generacion"):
            return
        pct = cfg_dec(cfg, "tope_perdida_pct")
        capital = snap["capital"]
        state["base"] = {"generacion": cfg.get("generacion"), "capital": money(capital),
                         "fijada_en": now_iso(self.clock())}
        limite = capital * (1 - pct / 100) if pct is not None else None
        texto = (f"Rescate habilitado: base {money(capital)} USDT (futuros + spot). Tope de pérdida {pct}%: "
                 f"se apaga todo si el capital total baja a {money(limite) if limite is not None else '?'} USDT.")
        self._event(state, "base", texto)
        self._notify(f"🛟 <b>Rescate</b>: {esc(texto)}")

    def _tope(self, state: Dict[str, Any], cfg: Dict[str, Any], snap: Dict[str, Any], now_ms: int) -> None:
        activo = tope_activo(state, cfg)
        if activo is not None:
            self._reintentar_apagar(state, activo, now_ms)
            return
        if not cfg.get("habilitado"):
            return
        base = state.get("base")
        pct = cfg_dec(cfg, "tope_perdida_pct")
        if not isinstance(base, dict) or base.get("generacion") != cfg.get("generacion") or pct is None:
            return
        base_capital = dec(base["capital"])
        limite = base_capital * (1 - pct / 100)
        capital = snap["capital"]
        if capital > limite:
            return
        tope = {
            "id": secrets.token_hex(8),
            "generacion": cfg.get("generacion"),
            "disparado_en": now_iso(self.clock()),
            "modo": self.mode,
            "capital": money(capital),
            "base": money(base_capital),
            "limite": money(limite),
            "pct": format(pct.normalize(), "f"),
            "apagar": {"resultado": "pendiente", "intentos": 0, "ultimo_ms": None, "detalle": ""},
        }
        state["tope"] = tope
        texto = (f"TOPE DE PÉRDIDA TOTAL disparado: capital {money(capital)} USDT <= límite {money(limite)} "
                 f"(base {money(base_capital)}, tope {tope['pct']}%). START queda bloqueado hasta reconocerlo.")
        self._event(state, "tope", texto)
        # Persistir antes de Apagar: el panel bloquea START desde este momento.
        save_state(self.data_dir, state)
        self._notify(f"🛑 <b>Rescate</b>: {esc(texto)}")
        self._ejecutar_apagar(state, tope, now_ms)

    def _reintentar_apagar(self, state: Dict[str, Any], tope: Dict[str, Any], now_ms: int) -> None:
        ap = tope.get("apagar") or {}
        if ap.get("resultado") != "error":
            return
        if int(ap.get("intentos") or 0) >= APAGAR_MAX_INTENTOS:
            self._alert("apagar", f"Apagar por tope falló {ap.get('intentos')} veces: requiere intervención manual "
                                  f"(último error: {ap.get('detalle')}).")
            return
        if now_ms - int(ap.get("ultimo_ms") or 0) < APAGAR_REINTENTO_MS:
            return
        self._ejecutar_apagar(state, tope, now_ms)

    def _ejecutar_apagar(self, state: Dict[str, Any], tope: Dict[str, Any], now_ms: int) -> None:
        ap = tope.setdefault("apagar", {"resultado": "pendiente", "intentos": 0, "ultimo_ms": None, "detalle": ""})
        if tope.get("modo") == MODE_DRY_RUN or self.mode == MODE_DRY_RUN:
            ap.update(resultado="simulado", detalle="Simulación: no se detuvo passivbot ni se cerró nada en Binance.")
            self._event(state, "apagar", "HARÍA Apagar (detener passivbot, cancelar órdenes y cerrar posiciones). "
                                         "No se ejecutó: modo dry_run.")
            return
        ap["intentos"] = int(ap.get("intentos") or 0) + 1
        ap["ultimo_ms"] = now_ms
        if self._apagar is None:
            outcome, payload = "error", {"ok": False, "error": "Apagar no disponible (falta FUTURESBOARD_DOCKER_URL)."}
        else:
            try:
                outcome, payload = self._apagar()
            except Exception as exc:  # defensivo: el tope nunca debe tirar el servicio
                log.exception("Apagar por tope: error inesperado")
                outcome, payload = "error", {"ok": False, "error": f"error inesperado ({exc.__class__.__name__})"}
        ap["resultado"] = "ok" if outcome == "ok" else "error"
        ap["detalle"] = str(payload.get("error") or payload.get("warning") or "Apagar completo.")[:500]
        self._event(state, "apagar", f"Apagar por tope (intento {ap['intentos']}): {ap['resultado']}. {ap['detalle']}")
        text = telegram_notify.format_bot_action("stop", {"modo": "apagar"}, outcome, payload, APAGAR_USUARIO)
        if text:
            self._notify(text)

    # -- transferencias ---------------------------------------------------------------
    def _cooldown(self, state: Dict[str, Any], tipo: str, now_ms: int) -> bool:
        return now_ms < int((state.get("cooldown_ms") or {}).get(tipo) or 0)

    def _set_cooldown(self, state: Dict[str, Any], tipo: str, until_ms: int) -> None:
        state.setdefault("cooldown_ms", {})[tipo] = until_ms

    def _inyectar(self, state: Dict[str, Any], cfg: Dict[str, Any], snap: Dict[str, Any], now_ms: int) -> None:
        if not cfg.get("habilitado") or tope_activo(state, cfg):
            return
        base = state.get("base")
        if not isinstance(base, dict) or base.get("generacion") != cfg.get("generacion"):
            return  # sin base fijada el tope no está armado: no se presta
        ratio = snap.get("ratio")
        umbral = cfg_dec(cfg, "umbral_ratio")
        if ratio is None or snap["posiciones"] <= 0 or umbral is None or not ratio < umbral:
            return
        if self._cooldown(state, TIPO_INYECCION, now_ms):
            return
        reserva = cfg_dec(cfg, "reserva_max_usdt") or Decimal(0)
        paso = cfg_dec(cfg, "paso_usdt") or Decimal(0)
        disponible = reserva - get_loan(state, self.mode)
        amount = floor_cent(min(paso, disponible, snap["spot_eff"]))
        if amount < MIN_INYECCION:
            if disponible < MIN_INYECCION:
                self._alert("sin_reserva", f"Ratio {ratio:.2f} < {umbral} pero la reserva máxima ({money(reserva)} USDT) "
                                           "ya está prestada: no se inyecta más.")
            else:
                self._alert("sin_reserva", f"Ratio {ratio:.2f} < {umbral} pero no hay USDT libre en spot "
                                           f"({money(snap['spot_eff'])}).")
            return
        self._clear("sin_reserva")
        motivo = f"ratio margen/mantenimiento {ratio:.2f} < {umbral}"
        self._transferir(state, TIPO_INYECCION, amount, snap, now_ms, motivo)

    def _devolver(self, state: Dict[str, Any], snap: Dict[str, Any], now_ms: int) -> None:
        loan = get_loan(state, self.mode)
        if loan <= 0 or snap["posiciones"] > 0 or self._cooldown(state, TIPO_DEVOLUCION, now_ms):
            return
        amount = floor_cent(min(loan, snap["max_withdraw_eff"]))
        if amount < MIN_DEVOLUCION:
            self._alert("devolucion", f"Hay {money(loan)} USDT prestados pero maxWithdrawAmount es "
                                      f"{money(snap['max_withdraw_eff'])}: se reintenta en el próximo ciclo.")
            return
        self._clear("devolucion")
        motivo = "sin posiciones abiertas" + ("" if amount == loan else f" (parcial: maxWithdrawAmount {money(snap['max_withdraw_eff'])})")
        self._transferir(state, TIPO_DEVOLUCION, amount, snap, now_ms, motivo)

    def _transferir(self, state: Dict[str, Any], tipo: str, amount: Decimal, snap: Dict[str, Any], now_ms: int,
                    motivo: str) -> None:
        entry: Dict[str, Any] = {
            "id": secrets.token_hex(8),
            "tipo": tipo,
            "tipo_api": TIPO_API[tipo],
            "modo": self.mode,
            "monto": money(amount),
            "creada_ms": now_ms,
            "creada_en": now_iso(now_ms / 1000),
            "spot_antes": money(snap["spot_real"]),
            "motivo": motivo,
            "estado": "enviando",
            "tranId": None,
            "chequeos": 0,
            "prestamo_aplicado": False,
        }
        if self.mode == MODE_DRY_RUN:
            # No se llama a /sapi: se registra lo que HARÍA y se actualiza el préstamo simulado.
            self._finalizar(state, entry, "simulada", None, f"Simulación ({motivo}).")
            return
        state["en_vuelo"] = entry
        save_state(self.data_dir, state)  # persistir antes del POST: un reinicio lo concilia
        try:
            tran_id = self.binance.transfer(entry["tipo_api"], amount)
        except TransferRejected as exc:
            state["en_vuelo"] = None
            self._set_cooldown(state, tipo, now_ms + COOLDOWN_RECHAZO_MS)
            self._registrar(state, entry, "rechazada", None, str(exc))
            texto = f"{self._titulo(tipo)} de {money(amount)} USDT rechazada por Binance: {exc}"
            self._event(state, "error", texto)
            self._notify(f"❌ <b>Rescate</b>: {esc(texto)}")
            return
        except TransferAmbiguous as exc:
            entry["estado"] = "incierto"
            if tipo == TIPO_INYECCION:
                self._aplicar_prestamo(state, entry)  # conservador: passivbot no dimensiona sobre lo prestado
            texto = (f"{self._titulo(tipo)} de {money(amount)} USDT con resultado incierto ({exc}). No se reintenta: "
                     "se concilia con el historial de transferencias.")
            self._event(state, "incierto", texto)
            self._notify(f"⚠️ <b>Rescate</b>: {esc(texto)}")
            return
        entry["tranId"] = tran_id
        entry["estado"] = "pendiente"
        if tipo == TIPO_INYECCION:
            self._aplicar_prestamo(state, entry)
        self._confirmar_por_historial(state, now_ms)

    @staticmethod
    def _titulo(tipo: str) -> str:
        return "Inyección" if tipo == TIPO_INYECCION else "Devolución"

    def _aplicar_prestamo(self, state: Dict[str, Any], entry: Dict[str, Any]) -> None:
        if entry.get("prestamo_aplicado"):
            return
        mode = entry["modo"]
        set_loan(state, mode, get_loan(state, mode) + dec(entry["monto"]))
        entry["prestamo_aplicado"] = True
        self._sync_loan_file(state)

    def _revertir_prestamo(self, state: Dict[str, Any], entry: Dict[str, Any]) -> None:
        if not entry.get("prestamo_aplicado"):
            return
        mode = entry["modo"]
        set_loan(state, mode, get_loan(state, mode) - dec(entry["monto"]))
        entry["prestamo_aplicado"] = False
        self._sync_loan_file(state)

    def _registrar(self, state: Dict[str, Any], entry: Dict[str, Any], estado: str, tran_id: Optional[int],
                   detalle: str) -> None:
        state["ledger"].append({
            "id": entry["id"], "ts": now_iso(self.clock()), "creada_en": entry.get("creada_en"),
            "tipo": entry["tipo"], "modo": entry["modo"], "monto": entry["monto"], "estado": estado,
            "tranId": tran_id, "detalle": detalle[:300],
            "prestamo_despues": money(get_loan(state, entry["modo"])),
        })
        state["ledger"] = state["ledger"][-MAX_LEDGER:]

    def _finalizar(self, state: Dict[str, Any], entry: Dict[str, Any], estado: str, tran_id: Optional[int],
                   detalle: str) -> None:
        """Cierra la transferencia: confirmada / simulada => aplica al préstamo; fallida => revierte."""
        tipo, mode, monto = entry["tipo"], entry["modo"], dec(entry["monto"])
        ok = estado in ("confirmada", "simulada")
        if tipo == TIPO_INYECCION:
            if ok:
                self._aplicar_prestamo(state, entry)
            else:
                self._revertir_prestamo(state, entry)
        elif ok:
            # La devolución recién baja el préstamo con la transferencia confirmada.
            set_loan(state, mode, get_loan(state, mode) - monto)
            self._sync_loan_file(state)
        state["en_vuelo"] = None
        self._registrar(state, entry, estado, tran_id, detalle)
        loan = money(get_loan(state, mode))
        if ok:
            flecha = "spot → futuros" if tipo == TIPO_INYECCION else "futuros → spot"
            ref = f" (tranId {tran_id})" if tran_id else ""
            texto = (f"{self._titulo(tipo)} {money(monto)} USDT {flecha}{ref}. {detalle} "
                     f"Préstamo pendiente: {loan} USDT.")
            self._event(state, tipo, texto)
            self._notify(f"🛟 <b>Rescate</b>: {esc(texto)}")
        else:
            texto = f"{self._titulo(tipo)} {money(monto)} USDT {estado}: {detalle} Préstamo pendiente: {loan} USDT."
            self._event(state, "error", texto)
            self._notify(f"❌ <b>Rescate</b>: {esc(texto)}")

    # -- conciliación -----------------------------------------------------------------
    def _known_tran_ids(self, state: Dict[str, Any]) -> set:
        return {e.get("tranId") for e in state["ledger"] if e.get("tranId") is not None}

    def _confirmar_por_historial(self, state: Dict[str, Any], now_ms: int) -> None:
        """Justo después de un POST con tranId: si el historial ya lo muestra CONFIRMED, se cierra."""
        entry = state["en_vuelo"]
        try:
            rows = self.binance.transfer_history(entry["tipo_api"], entry["creada_ms"] - CLOCK_SKEW_MS)
        except ReadError as exc:
            log.warning("Historial de transferencias no disponible (%s): se confirma en el próximo ciclo.", exc)
            return
        row = next((r for r in rows if r.get("tranId") == entry["tranId"]), None)
        if row is not None:
            self._aplicar_fila(state, entry, row)

    def _aplicar_fila(self, state: Dict[str, Any], entry: Dict[str, Any], row: Dict[str, Any]) -> None:
        status = str(row.get("status") or "").upper()
        tran_id = row.get("tranId")
        if status == "CONFIRMED":
            self._finalizar(state, entry, "confirmada", tran_id, f"Motivo: {entry.get('motivo')}.")
        elif status == "FAILED":
            self._set_cooldown(state, entry["tipo"], self._now_ms() + COOLDOWN_NO_EJECUTADA_MS)
            self._finalizar(state, entry, "fallida", tran_id, "Binance la marcó FAILED en el historial.")
        else:
            entry["tranId"] = tran_id
            entry["estado"] = "pendiente"

    def _conciliar(self, state: Dict[str, Any], snap: Dict[str, Any], now_ms: int) -> None:
        entry = state["en_vuelo"]
        if entry.get("modo") != MODE_REAL:
            # Una simulación nunca queda en vuelo; si quedó algo raro, se descarta sin tocar el préstamo real.
            state["en_vuelo"] = None
            return
        if self.mode != MODE_REAL:
            # En dry_run no se llama a /sapi: queda en vuelo (y bloquea) hasta volver a real.
            return
        if entry.get("estado") == "enviando":
            # Se cortó entre el POST y su respuesta (reinicio): resultado desconocido.
            entry["estado"] = "incierto"
            self._event(state, "incierto", f"{self._titulo(entry['tipo'])} {entry['monto']} USDT quedó sin "
                                           "respuesta (reinicio): se concilia con el historial.")
        entry["chequeos"] = int(entry.get("chequeos") or 0) + 1
        try:
            rows = self.binance.transfer_history(entry["tipo_api"], entry["creada_ms"] - CLOCK_SKEW_MS)
        except ReadError as exc:
            self._alert("conciliacion", f"No se pudo leer el historial de transferencias: {exc}")
            return
        self._clear("conciliacion")
        if entry.get("tranId") is not None:
            row = next((r for r in rows if r.get("tranId") == entry["tranId"]), None)
            if row is not None:
                self._aplicar_fila(state, entry, row)
            elif now_ms - int(entry["creada_ms"]) > RECONCILE_ALERTA_MS:
                self._alert("conciliacion_manual", f"La transferencia tranId {entry['tranId']} no aparece en el "
                                                   "historial: conciliar a mano. No se hacen transferencias nuevas.")
            return
        conocidos = self._known_tran_ids(state)
        monto = dec(entry["monto"])
        candidatas = []
        for r in rows:
            try:
                same_amount = dec(r.get("amount")) == monto
                ts_ok = int(r.get("timestamp") or 0) >= int(entry["creada_ms"]) - CLOCK_SKEW_MS
            except (ValueError, TypeError):
                continue
            if (r.get("asset") == TRANSFER_ASSET and str(r.get("type") or entry["tipo_api"]) == entry["tipo_api"]
                    and same_amount and ts_ok and r.get("tranId") not in conocidos):
                candidatas.append(r)
        if len(candidatas) > 1:
            self._alert("conciliacion_manual", f"Varias transferencias de {entry['monto']} USDT coinciden en el "
                                               "historial: conciliar a mano. No se hacen transferencias nuevas.")
            return
        if candidatas:
            self._aplicar_fila(state, entry, candidatas[0])
            return
        if now_ms - int(entry["creada_ms"]) < RECONCILE_GRACE_MS or entry["chequeos"] < RECONCILE_MIN_CHECKS:
            return
        # Sin rastro en el historial: decide la variación del saldo spot (solo el rescate mueve ese USDT).
        delta = snap["spot_real"] - dec(entry["spot_antes"])
        esperado = -monto if entry["tipo"] == TIPO_INYECCION else monto
        if abs(delta - esperado) <= TOLERANCIA_SALDO:
            self._finalizar(state, entry, "confirmada", None,
                            "Confirmada por la variación del saldo spot (no figura en el historial).")
        elif abs(delta) <= TOLERANCIA_SALDO:
            self._set_cooldown(state, entry["tipo"], now_ms + COOLDOWN_NO_EJECUTADA_MS)
            self._finalizar(state, entry, "no_ejecutada", None,
                            "No figura en el historial y el saldo spot no cambió: no se ejecutó.")
        else:
            self._alert("conciliacion_manual", f"{self._titulo(entry['tipo'])} {entry['monto']} USDT sin rastro en el "
                                               f"historial y el saldo spot cambió {money(delta)} USDT: conciliar a "
                                               "mano. No se hacen transferencias nuevas.")


# --------------------------------------------------------------------------------------
# Apagar (la misma rutina del panel, con el mismo lock)
# --------------------------------------------------------------------------------------


def make_apagar(data_dir: pathlib.Path, bot_settings: bot_control.Settings,
                futures_client: bot_control.BinanceFuturesClient,
                docker: Optional[bot_control.DockerClient] = None,
                lock_timeout: float = APAGAR_LOCK_TIMEOUT_S) -> Callable[[], Tuple[str, Dict[str, Any]]]:
    """Devuelve la acción Apagar del panel: ``bot_control.stop_bot(..., "apagar")`` bajo el lock de acciones."""
    store = bot_control.Store(data_dir)

    def apagar() -> Tuple[str, Dict[str, Any]]:
        deadline = time.monotonic() + lock_timeout
        handle = store.try_lock()
        while handle is None and time.monotonic() < deadline:
            time.sleep(LOCK_POLL_S)
            handle = store.try_lock()
        params = {"modo": "apagar", "origen": "rescate_tope"}
        if handle is None:
            payload = {"ok": False, "error": "No se pudo tomar el lock de acciones del panel (hay una acción en curso). "
                                             "Se reintenta."}
            return "error", payload
        try:
            try:
                payload = bot_control.stop_bot(bot_settings, store, "apagar", docker=docker, binance=futures_client)
                outcome = "ok"
            except bot_control.BotControlError as exc:
                payload = {**exc.payload, "ok": False, "error": str(exc)}
                outcome = "error"
        finally:
            handle.release()
        try:
            store.audit(APAGAR_USUARIO, "stop", params, outcome, jobs.audit_detail(payload), None)
        except OSError:
            log.exception("No se pudo escribir el log de auditoría")
        return outcome, payload

    return apagar


# --------------------------------------------------------------------------------------
# Panel: vista del estado
# --------------------------------------------------------------------------------------


def panel_status(data_dir: pathlib.Path, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    cfg, cfg_err = load_config(data_dir)
    state, state_err = load_state(data_dir)
    servicio = state.get("servicio") if isinstance(state.get("servicio"), dict) else None
    modo = (servicio or {}).get("modo")
    vivo = False
    if servicio and servicio.get("heartbeat"):
        try:
            age = now - dt.datetime.fromisoformat(servicio["heartbeat"]).timestamp()
            vivo = age <= 3 * int(servicio.get("intervalo") or DEFAULT_INTERVAL) + 30
        except (TypeError, ValueError):
            vivo = False
    loan, loan_mode = loan_publicado(state, modo)
    base = state.get("base")
    base_vigente = base if isinstance(base, dict) and base.get("generacion") == cfg.get("generacion") else None
    limite = None
    try:
        pct = cfg_dec(cfg, "tope_perdida_pct")
        if base_vigente and pct is not None:
            limite = money(dec(base_vigente["capital"]) * (1 - pct / 100))
    except ValueError:
        limite = None
    return {
        "config": {k: cfg.get(k) for k in ("habilitado", "generacion", "reserva_max_usdt", "paso_usdt",
                                            "umbral_ratio", "tope_perdida_pct", "actualizado_en",
                                            "actualizado_por")},
        "limites": {k: [str(lo), str(hi)] for k, (lo, hi) in LIMITES.items()},
        "errores": [e for e in (cfg_err, state_err) if e],
        "servicio": {"modo": modo, "vivo": vivo, "heartbeat": (servicio or {}).get("heartbeat"),
                     "testnet": (servicio or {}).get("testnet")},
        "bloqueo": state.get("bloqueo"),
        "ultimo": state.get("ultimo"),
        "prestamo": money(loan),
        "prestamo_modo": loan_mode,
        "base": base_vigente,
        "limite_tope": limite,
        "tope": tope_activo(state, cfg),
        "start_bloqueado": start_bloqueado(data_dir),
        "en_vuelo": state.get("en_vuelo"),
        "eventos": list(reversed(state["eventos"][-20:])),
        "aviso": AVISO,
    }


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _load_app_config() -> Any:
    from futuresboard.app import default_config_dir
    from futuresboard.config import Config

    return Config.from_config_dir(default_config_dir())


def build_service(settings: RescateSettings, config: Any) -> RescateService:
    from futuresboard import logs

    logs.register_secret(config.API_KEY)
    logs.register_secret(config.API_SECRET)
    data_dir = pathlib.Path(config.DATABASE).parent
    exchange = getattr(config.EXCHANGE, "value", config.EXCHANGE) or ""
    testnet = bool(config.BINANCE_TESTNET)
    binance = RescateBinance(config.API_BASE_URL or "", spot_base_url(testnet, settings.spot_url_override),
                             config.API_KEY or "", config.API_SECRET or "")
    bot_settings = bot_control.Settings()
    apagar = None
    if bot_settings.enabled:
        try:
            futures_client = bot_control.BinanceFuturesClient(config.API_BASE_URL or "", config.API_KEY or "",
                                                              config.API_SECRET or "", algo_optional=testnet)
            apagar = make_apagar(data_dir, bot_settings, futures_client)
        except bot_control.BotControlError as exc:
            log.error("Apagar no disponible: %s", exc)
    return RescateService(data_dir, settings, binance, testnet=testnet, exchange=str(exchange), apagar=apagar)


def _simular(settings: RescateSettings, config: Any, tipo: str, monto: Optional[str]) -> int:
    if settings.mode != MODE_DRY_RUN:
        print(f"Solo disponible con {ENV_MODE}=dry_run (modo actual: {settings.mode}).")
        return 2
    amount: Optional[Decimal] = None
    if monto is not None:
        try:
            amount = floor_cent(dec(monto))
        except ValueError:
            print("Monto inválido.")
            return 2
        if amount <= 0 or amount > LIMITES["reserva_max_usdt"][1]:
            print(f"El monto tiene que ser mayor que 0 y como máximo {LIMITES['reserva_max_usdt'][1]}.")
            return 2
    data_dir = pathlib.Path(config.DATABASE).parent
    with file_lock(data_dir / STATE_LOCK_NAME):
        state, err = load_state(data_dir)
        if err:
            print(err)
            return 1
        if real_pendiente(state):
            print(f"Hay un préstamo REAL pendiente ({money(get_loan(state, MODE_REAL))} USDT): no se simula nada "
                  "hasta devolverlo en modo real.")
            return 2
        loan = get_loan(state, MODE_DRY_RUN)
        if tipo == TIPO_INYECCION:
            if amount is None:
                print("Falta el monto.")
                return 2
            nuevo = loan + amount
        else:
            amount = loan if amount is None else amount
            if amount <= 0:
                print("No hay préstamo simulado para devolver.")
                return 2
            if amount > loan:
                print(f"No se puede devolver más que el préstamo simulado ({money(loan)} USDT).")
                return 2
            nuevo = loan - amount
        set_loan(state, MODE_DRY_RUN, nuevo)
        entry = {"id": secrets.token_hex(8), "tipo": tipo, "modo": MODE_DRY_RUN, "monto": money(amount),
                 "creada_en": now_iso()}
        state["ledger"].append({**entry, "ts": now_iso(), "estado": "simulada", "tranId": None,
                                "detalle": "Simulación manual (CLI).", "prestamo_despues": money(nuevo)})
        add_event(state, tipo, f"Simulación manual (CLI): {tipo} {money(amount)} USDT. Préstamo simulado: "
                               f"{money(nuevo)} USDT.", MODE_DRY_RUN)
        write_loan_state(settings.loan_state_path, nuevo, MODE_DRY_RUN)
        save_state(data_dir, state)
    print(f"{tipo} simulada de {money(amount)} USDT. Préstamo simulado: {money(nuevo)} USDT "
          f"({settings.loan_state_path}).")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="futuresboard.rescate",
                                     description="Rescate como colchón (ver docs/rescate.md).")
    parser.add_argument("--once", action="store_true", help="Un solo ciclo y salir.")
    sub = parser.add_subparsers(dest="comando")
    p_iny = sub.add_parser("simular-inyeccion", help="Solo dry_run: suma un préstamo simulado.")
    p_iny.add_argument("monto")
    p_dev = sub.add_parser("simular-devolucion", help="Solo dry_run: resta del préstamo simulado (default: todo).")
    p_dev.add_argument("monto", nargs="?")
    args = parser.parse_args(argv)

    from futuresboard import logs

    logs.configure_logging()
    settings = RescateSettings()
    if settings.mode_error:
        log.error("%s", settings.mode_error)
    config = _load_app_config()

    if args.comando == "simular-inyeccion":
        return _simular(settings, config, TIPO_INYECCION, args.monto)
    if args.comando == "simular-devolucion":
        return _simular(settings, config, TIPO_DEVOLUCION, args.monto)

    service = build_service(settings, config)
    telegram_notify.log_startup_state(log)
    log.info("Rescate iniciado: modo %s, intervalo %ss, loan_state %s, datos %s, testnet %s.", settings.mode,
             settings.interval, settings.loan_state_path, service.data_dir, service.testnet)
    # Una sola instancia: una segunda queda esperando el lock (standby), no opera en paralelo.
    service.data_dir.mkdir(parents=True, exist_ok=True)
    with open(service.data_dir / SERVICE_LOCK_NAME, "a") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.warning("Otra instancia del rescate tiene el lock: esperando.")
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        while True:
            try:
                service.tick()
            except Exception as exc:  # el loop no se cae por un ciclo con error
                log.exception("Ciclo del rescate con error (%s).", exc.__class__.__name__)
            if args.once:
                return 0
            time.sleep(settings.interval)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
