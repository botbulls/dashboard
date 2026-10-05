"""Acciones del panel como jobs: 202 + job_id, progreso por pasos, un job a la vez, interrumpido."""
from __future__ import annotations

import json
import pathlib
import threading
import time

import pytest

from futuresboard import bot_control
from futuresboard import jobs
from futuresboard import telegram_notify
from futuresboard.app import init_app
from futuresboard.config import Config

from test_bot_control import DOCKER_BASE
from test_bot_control import FakeDocker
from test_bot_control import app  # noqa: F401
from test_bot_control import audit_lines
from test_bot_control import binance  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401
from test_bot_control import wait_job


def claves(catalogo):
    return [c for c, _ in catalogo]


def estados(job):
    return {p["clave"]: p["estado"] for p in job["pasos"]}


def paso(job, clave):
    return next(p for p in job["pasos"] if p["clave"] == clave)


def registry(app):  # noqa: F811
    return app.extensions["futuresboard_bot_jobs"]


@pytest.fixture
def pasos_log(monkeypatch):
    """Registra cada llamada al reporter (clave, estado) en orden."""
    log = []
    original = jobs.Job.step

    def step(self, clave, estado, detalle=None, actual=None, total=None):
        log.append((clave, estado))
        return original(self, clave, estado, detalle, actual, total)

    monkeypatch.setattr(jobs.Job, "step", step)
    return log


def orden_de_inicio(log):
    vistos = []
    for clave, _ in log:
        if clave not in vistos:
            vistos.append(clave)
    return vistos


class Gate:
    """Bloquea una acción POST de Docker hasta que el test la libere."""

    def __init__(self, docker, action):  # noqa: F811
        self.entered = threading.Event()
        self.release = threading.Event()
        original = docker.request

        def request(method, url, **kwargs):
            if method == "POST" and url.endswith("/" + action):
                self.entered.set()
                assert self.release.wait(10), "el test no liberó la acción bloqueada"
            return original(method, url, **kwargs)

        docker.request = request


# ---------------------------------------------------------------- 202 y validaciones síncronas


def test_start_responde_202_con_job(client, app, env, docker):  # noqa: F811
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"}, wait=False)
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["ok"] is True and jobs.valid_job_id(body["job_id"])
    assert body["job"]["accion"] == "start" and body["job"]["id"] == body["job_id"]
    assert claves([(p["clave"], p["titulo"]) for p in body["job"]["pasos"]]) == claves(bot_control.START_STEPS)
    job = wait_job(client, body["job_id"])
    assert job["estado"] == "ok" and job["porcentaje"] == 100 and job["http_status"] == 200
    assert job["resultado"]["ok"] is True and job["resultado"]["riesgo"] == "bajo"
    assert job["inicio"] and job["fin"]


@pytest.mark.parametrize(
    "url,body,status",
    [
        ("/api/bot/start", {"riesgo": "loco"}, 400),
        ("/api/bot/stop", {"modo": "otro"}, 400),
        ("/api/bot/start", ["bajo"], 400),
    ],
)
def test_validaciones_siguen_siendo_sincronas(client, app, env, docker, url, body, status):  # noqa: F811
    resp = post(client, url, body)
    assert resp.status_code == status
    assert "job_id" not in resp.get_json()
    assert len(registry(app)) == 0
    assert not (pathlib.Path(app.config["DATABASE"]).parent / jobs.LAST_JOB_FILE_NAME).exists()
    assert docker.calls == []


def test_csrf_y_modos_sin_job(client, app, env, docker, monkeypatch):  # noqa: F811
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}, token="mal").status_code == 403
    monkeypatch.delenv(bot_control.ENV_MODES_SUPPORTED)
    assert post(client, "/api/bot/stop", {"modo": "graceful"}).status_code == 409
    app.config["EXCHANGE"] = "bybit"
    assert post(client, "/api/bot/stop", {"modo": "apagar"}).status_code == 409
    monkeypatch.delenv(bot_control.ENV_DOCKER_URL)
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 503
    assert len(registry(app)) == 0
    assert docker.calls == []


# ---------------------------------------------------------------- un job a la vez


