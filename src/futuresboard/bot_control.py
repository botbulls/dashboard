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
        )
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
    action, backup = _apply_and_restart(settings, store, docker, info, path, updates, prev_state, state)
    return {"ok": True, "modo": modo, "docker_action": action, "config": updates,
            "backup": backup.name}
