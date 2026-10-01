from __future__ import annotations

import json
import pathlib
from unittest import mock

import hjson
import pytest

from futuresboard import bot_control
from futuresboard.app import init_app
from futuresboard.config import Config

FORAGER_HJSON = """{
  // config de prueba
  user: binance_01
  twe_long: 6
  twe_short: 2
  n_longs: 4
  n_shorts: 2
  leverage: 10
  approved_symbols_long: [
    BTCUSDT
  ]
}
"""


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeDocker:
    """Simula la Docker Engine API detras del docker-socket-proxy."""

    def __init__(self, running=True, exists=True):
        self.running = running
        self.exists = exists
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("params")))
        if not self.exists:
            return FakeResponse(404)
        if method == "GET" and url.endswith("/json"):
            status = "running" if self.running else "exited"
            return FakeResponse(200, {"State": {"Status": status, "Running": self.running}})
        action = url.rsplit("/", 1)[-1]
        if action == "start":
            if self.running:
                return FakeResponse(304)
            self.running = True
        elif action == "restart":
            self.running = True
        elif action == "stop":
            if not self.running:
                return FakeResponse(304)
            self.running = False
        return FakeResponse(204)

    def actions(self):
        return [url.rsplit("/", 1)[-1] for m, url, _ in self.calls if m == "POST"]


@pytest.fixture
def forager_cfg(tmp_path):
    cfg_dir = tmp_path / "forager"
    cfg_dir.mkdir()
    path = cfg_dir / "new.json"
    path.write_text(FORAGER_HJSON)
    return path


@pytest.fixture
def env(monkeypatch, forager_cfg):
    monkeypatch.setenv(bot_control.ENV_DOCKER_URL, "http://docker-proxy:2375")
    monkeypatch.setenv(bot_control.ENV_CONTAINER, "client17-passivbot")
    monkeypatch.setenv(bot_control.ENV_FORAGER_CONFIG, str(forager_cfg))
    monkeypatch.setenv(bot_control.ENV_MODES_SUPPORTED, "1")
    return forager_cfg


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(bot_control.requests, "Session", lambda: fake)
    return fake