def test_un_solo_job_activo_409_con_su_id(client, app, env, docker):  # noqa: F811
    gate = Gate(docker, "restart")
    try:
        first = post(client, "/api/bot/start", {"riesgo": "alto"}, wait=False)
        assert first.status_code == 202
        job_id = first.get_json()["job_id"]
        assert gate.entered.wait(5)

        activo = client.get("/api/bot/jobs/activo").get_json()["job"]
        assert activo["id"] == job_id and activo["estado"] == "en_curso"
        assert estados(activo)["docker"] == "en_curso" and 0 < activo["porcentaje"] < 100

        for url, body in (("/api/bot/start", {"riesgo": "bajo"}), ("/api/bot/stop", {"modo": "apagar"})):
            resp = post(client, url, body)
            assert resp.status_code == 409
            assert resp.get_json()["job_id"] == job_id
    finally:
        gate.release.set()
    assert wait_job(client, job_id)["estado"] == "ok"
    assert client.get("/api/bot/jobs/activo").get_json() == {"job": None}
    # Terminado el job, el lock queda libre.
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 200


def test_lock_tomado_por_otro_proceso_409(client, app, env, docker):  # noqa: F811
    handle = bot_control.Store(pathlib.Path(app.config["DATABASE"]).parent).try_lock()
    assert handle is not None
    try:
        resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
        assert resp.status_code == 409 and resp.get_json()["job_id"] is None
    finally:
        handle.release()
    assert docker.actions() == []


# ---------------------------------------------------------------- pasos en orden


def test_pasos_start_en_orden(client, env, docker, pasos_log):  # noqa: F811
    resp = post(client, "/api/bot/start", {"riesgo": "medio"})
    assert resp.status_code == 200
    assert orden_de_inicio(pasos_log) == claves(bot_control.START_STEPS)
    assert set(estados(resp.job).values()) == {"ok"}
    assert paso(resp.job, "backup")["detalle"].startswith("new.json.bak-")
    assert paso(resp.job, "docker")["detalle"] == "docker restart"


def test_pasos_graceful_en_orden(client, env, docker, pasos_log):  # noqa: F811
    docker.running = False
    resp = post(client, "/api/bot/stop", {"modo": "graceful"})
    assert resp.status_code == 200
    assert resp.job["accion"] == "graceful"
    assert orden_de_inicio(pasos_log) == claves(bot_control.GRACEFUL_STEPS)
    assert set(estados(resp.job).values()) == {"ok"}
    assert paso(resp.job, "docker")["detalle"] == "docker start"


def test_pasos_apagar_en_orden_con_progreso(client, env, docker, binance, pasos_log):  # noqa: F811
    binance.orders = [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}, {"symbol": "ETHUSDT"}]
    binance.algo = [{"symbol": "BTCUSDT", "algoId": 1}]
    binance.add_position("BTCUSDT", "0.5")
    binance.add_position("ETHUSDT", "-2")
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200, resp.data
    job = resp.job
    assert job["accion"] == "apagar"
    assert orden_de_inicio(pasos_log) == claves(bot_control.APAGAR_STEPS)
    assert set(estados(job).values()) == {"ok"}
    assert paso(job, "cancelar_ordenes")["progreso"] == {"actual": 2, "total": 2}
    assert paso(job, "cancelar_ordenes")["detalle"] == "3 órdenes en 2 símbolos."
    assert paso(job, "cancelar_condicionales")["progreso"] == {"actual": 1, "total": 1}
    assert paso(job, "cerrar_posiciones")["progreso"] == {"actual": 2, "total": 2}
    assert paso(job, "verificacion")["detalle"] == "Sin posiciones ni órdenes abiertas en Binance."
    # Progreso intermedio reportado: 1/2 símbolos antes de 2/2.
    assert ("cancelar_ordenes", "en_curso") in pasos_log
    # El resultado es el mismo JSON que devolvía la acción síncrona (con el resumen de Apagar).
    assert resp.get_json()["resumen"]["completo"] is True


def test_apagar_sin_nada_abierto(client, env, docker, binance):  # noqa: F811
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200
    assert set(estados(resp.job).values()) == {"ok"}
    assert paso(resp.job, "cerrar_posiciones")["detalle"] == "No había posiciones abiertas."
    assert ("GET", "/fapi/v1/exchangeInfo") not in binance.paths()


def test_apagar_reintento_resuelto_marca_ok(client, env, docker, binance):  # noqa: F811
    binance.add_position("BTCUSDT", "1")
    binance.fail_orders = 1
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200
    cerrar = paso(resp.job, "cerrar_posiciones")
    assert cerrar["estado"] == "ok" and "Resuelto al reintentar" in cerrar["detalle"]


