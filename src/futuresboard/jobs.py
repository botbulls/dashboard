"""Acciones del panel (START / Graceful stop / Apagar) como jobs con progreso por pasos.

El POST valida de forma síncrona (auth, CSRF, enum, modos) y, si todo está bien, crea un job que
corre en un hilo y responde 202 con su id. La UI consulta ``GET /api/bot/jobs/<id>`` cada segundo.

* Registro en memoria, thread-safe, con TTL y límite de cantidad (los jobs activos no se purgan).
* Un solo job activo a la vez: el job toma el mismo lock de archivo que antes tomaba la acción
  síncrona (``bot_control.Store.lock``) y lo suelta al terminar la parte que toca config/Docker/
  Binance. La espera informativa a forager corre sin el lock y es cancelable: una acción nueva
  (ej. Apagar) la corta en vez de esperar hasta 90 s.
* El último job se persiste en el directorio de datos. Si el proceso se reinicia con un job en
  curso, al arrancar se lo marca ``interrumpido`` (y se audita / notifica). Mientras un job vive,
  su hilo tiene un lock compartido sobre ``bot_job.alive``: si al arrancar otro proceso (ej. el
  worker nuevo de un reload de gunicorn) lo encuentra tomado, el job sigue vivo y no se toca.

No depende de Flask: el blueprint le pasa todo lo que necesita (store, usuario, logger, etc.).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

from futuresboard import bot_control
from futuresboard import telegram_notify

JOB_TTL_SECONDS = 3600
MAX_JOBS = 50
LAST_JOB_FILE_NAME = "bot_job_last.json"
ALIVE_FILE_NAME = "bot_job.alive"
# Al cortar la espera a forager, cuánto espera un POST nuevo a que el job viejo suelte el lock
# de acciones. El job lo suelta justo después de marcarse cancelable, así que alcanza con poco.
LOCK_WAIT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.02

ESTADO_EN_CURSO = "en_curso"
ESTADO_OK = "ok"
ESTADO_ERROR = "error"
ESTADO_INTERRUMPIDO = "interrumpido"
ESTADOS_FINALES = (ESTADO_OK, ESTADO_ERROR, ESTADO_INTERRUMPIDO)

_JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_DONE_STEP_STATES = (bot_control.PASO_OK, bot_control.PASO_ERROR, bot_control.PASO_OMITIDO)

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _start_thread(fn: Callable[[], None]) -> threading.Thread:
    """Arranca el hilo del job. Los tests lo pueden reemplazar."""
    t = threading.Thread(target=fn, name="bot-job", daemon=True)
    t.start()
    return t


def valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.match(job_id or ""))


class Job(bot_control.Reporter):
    """Un job de acción del panel. Es el ``Reporter`` que recibe ``bot_control``."""

    def __init__(self, accion: str, params: Dict[str, Any], catalogo: List[tuple], usuario: str,
                 on_change: Optional[Callable[["Job"], None]] = None, clock: Callable[[], float] = time.time) -> None:
        self.id = secrets.token_hex(8)
        self.accion = accion
        self.params = dict(params)
        self.usuario = usuario
        self.pasos: List[Dict[str, Any]] = [
            {"clave": clave, "titulo": titulo, "estado": bot_control.PASO_PENDIENTE, "detalle": ""}
            for clave, titulo in catalogo
        ]
        self.estado = ESTADO_EN_CURSO
        self.resultado: Optional[Dict[str, Any]] = None
        self.http_status: Optional[int] = None
        self.inicio = _now_iso()
        self.fin: Optional[str] = None
        self.fin_ts: Optional[float] = None
        self._clock = clock
        # La espera a forager se puede cortar desde otro POST.
        self.cancel = threading.Event()
        self.cancelable = False
        self.done = threading.Event()
        self._lock = threading.RLock()
        self._on_change = on_change
        self._alive: Any = None  # fh con LOCK_SH sobre bot_job.alive mientras el job vive

    # -- Reporter -------------------------------------------------------------------
    def step(self, clave: str, estado: str, detalle: Optional[str] = None,
             actual: Optional[int] = None, total: Optional[int] = None) -> None:
        with self._lock:
            paso = next((p for p in self.pasos if p["clave"] == clave), None)
            if paso is None or self.estado != ESTADO_EN_CURSO:
                return
            paso["estado"] = estado
            if detalle is not None:
                paso["detalle"] = detalle
            if total is not None and total > 0:
                paso["progreso"] = {"actual": max(0, min(int(actual or 0), int(total))), "total": int(total)}
            elif total is not None:
                paso.pop("progreso", None)
        self._changed()

    # -- estado ---------------------------------------------------------------------
    def current_step(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return next((p for p in self.pasos if p["estado"] == bot_control.PASO_EN_CURSO), None)

    def fail_current(self, detalle: str) -> None:
        """Marca como error el paso en curso (o el primero pendiente si ninguno arrancó)."""
        with self._lock:
            paso = self.current_step() or next(
                (p for p in self.pasos if p["estado"] == bot_control.PASO_PENDIENTE), None)
            if paso is not None:
                paso["estado"] = bot_control.PASO_ERROR
                paso["detalle"] = detalle
        self._changed()

    def finish(self, estado: str, resultado: Dict[str, Any], http_status: int) -> None:
        with self._lock:
            for paso in self.pasos:
                if paso["estado"] == bot_control.PASO_EN_CURSO:
                    paso["estado"] = bot_control.PASO_OK if estado == ESTADO_OK else bot_control.PASO_ERROR
                elif paso["estado"] == bot_control.PASO_PENDIENTE:
                    paso["estado"] = bot_control.PASO_OMITIDO
            self.estado = estado
            self.resultado = resultado
            self.http_status = http_status
            self.fin = _now_iso()
            self.fin_ts = self._clock()
            self.cancelable = False
        self._changed()
        self.done.set()
        self.release_alive()

    def hold_alive(self, path: Any) -> None:
        """Lock compartido sobre ``bot_job.alive`` mientras el job vive (lo ve ``recover``)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a")
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        except OSError:  # pragma: no cover - sin lock solo se pierde la protección de recover
            log.exception("No se pudo tomar %s", path)
            return
        self._alive = fh

    def release_alive(self) -> None:
        fh, self._alive = self._alive, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def set_cancelable(self, value: bool) -> None:
        with self._lock:
            self.cancelable = value and self.estado == ESTADO_EN_CURSO
        self._changed()

    @property
    def bloqueante(self) -> bool:
        """En curso y sin cortar: impide lanzar otra acción (un job cortado ya no bloquea)."""
        return self.activo and not self.cancel.is_set()

    @property
    def activo(self) -> bool:
        return self.estado == ESTADO_EN_CURSO

    def porcentaje(self) -> int:
        with self._lock:
            if self.estado in ESTADOS_FINALES:
                return 100
            if not self.pasos:
                return 0
            hecho = 0.0
            for paso in self.pasos:
                if paso["estado"] in _DONE_STEP_STATES:
                    hecho += 1
                elif paso["estado"] == bot_control.PASO_EN_CURSO and paso.get("progreso"):
                    prog = paso["progreso"]
                    hecho += prog["actual"] / prog["total"] if prog["total"] else 0
            return min(99, int(hecho * 100 / len(self.pasos)))

    def to_dict(self, internal: bool = False) -> Dict[str, Any]:
        with self._lock:
            out: Dict[str, Any] = {
                "id": self.id,
                "accion": self.accion,
                "params": dict(self.params),
                "estado": self.estado,
                "pasos": [dict(p, **({"progreso": dict(p["progreso"])} if p.get("progreso") else {}))
                          for p in self.pasos],
                "porcentaje": self.porcentaje(),
                # True durante la espera a forager: se puede lanzar Apagar (corta la espera).
                "cancelable": bool(self.cancelable and self.estado == ESTADO_EN_CURSO
                                   and not self.cancel.is_set()),
                "resultado": self.resultado,
                "http_status": self.http_status,
                "inicio": self.inicio,
                "fin": self.fin,
            }
            if internal:
                out["usuario"] = self.usuario
                out["pid"] = os.getpid()
            return out

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self)
            except Exception:  # pragma: no cover - persistir nunca debe romper la acción
                log.exception("No se pudo persistir el job %s", self.id)