@pytest.fixture
def app(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = Config(
        CONFIG_DIR=tmp_path,
        DATABASE=data_dir / "futures.db",
        API_KEY="x",
        API_SECRET="x",
        DISABLE_AUTO_SCRAPE=True,
    )
    with mock.patch("futuresboard.app._get_server_ip", return_value="127.0.0.1"):
        application = init_app(cfg)
        application.config["TESTING"] = True
        yield application


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["username"] = "cliente17"
        sess["csrf_token"] = "tok"
    return c


def post(client, url, body, token="tok"):
    headers = {"X-CSRF-Token": token} if token is not None else {}
    return client.post(url, data=json.dumps(body), content_type="application/json", headers=headers)


def audit_lines(app):
    path = pathlib.Path(app.config["DATABASE"]).parent / bot_control.AUDIT_LOG_NAME
    return [json.loads(line) for line in path.read_text().splitlines()]


# ---------------------------------------------------------------- auth / csrf


@pytest.mark.parametrize(
    "method,url",
    [("get", "/api/bot/status"), ("post", "/api/bot/start"), ("post", "/api/bot/stop")],
)
def test_auth_required(app, env, docker, method, url):
    resp = getattr(app.test_client(), method)(url)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert docker.calls == []


@pytest.mark.parametrize("token", [None, "", "otro"])
def test_csrf_required(client, env, docker, token):
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"}, token=token)
    assert resp.status_code == 403
    resp = post(client, "/api/bot/stop", {"modo": "apagar"}, token=token)
    assert resp.status_code == 403
    assert docker.calls == []
    assert hjson.loads(env.read_text())["twe_long"] == 6


def test_csrf_requires_json(client, env, docker):
    resp = client.post("/api/bot/start", data={"riesgo": "bajo"}, headers={"X-CSRF-Token": "tok"})
    assert resp.status_code == 415
    assert docker.calls == []


def test_mutations_are_post_only(client, env, docker):
    assert client.get("/api/bot/start").status_code == 405
    assert client.get("/api/bot/stop").status_code == 405


def test_old_admin_proxy_removed(client):
    assert client.get("/api/admin/slctdaeo").status_code == 404
    assert client.post("/api/admin/stop_cu").status_code in (404, 405)
    assert client.get("/api/bot/guardian/disabled").status_code == 404


def test_csrf_meta_rendered(client, env, docker):
    with mock.patch("futuresboard.blueprint.requests.get", side_effect=Exception("offline")):
        resp = client.get("/settings")
    assert b'name="csrf-token" content="tok"' in resp.data


# ---------------------------------------------------------------- enum validation


@pytest.mark.parametrize("riesgo", ["", "BAJO", "extremo", None, 4, ["bajo"], "bajo "])
def test_start_rejects_invalid_riesgo(client, env, docker, riesgo):
    resp = post(client, "/api/bot/start", {"riesgo": riesgo})
    assert resp.status_code == 400
    assert docker.actions() == []
    assert env.read_text() == FORAGER_HJSON


@pytest.mark.parametrize("modo", ["", "GRACEFUL", "panic", None, 1])
def test_stop_rejects_invalid_modo(client, env, docker, modo):
    resp = post(client, "/api/bot/stop", {"modo": modo})
    assert resp.status_code == 400
    assert docker.actions() == []


def test_invalid_body(client, env, docker):
    resp = post(client, "/api/bot/start", ["bajo"])
    assert resp.status_code == 400


# ---------------------------------------------------------------- start / stop


@pytest.mark.parametrize("riesgo,tl,ts", [("bajo", 4, 1), ("medio", 6, 2), ("alto", 8, 3)])
def test_start_writes_preset_and_restarts(client, app, env, docker, riesgo, tl, ts):
    resp = post(client, "/api/bot/start", {"riesgo": riesgo})
    assert resp.status_code == 200, resp.data
    cfg = hjson.loads(env.read_text())
    assert (cfg["twe_long"], cfg["twe_short"]) == (tl, ts)
    assert cfg["long_mode"] == "normal"
    assert cfg["short_mode"] == "normal"
    # resto de claves preservadas
    assert cfg["user"] == "binance_01"
    assert cfg["n_longs"] == 4
    assert cfg["approved_symbols_long"] == ["BTCUSDT"]
    assert docker.actions() == ["restart"]
    entry = audit_lines(app)[-1]
    assert entry["user"] == "cliente17"
    assert entry["action"] == "start"
    assert entry["params"] == {"riesgo": riesgo}
    assert entry["result"] == "ok"


def test_start_when_stopped_uses_start(client, env, docker):
    docker.running = False
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 200
    assert docker.actions() == ["start"]


def test_stop_apagar(client, app, env, docker):
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200
    assert "sin gestion" in resp.get_json()["warning"]
    assert docker.actions() == ["stop"]
    assert env.read_text() == FORAGER_HJSON  # apagar no toca la config
    assert audit_lines(app)[-1]["params"] == {"modo": "apagar"}


def test_stop_graceful_then_start_restores_short_mode(client, env, docker):
    cfg = hjson.loads(env.read_text())
    cfg["short_mode"] = "normal"
    env.write_text(hjson.dumps(cfg))

    resp = post(client, "/api/bot/stop", {"modo": "graceful"})
    assert resp.status_code == 200, resp.data
    cfg = hjson.loads(env.read_text())
    assert cfg["long_mode"] == cfg["short_mode"] == "graceful_stop"
    assert cfg["twe_long"] == 6  # graceful no toca la exposicion
    assert docker.actions() == ["restart"]

    resp = post(client, "/api/bot/start", {"riesgo": "medio"})
    assert resp.status_code == 200
    cfg = hjson.loads(env.read_text())
    assert cfg["long_mode"] == "normal"
    assert cfg["short_mode"] == "normal"


def test_start_keeps_non_stop_short_mode(client, env, docker):
    cfg = hjson.loads(env.read_text())
    cfg["short_mode"] = "tp_only"
    env.write_text(hjson.dumps(cfg))
    assert post(client, "/api/bot/start", {"riesgo": "alto"}).status_code == 200
    assert hjson.loads(env.read_text())["short_mode"] == "tp_only"


def test_graceful_disabled_without_modes_support(client, env, docker, monkeypatch):
    monkeypatch.delenv(bot_control.ENV_MODES_SUPPORTED)
    resp = post(client, "/api/bot/stop", {"modo": "graceful"})
    assert resp.status_code == 409
    assert docker.actions() == []
    assert env.read_text() == FORAGER_HJSON


def test_docker_error_returns_502(client, app, env, monkeypatch):
    import requests

    class Broken:
        def request(self, *a, **k):
            raise requests.ConnectionError("boom")

    monkeypatch.setattr(bot_control.requests, "Session", lambda: Broken())
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 502
    assert audit_lines(app)[-1]["result"] == "error"


def test_container_not_found(client, env, docker):
    docker.exists = False
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 502


# ---------------------------------------------------------------- status


def test_status(client, env, docker):
    resp = client.get("/api/bot/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["container"]["status"] == "running"
    assert data["container"]["running"] is True
    assert data["riesgo"] == "medio"
    assert data["long_mode"] == "normal"
    assert data["short_mode"] == "normal"
    assert docker.calls[0][1] == "http://docker-proxy:2375/containers/client17-passivbot/json"


def test_status_custom_preset(client, env, docker):
    env.write_text(FORAGER_HJSON.replace("twe_long: 6", "twe_long: 1.6"))
    assert client.get("/api/bot/status").get_json()["riesgo"] == "personalizado"


def test_panel_disabled_without_env(client, monkeypatch, docker):
    monkeypatch.delenv(bot_control.ENV_DOCKER_URL, raising=False)
    resp = client.get("/api/bot/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is False
    assert bot_control.ENV_DOCKER_URL in data["message"]
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 503
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 503
    assert docker.calls == []


def test_home_renders_without_env(client, monkeypatch):
    monkeypatch.delenv(bot_control.ENV_DOCKER_URL, raising=False)
    with mock.patch("futuresboard.blueprint.requests.get", side_effect=Exception("offline")):
        resp = client.get("/")
    assert resp.status_code == 200
    assert b'id="menu_admin"' in resp.data
    assert b"slctdaeo" not in resp.data
    assert b"guardian" not in resp.data.lower()


# ---------------------------------------------------------------- escritura HJSON


def test_write_preserves_keys_and_backup(forager_cfg):
    backup = bot_control.write_forager_config(forager_cfg, {"twe_long": 8, "long_mode": "normal"})
    assert backup.exists()
    assert backup.read_text() == FORAGER_HJSON  # backup exacto, con comentarios
    assert backup.parent == forager_cfg.parent
    cfg = hjson.loads(forager_cfg.read_text())
    assert cfg["twe_long"] == 8
    assert cfg["long_mode"] == "normal"
    assert cfg["twe_short"] == 2
    assert cfg["user"] == "binance_01"
    assert list(cfg)[:3] == ["user", "twe_long", "twe_short"]  # orden preservado
    assert not list(forager_cfg.parent.glob("*.tmp"))


def test_write_keeps_strict_json_as_json(tmp_path):
    path = tmp_path / "new.json"
    path.write_text(json.dumps({"user": "u", "twe_long": 1, "twe_short": 1}))
    bot_control.write_forager_config(path, {"twe_long": 4})
    assert json.loads(path.read_text())["twe_long"] == 4


def test_write_invalid_original_not_replaced(tmp_path):
    path = tmp_path / "new.json"
    path.write_text("{ user: [ ")
    with pytest.raises(bot_control.ConfigError):
        bot_control.write_forager_config(path, {"twe_long": 4})
    assert path.read_text() == "{ user: [ "


def test_write_validation_failure_keeps_original(forager_cfg, monkeypatch):
    real_loads = hjson.loads
    calls = {"n": 0}

    def flaky_loads(text, *a, **k):
        calls["n"] += 1
        data = real_loads(text, *a, **k)
        if calls["n"] == 2:  # la relectura del tmp
            data["twe_long"] = 999
        return data

    monkeypatch.setattr(bot_control.hjson, "loads", flaky_loads)
    with pytest.raises(bot_control.ConfigError):
        bot_control.write_forager_config(forager_cfg, {"twe_long": 4})
    assert forager_cfg.read_text() == FORAGER_HJSON
    assert not list(forager_cfg.parent.glob(".*.tmp"))


def test_backups_pruned(forager_cfg, monkeypatch):
    monkeypatch.setattr(bot_control, "MAX_BACKUPS", 3)
    for i in range(5):
        bot_control.write_forager_config(forager_cfg, {"twe_long": i})
    assert len(list(forager_cfg.parent.glob("new.json.bak-*"))) == 3


# ---------------------------------------------------------------- cliente docker


def test_docker_client_endpoints():
    fake = FakeDocker(running=False)
    dc = bot_control.DockerClient("http://docker-proxy:2375/", session=fake)
    assert dc.start("client17-passivbot") == "ok"
    assert dc.start("client17-passivbot") == "sin_cambios"
    assert dc.restart("client17-passivbot") == "ok"
    assert dc.stop("client17-passivbot") == "ok"
    assert dc.stop("client17-passivbot") == "sin_cambios"
    urls = [(m, u, p) for m, u, p in fake.calls]
    assert urls[0] == ("POST", "http://docker-proxy:2375/containers/client17-passivbot/start", None)
    assert urls[2] == ("POST", "http://docker-proxy:2375/containers/client17-passivbot/restart",
                       {"t": bot_control.STOP_TIMEOUT_SECONDS})
    assert urls[3][1].endswith("/containers/client17-passivbot/stop")


@pytest.mark.parametrize("name", ["../images", "a/b", "", "x?all=1"])
def test_docker_client_rejects_bad_names(name):
    dc = bot_control.DockerClient("http://docker-proxy:2375", session=FakeDocker())
    with pytest.raises(bot_control.DockerError):
        dc.stop(name)


def test_detect_preset():
    assert bot_control.detect_preset({"twe_long": 4, "twe_short": 1}) == "bajo"
    assert bot_control.detect_preset({"twe_long": 6.0, "twe_short": 2.0}) == "medio"
    assert bot_control.detect_preset({"twe_long": 8, "twe_short": 3}) == "alto"
    assert bot_control.detect_preset({"twe_long": 8, "twe_short": 1}) == "personalizado"
    assert bot_control.detect_preset({}) == "desconocido"