def test_apagar_testnet_condicionales_omitido(client, app, env, docker, binance):  # noqa: F811
    app.config["BINANCE_TESTNET"] = True
    binance.algo_status = 404
    binance.add_position("BTCUSDT", "1")
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200, resp.data
    assert estados(resp.job)["cancelar_condicionales"] == "omitido"
    assert resp.job["estado"] == "ok"


# ---------------------------------------------------------------- errores en un paso


def test_error_docker_en_start(client, env, docker):  # noqa: F811
    docker.fail_action = 500
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 502
    job = resp.job
    assert job["estado"] == "error"
    assert estados(job) == {"validar": "ok", "backup": "ok", "escribir": "ok", "docker": "error",
                            "verificar": "omitido", "forager": "omitido"}
    assert "500" in paso(job, "docker")["detalle"]
    assert "Se restauro la config" in job["resultado"]["error"]


def test_error_contenedor_inexistente_en_validar(client, env, docker):  # noqa: F811
    docker.exists = False
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 502
    assert estados(resp.job)["validar"] == "error"
    assert estados(resp.job)["backup"] == "omitido"


def test_contenedor_no_queda_corriendo_es_error(client, app, env, docker):  # noqa: F811
    original = docker.request

    def request(method, url, **kwargs):
        resp = original(method, url, **kwargs)
        if method == "POST" and url.endswith("/restart"):
            docker.running = False  # se cayó apenas arrancó
        return resp

    docker.request = request
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 502
    assert estados(resp.job)["verificar"] == "error" and estados(resp.job)["forager"] == "omitido"
    assert "no quedó en ejecución" in resp.get_json()["error"]
    assert resp.get_json()["docker_action"] == "restart"
    assert audit_lines(app)[-1]["result"] == "error"


def test_apagar_falla_al_detener(client, env, docker, binance):  # noqa: F811
    docker.fail_action = 500
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 502
    assert estados(resp.job) == {"detener": "error", "verificar_detenido": "omitido", "cancelar_ordenes": "omitido",
                                 "cancelar_condicionales": "omitido", "cerrar_posiciones": "omitido",
                                 "verificacion": "omitido"}
    assert binance.calls == []


def test_apagar_parcial_verificacion_error(client, env, docker, binance):  # noqa: F811
    binance.add_position("ETHUSDT", "-1")
    binance.stuck.add(("ETHUSDT", "BOTH"))
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 502
    verif = paso(resp.job, "verificacion")
    assert verif["estado"] == "error" and "Quedaron abiertas 1 posiciones" in verif["detalle"]
    assert verif["progreso"] == {"actual": 3, "total": 3}
    assert resp.job["estado"] == "error"
    assert resp.get_json()["resumen"]["restante"]["posiciones"][0]["symbol"] == "ETHUSDT"


# ---------------------------------------------------------------- espera a forager


def test_espera_a_forager_con_cuenta_regresiva(client, env, docker, monkeypatch, pasos_log):  # noqa: F811
    monkeypatch.setenv(bot_control.ENV_FORAGER_WARMUP, "1")
    monkeypatch.setattr(bot_control, "WARMUP_TICK_SECONDS", 0.05)
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 200
    forager = paso(resp.job, "forager")
    assert forager["estado"] == "ok" and forager["progreso"] == {"actual": 1, "total": 1}
    assert pasos_log.count(("forager", "en_curso")) >= 3


def test_forager_se_cae_durante_la_espera():
    fake = FakeDocker(running=False)
    settings = bot_control.Settings()
    settings.container = "client17-passivbot"
    docker_client = bot_control.DockerClient(DOCKER_BASE, session=fake)
    with pytest.raises(bot_control.DockerError, match="se detuvo"):
        bot_control.wait_for_forager(settings, 0, docker=docker_client)


def test_warmup_env(monkeypatch):
    monkeypatch.delenv(bot_control.ENV_FORAGER_WARMUP, raising=False)
    assert bot_control.forager_warmup_seconds() == 90
    for raw, expected in (("30", 30), ("-5", 0), ("abc", 90), ("99999", 900)):
        monkeypatch.setenv(bot_control.ENV_FORAGER_WARMUP, raw)
        assert bot_control.forager_warmup_seconds() == expected


