"""Control de passivbot (forager) desde el dashboard.

Reemplaza al guardian / servicio SCC (puerto 9009). El dashboard:

* lee y escribe la config HJSON de forager (montada RW en el contenedor del dashboard),
* controla el contenedor de passivbot via Docker Engine API HTTP, siempre a traves de un
  docker-socket-proxy (nunca se monta docker.sock en el dashboard),
* deja un log de auditoria (JSONL) en el directorio de datos.

Toda la configuracion es por variables de entorno (ver ``docs/panel-passivbot.md``).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import fcntl
import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import tempfile
import time
from typing import Any
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from urllib.parse import quote
from urllib.parse import urlencode

import hjson
import requests

from futuresboard import credenciales as credenciales_mod

ENV_DOCKER_URL = "FUTURESBOARD_DOCKER_URL"
ENV_CONTAINER = "FUTURESBOARD_PASSIVBOT_CONTAINER"
ENV_FORAGER_CONFIG = "FUTURESBOARD_FORAGER_CONFIG"
ENV_MODES_SUPPORTED = "FUTURESBOARD_FORAGER_SUPPORTS_MODES"

DEFAULT_CONTAINER = "client17-passivbot"

# Presets historicos del guardian: (twe_long, twe_short)
RISK_PRESETS: Dict[str, tuple] = {
    "bajo": (4, 1),
    "medio": (6, 2),
    "alto": (8, 3),
}
STOP_MODES = ("graceful", "apagar")

# TWE manual (además de los presets y los perfiles): rango y paso validados en el servidor. Arriba de
# TWE_ADVERTENCIA el panel muestra una advertencia y pide confirmarlo: en el backtest multianual 2021-2026 (botbulls-lab
# multianual) aparecen liquidaciones desde TWE 6 y sin rescate se midió hasta TWE 4.
TWE_MANUAL_MIN = decimal.Decimal("0.5")
TWE_MANUAL_MAX = decimal.Decimal("8")
TWE_MANUAL_PASO = decimal.Decimal("0.25")
TWE_ADVERTENCIA = decimal.Decimal("4")
# twe_short que se escribe con n_shorts 0 (TWE manual y perfiles): forager no lo usa (sw = 0.1 sin shorts), y no se
# usa 0 porque cambia el WE limit de un short que hubiera quedado abierto (ver "Bloqueante conocido" en
# docs/panel-passivbot.md). Es el del preset bajo: el perfil "Con rescate" (TWE 4) queda igual a ese preset.
TWE_SHORT_SOLO_LONG = 1

MODE_NORMAL = "normal"
MODE_GRACEFUL_STOP = "graceful_stop"

# Alias de long_mode/short_mode -> nombre canonico. Es la misma tabla que MODE_ALIASES de
# forager_modes.py en passivbot (botbulls/passivbot#2), que normaliza con strip().lower().
# Si cambia alla, cambiar aca: tests/test_bot_control.py la fija como literal.
MODE_ALIASES: Dict[str, str] = {
    "n": "normal",
    "normal": "normal",
    "gs": "graceful_stop",
    "graceful_stop": "graceful_stop",
    "graceful-stop": "graceful_stop",
    "m": "manual",
    "manual": "manual",
    "p": "panic",
    "panic": "panic",
    "t": "tp_only",
    "tp_only": "tp_only",
    "tp-only": "tp_only",
}
ACCEPTED_MODES = "normal (n), graceful_stop (gs, graceful-stop), manual (m), panic (p), tp_only (t, tp-only)"
# Modos canonicos que significan "no abrir nuevas posiciones" (los alias ya vienen normalizados).
STOP_MODE_VALUES = {MODE_GRACEFUL_STOP}

STOP_TIMEOUT_SECONDS = 20
HTTP_TIMEOUT_SECONDS = 10
MAX_BACKUPS = 20

AUDIT_LOG_NAME = "bot_actions.log"
# Un renglón por arranque de passivbot (Iniciar, Reanudar, Reiniciar, START por API, perfil y el reinicio automático
# de C3): con qué estrategia arrancó (hash y resumen). Iniciar lo usa para mostrar qué cambió desde el último arranque.
ARRANQUES_FILE_NAME = "bot_arranques.jsonl"
MAX_ARRANQUES = 200
STATE_FILE_NAME = "bot_control_state.json"
LOCK_FILE_NAME = ".bot_control.lock"

_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


# --------------------------------------------------------------------------------------
# Progreso por pasos
# --------------------------------------------------------------------------------------

# Estados de un paso.
PASO_PENDIENTE = "pendiente"
PASO_EN_CURSO = "en_curso"
PASO_OK = "ok"
PASO_ERROR = "error"
PASO_OMITIDO = "omitido"

# Catálogo de pasos por acción, en orden: (clave, título). START y Graceful stop comparten pasos.
START_STEPS: List[tuple] = [
    ("validar", "Validar configuración"),
    ("backup", "Backup de la config"),
    ("escribir", "Escribir config"),
    ("docker", "Iniciar / reiniciar passivbot"),
    ("verificar", "Verificar contenedor en ejecución"),
    ("forager", "Esperando a forager"),
]
GRACEFUL_STEPS: List[tuple] = list(START_STEPS)
APAGAR_STEPS: List[tuple] = [
    # Antes del stop (decisión 2, spec bot-estrategia § 3.4): si algo vuelve a arrancar passivbot, no abre pares nuevos.
    ("parada_gradual", "Dejar forager en parada gradual"),
    ("detener", "Detener passivbot"),
    ("verificar_detenido", "Verificar que passivbot está detenido"),
    ("cancelar_ordenes", "Cancelar órdenes"),
    ("cancelar_condicionales", "Cancelar órdenes condicionales"),
    ("cerrar_posiciones", "Cerrar posiciones"),
    ("verificacion", "Verificación final"),
]

# Espera informativa tras START/Graceful: forager tarda en abrir los pares (no bloquea más que esto).
ENV_FORAGER_WARMUP = "FUTURESBOARD_FORAGER_WARMUP_SECONDS"
DEFAULT_FORAGER_WARMUP_SECONDS = 90
MAX_FORAGER_WARMUP_SECONDS = 900
# Cada cuánto se actualiza la cuenta regresiva y cada cuánto se consulta el contenedor.
WARMUP_TICK_SECONDS = 1.0
WARMUP_CHECK_SECONDS = 5.0


class Reporter:
    """Recibe el avance de una acción paso a paso. La implementación por defecto no hace nada.

    ``bot_control`` no conoce Flask ni los jobs: quien ejecuta la acción inyecta un reporter
    (ver ``futuresboard.jobs``). ``actual``/``total`` son opcionales (ej. 2/5 símbolos).
    """

    def step(self, clave: str, estado: str, detalle: Optional[str] = None,
             actual: Optional[int] = None, total: Optional[int] = None) -> None:
        return None


NULL_REPORTER = Reporter()


def forager_warmup_seconds() -> int:
    """Segundos de la espera informativa a forager (env, default 90, entre 0 y 900)."""
    raw = os.environ.get(ENV_FORAGER_WARMUP, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_FORAGER_WARMUP_SECONDS
    except ValueError:
        value = DEFAULT_FORAGER_WARMUP_SECONDS
    return max(0, min(MAX_FORAGER_WARMUP_SECONDS, value))


class BotControlError(Exception):
    """Error con mensaje apto para mostrar al usuario.

    ``payload`` (opcional) se agrega al JSON de error de la API (ej. el resumen de un apagado
    con cierre parcial).
    """

    status_code = 500

    def __init__(self, message: str = "", payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.payload: Dict[str, Any] = payload or {}


class DisabledError(BotControlError):
    status_code = 503


class ConfigError(BotControlError):
    status_code = 500


class InvalidModeError(BotControlError):
    """long_mode/short_mode con un valor que forager rechaza al arrancar."""

    status_code = 409


class DockerError(BotControlError):
    status_code = 502
    # True cuando no hubo respuesta HTTP (timeout / conexion): no se sabe si Docker ejecuto
    # la accion, asi que no es seguro revertir la config.
    uncertain = False


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.docker_url = os.environ.get(ENV_DOCKER_URL, "").strip().rstrip("/")
        self.container = os.environ.get(ENV_CONTAINER, "").strip() or DEFAULT_CONTAINER
        forager = os.environ.get(ENV_FORAGER_CONFIG, "").strip()
        self.forager_config: Optional[pathlib.Path] = pathlib.Path(forager) if forager else None
        self.modes_supported = _env_flag(ENV_MODES_SUPPORTED)

    @property
    def enabled(self) -> bool:
        return bool(self.docker_url)

    def require_enabled(self) -> None:
        if not self.enabled:
            raise DisabledError(
                f"Panel deshabilitado: falta configurar {ENV_DOCKER_URL} (docker-socket-proxy)."
            )
        if not _CONTAINER_NAME_RE.match(self.container):
            raise DisabledError(f"Nombre de contenedor invalido en {ENV_CONTAINER}.")

    def require_config(self) -> pathlib.Path:
        if self.forager_config is None:
            raise DisabledError(f"Falta configurar {ENV_FORAGER_CONFIG} (ruta del HJSON de forager).")
        if not self.forager_config.is_file():
            raise ConfigError(f"No se encuentra la config de forager en {self.forager_config}.")
        return self.forager_config


# --------------------------------------------------------------------------------------
# Docker Engine API (via docker-socket-proxy)
# --------------------------------------------------------------------------------------


class DockerClient:
    """Cliente minimo de la Docker Engine API. Solo usa endpoints /containers/*."""

    def __init__(self, base_url: str, session: Optional[requests.Session] = None,
                 timeout: float = HTTP_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = session or requests.Session()
        # Timeout de cada pedido (salvo start/stop/restart, que suman el "t" del stop). /api/vivo/inicio usa uno
        # corto para responder los números antes de que el navegador corte (blueprint.bot_status_payload).
        self.timeout = timeout

    def _url(self, name: str, suffix: str = "") -> str:
        if not _CONTAINER_NAME_RE.match(name):
            raise DockerError("Nombre de contenedor invalido.")
        return f"{self.base_url}/containers/{quote(name, safe='')}{suffix}"

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        try:
            return self.http.request(method, url, **kwargs)
        except requests.RequestException as exc:
            err = DockerError(f"No se pudo contactar al docker-proxy: {exc.__class__.__name__}.")
            err.uncertain = True
            raise err from exc

    def inspect(self, name: str) -> Dict[str, Any]:
        resp = self._request("GET", self._url(name, "/json"))
        if resp.status_code == 404:
            return {"name": name, "status": "not_found", "running": False, "started_at": None, "finished_at": None,
                    "restart_count": None, "image": None}
        if resp.status_code != 200:
            raise DockerError(f"Docker respondio {resp.status_code} al consultar el contenedor.")
        data = resp.json() or {}
        state = data.get("State") or {}
        restart_count = data.get("RestartCount")  # nivel superior del inspect, no adentro de State
        return {
            "name": name,
            "status": state.get("Status") or "unknown",
            "running": bool(state.get("Running")),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "restart_count": restart_count if isinstance(restart_count, int) and not isinstance(restart_count, bool)
            else None,
            "image": (data.get("Config") or {}).get("Image"),
        }

    def _action(self, name: str, action: str, params: Optional[Dict[str, Any]] = None) -> str:
        timeout = HTTP_TIMEOUT_SECONDS + (params or {}).get("t", 0)
        resp = self._request("POST", self._url(name, f"/{action}"), params=params, timeout=timeout)
        if resp.status_code == 204:
            return "ok"
        if resp.status_code == 304:
            # start sobre uno corriendo / stop sobre uno detenido
            return "sin_cambios"
        if resp.status_code == 404:
            raise DockerError(f"El contenedor {name} no existe.")
        raise DockerError(f"Docker respondio {resp.status_code} a {action}.")

    def start(self, name: str) -> str:
        return self._action(name, "start")

    def restart(self, name: str) -> str:
        return self._action(name, "restart", {"t": STOP_TIMEOUT_SECONDS})

    def stop(self, name: str) -> str:
        return self._action(name, "stop", {"t": STOP_TIMEOUT_SECONDS})


# --------------------------------------------------------------------------------------
# Binance USDⓈ-M Futures (cierre de posiciones y órdenes al Apagar)
# --------------------------------------------------------------------------------------

BINANCE_RECV_WINDOW = 5000
# Rondas de cancelar + cerrar; después de la última se hace una lectura final de verificación.
CLOSE_ROUNDS = 3
# Pausa entre una ronda con acciones y la lectura siguiente (los tests la ponen en 0).
ROUND_PAUSE_SECONDS = 1.0
_MAX_ERROR_MSG = 200


class BinanceError(BotControlError):
    status_code = 502

    def __init__(self, message: str, http_status: Optional[int] = None, code: Any = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code


class PartialCloseError(BotControlError):
    """passivbot quedó detenido pero quedaron posiciones u órdenes abiertas (o sin verificar)."""

    status_code = 502


class BinanceFuturesClient:
    """Cliente mínimo de la API REST de USDⓈ-M Futures.

    Firma igual que el scraper (HMAC-SHA256 del query string con API_SECRET, header
    X-MBX-APIKEY), pero con timeout, sesión reutilizada y errores sin URL/firma/credenciales.
    """

    def __init__(self, base_url: str, api_key: str = "", api_secret: str = "",
                 session: Optional[requests.Session] = None, algo_optional: bool = False,
                 credenciales: Optional[Any] = None) -> None:
        # Key y secret se piden en CADA request (spec bot-estrategia § 10.3): con un proveedor
        # (futuresboard.credenciales), un cambio de key llega a este cliente sin recrearlo.
        self._cred = credenciales if credenciales is not None else credenciales_mod.Fijas(api_key, api_secret)
        if not base_url or not self._cred.disponible():
            raise DisabledError(
                "Apagar requiere API_KEY, API_SECRET y API_BASE_URL de Binance Futures configurados."
            )
        self.base_url = base_url.rstrip("/")
        self.http = session or requests.Session()
        # Solo en demo/testnet: si el endpoint de órdenes algo no existe (404) se puede omitir.
        # En producción el endpoint existe, así que un 404 es un error de configuración.
        self.algo_optional = bool(algo_optional)

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 signed: bool = True) -> Any:
        query_params = dict(params or {})
        headers: Dict[str, str] = {}
        cred = None
        if signed:
            # Una sola lectura por pedido: la firma y el header salen de la misma key aunque el archivo cambie.
            try:
                cred = self._cred.actuales()
            except credenciales_mod.SinCredenciales as exc:
                raise DisabledError(f"Sin credenciales de Binance: {exc}") from exc
            query_params["recvWindow"] = BINANCE_RECV_WINDOW
            query_params["timestamp"] = int(time.time() * 1000)
        query = urlencode(query_params)
        if cred is not None:
            signature = hmac.new(cred.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
            query = f"{query}&signature={signature}"
            headers["X-MBX-APIKEY"] = cred.api_key
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        try:
            resp = self.http.request(method, url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise BinanceError(f"Sin respuesta de Binance en {method} {path} ({exc.__class__.__name__}).") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        code = data.get("code") if isinstance(data, dict) else None
        if resp.status_code != 200 or (code is not None and code not in (0, 200)):
            msg = str(data.get("msg", "")) if isinstance(data, dict) else ""
            raise BinanceError(
                f"Binance respondió {resp.status_code} en {method} {path}: {code} {msg[:_MAX_ERROR_MSG]}".rstrip(),
                http_status=resp.status_code,
                code=code,
            )
        if data is None:
            # 200 sin JSON (proxy, WAF, página de mantenimiento): no es una respuesta de Binance.
            raise BinanceError(f"Binance respondió {resp.status_code} sin JSON válido en {method} {path}.",
                               http_status=resp.status_code)
        if cred is not None:
            self._cred.firma_ok(cred)
        return data

    def _list(self, path: str) -> List[Dict[str, Any]]:
        return _require_list(self._request("GET", path), path)

    def open_orders(self) -> List[Dict[str, Any]]:
        return self._list("/fapi/v1/openOrders")

    def open_algo_orders(self) -> List[Dict[str, Any]]:
        path = "/fapi/v1/openAlgoOrders"
        data = self._request("GET", path)
        if isinstance(data, dict):  # defensivo: algunas variantes envuelven la lista
            for key in ("orders", "rows"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        return _require_list(data, path)

    def cancel_all_open_orders(self, symbol: str) -> None:
        self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def cancel_all_algo_orders(self, symbol: str) -> None:
        self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    def positions(self) -> List[Dict[str, Any]]:
        rows = self._list("/fapi/v2/positionRisk")
        return [r for r in rows if _dec(r.get("positionAmt")) != 0]

    def exchange_info(self) -> Dict[str, Any]:
        data = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        if not isinstance(data, dict):
            raise BinanceError("Respuesta inesperada de Binance en GET /fapi/v1/exchangeInfo (no es un objeto).")
        return data

    def klines(self, symbol: str, interval: str, start_ms: int, limit: int) -> List[List[Any]]:
        """Velas públicas (``GET /fapi/v1/klines``, sin firma). Cada fila: [openTime, open, high, low, close,
        volume, closeTime, ...]. La última puede estar abierta todavía (closeTime en el futuro)."""
        path = "/fapi/v1/klines"
        data = self._request("GET", path, {"symbol": symbol, "interval": interval, "startTime": int(start_ms),
                                           "limit": int(limit)}, signed=False)
        if not isinstance(data, list) or not all(isinstance(r, list) and len(r) >= 7 for r in data):
            raise BinanceError(f"Respuesta inesperada de Binance en GET {path} (se esperaba una lista de velas).")
        return data

    def market_order(self, symbol: str, side: str, quantity: str, position_side: Optional[str],
                     reduce_only: bool) -> Dict[str, Any]:
        params: Dict[str, Any] = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity,
                                  "newOrderRespType": "RESULT"}
        if position_side:
            params["positionSide"] = position_side
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._request("POST", "/fapi/v1/order", params) or {}


def _dec(value: Any) -> decimal.Decimal:
    try:
        return decimal.Decimal(str(value))
    except (decimal.InvalidOperation, ValueError, TypeError):
        return decimal.Decimal(0)


def _fmt(value: decimal.Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def lot_filters(exchange_info: Dict[str, Any], symbol: str) -> Optional[Dict[str, decimal.Decimal]]:
    """stepSize / minQty / maxQty para órdenes MARKET (MARKET_LOT_SIZE, o LOT_SIZE si falta o es 0)."""
    for sym in exchange_info.get("symbols") or []:
        if sym.get("symbol") != symbol:
            continue
        filters = {f.get("filterType"): f for f in sym.get("filters") or []}
        market, lot = filters.get("MARKET_LOT_SIZE") or {}, filters.get("LOT_SIZE") or {}
        out = {}
        for key, name in (("step", "stepSize"), ("min", "minQty"), ("max", "maxQty")):
            value = _dec(market.get(name))
            out[key] = value if value > 0 else _dec(lot.get(name))
        if out["step"] <= 0:
            return None
        return out
    return None


def split_quantity(amount: decimal.Decimal, step: decimal.Decimal, min_qty: decimal.Decimal,
                   max_qty: decimal.Decimal) -> tuple:
    """Redondea ``amount`` hacia abajo al ``step`` y lo parte en órdenes de a lo sumo ``max_qty``.

    Devuelve (lista de cantidades, residuo no cerrable). Un tramo menor que ``min_qty`` no se
    envía (Binance lo rechaza) y queda en el residuo.
    """
    total = (amount // step) * step
    cap = (max_qty // step) * step if max_qty > 0 else total
    chunks: List[decimal.Decimal] = []
    remaining = total
    while remaining > 0 and cap > 0:
        chunk = min(remaining, cap)
        if chunk < min_qty:
            break
        chunks.append(chunk)
        remaining -= chunk
    return chunks, amount - sum(chunks, decimal.Decimal(0))


def _require_list(data: Any, path: str) -> List[Dict[str, Any]]:
    """Exige una lista de objetos: cualquier otra forma (dict, texto) no se toma como "vacío"."""
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise BinanceError(f"Respuesta inesperada de Binance en GET {path} (se esperaba una lista de objetos).")
    return data


def _exc_text(exc: Exception) -> str:
    if isinstance(exc, BinanceError):
        return str(exc)
    return f"error inesperado ({exc.__class__.__name__})"


def _pos_view(p: Dict[str, Any]) -> Dict[str, Any]:
    amt = _dec(p.get("positionAmt"))
    pside = str(p.get("positionSide") or "BOTH").upper()
    lado = pside if pside in ("LONG", "SHORT") else ("LONG" if amt > 0 else "SHORT")
    return {"symbol": p.get("symbol"), "lado": lado, "modo": "hedge" if pside in ("LONG", "SHORT") else "one-way",
            "cantidad": _fmt(abs(amt))}


def _order_counts(orders: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for o in orders:
        sym = o.get("symbol")
        if sym:
            counts[sym] = counts.get(sym, 0) + 1
    return counts


def close_all_futures(client: BinanceFuturesClient, reporter: Optional[Reporter] = None) -> Dict[str, Any]:
    """Cancela TODAS las órdenes abiertas (normales y algo/condicionales) y cierra TODAS las
    posiciones a mercado. Verifica con hasta ``CLOSE_ROUNDS`` rondas.

    Nunca lanza por errores de Binance: los junta en ``errores``. ``completo`` es True solo si la
    lectura final confirma 0 posiciones y 0 órdenes.

    Progreso (``reporter``): la primera ronda reporta cancelar órdenes (x/y símbolos), cancelar
    condicionales y cerrar posiciones (x/y); las lecturas siguientes son la verificación (ronda n/3).
    """
    rep = reporter or NULL_REPORTER
    resumen: Dict[str, Any] = {
        "completo": False,
        "rondas": 0,
        "ordenes_canceladas": [],
        "posiciones_cerradas": [],
        "errores": [],
        "restante": {"posiciones": [], "ordenes": [], "ordenes_algo": [], "verificado": False},
        # False solo en demo/testnet cuando el endpoint de órdenes algo no existe y se omitió.
        "algo_verificado": True,
    }
    errores: List[str] = resumen["errores"]
    state = {"algo": True, "info": None}

    def snapshot() -> tuple:
        ok = True
        try:
            orders = client.open_orders()
        except Exception as exc:
            errores.append(f"Lectura de órdenes: {_exc_text(exc)}")
            orders, ok = [], False
        algo: List[Dict[str, Any]] = []
        if state["algo"]:
            try:
                algo = client.open_algo_orders()
            except Exception as exc:
                if (isinstance(exc, BinanceError) and exc.http_status == 404
                        and getattr(client, "algo_optional", False)):
                    # Solo demo/testnet: el endpoint puede no existir ahí. Se omite, pero el
                    # resumen lo marca (algo_verificado=False) para no anunciar un cierre total.
                    state["algo"] = False
                    resumen["algo_verificado"] = False
                    errores.append("Órdenes condicionales: endpoint no disponible en demo/testnet "
                                   "(se omiten, sin verificar).")
                else:
                    # En producción el endpoint existe: un 404 (o cualquier fallo) queda sin verificar.
                    errores.append(f"No se pudo verificar órdenes condicionales: {_exc_text(exc)}")
                    ok = False
        try:
            positions = client.positions()
        except Exception as exc:
            errores.append(f"Lectura de posiciones: {_exc_text(exc)}")
            positions, ok = [], False
        return ok, orders, algo, positions

    # Pasos de la primera ronda que terminaron con errores (se re-marcan ok si la verificación cierra todo).
    con_errores: Dict[str, str] = {}

    def cancel(orders: List[Dict[str, Any]], tipo: str, clave: Optional[str] = None) -> None:
        counts = sorted(_order_counts(orders).items())
        total, hechos, antes = len(counts), 0, len(errores)
        if clave:
            rep.step(clave, PASO_EN_CURSO, f"0/{total} símbolos", 0, total)
        for symbol, count in counts:
            try:
                if tipo == "algo":
                    client.cancel_all_algo_orders(symbol)
                else:
                    client.cancel_all_open_orders(symbol)
            except Exception as exc:
                errores.append(f"Cancelar órdenes {tipo} de {symbol}: {_exc_text(exc)}")
                continue
            finally:
                hechos += 1
                if clave:
                    rep.step(clave, PASO_EN_CURSO, f"{hechos}/{total} símbolos ({symbol})", hechos, total)
            resumen["ordenes_canceladas"].append({"symbol": symbol, "tipo": tipo, "cantidad": count})
        if clave:
            if not total:
                rep.step(clave, PASO_OK, "No había órdenes abiertas.")
            elif len(errores) > antes:
                detalle = f"{total} símbolos, {len(errores) - antes} con error (se reintenta en la verificación)."
                con_errores[clave] = detalle
                rep.step(clave, PASO_ERROR, detalle, total, total)
            else:
                ordenes = sum(c for _, c in counts)
                rep.step(clave, PASO_OK, f"{ordenes} órdenes en {total} símbolos.", total, total)

    def close(positions: List[Dict[str, Any]], reportar: bool = False) -> None:
        total, antes = len(positions), len(errores)
        cerradas_antes = len(resumen["posiciones_cerradas"])
        if reportar:
            rep.step("cerrar_posiciones", PASO_EN_CURSO, f"0/{total} posiciones", 0, total)
        try:
            _close(positions, reportar, total)
        finally:
            if reportar:
                cerradas = len(resumen["posiciones_cerradas"]) - cerradas_antes
                if len(errores) > antes:
                    detalle = (f"{cerradas}/{total} posiciones, {len(errores) - antes} errores "
                               "(se reintenta en la verificación).")
                    con_errores["cerrar_posiciones"] = detalle
                    rep.step("cerrar_posiciones", PASO_ERROR, detalle, cerradas, total)
                else:
                    rep.step("cerrar_posiciones", PASO_OK, f"{cerradas}/{total} posiciones cerradas.", total, total)

    def _close(positions: List[Dict[str, Any]], reportar: bool, total: int) -> None:
        if not positions:
            return
        if state["info"] is None:
            try:
                state["info"] = client.exchange_info()
            except Exception as exc:
                errores.append(f"Lectura de exchangeInfo: {_exc_text(exc)}")
                return
        for idx, p in enumerate(positions, start=1):
            view = _pos_view(p)
            symbol, lado = view["symbol"], view["lado"]
            if reportar:
                rep.step("cerrar_posiciones", PASO_EN_CURSO, f"{idx}/{total} posiciones ({symbol} {lado})",
                         idx - 1, total)
            amt = _dec(p.get("positionAmt"))
            filters = lot_filters(state["info"], symbol)
            if not filters:
                errores.append(f"{symbol} {lado}: sin filtros de cantidad en exchangeInfo.")
                continue
            chunks, residual = split_quantity(abs(amt), filters["step"], filters["min"], filters["max"])
            if not chunks:
                errores.append(f"{symbol} {lado}: cantidad {_fmt(abs(amt))} menor al mínimo operable.")
                continue
            hedge = view["modo"] == "hedge"
            side = "SELL" if amt > 0 else "BUY"
            closed = decimal.Decimal(0)
            sent = 0
            for chunk in chunks:
                try:
                    client.market_order(symbol, side, _fmt(chunk), lado if hedge else None, reduce_only=not hedge)
                except Exception as exc:
                    errores.append(f"Cerrar {symbol} {lado} ({_fmt(chunk)}): {_exc_text(exc)}")
                    break
                closed += chunk
                sent += 1
            if sent:
                resumen["posiciones_cerradas"].append({"symbol": symbol, "lado": lado, "modo": view["modo"],
                                                       "cantidad": _fmt(closed), "ordenes": sent})
            if residual > 0 and sent == len(chunks):
                errores.append(f"{symbol} {lado}: residuo {_fmt(residual)} por debajo del step/mínimo.")

    def algo_omitido() -> None:
        rep.step("cancelar_condicionales", PASO_OMITIDO,
                 "Endpoint de órdenes condicionales no disponible en demo/testnet: sin verificar.")

    def rounds() -> None:
        for ronda in range(1, CLOSE_ROUNDS + 2):
            if ronda > 1:
                vuelta = ronda - 1
                rep.step("verificacion", PASO_EN_CURSO, f"Ronda {vuelta}/{CLOSE_ROUNDS}: leyendo Binance",
                         vuelta - 1, CLOSE_ROUNDS)
            ok, orders, algo, positions = snapshot()
            if ok and not orders and not algo and not positions:
                resumen["completo"] = True
                resumen["restante"]["verificado"] = True
                if ronda == 1:
                    rep.step("cancelar_ordenes", PASO_OK, "No había órdenes abiertas.")
                    if state["algo"]:
                        rep.step("cancelar_condicionales", PASO_OK, "No había órdenes condicionales.")
                    else:
                        algo_omitido()
                    rep.step("cerrar_posiciones", PASO_OK, "No había posiciones abiertas.")
                for clave, detalle in con_errores.items():
                    rep.step(clave, PASO_OK, f"{detalle} Resuelto al reintentar.")
                rep.step("verificacion", PASO_OK, "Sin posiciones ni órdenes abiertas en Binance.",
                         CLOSE_ROUNDS, CLOSE_ROUNDS)
                return
            if ronda > CLOSE_ROUNDS:
                resumen["restante"] = {
                    "posiciones": [_pos_view(p) for p in positions],
                    "ordenes": [{"symbol": s, "cantidad": c} for s, c in sorted(_order_counts(orders).items())],
                    "ordenes_algo": [{"symbol": s, "cantidad": c} for s, c in sorted(_order_counts(algo).items())],
                    "verificado": ok,
                }
                if ok:
                    detalle = (f"Quedaron abiertas {len(positions)} posiciones, {len(orders)} órdenes y "
                               f"{len(algo)} órdenes condicionales tras {CLOSE_ROUNDS} rondas.")
                else:
                    detalle = "No se pudo leer el estado final en Binance: revisar a mano."
                rep.step("verificacion", PASO_ERROR, detalle, CLOSE_ROUNDS, CLOSE_ROUNDS)
                return
            resumen["rondas"] = ronda
            if ronda > 1:
                rep.step("verificacion", PASO_EN_CURSO,
                         f"Ronda {ronda - 1}/{CLOSE_ROUNDS}: reintentando ({len(orders) + len(algo)} órdenes, "
                         f"{len(positions)} posiciones)", ronda - 1, CLOSE_ROUNDS)
            primera = ronda == 1
            # Primero las órdenes (para que ninguna reabra posición), después las posiciones.
            cancel(orders, "normal", "cancelar_ordenes" if primera else None)
            if primera and not state["algo"]:
                algo_omitido()
            else:
                cancel(algo, "algo", "cancelar_condicionales" if primera else None)
            if positions or primera:
                try:
                    close(positions, reportar=primera)
                except Exception as exc:  # defensivo: un dato inesperado no debe perder el resumen
                    errores.append(f"Cierre de posiciones: {_exc_text(exc)}")
            if ROUND_PAUSE_SECONDS:
                time.sleep(ROUND_PAUSE_SECONDS)

    try:
        rounds()
    except Exception as exc:  # defensivo: nunca perder el resumen parcial (respuesta y auditoría)
        errores.append(f"Cierre interrumpido: {_exc_text(exc)}")
        resumen["completo"] = False
        resumen["restante"]["verificado"] = False
        rep.step("verificacion", PASO_ERROR, f"Cierre interrumpido: {_exc_text(exc)}")
    return resumen


# --------------------------------------------------------------------------------------
# Config HJSON de forager
# --------------------------------------------------------------------------------------


def read_forager_config(path: pathlib.Path) -> Dict[str, Any]:
    try:
        data = hjson.loads(path.read_text(encoding="utf-8"))
    except (OSError, hjson.HjsonDecodeError) as exc:
        raise ConfigError(f"No se pudo leer la config de forager: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("La config de forager no es un objeto.")
    return data


def _is_strict_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def _prune_backups(path: pathlib.Path) -> None:
    """Limpieza de backups viejos: best effort (la config ya quedó escrita; un error acá no es un error del cambio)."""
    try:
        backups = sorted(path.parent.glob(f"{path.name}.bak-*"))
    except OSError:
        return
    for old in backups[:-MAX_BACKUPS]:
        with contextlib.suppress(OSError):
            old.unlink()


def write_forager_config(path: pathlib.Path, updates: Dict[str, Any],
                         reporter: Optional[Reporter] = None) -> pathlib.Path:
    """Aplica ``updates`` al HJSON de forager de forma atomica.

    1. backup con timestamp (copia exacta, conserva comentarios),
    2. escribe a un tmp en el mismo directorio,
    3. relee el tmp y verifica que parsea y contiene los valores nuevos,
    4. ``os.replace`` sobre el original.

    Devuelve la ruta del backup. Nota: el round-trip de hjson no conserva comentarios;
    el backup si. Si el original era JSON estricto se escribe JSON.
    """
    rep = reporter or NULL_REPORTER
    rep.step("backup", PASO_EN_CURSO)
    try:
        original_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"No se pudo leer la config de forager: {exc}") from exc
    try:
        data = hjson.loads(original_text)
    except hjson.HjsonDecodeError as exc:
        raise ConfigError(f"La config de forager no es HJSON valido: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("La config de forager no es un objeto.")

    data.update(updates)
    if _is_strict_json(original_text):
        new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    else:
        new_text = hjson.dumps(data, indent=2) + "\n"

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    tmp_name = None
    try:
        shutil.copy2(path, backup)
        rep.step("backup", PASO_OK, backup.name)
        rep.step("escribir", PASO_EN_CURSO)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(path, tmp_name)

        reread = hjson.loads(pathlib.Path(tmp_name).read_text(encoding="utf-8"))
        for key, value in updates.items():
            if reread.get(key) != value:
                raise ConfigError(f"Validacion fallida tras escribir la config ({key}).")

        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        raise ConfigError(f"No se pudo escribir la config de forager: {exc}") from exc
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)
    _prune_backups(path)
    rep.step("escribir", PASO_OK, ", ".join(f"{k}={v}" for k, v in updates.items()))
    return backup


def restore_forager_config(path: pathlib.Path, backup: pathlib.Path) -> None:
    """Vuelve la config al contenido del backup (atomico; el backup se conserva).

    ``copy2`` preserva el mtime del backup (= el del original previo), asi el marcador
    "pendiente de reinicio" (mtime de la config vs StartedAt del contenedor) vuelve a su valor.
    """
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".restore", dir=str(path.parent))
        os.close(fd)
        shutil.copy2(backup, tmp_name)
        hjson.loads(pathlib.Path(tmp_name).read_text(encoding="utf-8"))
        os.replace(tmp_name, path)
        tmp_name = None
    except (OSError, hjson.HjsonDecodeError) as exc:
        raise ConfigError(f"No se pudo restaurar la config desde {backup.name}: {exc}") from exc
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


_DOCKER_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$")


def parse_docker_time(value: Any) -> Optional[dt.datetime]:
    """Parsea ``State.StartedAt`` (RFC3339 con nanosegundos). None si falta o es el valor cero."""
    if not isinstance(value, str):
        return None
    m = _DOCKER_TS_RE.match(value.strip())
    if not m:
        return None
    base, frac, tz = m.groups()
    if base.startswith("0001-"):
        return None
    tz = "+00:00" if tz == "Z" else tz
    frac = (frac or "0")[:6].ljust(6, "0")
    try:
        return dt.datetime.fromisoformat(f"{base}.{frac}{tz}")
    except ValueError:
        return None


def config_pending(path: pathlib.Path, container: Dict[str, Any]) -> Optional[bool]:
    """True si la config se modifico despues de que arranco el contenedor (forager no la cargo).

    None si no se puede determinar (contenedor detenido / inexistente / sin StartedAt): en ese
    caso la config se aplica en el proximo arranque.
    """
    if not container.get("running"):
        return None
    started = parse_docker_time(container.get("started_at"))
    if started is None:
        return None
    try:
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return None
    return mtime > started


def _num_eq(a: Any, b: Any) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def detect_preset(cfg: Dict[str, Any]) -> str:
    tl, ts = cfg.get("twe_long"), cfg.get("twe_short")
    if tl is None or ts is None:
        return "desconocido"
    for name, (pl, ps) in RISK_PRESETS.items():
        if _num_eq(tl, pl) and _num_eq(ts, ps):
            return name
    return "personalizado"


def normalize_mode(value: Any, key: str = "mode") -> str:
    """Normaliza long_mode/short_mode igual que forager y devuelve el nombre canonico.

    None o "" -> "normal" (forager lo trata como ausente). Un valor desconocido o que no es
    texto lanza InvalidModeError (409): con ese valor forager no arranca.
    """
    if value is None:
        return MODE_NORMAL
    if not isinstance(value, str):
        raise InvalidModeError(
            f"La config de forager tiene {key}={value!r} (tipo {type(value).__name__}), que forager "
            f"rechaza al arrancar. Valores aceptados: {ACCEPTED_MODES}. No se modifico la config."
        )
    normalized = value.strip().lower()
    if normalized == "":
        return MODE_NORMAL
    if normalized not in MODE_ALIASES:
        raise InvalidModeError(
            f"La config de forager tiene {key}={value!r}, que forager rechaza al arrancar. "
            f"Valores aceptados: {ACCEPTED_MODES}. No se modifico la config."
        )
    return MODE_ALIASES[normalized]


def _mode(cfg: Dict[str, Any], side: str) -> str:
    return normalize_mode(cfg.get(f"{side}_mode"), f"{side}_mode")


def _saved_short_mode(value: Any) -> Optional[str]:
    """short_mode_before_stop del estado local, normalizado. None si falta o es invalido."""
    if not isinstance(value, str):
        return None
    try:
        return normalize_mode(value, "short_mode_before_stop")
    except InvalidModeError:
        return None


# --------------------------------------------------------------------------------------
# Estado local, auditoria y lock (en el directorio de datos)
# --------------------------------------------------------------------------------------


class LockHandle:
    """Lock tomado con ``Store.try_lock``. ``release`` es idempotente."""

    def __init__(self, fh: Any) -> None:
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


class Store:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self.data_dir = data_dir
        self.audit_path = data_dir / AUDIT_LOG_NAME
        self.state_path = data_dir / STATE_FILE_NAME
        self.lock_path = data_dir / LOCK_FILE_NAME

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Serializa acciones entre hilos/procesos del dashboard."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def try_lock(self) -> Optional["LockHandle"]:
        """Toma el mismo lock que ``lock()`` sin esperar. None si otra acción lo tiene.

        El handle se puede liberar desde otro hilo (el job que ejecuta la acción).
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        return LockHandle(fh)

    def load_state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, state: Dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def audit(self, user: str, action: str, params: Dict[str, Any], result: str, detail: str = "",
              remote_addr: Optional[str] = None) -> None:
        entry = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "user": user,
            "action": action,
            "params": params,
            "result": result,
            "detail": detail,
            "remote_addr": remote_addr,
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------------------
# Operaciones de alto nivel
# --------------------------------------------------------------------------------------


def get_status(settings: Settings, docker: Optional[DockerClient] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "enabled": settings.enabled,
        "modes_supported": settings.modes_supported,
        "container": None,
        "riesgo": "desconocido",
        "twe_long": None,
        "twe_short": None,
        "n_longs": None,
        "n_shorts": None,
        "long_mode": None,
        "short_mode": None,
        "config_available": False,
        # True: la config cambio despues del ultimo arranque de passivbot (no esta en efecto).
        "config_pending": None,
        "started_at": None,
        "restart_count": None,
        "message": "",
        "presets": {k: {"twe_long": v[0], "twe_short": v[1]} for k, v in RISK_PRESETS.items()},
    }
    if not settings.enabled:
        out["message"] = f"Panel deshabilitado: falta configurar {ENV_DOCKER_URL}."
        return out
    settings.require_enabled()

    messages = []
    try:
        cfg = read_forager_config(settings.require_config())
        out.update(
            config_available=True,
            riesgo=detect_preset(cfg),
            twe_long=cfg.get("twe_long"),
            twe_short=cfg.get("twe_short"),
        )
        out["n_longs"], out["n_shorts"] = n_pares(cfg)
        out["estrategia"] = resumen_estrategia(cfg)
        # Un modo invalido no rompe el status: se muestra el valor crudo y el motivo.
        for side in ("long", "short"):
            key = f"{side}_mode"
            try:
                out[key] = _mode(cfg, side)
            except InvalidModeError as exc:
                out[key] = str(cfg.get(key))
                messages.append(str(exc).replace(" No se modifico la config.", ""))
    except BotControlError as exc:
        messages.append(str(exc))

    docker = docker or DockerClient(settings.docker_url)
    out["container"] = docker.inspect(settings.container)
    # Desde cuándo corre y cuántas veces lo reinició Docker (un bucle de reinicios se ve acá, spec § 8 H17).
    out["started_at"] = out["container"].get("started_at")
    out["restart_count"] = out["container"].get("restart_count")
    if out["config_available"] and settings.forager_config is not None:
        out["config_pending"] = config_pending(settings.forager_config, out["container"])
    out["message"] = " ".join(messages)
    return out


# Estado del bot en una palabra (spec bot-estrategia § 2.1-2.2): píldora, punto del menú y control rápido de Inicio.
ESTADO_OPERANDO = "operando"
ESTADO_GRADUAL = "gradual"
ESTADO_DETENIDO = "detenido"
# Detenido con posiciones u órdenes abiertas: nadie las gestiona (Apagar que cerró a medias, docker stop a mano).
ESTADO_EXPUESTO = "expuesto"
ESTADO_SIN_DATOS = "sin_datos"


def estado_del_bot(status: Dict[str, Any], exposicion: Optional[Dict[str, Any]]) -> str:
    """Uno de los ``ESTADO_*`` a partir de ``get_status`` y de la exposición que ve el scraper.

    Un error de Docker o el panel deshabilitado es ``sin_datos``, nunca "detenido": no se sabe si corre. En parada
    gradual cuenta el lado long (con ``n_shorts`` 0 es el único que opera) y solo si forager lee los modos.
    """
    if not status.get("enabled") or status.get("error"):
        return ESTADO_SIN_DATOS
    container = status.get("container") or {}
    if container.get("status") in (None, "unknown"):
        return ESTADO_SIN_DATOS
    if container.get("running"):
        if status.get("modes_supported") and status.get("long_mode") == MODE_GRACEFUL_STOP:
            return ESTADO_GRADUAL
        return ESTADO_OPERANDO
    expo = exposicion or {}
    if (expo.get("posiciones") or 0) > 0 or (expo.get("ordenes") or 0) > 0:
        return ESTADO_EXPUESTO
    return ESTADO_DETENIDO


def _lista_pares(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(s) for s in value if isinstance(s, str)]


def resumen_estrategia(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Las claves de estrategia de la config de forager, como las muestran Estrategia, Iniciar y el registro de arranques.

    ``lw`` = twe_long / n_longs (lo que forager le da a cada bot, ``forager.py:126``); None si no se puede calcular.
    """
    n_longs, n_shorts = n_pares(cfg)
    twe = cfg.get("twe_long")
    lw = None
    if isinstance(twe, (int, float)) and not isinstance(twe, bool) and n_longs:
        lw = float(twe) / n_longs
    return {
        "twe_long": twe,
        "twe_short": cfg.get("twe_short"),
        "n_longs": n_longs,
        "n_shorts": n_shorts,
        "pares_long": _lista_pares(cfg.get("approved_symbols_long")),
        "pares_short": _lista_pares(cfg.get("approved_symbols_short")),
        "lw": lw,
    }


# Pares del backtest multianual de BotBulls (los de los perfiles). Un par fuera de esta lista no tiene backtest propio.
# El chequeo por hash de la config clock necesita el manifiesto de configs (spec § 5.1): acá solo se mira el símbolo.
PARES_BACKTEST = ("BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT")


def _canon_num(value: Any) -> Any:
    """4, 4.0 y Decimal("4.00") -> "4": que el formato del HJSON no cambie el hash."""
    if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal)):
        return value
    d = decimal.Decimal(str(value))
    return format(d.normalize(), "f") if d.is_finite() else str(value)


def estrategia_hash(cfg: Dict[str, Any]) -> str:
    """sha256 de la estrategia: ``twe_long``, ``twe_short``, ``n_longs``, ``approved_symbols_long`` y ``_short``.

    **No** incluye ``long_mode`` / ``short_mode``: Apagar y Parada gradual los cambian sin cambiar la estrategia (si
    entraran, C3 no reiniciaría nunca después de su propio Apagar). Las listas van ordenadas (forager no usa el orden)
    y los números normalizados."""
    datos: Dict[str, Any] = {k: _canon_num(cfg.get(k)) for k in ("twe_long", "twe_short", "n_longs")}
    for key in ("approved_symbols_long", "approved_symbols_short"):
        value = cfg.get(key)
        datos[key] = sorted(str(x) for x in value) if isinstance(value, list) else value
    texto = json.dumps(datos, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _append_arranque(path: pathlib.Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    # Poda: se queda con los últimos MAX_ARRANQUES (reescritura atómica) cuando duplica el tope.
    lineas = path.read_text(encoding="utf-8").splitlines()
    if len(lineas) > 2 * MAX_ARRANQUES:
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text("\n".join(lineas[-MAX_ARRANQUES:]) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def registrar_arranque(store: Store, cfg: Dict[str, Any], usuario: Optional[str], origen: str,
                       docker_action: Optional[str]) -> Optional[Dict[str, Any]]:
    """Deja en ``bot_arranques.jsonl`` con qué estrategia arrancó passivbot. Devuelve el registro (None si no se
    pudo escribir: nunca frena un arranque que Docker ya hizo)."""
    entry = {
        "id": secrets.token_hex(8),
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "usuario": usuario or "?",
        "origen": origen,
        "docker_action": docker_action,
        "hash": estrategia_hash(cfg),
        "resumen": resumen_estrategia(cfg),
    }
    try:
        _append_arranque(store.data_dir / ARRANQUES_FILE_NAME, json.dumps(entry, ensure_ascii=False))
    except (OSError, ValueError):
        return None
    return entry


def ultimo_arranque(store: Store) -> Optional[Dict[str, Any]]:
    """El último arranque registrado (renglones ilegibles se saltean) o None."""
    try:
        lineas = (store.data_dir / ARRANQUES_FILE_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for linea in reversed(lineas):
        try:
            entry = json.loads(linea)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("hash"), str):
            return entry
    return None


_TITULOS_ESTRATEGIA = (("twe_long", "TWE long"), ("twe_short", "TWE short"), ("n_longs", "Pares a la vez"),
                       ("pares_long", "Pares"), ("pares_short", "Pares short"))


def cambios_estrategia(antes: Optional[Dict[str, Any]], ahora: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Diferencias entre dos ``resumen_estrategia`` (claves de ``_TITULOS_ESTRATEGIA``)."""
    if not isinstance(antes, dict):
        return []
    out = []
    for clave, titulo in _TITULOS_ESTRATEGIA:
        a, b = antes.get(clave), ahora.get(clave)
        if clave.startswith("pares"):
            igual = sorted(a or []) == sorted(b or [])
        else:
            igual = _canon_num(a) == _canon_num(b)
        if not igual:
            out.append({"clave": clave, "titulo": titulo, "antes": a, "ahora": b})
    return out


def _fmt_num(value: Any) -> str:
    texto = _canon_num(value)
    return str(texto).replace(".", ",") if texto is not None else "—"


def plan_reanudar(settings: Settings, store: Store, docker: Optional[DockerClient] = None) -> Dict[str, Any]:
    """Resumen de Iniciar / Reanudar / Reiniciar con la estrategia guardada (``mantener_twe``), sin escribir nada.

    ``como``: ``iniciar`` (detenido), ``reanudar`` (parada gradual) o ``reiniciar`` (operando). ``requiere_plan``:
    detenido o con la config pendiente de reinicio (algo que todavía no operó): el panel muestra el resumen y arranca
    solo con ``confirmar`` y este ``plan_hash``. ``requiere_casilla``: cambios desde el último arranque (o sin
    registro), TWE > 4 o pares sin backtest. ``plan_hash`` cambia si cambia la estrategia, el último arranque, el
    estado del contenedor o lo que pide la casilla.
    """
    settings.require_enabled()
    path = settings.require_config()
    docker = docker or DockerClient(settings.docker_url)
    info = _inspect_existing(docker, settings.container)
    cfg = read_forager_config(path)
    _mode(cfg, "short")  # un short_mode que forager rechaza: 409 antes de mostrar nada (START lo rechazaría igual)
    try:
        long_mode = _mode(cfg, "long")
    except InvalidModeError:
        long_mode = str(cfg.get("long_mode"))
    running = bool(info.get("running"))
    if running:
        como = "reanudar" if settings.modes_supported and long_mode == MODE_GRACEFUL_STOP else "reiniciar"
    else:
        como = "iniciar"
    pendiente = config_pending(path, info) is True
    vigente = resumen_estrategia(cfg)
    h = estrategia_hash(cfg)
    ultimo = ultimo_arranque(store)
    cambios = cambios_estrategia(ultimo.get("resumen"), vigente) if ultimo else []
    avisos: List[Dict[str, str]] = []
    casilla = False
    if ultimo is None:
        casilla = True
        avisos.append({"tipo": "aviso", "titulo": "Sin registro del último arranque.",
                       "texto": "No se puede mostrar qué cambió desde el último arranque: revisá la estrategia antes "
                                "de iniciar."})
    elif ultimo.get("hash") != h:
        casilla = True
        avisos.append({"tipo": "aviso", "titulo": "La estrategia cambió desde el último arranque.",
                       "texto": (f"La estrategia cambió desde el último arranque ({ultimo.get('ts')}, "
                                 f"{ultimo.get('usuario')}) y todavía no operó: revisá qué cambió.")})
    twe = cfg.get("twe_long")
    if isinstance(twe, (int, float)) and not isinstance(twe, bool) and decimal.Decimal(str(twe)) > TWE_ADVERTENCIA:
        casilla = True
        avisos.append({"tipo": "aviso", "titulo": f"TWE {_fmt_num(twe)} por encima de {_fmt_num(TWE_ADVERTENCIA)}.",
                       "texto": (f"TWE {_fmt_num(twe)} está por encima de {_fmt_num(TWE_ADVERTENCIA)}: en el backtest "
                                 "2021-2026 aparecen liquidaciones desde TWE 6.")})
    pares = vigente["pares_long"]
    sin_backtest = [p for p in pares if p not in PARES_BACKTEST]
    if not pares:
        casilla = True
        avisos.append({"tipo": "aviso", "titulo": "Sin lista de pares.",
                       "texto": "approved_symbols_long está vacía: forager puede operar cualquier par."})
    elif sin_backtest:
        casilla = True
        avisos.append({"tipo": "aviso", "titulo": "Pares sin backtest de BotBulls.",
                       "texto": (f"{', '.join(sin_backtest)} sin backtest de BotBulls: el backtest y el rescate se "
                                 f"midieron con {', '.join(PARES_BACKTEST)}.")})
    accion = "reinicia" if running else "arranca"
    avisos.append({"tipo": "info", "titulo": "Cómo arranca.",
                   "texto": (f"Pone long_mode y short_mode en normal (sin tocar TWE ni pares), {accion} "
                             f"{settings.container} y forager tarda ~90 s en elegir los pares.")})
    requiere_plan = (not running) or pendiente
    plan_hash = hashlib.sha256(json.dumps(
        {"hash": h, "ultimo": (ultimo or {}).get("id"), "como": como, "casilla": casilla, "pendiente": pendiente},
        sort_keys=True).encode("utf-8")).hexdigest()
    lineas = [f"{'Iniciar' if como == 'iniciar' else como.capitalize()} passivbot con la estrategia guardada: "
              f"TWE {_fmt_num(vigente['twe_long'])}, {vigente['n_longs']} a la vez, pares "
              f"{', '.join(pares) if pares else 'todos'}."]
    for c in cambios:
        lineas.append(f"Cambió {c['titulo']}: {c['antes']} -> {c['ahora']}.")
    lineas += [a["texto"] for a in avisos]
    return {
        "como": como,
        "running": running,
        "config_pending": pendiente,
        "requiere_plan": requiere_plan,
        "requiere_casilla": casilla,
        "vigente": vigente,
        "hash": h,
        "modos": {"long_mode": long_mode, "despues": MODE_NORMAL},
        "ultimo": ({k: ultimo.get(k) for k in ("id", "ts", "usuario", "origen", "resumen")} if ultimo else None),
        "cambios": cambios,
        "avisos": avisos,
        "texto": "\n\n".join(lineas),
        "plan_hash": plan_hash,
    }


def _inspect_existing(docker: DockerClient, name: str) -> Dict[str, Any]:
    """Inspect previo a tocar la config: falla rapido si el proxy no responde o no hay contenedor."""
    info = docker.inspect(name)
    if info["status"] == "not_found":
        raise DockerError(f"El contenedor {name} no existe. No se modifico la config.")
    return info


def _start_or_restart(docker: DockerClient, name: str, info: Dict[str, Any]) -> str:
    if info["running"]:
        docker.restart(name)
        return "restart"
    docker.start(name)
    return "start"


def _apply_and_restart(settings: Settings, store: Store, docker: DockerClient, info: Dict[str, Any],
                       path: pathlib.Path, updates: Dict[str, Any], prev_state: Dict[str, Any],
                       new_state: Dict[str, Any], reporter: Optional[Reporter] = None) -> tuple:
    """Escribe la config + estado y reinicia passivbot. Si Docker rechaza la accion, revierte.

    - Error con respuesta HTTP (4xx/5xx, contenedor inexistente): Docker no aplico la accion, se
      restaura el HJSON desde el backup y el estado local previo.
    - Error sin respuesta (timeout / conexion): no se sabe si el reinicio ocurrio; NO se revierte
      (revertir podria dejar el archivo distinto de lo que forager ya cargo). El mensaje lo dice y
      /api/bot/status muestra "pendiente de reinicio" comparando mtime vs StartedAt.
    """
    rep = reporter or NULL_REPORTER
    rep.step("validar", PASO_OK, f"Contenedor {settings.container}: {info.get('status')}")
    backup = write_forager_config(path, updates, rep)
    try:
        store.save_state(new_state)
    except OSError as exc:
        # Antes de Docker: sin vuelta atrás quedaría la config de forager escrita sin reiniciar (y sin el estado local).
        try:
            restore_forager_config(path, backup)
        except (BotControlError, OSError) as restore_exc:
            raise ConfigError(
                f"No se pudo guardar el estado del panel ({exc.__class__.__name__}). ATENCION: la config de forager "
                f"quedo modificada y no se pudo restaurar ({restore_exc}); backup: {backup.name}."
            ) from exc
        raise ConfigError(f"No se pudo guardar el estado del panel ({exc.__class__.__name__}): se restauro la config "
                          "de forager; no se aplico ningun cambio.") from exc
    try:
        rep.step("docker", PASO_EN_CURSO,
                 f"{'Reiniciando' if info.get('running') else 'Iniciando'} {settings.container}")
        action = _start_or_restart(docker, settings.container, info)
        rep.step("docker", PASO_OK, f"docker {action}")
        return action, backup
    except DockerError as exc:
        if exc.uncertain:
            err = DockerError(
                f"{exc} Resultado incierto: la config de forager quedo escrita (backup {backup.name}); "
                "revisar el estado del bot antes de reintentar."
            )
            err.uncertain = True  # sigue siendo incierto (lo mira la aplicación de un perfil)
            raise err from exc
        try:
            restore_forager_config(path, backup)
            store.save_state(prev_state)
        except (BotControlError, OSError) as restore_exc:
            err = DockerError(
                f"{exc} ATENCION: la config de forager quedo modificada y no se pudo restaurar "
                f"({restore_exc}); backup: {backup.name}."
            )
            raise err from exc
        raise DockerError(f"{exc} Se restauro la config de forager; no se aplico ningun cambio.") from exc


def validate_twe_manual(value: Any) -> decimal.Decimal:
    """TWE long a mano: número JSON finito (no bool ni texto) entre 0,5 y 8, en pasos de 0,25. ValueError si no.

    Acepta también un ``Decimal`` (los perfiles)."""
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise ValueError("twe_long tiene que ser un número finito.")
        twe = value
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("twe_long tiene que ser un número.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("twe_long tiene que ser un número finito.")
        twe = decimal.Decimal(str(value))
    if twe < TWE_MANUAL_MIN or twe > TWE_MANUAL_MAX:
        raise ValueError(f"twe_long fuera de rango: tiene que estar entre {TWE_MANUAL_MIN} y {TWE_MANUAL_MAX}.")
    if twe % TWE_MANUAL_PASO != 0:
        raise ValueError(f"twe_long va en pasos de {TWE_MANUAL_PASO} (por ejemplo 3, 3.25, 3.5).")
    return twe


def twe_valor(twe: decimal.Decimal) -> Any:
    """Valor para el HJSON: entero si no tiene decimales (3, no 3.0), si no float (3.25)."""
    return int(twe) if twe == twe.to_integral_value() else float(twe)


def n_pares(cfg: Dict[str, Any]) -> tuple:
    """(n_longs, n_shorts) de la config de forager como enteros, o None si falta o no es un entero >= 0."""
    out = []
    for key in ("n_longs", "n_shorts"):
        value = cfg.get(key)
        if isinstance(value, bool):
            out.append(None)
        elif isinstance(value, int) and value >= 0:
            out.append(value)
        elif isinstance(value, float) and value.is_integer() and value >= 0:
            out.append(int(value))
        else:
            out.append(None)
    return tuple(out)


def validate_riesgo(riesgo: Any) -> str:
    if not isinstance(riesgo, str) or riesgo not in RISK_PRESETS:
        raise ValueError("riesgo invalido: usar bajo, medio o alto")
    return riesgo


def validate_stop(settings: Settings, modo: Any) -> str:
    """Validaciones sin I/O de STOP (enum y soporte de modos). Lanza antes de crear un job."""
    if not isinstance(modo, str) or modo not in STOP_MODES:
        raise ValueError("modo invalido: usar graceful o apagar")
    settings.require_enabled()
    if modo == "graceful" and not settings.modes_supported:
        err = BotControlError(
            "Graceful stop no disponible: forager no lee long_mode/short_mode del HJSON "
            f"(habilitar {ENV_MODES_SUPPORTED}=1 solo con forager parcheado)."
        )
        err.status_code = 409
        raise err
    return modo


def start_bot(settings: Settings, store: Store, riesgo: Any,
              docker: Optional[DockerClient] = None, reporter: Optional[Reporter] = None,
              mantener_twe: bool = False, twe_long: Optional[decimal.Decimal] = None,
              usuario: Optional[str] = None, origen: Optional[str] = None,
              estrategia: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """START del panel: aplica el preset ``riesgo`` (twe_long/twe_short), modos normales y reinicia passivbot.

    ``mantener_twe=True`` (solo lo usa el reinicio automático de C3 con una config "personalizado") no toca
    twe_long/twe_short: ``riesgo`` se ignora y el resto es idéntico (modos, backup, escritura, reinicio).

    ``twe_long`` (TWE manual o un perfil, ver ``futuresboard.perfiles``): escribe ese ``twe_long`` en vez del preset
    (``riesgo`` se ignora). ``twe_short``: ``TWE_SHORT_SOLO_LONG`` con ``n_shorts`` 0 (forager no lo usa); con shorts
    se deja el que está. El resto es idéntico al START con preset.

    ``estrategia`` (solo con ``twe_long``: un perfil, S2): claves de estrategia que se escriben junto con la TWE (los 4
    pares medidos y ``n_longs``), en la misma escritura atómica.

    Con Docker OK deja el arranque en ``bot_arranques.jsonl`` (``registrar_arranque``: hash y resumen de la estrategia
    con la que arrancó, ``usuario`` y ``origen``). Si el registro falla, el arranque sigue igual.
    """
    twe_manual = validate_twe_manual(twe_long) if twe_long is not None else None
    if twe_manual is None and not mantener_twe:
        validate_riesgo(riesgo)
    (reporter or NULL_REPORTER).step("validar", PASO_EN_CURSO)
    settings.require_enabled()
    path = settings.require_config()
    docker = docker or DockerClient(settings.docker_url)
    info = _inspect_existing(docker, settings.container)

    cfg = read_forager_config(path)
    state = store.load_state()
    prev_state = dict(state)
    # Valida antes de escribir nada: con un short_mode que forager rechaza, 409 sin tocar la
    # config ni reiniciar (si no, el contenedor quedaria reiniciando en loop).
    current_short = _mode(cfg, "short")
    # Decision: short_mode se mantiene salvo que sea un modo de stop; en ese caso se vuelve
    # al valor previo al stop (guardado en el estado local) o a "normal". Si el valor guardado
    # es invalido (estado viejo o editado a mano) se usa "normal": es estado del dashboard.
    if current_short in STOP_MODE_VALUES:
        previous = _saved_short_mode(state.get("short_mode_before_stop"))
        new_short = previous if previous and previous not in STOP_MODE_VALUES else MODE_NORMAL
    else:
        new_short = current_short

    if mantener_twe:
        riesgo = detect_preset(cfg)
        updates = {"long_mode": MODE_NORMAL, "short_mode": new_short}
    elif twe_manual is not None:
        updates = {"twe_long": twe_valor(twe_manual)}
        if n_pares(cfg)[1] == 0:
            updates["twe_short"] = TWE_SHORT_SOLO_LONG
        if estrategia:
            updates.update({k: v for k, v in estrategia.items()
                            if k in ("n_longs", "approved_symbols_long", "approved_symbols_short")})
        updates.update(long_mode=MODE_NORMAL, short_mode=new_short)
        riesgo = detect_preset({**cfg, **updates})
    else:
        twe_long, twe_short = RISK_PRESETS[riesgo]
        updates = {
            "twe_long": twe_long,
            "twe_short": twe_short,
            "long_mode": MODE_NORMAL,
            "short_mode": new_short,
        }
    state.pop("short_mode_before_stop", None)
    action, backup = _apply_and_restart(settings, store, docker, info, path, updates, prev_state, state,
                                        reporter)
    out = {"ok": True, "riesgo": riesgo, "docker_action": action, "config": updates, "backup": backup.name}
    arranque = registrar_arranque(store, {**cfg, **updates}, usuario, origen or "start", action)
    if arranque:
        out["arranque"] = arranque["id"]
    if twe_manual is not None and twe_manual > TWE_ADVERTENCIA:
        out["warning"] = (f"TWE {twe_valor(twe_manual)} por encima de {twe_valor(TWE_ADVERTENCIA)}: en el backtest 2021-2026 "
                          "aparecen liquidaciones desde TWE 6. Herramienta, no asesor.")
    return out


def stop_bot(settings: Settings, store: Store, modo: Any,
             docker: Optional[DockerClient] = None,
             binance: Optional[BinanceFuturesClient] = None,
             reporter: Optional[Reporter] = None) -> Dict[str, Any]:
    validate_stop(settings, modo)
    docker = docker or DockerClient(settings.docker_url)

    if modo == "apagar":
        return _apagar(settings, docker, binance, reporter, store=store)

    (reporter or NULL_REPORTER).step("validar", PASO_EN_CURSO)
    path = settings.require_config()
    info = _inspect_existing(docker, settings.container)
    cfg = read_forager_config(path)
    state = store.load_state()
    prev_state = dict(state)
    # Decision: un short_mode invalido no bloquea el graceful stop (se va a pisar con
    # graceful_stop), pero tampoco se guarda para restaurarlo en el proximo START.
    try:
        current_short: Optional[str] = _mode(cfg, "short")
    except InvalidModeError:
        current_short = None
    if current_short is None:
        state.pop("short_mode_before_stop", None)
    elif current_short not in STOP_MODE_VALUES:
        state["short_mode_before_stop"] = current_short

    updates = {"long_mode": MODE_GRACEFUL_STOP, "short_mode": MODE_GRACEFUL_STOP}
    action, backup = _apply_and_restart(settings, store, docker, info, path, updates, prev_state, state,
                                        reporter)
    return {"ok": True, "modo": modo, "docker_action": action, "config": updates,
            "backup": backup.name}


def _dejar_en_parada_gradual(settings: Settings, store: Optional[Store], rep: Reporter) -> Optional[str]:
    """Paso 1 de Apagar: ``long_mode`` y ``short_mode`` en ``graceful_stop`` (backup y escritura atómica, como Parada
    gradual), para que un arranque externo (``docker compose up -d``) no vuelva a operar.

    **Nunca lanza ni frena el Apagar**, que es una acción de emergencia: si no puede escribir, marca el paso (error u
    omitido) y devuelve el aviso, que va al resultado y a Telegram. Guarda ``short_mode_before_stop`` igual que
    Parada gradual, así Iniciar vuelve al short_mode de antes. Si ya estaba en parada gradual no escribe."""
    clave = "parada_gradual"
    rep.step(clave, PASO_EN_CURSO)
    path = settings.forager_config
    if path is None:
        aviso = (f"No se dejó forager en parada gradual: falta {ENV_FORAGER_CONFIG}. Se sigue con el Apagar; si algo "
                 "vuelve a arrancar passivbot, opera normal.")
        rep.step(clave, PASO_OMITIDO, aviso)
        return aviso
    try:
        cfg = read_forager_config(path)
        modos = []
        for side in ("long", "short"):
            try:
                modos.append(_mode(cfg, side))
            except InvalidModeError:
                modos.append(None)
        if modos == [MODE_GRACEFUL_STOP, MODE_GRACEFUL_STOP]:
            rep.step(clave, PASO_OK, "Ya estaba en parada gradual: no se escribió nada.")
            return None
        state = store.load_state() if store is not None else None
        backup = write_forager_config(path, {"long_mode": MODE_GRACEFUL_STOP, "short_mode": MODE_GRACEFUL_STOP})
        if state is not None and modos[1] is not None and modos[1] not in STOP_MODE_VALUES:
            state["short_mode_before_stop"] = modos[1]
            with contextlib.suppress(OSError):
                store.save_state(state)
        rep.step(clave, PASO_OK, f"long_mode y short_mode en graceful_stop (backup {backup.name}). Si algo vuelve a "
                                 "arrancar passivbot, no abre pares nuevos; Iniciar los vuelve a normal.")
        return None
    except Exception as exc:  # noqa: BLE001 - la escritura nunca frena el Apagar
        motivo = str(exc)[:200] if isinstance(exc, BotControlError) else exc.__class__.__name__
        aviso = (f"No se pudo dejar forager en parada gradual ({motivo}). Se siguió con el Apagar; si algo vuelve a "
                 "arrancar passivbot, opera normal.")
        rep.step(clave, PASO_ERROR, aviso)
        return aviso


def _sumar_aviso(payload: Dict[str, Any], aviso: Optional[str]) -> Dict[str, Any]:
    if aviso:
        payload["warning"] = f"{payload['warning']} {aviso}" if payload.get("warning") else aviso
    return payload


def _apagar(settings: Settings, docker: DockerClient,
            binance: Optional[BinanceFuturesClient], reporter: Optional[Reporter] = None,
            store: Optional[Store] = None) -> Dict[str, Any]:
    """Deja forager en parada gradual, detiene passivbot y después cancela todas las órdenes y cierra todas las
    posiciones.

    Lo primero nunca frena lo demás (``_dejar_en_parada_gradual``). Si el stop falla (error HTTP, timeout,
    contenedor inexistente) o el contenedor sigue corriendo, se corta con error SIN tocar Binance: cerrar con el bot
    vivo haría que reabra.
    """
    if binance is None:
        raise DisabledError("Apagar requiere el cliente de Binance Futures configurado.")
    rep = reporter or NULL_REPORTER
    aviso = _dejar_en_parada_gradual(settings, store, rep)
    try:
        rep.step("detener", PASO_EN_CURSO, f"docker stop {settings.container} (hasta {STOP_TIMEOUT_SECONDS} s)")
        result = docker.stop(settings.container)
        rep.step("detener", PASO_OK, "Detenido." if result == "ok" else "Ya estaba detenido.")
        rep.step("verificar_detenido", PASO_EN_CURSO)
        info = docker.inspect(settings.container)
        if info["status"] == "not_found":
            raise DockerError(f"El contenedor {settings.container} no existe. No se cerró nada en Binance.")
        if info["running"]:
            raise DockerError("passivbot sigue corriendo después del stop. No se cerró nada en Binance.")
    except DockerError as exc:
        _sumar_aviso(exc.payload, aviso)
        raise
    rep.step("verificar_detenido", PASO_OK, f"Estado: {info.get('status')}")

    resumen = close_all_futures(binance, rep)
    base = {"modo": "apagar", "docker_action": "stop", "docker_result": result, "resumen": resumen}
    if not resumen["completo"]:
        raise PartialCloseError(
            "passivbot quedó detenido pero el cierre fue parcial: revisar lo que quedó abierto en Binance.",
            payload=_sumar_aviso(base, aviso),
        )
    if not resumen.get("algo_verificado", True):
        base["warning"] = ("Órdenes condicionales sin verificar: el endpoint no existe en demo/testnet. "
                           "Revisar en Binance.")
    return {"ok": True, **_sumar_aviso(base, aviso)}


# --------------------------------------------------------------------------------------
# Después de START / Graceful stop: verificar el contenedor y esperar a forager
# --------------------------------------------------------------------------------------


def verify_running(settings: Settings, docker: Optional[DockerClient] = None,
                   reporter: Optional[Reporter] = None) -> Dict[str, Any]:
    """Confirma que passivbot quedó corriendo tras start/restart. DockerError si no."""
    rep = reporter or NULL_REPORTER
    docker = docker or DockerClient(settings.docker_url)
    rep.step("verificar", PASO_EN_CURSO)
    info = docker.inspect(settings.container)
    if not info["running"]:
        raise DockerError(
            f"passivbot no quedó en ejecución tras el reinicio (estado {info['status']}). "
            "La config ya quedó escrita: revisar los logs del contenedor."
        )
    rep.step("verificar", PASO_OK, f"{settings.container}: {info['status']}")
    return info


def wait_for_forager(settings: Settings, seconds: int, docker: Optional[DockerClient] = None,
                     reporter: Optional[Reporter] = None, cancel: Any = None) -> str:
    """Espera informativa mientras forager abre los pares (cuenta regresiva de ``seconds``).

    No hace nada en passivbot: solo reporta el tiempo restante y consulta el contenedor cada
    ``WARMUP_CHECK_SECONDS``. Si se cae, DockerError. ``cancel`` (``threading.Event``) corta la
    espera (otra acción del panel la reemplaza) y el paso queda omitido. Devuelve el estado final
    del paso ("ok" u "omitido").
    """
    rep = reporter or NULL_REPORTER
    docker = docker or DockerClient(settings.docker_url)
    total = max(0, int(seconds))
    start = time.monotonic()
    last_check = start
    rep.step("forager", PASO_EN_CURSO, f"Forager abre los pares en ~{total} s.", 0, total)
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= total:
            break
        pause = min(WARMUP_TICK_SECONDS, total - elapsed) if WARMUP_TICK_SECONDS > 0 else total - elapsed
        if cancel is not None:
            if cancel.wait(pause):
                rep.step("forager", PASO_OMITIDO, "Espera interrumpida por otra acción del panel.")
                return PASO_OMITIDO
        else:
            time.sleep(pause)
        elapsed = min(total, time.monotonic() - start)
        restante = max(0, int(round(total - elapsed)))
        rep.step("forager", PASO_EN_CURSO, f"Quedan ~{restante} s (estimado).", int(elapsed), total)
        if time.monotonic() - last_check >= WARMUP_CHECK_SECONDS:
            last_check = time.monotonic()
            try:
                info = docker.inspect(settings.container)
            except DockerError:
                continue  # un corte breve del proxy no invalida la espera; la consulta final decide
            if cancel is not None and cancel.is_set():
                continue  # la cortó otra acción (ej. Apagar detuvo passivbot): no es un error
            if not info["running"]:
                raise DockerError(f"passivbot se detuvo mientras forager arrancaba (estado {info['status']}).")

    def cortada() -> bool:
        if cancel is not None and cancel.is_set():
            rep.step("forager", PASO_OMITIDO, "Espera interrumpida por otra acción del panel.")
            return True
        return False

    if cortada():
        return PASO_OMITIDO
    try:
        info = docker.inspect(settings.container)
    except DockerError:
        if cortada():
            return PASO_OMITIDO
        raise
    if not info["running"] and cortada():
        # Otra acción (ej. Apagar) ya detuvo passivbot mientras se consultaba: no es un error.
        return PASO_OMITIDO
    if not info["running"]:
        raise DockerError(f"passivbot se detuvo mientras forager arrancaba (estado {info['status']}).")
    rep.step("forager", PASO_OK, "passivbot sigue en ejecución; forager ya debería estar operando los pares.",
             total, total)
    return PASO_OK
