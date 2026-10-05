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
import os
import pathlib
import re
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

MODE_NORMAL = "normal"
MODE_GRACEFUL_STOP = "graceful_stop"
# Valores de modo que significan "no abrir nuevas posiciones". Solo exponemos graceful_stop,
# pero si alguien dejo un alias a mano en el HJSON tambien lo tratamos como stop.
STOP_MODE_VALUES = {"graceful_stop", "gs", "graceful-stop"}

STOP_TIMEOUT_SECONDS = 20
HTTP_TIMEOUT_SECONDS = 10
MAX_BACKUPS = 20

AUDIT_LOG_NAME = "bot_actions.log"
STATE_FILE_NAME = "bot_control_state.json"
LOCK_FILE_NAME = ".bot_control.lock"

_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


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

    def __init__(self, base_url: str, session: Optional[requests.Session] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = session or requests.Session()

    def _url(self, name: str, suffix: str = "") -> str:
        if not _CONTAINER_NAME_RE.match(name):
            raise DockerError("Nombre de contenedor invalido.")
        return f"{self.base_url}/containers/{quote(name, safe='')}{suffix}"

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", HTTP_TIMEOUT_SECONDS)
        try:
            return self.http.request(method, url, **kwargs)
        except requests.RequestException as exc:
            err = DockerError(f"No se pudo contactar al docker-proxy: {exc.__class__.__name__}.")
            err.uncertain = True
            raise err from exc

    def inspect(self, name: str) -> Dict[str, Any]:
        resp = self._request("GET", self._url(name, "/json"))
        if resp.status_code == 404:
            return {"name": name, "status": "not_found", "running": False, "started_at": None}
        if resp.status_code != 200:
            raise DockerError(f"Docker respondio {resp.status_code} al consultar el contenedor.")
        state = (resp.json() or {}).get("State") or {}
        return {
            "name": name,
            "status": state.get("Status") or "unknown",
            "running": bool(state.get("Running")),
            "started_at": state.get("StartedAt"),
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

    def __init__(self, base_url: str, api_key: str, api_secret: str,
                 session: Optional[requests.Session] = None) -> None:
        if not base_url or not api_key or not api_secret:
            raise DisabledError(
                "Apagar requiere API_KEY, API_SECRET y API_BASE_URL de Binance Futures configurados."
            )
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.http = session or requests.Session()

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 signed: bool = True) -> Any:
        query_params = dict(params or {})
        headers: Dict[str, str] = {}
        if signed:
            query_params["recvWindow"] = BINANCE_RECV_WINDOW
            query_params["timestamp"] = int(time.time() * 1000)
        query = urlencode(query_params)
        if signed:
            signature = hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
            query = f"{query}&signature={signature}"
            headers["X-MBX-APIKEY"] = self._api_key
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
        return data

    def open_orders(self) -> List[Dict[str, Any]]:
        return list(self._request("GET", "/fapi/v1/openOrders") or [])

    def open_algo_orders(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/fapi/v1/openAlgoOrders")
        if isinstance(data, dict):  # defensivo: algunas variantes envuelven la lista
            data = data.get("orders") or data.get("rows") or []
        return list(data or [])

    def cancel_all_open_orders(self, symbol: str) -> None:
        self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def cancel_all_algo_orders(self, symbol: str) -> None:
        self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    def positions(self) -> List[Dict[str, Any]]:
        rows = self._request("GET", "/fapi/v2/positionRisk") or []
        return [r for r in rows if _dec(r.get("positionAmt")) != 0]

    def exchange_info(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo", signed=False) or {}

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


def close_all_futures(client: BinanceFuturesClient) -> Dict[str, Any]:
    """Cancela TODAS las órdenes abiertas (normales y algo/condicionales) y cierra TODAS las
    posiciones a mercado. Verifica con hasta ``CLOSE_ROUNDS`` rondas.

    Nunca lanza por errores de Binance: los junta en ``errores``. ``completo`` es True solo si la
    lectura final confirma 0 posiciones y 0 órdenes.
    """
    resumen: Dict[str, Any] = {
        "completo": False,
        "rondas": 0,
        "ordenes_canceladas": [],
        "posiciones_cerradas": [],
        "errores": [],
        "restante": {"posiciones": [], "ordenes": [], "ordenes_algo": [], "verificado": False},
    }
    errores: List[str] = resumen["errores"]
    state = {"algo": True, "info": None}

    def snapshot() -> tuple:
        ok = True
        try:
            orders = client.open_orders()
        except BinanceError as exc:
            errores.append(f"Lectura de órdenes: {exc}")
            orders, ok = [], False
        algo: List[Dict[str, Any]] = []
        if state["algo"]:
            try:
                algo = client.open_algo_orders()
            except BinanceError as exc:
                if exc.http_status == 404:
                    # El endpoint no existe en este entorno (ej. demo): no hay órdenes algo que cerrar.
                    state["algo"] = False
                    errores.append("Órdenes algo/condicionales: endpoint no disponible en este entorno (se omite).")
                else:
                    errores.append(f"Lectura de órdenes algo: {exc}")
                    ok = False
        try:
            positions = client.positions()
        except BinanceError as exc:
            errores.append(f"Lectura de posiciones: {exc}")
            positions, ok = [], False
        return ok, orders, algo, positions

    def cancel(orders: List[Dict[str, Any]], tipo: str) -> None:
        for symbol, count in sorted(_order_counts(orders).items()):
            try:
                if tipo == "algo":
                    client.cancel_all_algo_orders(symbol)
                else:
                    client.cancel_all_open_orders(symbol)
            except BinanceError as exc:
                errores.append(f"Cancelar órdenes {tipo} de {symbol}: {exc}")
                continue
            resumen["ordenes_canceladas"].append({"symbol": symbol, "tipo": tipo, "cantidad": count})

    def close(positions: List[Dict[str, Any]]) -> None:
        if state["info"] is None:
            try:
                state["info"] = client.exchange_info()
            except BinanceError as exc:
                errores.append(f"Lectura de exchangeInfo: {exc}")
                return
        for p in positions:
            view = _pos_view(p)
            symbol, lado = view["symbol"], view["lado"]
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
                except BinanceError as exc:
                    errores.append(f"Cerrar {symbol} {lado} ({_fmt(chunk)}): {exc}")
                    break
                closed += chunk
                sent += 1
            if sent:
                resumen["posiciones_cerradas"].append({"symbol": symbol, "lado": lado, "modo": view["modo"],
                                                       "cantidad": _fmt(closed), "ordenes": sent})
            if residual > 0 and sent == len(chunks):
                errores.append(f"{symbol} {lado}: residuo {_fmt(residual)} por debajo del step/mínimo.")

    for ronda in range(1, CLOSE_ROUNDS + 2):
        ok, orders, algo, positions = snapshot()
        if ok and not orders and not algo and not positions:
            resumen["completo"] = True
            resumen["restante"]["verificado"] = True
            break
        if ronda > CLOSE_ROUNDS:
            resumen["restante"] = {
                "posiciones": [_pos_view(p) for p in positions],
                "ordenes": [{"symbol": s, "cantidad": c} for s, c in sorted(_order_counts(orders).items())],
                "ordenes_algo": [{"symbol": s, "cantidad": c} for s, c in sorted(_order_counts(algo).items())],
                "verificado": ok,
            }
            break
        resumen["rondas"] = ronda
        # Primero las órdenes (para que ninguna reabra posición), después las posiciones.
        cancel(orders, "normal")
        cancel(algo, "algo")
        if positions:
            close(positions)
        if ROUND_PAUSE_SECONDS:
            time.sleep(ROUND_PAUSE_SECONDS)
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
    backups = sorted(path.parent.glob(f"{path.name}.bak-*"))
    for old in backups[:-MAX_BACKUPS]:
        with contextlib.suppress(OSError):
            old.unlink()


def write_forager_config(path: pathlib.Path, updates: Dict[str, Any]) -> pathlib.Path:
    """Aplica ``updates`` al HJSON de forager de forma atomica.

    1. backup con timestamp (copia exacta, conserva comentarios),
    2. escribe a un tmp en el mismo directorio,
    3. relee el tmp y verifica que parsea y contiene los valores nuevos,
    4. ``os.replace`` sobre el original.

    Devuelve la ruta del backup. Nota: el round-trip de hjson no conserva comentarios;
    el backup si. Si el original era JSON estricto se escribe JSON.
    """
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


def _mode(cfg: Dict[str, Any], side: str) -> str:
    value = cfg.get(f"{side}_mode")
    return str(value) if value not in (None, "") else MODE_NORMAL


# --------------------------------------------------------------------------------------
# Estado local, auditoria y lock (en el directorio de datos)
# --------------------------------------------------------------------------------------


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
        "long_mode": None,
        "short_mode": None,
        "config_available": False,
        # True: la config cambio despues del ultimo arranque de passivbot (no esta en efecto).
        "config_pending": None,
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
            long_mode=_mode(cfg, "long"),
            short_mode=_mode(cfg, "short"),
        )
    except BotControlError as exc:
        messages.append(str(exc))

    docker = docker or DockerClient(settings.docker_url)
    out["container"] = docker.inspect(settings.container)
    if out["config_available"] and settings.forager_config is not None:
        out["config_pending"] = config_pending(settings.forager_config, out["container"])
    out["message"] = " ".join(messages)
    return out


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
                       new_state: Dict[str, Any]) -> tuple:
    """Escribe la config + estado y reinicia passivbot. Si Docker rechaza la accion, revierte.

    - Error con respuesta HTTP (4xx/5xx, contenedor inexistente): Docker no aplico la accion, se
      restaura el HJSON desde el backup y el estado local previo.
    - Error sin respuesta (timeout / conexion): no se sabe si el reinicio ocurrio; NO se revierte
      (revertir podria dejar el archivo distinto de lo que forager ya cargo). El mensaje lo dice y
      /api/bot/status muestra "pendiente de reinicio" comparando mtime vs StartedAt.
    """
    backup = write_forager_config(path, updates)
    store.save_state(new_state)
    try:
        return _start_or_restart(docker, settings.container, info), backup
    except DockerError as exc:
        if exc.uncertain:
            err = DockerError(
                f"{exc} Resultado incierto: la config de forager quedo escrita (backup {backup.name}); "
                "revisar el estado del bot antes de reintentar."
            )
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


def start_bot(settings: Settings, store: Store, riesgo: Any,
              docker: Optional[DockerClient] = None) -> Dict[str, Any]:
    if not isinstance(riesgo, str) or riesgo not in RISK_PRESETS:
        raise ValueError("riesgo invalido: usar bajo, medio o alto")
    settings.require_enabled()
    path = settings.require_config()
    docker = docker or DockerClient(settings.docker_url)
    info = _inspect_existing(docker, settings.container)

    cfg = read_forager_config(path)
    state = store.load_state()
    prev_state = dict(state)
    current_short = _mode(cfg, "short")
    # Decision: short_mode se mantiene salvo que sea un modo de stop; en ese caso se vuelve
    # al valor previo al stop (guardado en el estado local) o a "normal".
    if current_short in STOP_MODE_VALUES:
        previous = state.get("short_mode_before_stop")
        new_short = previous if isinstance(previous, str) and previous not in STOP_MODE_VALUES and previous else MODE_NORMAL
    else:
        new_short = current_short

    twe_long, twe_short = RISK_PRESETS[riesgo]
    updates = {
        "twe_long": twe_long,
        "twe_short": twe_short,
        "long_mode": MODE_NORMAL,
        "short_mode": new_short,
    }
    state.pop("short_mode_before_stop", None)
    action, backup = _apply_and_restart(settings, store, docker, info, path, updates, prev_state, state)
    return {"ok": True, "riesgo": riesgo, "docker_action": action, "config": updates,
            "backup": backup.name}


def stop_bot(settings: Settings, store: Store, modo: Any,
             docker: Optional[DockerClient] = None,
             binance: Optional[BinanceFuturesClient] = None) -> Dict[str, Any]:
    if not isinstance(modo, str) or modo not in STOP_MODES:
        raise ValueError("modo invalido: usar graceful o apagar")
    settings.require_enabled()
    docker = docker or DockerClient(settings.docker_url)

    if modo == "apagar":
        return _apagar(settings, docker, binance)

    if not settings.modes_supported:
        err = BotControlError(
            "Graceful stop no disponible: forager no lee long_mode/short_mode del HJSON "
            f"(habilitar {ENV_MODES_SUPPORTED}=1 solo con forager parcheado)."
        )
        err.status_code = 409
        raise err

    path = settings.require_config()
    info = _inspect_existing(docker, settings.container)
    cfg = read_forager_config(path)
    state = store.load_state()
    prev_state = dict(state)
    current_short = _mode(cfg, "short")
    if current_short not in STOP_MODE_VALUES:
        state["short_mode_before_stop"] = current_short

    updates = {"long_mode": MODE_GRACEFUL_STOP, "short_mode": MODE_GRACEFUL_STOP}
    action, backup = _apply_and_restart(settings, store, docker, info, path, updates, prev_state, state)
    return {"ok": True, "modo": modo, "docker_action": action, "config": updates,
            "backup": backup.name}


def _apagar(settings: Settings, docker: DockerClient,
            binance: Optional[BinanceFuturesClient]) -> Dict[str, Any]:
    """Detiene passivbot y después cancela todas las órdenes y cierra todas las posiciones.

    Si el stop falla (error HTTP, timeout, contenedor inexistente) o el contenedor sigue
    corriendo, se corta con error SIN tocar Binance: cerrar con el bot vivo haría que reabra.
    """
    if binance is None:
        raise DisabledError("Apagar requiere el cliente de Binance Futures configurado.")
    result = docker.stop(settings.container)
    info = docker.inspect(settings.container)
    if info["status"] == "not_found":
        raise DockerError(f"El contenedor {settings.container} no existe. No se cerró nada en Binance.")
    if info["running"]:
        raise DockerError("passivbot sigue corriendo después del stop. No se cerró nada en Binance.")

    resumen = close_all_futures(binance)
    base = {"modo": "apagar", "docker_action": "stop", "docker_result": result, "resumen": resumen}
    if not resumen["completo"]:
        raise PartialCloseError(
            "passivbot quedó detenido pero el cierre fue parcial: revisar lo que quedó abierto en Binance.",
            payload=base,
        )
    return {"ok": True, **base}