def _esperar_forager(client, job_id):  # noqa: F811
    deadline = time.monotonic() + 5
    while True:
        job = client.get(f"/api/bot/jobs/{job_id}").get_json()
        if estados(job)["forager"] == "en_curso":
            return job
        assert time.monotonic() < deadline, job
        time.sleep(0.01)


def test_apagar_corta_la_espera_a_forager(client, env, docker, binance, monkeypatch):  # noqa: F811
    monkeypatch.setenv(bot_control.ENV_FORAGER_WARMUP, "30")
    monkeypatch.setattr(bot_control, "WARMUP_TICK_SECONDS", 0.02)
    first = post(client, "/api/bot/start", {"riesgo": "bajo"}, wait=False)
    assert first.get_json()["job"]["cancelable"] is False
    job_id = first.get_json()["job_id"]
    start_job = _esperar_forager(client, job_id)
    # La UI usa ``cancelable`` para dejar cerrar el modal y habilitar Apagar.
    assert start_job["cancelable"] is True
    assert client.get("/api/bot/jobs/activo").get_json()["job"]["cancelable"] is True
    binance.add_position("BTCUSDT", "1")
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200, resp.data
    assert resp.job["cancelable"] is False
    start_job = wait_job(client, job_id)
    assert start_job["estado"] == "ok" and start_job["cancelable"] is False
    assert paso(start_job, "forager")["estado"] == "omitido"
    assert docker.actions() == ["restart", "stop"]


def test_apagar_no_espera_a_que_termine_el_start_cortado(client, app, env, docker, binance, monkeypatch):  # noqa: F811
    """Proxy de Docker lento durante la espera: Apagar arranca igual (202), sin 409."""
    monkeypatch.setenv(bot_control.ENV_FORAGER_WARMUP, "30")
    monkeypatch.setattr(bot_control, "WARMUP_TICK_SECONDS", 0.02)
    monkeypatch.setattr(bot_control, "WARMUP_CHECK_SECONDS", 0.05)
    lento = threading.Event()
    soltar = threading.Event()
    en_inspect = threading.Event()
    original = docker.request

    def request(method, url, **kwargs):
        if lento.is_set() and method == "GET" and url.endswith("/json") and threading.current_thread().name == "bot-job":
            if not en_inspect.is_set():
                en_inspect.set()
                assert soltar.wait(10)
        return original(method, url, **kwargs)

    docker.request = request
    data_dir = pathlib.Path(app.config["DATABASE"]).parent
    start_id = post(client, "/api/bot/start", {"riesgo": "bajo"}, wait=False).get_json()["job_id"]
    _esperar_forager(client, start_id)
    lento.set()
    assert en_inspect.wait(5)  # el hilo de START está trabado en docker.inspect
    try:
        t0 = time.monotonic()
        resp = post(client, "/api/bot/stop", {"modo": "apagar"}, wait=False)
        assert resp.status_code == 202, resp.data
        assert time.monotonic() - t0 < jobs.LOCK_WAIT_SECONDS
        apagar_id = resp.get_json()["job_id"]
        # START sigue en curso (trabado) pero ya no bloquea: el activo es Apagar.
        assert client.get(f"/api/bot/jobs/{start_id}").get_json()["estado"] == "en_curso"
        assert client.get("/api/bot/jobs/activo").get_json()["job"]["id"] == apagar_id
        apagar = wait_job(client, apagar_id)
        assert apagar["estado"] == "ok", apagar
    finally:
        soltar.set()
    start_job = wait_job(client, start_id)
    assert start_job["estado"] == "ok" and paso(start_job, "forager")["estado"] == "omitido"
    # El final del START cortado no pisa el persistido del Apagar.
    persisted = json.loads((data_dir / jobs.LAST_JOB_FILE_NAME).read_text())
    assert persisted["id"] == apagar_id and persisted["estado"] == "ok"


def test_panel_tiene_apagar_en_el_modal_de_progreso(client):  # noqa: F811
    html = client.get("/").get_data(as_text=True)
    assert 'data-bot-job="apagar"' in html and 'data-bot-job="cerrar"' in html
    assert "job.cancelable" in html


# ---------------------------------------------------------------- telegram: una vez al terminar


