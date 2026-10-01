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
import fcntl
import json
import os
import pathlib
import re
import shutil
import tempfile
from typing import Any
from typing import Dict
from typing import Iterator
from typing import Optional
from urllib.parse import quote

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
    """Error con mensaje apto para mostrar al usuario."""

    status_code = 500


class DisabledError(BotControlError):
    status_code = 503


class ConfigError(BotControlError):
    status_code = 500


class DockerError(BotControlError):
    status_code = 502


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
            raise DockerError(f"No se pudo contactar al docker-proxy: {exc.__class__.__name__}") from exc

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
    out["message"] = " ".join(messages)
    return out


def _start_or_restart(docker: DockerClient, name: str) -> str:
    info = docker.inspect(name)
    if info["status"] == "not_found":
        raise DockerError(f"El contenedor {name} no existe.")
    if info["running"]:
        docker.restart(name)
        return "restart"
    docker.start(name)
    return "start"


def start_bot(settings: Settings, store: Store, riesgo: Any,
              docker: Optional[DockerClient] = None) -> Dict[str, Any]:
    if not isinstance(riesgo, str) or riesgo not in RISK_PRESETS:
        raise ValueError("riesgo invalido: usar bajo, medio o alto")
    settings.require_enabled()
    path = settings.require_config()
    docker = docker or DockerClient(settings.docker_url)

    cfg = read_forager_config(path)
    state = store.load_state()
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
    backup = write_forager_config(path, updates)
    state.pop("short_mode_before_stop", None)
    store.save_state(state)

    action = _start_or_restart(docker, settings.container)
    return {"ok": True, "riesgo": riesgo, "docker_action": action, "config": updates,
            "backup": backup.name}


def stop_bot(settings: Settings, store: Store, modo: Any,
             docker: Optional[DockerClient] = None) -> Dict[str, Any]:
    if not isinstance(modo, str) or modo not in STOP_MODES:
        raise ValueError("modo invalido: usar graceful o apagar")
    settings.require_enabled()
    docker = docker or DockerClient(settings.docker_url)

    if modo == "apagar":
        result = docker.stop(settings.container)
        return {"ok": True, "modo": modo, "docker_action": "stop", "docker_result": result,
                "warning": "Las posiciones y ordenes abiertas quedan en el exchange sin gestion."}

    if not settings.modes_supported:
        err = BotControlError(
            "Graceful stop no disponible: forager no lee long_mode/short_mode del HJSON "
            f"(habilitar {ENV_MODES_SUPPORTED}=1 solo con forager parcheado)."
        )
        err.status_code = 409
        raise err

    path = settings.require_config()
    cfg = read_forager_config(path)
    state = store.load_state()
    current_short = _mode(cfg, "short")
    if current_short not in STOP_MODE_VALUES:
        state["short_mode_before_stop"] = current_short

    updates = {"long_mode": MODE_GRACEFUL_STOP, "short_mode": MODE_GRACEFUL_STOP}
    backup = write_forager_config(path, updates)
    store.save_state(state)

    action = _start_or_restart(docker, settings.container)
    return {"ok": True, "modo": modo, "docker_action": action, "config": updates,
            "backup": backup.name}