def accion_de(action: str, params: Dict[str, Any]) -> str:
    """Nombre corto de la acción para la UI: start, graceful o apagar."""
    if action == "stop":
        return str(params.get("modo") or "stop")
    return action


def catalogo_de(accion: str) -> List[tuple]:
    if accion == "apagar":
        return bot_control.APAGAR_STEPS
    if accion == "graceful":
        return bot_control.GRACEFUL_STEPS
    return bot_control.START_STEPS


class ActiveJobError(Exception):
    def __init__(self, job_id: Optional[str]) -> None:
        super().__init__("Ya hay una acción del panel en curso.")
        self.job_id = job_id


class JobRegistry:
    """Jobs en memoria del proceso + persistencia del último en el directorio de datos."""

    def __init__(self, store: bot_control.Store, clock: Callable[[], float] = time.time,
                 ttl_seconds: int = JOB_TTL_SECONDS, max_jobs: int = MAX_JOBS) -> None:
        self.store = store
        self.path = store.data_dir / LAST_JOB_FILE_NAME
        self.alive_path = store.data_dir / ALIVE_FILE_NAME
        self.clock = clock
        self.ttl_seconds = ttl_seconds
        self.max_jobs = max_jobs
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()
        # Serializa los POST de este proceso: chequear activo + tomar lock + crear job.
        self.launch_lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._latest_id: Optional[str] = None

    # -- persistencia ---------------------------------------------------------------
    def persist(self, job: Job) -> None:
        data = job.to_dict(internal=True)
        with self._persist_lock:
            if job.id != self._latest_id:
                # Un job cortado (ej. START tras un Apagar) termina después del nuevo: no pisarlo.
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{job.id}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    def load_persisted(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and valid_job_id(str(data.get("id") or "")) else None

    def job_alive_elsewhere(self) -> bool:
        """True si algún job sigue vivo (en este u otro proceso) o alguien tiene el lock de acciones.

        Cada job vivo tiene un lock compartido sobre ``bot_job.alive`` (también durante la espera
        a forager, que corre sin el lock de acciones). flock se libera solo si el proceso muere,
        así que no depende de pids (que en un contenedor se reusan tras un reinicio).
        """
        handle = self.store.try_lock()
        if handle is None:
            return True
        try:
            self.alive_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.alive_path, "a") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            handle.release()

    def recover(self) -> Optional[Dict[str, Any]]:
        """Al arrancar: un job persistido como en curso quedó cortado por un reinicio del proceso.

        Lo marca ``interrumpido`` (paso en curso -> error, pendientes -> omitido) y lo devuelve
        para auditar / notificar. None si no había nada que recuperar o si el job sigue vivo en
        otro proceso (ej. el worker viejo durante un reload de gunicorn): ese lo termina él.
        """
        data = self.load_persisted()
        if not data or data.get("estado") != ESTADO_EN_CURSO:
            return None
        if self.job_alive_elsewhere():
            log.info("El job %s sigue en curso en otro proceso: no se marca interrumpido.", data.get("id"))
            return None
        motivo = "Interrumpido: el dashboard se reinició durante la acción."
        for paso in data.get("pasos") or []:
            if not isinstance(paso, dict):
                continue
            if paso.get("estado") == bot_control.PASO_EN_CURSO:
                paso["estado"] = bot_control.PASO_ERROR
                paso["detalle"] = f"{paso.get('detalle') or ''} {motivo}".strip()
            elif paso.get("estado") == bot_control.PASO_PENDIENTE:
                paso["estado"] = bot_control.PASO_OMITIDO
        data["estado"] = ESTADO_INTERRUMPIDO
        data["porcentaje"] = 100
        data["fin"] = _now_iso()
        data["resultado"] = {
            "ok": False,
            "error": (f"{motivo} Revisar el estado de passivbot"
                      f"{' y de Binance' if data.get('accion') == 'apagar' else ''} antes de reintentar."),
        }
        data["http_status"] = 500
        with self._persist_lock:
            tmp = self.path.with_name(f".{self.path.name}.recover.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        return data

    # -- registro -------------------------------------------------------------------
    def _purge(self) -> None:
        now = self.clock()
        for job_id, job in list(self._jobs.items()):
            if not job.activo and job.fin_ts is not None and now - job.fin_ts > self.ttl_seconds:
                del self._jobs[job_id]
        finished = [jid for jid, job in self._jobs.items() if not job.activo]
        while len(self._jobs) > self.max_jobs and finished:
            del self._jobs[finished.pop(0)]

    def active(self) -> Optional[Job]:
        """Job en curso que bloquea otra acción (no cuenta uno cuya espera a forager se cortó)."""
        with self._lock:
            return next((j for j in reversed(self._jobs.values()) if j.bloqueante), None)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Snapshot del job (memoria, o el último persistido si coincide el id)."""
        if not valid_job_id(job_id):
            return None
        with self._lock:
            self._purge()
            job = self._jobs.get(job_id)
        if job is not None:
            return job.to_dict()
        data = self.load_persisted()
        if data and data.get("id") == job_id and data.get("estado") in ESTADOS_FINALES:
            fin = _parse_iso(data.get("fin"))
            if fin is not None and self.clock() - fin > self.ttl_seconds:
                return None
            return {k: v for k, v in data.items() if k not in ("usuario", "pid")}
        return None

    def create(self, accion: str, params: Dict[str, Any], usuario: str) -> Job:
        job = Job(accion, params, catalogo_de(accion), usuario, on_change=self.persist, clock=self.clock)
        job.hold_alive(self.alive_path)
        with self._lock:
            self._purge()
            self._jobs[job.id] = job
        with self._persist_lock:
            self._latest_id = job.id
        self.persist(job)
        return job

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)


def _parse_iso(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Ejecución
# --------------------------------------------------------------------------------------


class JobContext:
    """Lo que el job necesita de la request (capturado antes de salir del contexto de Flask)."""

    def __init__(self, store: bot_control.Store, settings: bot_control.Settings, user: str,
                 remote_addr: Optional[str], logger: Any) -> None:
        self.store = store
        self.settings = settings
        self.user = user
        self.remote_addr = remote_addr
        self.logger = logger


def launch(registry: JobRegistry, action: str, params: Dict[str, Any], ctx: JobContext,
           work: Callable[[Job], Dict[str, Any]]) -> Job:
    """Crea y arranca el job. ``ActiveJobError`` si ya hay una acción en curso.

    Si el job activo está en la espera a forager (sin lock), se la corta y se sigue sin esperar a
    que termine: el job cortado deja de bloquear y su final no pisa el persistido del nuevo.
    """
    accion = accion_de(action, params)
    with registry.launch_lock:
        active = registry.active()
        cortado = False
        if active is not None and active.cancelable:
            active.cancel.set()
            active, cortado = None, True
        if active is not None:
            raise ActiveJobError(active.id)
        handle = _try_lock_wait(ctx.store, LOCK_WAIT_SECONDS if cortado else 0)
        if handle is None:
            # Otro proceso tiene el lock (o una acción vieja sigue corriendo).
            data = registry.load_persisted()
            raise ActiveJobError(data.get("id") if data and data.get("estado") == ESTADO_EN_CURSO else None)
        try:
            job = registry.create(accion, params, ctx.user)
        except BaseException:
            handle.release()
            raise
        try:
            _start_thread(lambda: _run(job, action, params, ctx, work, handle))
        except BaseException:
            handle.release()
            job.release_alive()
            raise
    return job


def _try_lock_wait(store: bot_control.Store, timeout: float) -> Optional[bot_control.LockHandle]:
    deadline = time.monotonic() + timeout
    while True:
        handle = store.try_lock()
        if handle is not None or time.monotonic() >= deadline:
            return handle
        time.sleep(LOCK_POLL_SECONDS)


def _run(job: Job, action: str, params: Dict[str, Any], ctx: JobContext,
         work: Callable[[Job], Dict[str, Any]], handle: bot_control.LockHandle) -> None:
    status, payload, outcome = 500, {"ok": False, "error": "Error interno."}, "error"
    try:
        try:
            status, payload, outcome = _execute(job, ctx, work)
            if outcome == "ok" and job.accion in ("start", "graceful"):
                try:
                    bot_control.verify_running(ctx.settings, reporter=job)
                except bot_control.BotControlError as exc:
                    job.fail_current(str(exc))
                    status, outcome = exc.status_code, "error"
                    payload = {**payload, "ok": False, "error": str(exc)}
        finally:
            if outcome == "ok" and job.accion in ("start", "graceful"):
                # Cancelable ANTES de soltar el lock: un Apagar que llegue en el medio no recibe 409.
                job.set_cancelable(True)
            handle.release()
        if outcome == "ok" and job.accion in ("start", "graceful"):
            # Paso informativo, sin lock: otra acción lo puede cortar.
            try:
                bot_control.wait_for_forager(ctx.settings, bot_control.forager_warmup_seconds(),
                                             reporter=job, cancel=job.cancel)
            except bot_control.BotControlError as exc:
                job.fail_current(str(exc))
                status, outcome = exc.status_code, "error"
                payload = {**payload, "ok": False, "error": str(exc)}
            finally:
                job.set_cancelable(False)
    except Exception:  # pragma: no cover - defensivo
        ctx.logger.exception("Error inesperado en el job %s", job.id)
        job.fail_current("Error interno.")
        status, payload, outcome = 500, {"ok": False, "error": "Error interno."}, "error"
    _audit_and_notify(ctx, action, params, outcome, payload)
    job.finish(ESTADO_OK if outcome == "ok" else ESTADO_ERROR, payload, status)


def _execute(job: Job, ctx: JobContext, work: Callable[[Job], Dict[str, Any]]) -> tuple:
    try:
        result = work(job)
    except ValueError as exc:
        job.fail_current(str(exc))
        return 400, {"ok": False, "error": str(exc)}, "rechazado"
    except bot_control.BotControlError as exc:
        job.fail_current(str(exc))
        return exc.status_code, {**exc.payload, "ok": False, "error": str(exc)}, "error"
    except Exception:
        ctx.logger.exception("Error en accion de bot %s", job.accion)
        job.fail_current("Error interno.")
        return 500, {"ok": False, "error": "Error interno."}, "error"
    return 200, result, "ok"


def audit_detail(payload: Dict[str, Any]) -> str:
    detail = payload.get("error") or payload.get("docker_action", "")
    if payload.get("resumen") is not None:
        # Apagar: el resumen del cierre (órdenes canceladas, posiciones cerradas, errores, restante).
        detail = f"{detail} | resumen: {json.dumps(payload['resumen'], ensure_ascii=False)}"
    return detail


def _audit_and_notify(ctx: JobContext, action: str, params: Dict[str, Any], outcome: str,
                      payload: Dict[str, Any]) -> None:
    try:
        ctx.store.audit(ctx.user, action, params, outcome, audit_detail(payload), ctx.remote_addr)
    except OSError:
        ctx.logger.exception("No se pudo escribir el log de auditoria")
    try:
        # Una notificación al terminar el job (no por paso). Asíncrona y sin efecto en el resultado.
        telegram_notify.notify_bot_action(action, params, outcome, payload, ctx.user)
    except Exception:  # pragma: no cover - notify_bot_action ya no lanza
        ctx.logger.warning("No se pudo notificar la accion %s a Telegram", action)


def recover_interrupted(registry: JobRegistry, logger: Any) -> Optional[Dict[str, Any]]:
    """Al arrancar la app: marca interrumpido el job que quedó en curso, lo audita y lo notifica."""
    try:
        data = registry.recover()
    except Exception:  # pragma: no cover - defensivo: no impedir el arranque
        logger.exception("No se pudo recuperar el último job del panel")
        return None
    if not data:
        return None
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    action = "start" if data.get("accion") == "start" else "stop"
    user = str(data.get("usuario") or "?")
    logger.warning("Acción del panel %s interrumpida por un reinicio (job %s).", data.get("accion"), data.get("id"))
    try:
        registry.store.audit(user, action, params, ESTADO_INTERRUMPIDO, data["resultado"]["error"], None)
    except OSError:
        logger.exception("No se pudo escribir el log de auditoria")
    telegram_notify.notify_bot_action(action, params, "error", data["resultado"], user)
    return data