def test_telegram_una_sola_notificacion_al_terminar(client, env, docker, binance, monkeypatch):  # noqa: F811
    llamadas = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action",
                        lambda *a, **k: llamadas.append(a))
    binance.add_position("BTCUSDT", "1")
    binance.orders = [{"symbol": "BTCUSDT"}]
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200
    assert len(llamadas) == 1
    action, params, outcome, payload, user = llamadas[0]
    assert (action, params, outcome, user) == ("stop", {"modo": "apagar"}, "ok", "cliente17")
    assert payload["resumen"]["completo"] is True


# ---------------------------------------------------------------- reinicio: interrumpido


def _app_on(data_dir):
    cfg = Config(CONFIG_DIR=data_dir.parent, DATABASE=data_dir / "futures.db", API_KEY="x", API_SECRET="x",
                 DISABLE_AUTO_SCRAPE=True)
    application = init_app(cfg)
    application.config["TESTING"] = True
    return application


def test_reinicio_marca_interrumpido(client, app, env, docker, monkeypatch):  # noqa: F811
    notificaciones = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action", lambda *a, **k: notificaciones.append(a))
    gate = Gate(docker, "stop")
    data_dir = pathlib.Path(app.config["DATABASE"]).parent
    path = data_dir / jobs.LAST_JOB_FILE_NAME
    try:
        first = post(client, "/api/bot/stop", {"modo": "apagar"}, wait=False)
        job_id = first.get_json()["job_id"]
        assert gate.entered.wait(5)
        snapshot = path.read_text()
        persisted = json.loads(snapshot)
        assert persisted["id"] == job_id and persisted["estado"] == "en_curso"
    finally:
        gate.release.set()
    wait_job(client, job_id)
    # "Reinicio": el proceso murió con el job a mitad (el archivo quedó en_curso y ya nadie tiene
    # los locks) y un proceso nuevo arranca la app sobre el mismo directorio de datos.
    path.write_text(snapshot)
    notificaciones.clear()
    app2 = _app_on(data_dir)
    job = registry(app2).get(job_id)
    assert job["estado"] == "interrumpido"
    assert estados(job)["detener"] == "error" and "se reinició" in paso(job, "detener")["detalle"]
    assert estados(job)["verificacion"] == "omitido"
    assert "Binance" in job["resultado"]["error"]
    assert "usuario" not in job and "pid" not in job
    assert registry(app2).active() is None
    assert audit_lines(app2)[-1]["result"] == "interrumpido"
    assert len(notificaciones) == 1 and notificaciones[0][2] == "error"
    assert notificaciones[0][1] == {"modo": "apagar"}


def test_arranque_con_job_vivo_en_otro_proceso_no_lo_toca(client, app, env, docker, monkeypatch):  # noqa: F811
    """Reload de gunicorn: el worker nuevo arranca mientras el viejo sigue con Apagar (con lock)."""
    notificaciones = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action", lambda *a, **k: notificaciones.append(a[2]))
    gate = Gate(docker, "stop")
    data_dir = pathlib.Path(app.config["DATABASE"]).parent
    try:
        job_id = post(client, "/api/bot/stop", {"modo": "apagar"}, wait=False).get_json()["job_id"]
        assert gate.entered.wait(5)
        app2 = _app_on(data_dir)
        persisted = json.loads((data_dir / jobs.LAST_JOB_FILE_NAME).read_text())
        assert persisted["id"] == job_id and persisted["estado"] == "en_curso"
        assert registry(app2).get(job_id) is None  # en curso en otro proceso: no hay final que mostrar
        assert notificaciones == []
        audit = data_dir / bot_control.AUDIT_LOG_NAME
        assert not audit.exists() or all(line["result"] != "interrumpido" for line in audit_lines(app2))
    finally:
        gate.release.set()
    assert wait_job(client, job_id)["estado"] == "ok"
    assert json.loads((data_dir / jobs.LAST_JOB_FILE_NAME).read_text())["estado"] == "ok"
    assert notificaciones == ["ok"]


def test_arranque_durante_la_espera_a_forager_no_la_marca(client, app, env, docker, binance, monkeypatch):  # noqa: F811
    """La espera a forager corre sin el lock de acciones: igual se detecta que el job sigue vivo."""
    notificaciones = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action", lambda *a, **k: notificaciones.append(a[2]))
    monkeypatch.setenv(bot_control.ENV_FORAGER_WARMUP, "30")
    monkeypatch.setattr(bot_control, "WARMUP_TICK_SECONDS", 0.02)
    data_dir = pathlib.Path(app.config["DATABASE"]).parent
    job_id = post(client, "/api/bot/start", {"riesgo": "bajo"}, wait=False).get_json()["job_id"]
    _esperar_forager(client, job_id)
    handle = bot_control.Store(data_dir).try_lock()
    assert handle is not None  # el lock de acciones está libre durante la espera
    handle.release()
    app2 = _app_on(data_dir)
    assert json.loads((data_dir / jobs.LAST_JOB_FILE_NAME).read_text())["estado"] == "en_curso"
    assert registry(app2).active() is None and notificaciones == []
    assert post(client, "/api/bot/stop", {"modo": "apagar"}).status_code == 200
    assert wait_job(client, job_id)["estado"] == "ok"
    assert sorted(notificaciones) == ["ok", "ok"]


def test_reinicio_sin_job_en_curso_no_hace_nada(tmp_path, monkeypatch):
    llamadas = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action", lambda *a, **k: llamadas.append(a))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / jobs.LAST_JOB_FILE_NAME).write_text(json.dumps({"id": "0123456789abcdef", "estado": "ok"}))
    _app_on(data_dir)
    assert llamadas == []
    assert json.loads((data_dir / jobs.LAST_JOB_FILE_NAME).read_text())["estado"] == "ok"


# ---------------------------------------------------------------- registro: TTL, límite, 404


def test_ttl_y_limite(tmp_path):
    now = [1000.0]
    reg = jobs.JobRegistry(bot_control.Store(tmp_path), clock=lambda: now[0], ttl_seconds=60, max_jobs=3)
    viejo = reg.create("start", {"riesgo": "bajo"}, "u")
    viejo.finish("ok", {"ok": True}, 200)
    assert reg.get(viejo.id)["estado"] == "ok"
    now[0] += 61
    nuevo = reg.create("start", {"riesgo": "bajo"}, "u")
    assert reg.get(viejo.id) is None  # vencido (y el persistido ya es otro)
    assert reg.get(nuevo.id)["estado"] == "en_curso"
    # Límite: se purgan los terminados más viejos, nunca el activo.
    terminados = []
    for _ in range(4):
        j = reg.create("apagar", {"modo": "apagar"}, "u")
        j.finish("error", {"ok": False}, 502)
        terminados.append(j)
    assert len(reg) <= 3 + 1
    assert reg.get(nuevo.id)["estado"] == "en_curso"
    assert reg.get(terminados[-1].id) is not None
    assert reg.get(terminados[0].id) is None


def test_ttl_aplica_al_persistido(tmp_path):
    now = [1000.0]
    reg = jobs.JobRegistry(bot_control.Store(tmp_path), clock=lambda: now[0], ttl_seconds=60)
    job = reg.create("start", {"riesgo": "bajo"}, "u")
    job.finish("ok", {"ok": True}, 200)
    reg2 = jobs.JobRegistry(bot_control.Store(tmp_path), clock=time.time, ttl_seconds=10 ** 9)
    assert reg2.get(job.id)["estado"] == "ok"  # otro proceso lo encuentra en disco
    reg3 = jobs.JobRegistry(bot_control.Store(tmp_path), clock=lambda: time.time() + 10 ** 6, ttl_seconds=60)
    assert reg3.get(job.id) is None


@pytest.mark.parametrize("job_id", ["0123456789abcdef", "no-es-un-id", "../../etc"])
def test_job_inexistente_404(client, job_id):  # noqa: F811
    assert client.get(f"/api/bot/jobs/{job_id}").status_code == 404


def test_activo_sin_jobs(client):  # noqa: F811
    resp = client.get("/api/bot/jobs/activo")
    assert resp.status_code == 200 and resp.get_json() == {"job": None}


def test_porcentaje_avanza():
    job = jobs.Job("apagar", {}, bot_control.APAGAR_STEPS, "u")
    assert job.porcentaje() == 0
    job.step("detener", "ok")
    job.step("verificar_detenido", "ok")
    job.step("cancelar_ordenes", "en_curso", "1/2", 1, 2)
    assert job.porcentaje() == int(2.5 * 100 / 6)
    job.finish("ok", {"ok": True}, 200)
    assert job.porcentaje() == 100
    assert {p["estado"] for p in job.to_dict()["pasos"]} == {"ok", "omitido"}
