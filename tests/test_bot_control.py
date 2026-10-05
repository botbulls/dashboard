from __future__ import annotations

import json
import pathlib
import sqlite3
from unittest import mock

import hjson
import pytest

from futuresboard import auth
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
        self._payload = {} if payload is None else payload

    def json(self):
        return self._payload


class FakeTextResponse:
    """200 sin JSON (proxy, WAF o página de mantenimiento)."""

    def __init__(self, status_code=200):
        self.status_code = status_code

    def json(self):
        raise ValueError("not json")


class FakeDocker:
    """Simula la Docker Engine API detras del docker-socket-proxy."""

    def __init__(self, running=True, exists=True):
        self.running = running
        self.exists = exists
        self.calls = []
        self.fail_action = None      # codigo HTTP con el que fallan start/restart/stop
        self.timeout_action = False  # start/restart/stop sin respuesta (timeout)
        self.started_at = None

    def request(self, method, url, **kwargs):
        import requests

        self.calls.append((method, url, kwargs.get("params")))
        if not self.exists:
            return FakeResponse(404)
        if method == "GET" and url.endswith("/json"):
            status = "running" if self.running else "exited"
            state = {"Status": status, "Running": self.running}
            if self.started_at:
                state["StartedAt"] = self.started_at
            return FakeResponse(200, {"State": state})
        if self.timeout_action:
            raise requests.ReadTimeout("timeout")
        if self.fail_action:
            return FakeResponse(self.fail_action)
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
    monkeypatch.setenv(bot_control.ENV_DOCKER_URL, DOCKER_BASE)
    monkeypatch.setenv(bot_control.ENV_CONTAINER, "client17-passivbot")
    monkeypatch.setenv(bot_control.ENV_FORAGER_CONFIG, str(forager_cfg))
    monkeypatch.setenv(bot_control.ENV_MODES_SUPPORTED, "1")
    return forager_cfg


DOCKER_BASE = "http://docker-proxy:2375"
BINANCE_BASE = "https://fapi.binance.com"


class FakeBinance:
    """Simula Binance USDⓈ-M Futures: posiciones, órdenes normales/algo, exchangeInfo y MARKET."""

    def __init__(self):
        self.dual = False
        self.positions = {}       # (symbol, positionSide) -> Decimal
        self.orders = []          # [{"symbol": ...}]
        self.algo = []            # [{"symbol": ..., "algoId": ...}]
        self.filters = {}         # symbol -> (step, min, max) de MARKET_LOT_SIZE
        self.lot_size = {}        # symbol -> (step, min, max) de LOT_SIZE (si no, igual a MARKET)
        self.fail_orders = 0      # las primeras N órdenes MARKET fallan con -1001
        self.stuck = set()        # (symbol, positionSide) cuyas órdenes "se llenan" sin reducir
        self.algo_status = None   # código HTTP para GET openAlgoOrders (ej. 404)
        self.positions_status = None
        self.non_json = set()     # paths que responden 200 con HTML/texto (no JSON)
        self.as_dict = set()      # paths que responden 200 con un objeto JSON en vez de lista
        self.calls = []           # (method, path, params, headers)

    def add_position(self, symbol, amt, side=None):
        from decimal import Decimal

        side = side or ("BOTH" if not self.dual else ("LONG" if Decimal(amt) > 0 else "SHORT"))
        self.positions[(symbol, side)] = Decimal(amt)
        self.filters.setdefault(symbol, ("0.001", "0.001", "1000"))

    def market_orders(self):
        return [p for m, path, p, _ in self.calls if m == "POST" and path == "/fapi/v1/order"]

    def paths(self):
        return [(m, path) for m, path, _, _ in self.calls]

    def request(self, method, url, **kwargs):
        from decimal import Decimal
        from urllib.parse import parse_qsl, urlparse

        u = urlparse(url)
        params = dict(parse_qsl(u.query))
        self.calls.append((method, u.path, params, kwargs.get("headers") or {}))
        key = (method, u.path)
        if u.path in self.non_json:
            return FakeTextResponse(200)
        if u.path in self.as_dict:
            return FakeResponse(200, {"symbol": "BTCUSDT", "positionAmt": "1"})
        if key == ("GET", "/fapi/v1/openOrders"):
            return FakeResponse(200, list(self.orders))
        if key == ("GET", "/fapi/v1/openAlgoOrders"):
            if self.algo_status:
                return FakeResponse(self.algo_status, {"code": -5000, "msg": "Path not found"})
            return FakeResponse(200, list(self.algo))
        if key == ("DELETE", "/fapi/v1/allOpenOrders"):
            self.orders = [o for o in self.orders if o["symbol"] != params["symbol"]]
            return FakeResponse(200, {"code": 200, "msg": "The operation of cancel all open order is done."})
        if key == ("DELETE", "/fapi/v1/algoOpenOrders"):
            self.algo = [o for o in self.algo if o["symbol"] != params["symbol"]]
            return FakeResponse(200, {"code": 200, "msg": "success"})
        if key == ("GET", "/fapi/v2/positionRisk"):
            if self.positions_status:
                return FakeResponse(self.positions_status, {"code": -1001, "msg": "Internal error"})
            rows = [{"symbol": s, "positionSide": ps, "positionAmt": str(a)} for (s, ps), a in self.positions.items()]
            rows.append({"symbol": "LTCUSDT", "positionSide": "BOTH", "positionAmt": "0.000"})
            return FakeResponse(200, rows)
        if key == ("GET", "/fapi/v1/exchangeInfo"):
            symbols = []
            for sym, (step, mn, mx) in self.filters.items():
                lstep, lmn, lmx = self.lot_size.get(sym, (step, mn, mx))
                symbols.append({"symbol": sym, "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": lstep, "minQty": lmn, "maxQty": lmx},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": step, "minQty": mn, "maxQty": mx},
                ]})
            return FakeResponse(200, {"symbols": symbols})
        if key == ("POST", "/fapi/v1/order"):
            if self.fail_orders:
                self.fail_orders -= 1
                return FakeResponse(400, {"code": -1001, "msg": "Internal error; unable to process your request."})
            symbol, qty = params["symbol"], Decimal(params["quantity"])
            if self.dual:
                if "reduceOnly" in params or params.get("positionSide") not in ("LONG", "SHORT"):
                    return FakeResponse(400, {"code": -1106, "msg": "Parameter 'reduceonly' sent when not required."})
                pside = params["positionSide"]
            else:
                if params.get("reduceOnly") != "true" or "positionSide" in params:
                    return FakeResponse(400, {"code": -1106, "msg": "bad params one-way"})
                pside = "BOTH"
            step, mn, mx = (Decimal(x) for x in self.filters[symbol])
            if step == 0:  # MARKET_LOT_SIZE sin step: Binance aplica LOT_SIZE
                step, mn, mx = (Decimal(x) for x in self.lot_size[symbol])
            if qty % step != 0 or qty < mn or qty > mx:
                return FakeResponse(400, {"code": -1111, "msg": "Precision is over the maximum defined for this asset."})
            amt = self.positions.get((symbol, pside), Decimal(0))
            if (symbol, pside) not in self.stuck:
                amt = amt - qty if params["side"] == "SELL" else amt + qty
                self.positions[(symbol, pside)] = amt
                if amt == 0:
                    del self.positions[(symbol, pside)]
            return FakeResponse(200, {"symbol": symbol, "status": "FILLED", "executedQty": str(qty)})
        return FakeResponse(404, {"code": -5000, "msg": "Path not found"})


class Router:
    """requests.Session falso: enruta al docker-proxy o a Binance según la URL."""

    def __init__(self, docker, binance):
        self.docker = docker
        self.binance = binance
        self.log = []

    def request(self, method, url, **kwargs):
        target = "docker" if url.startswith(DOCKER_BASE) else "binance"
        self.log.append(target)
        return getattr(self, target).request(method, url, **kwargs)


@pytest.fixture
def router(monkeypatch):
    r = Router(FakeDocker(), FakeBinance())
    monkeypatch.setattr(bot_control.requests, "Session", lambda: r)
    monkeypatch.setattr(bot_control, "ROUND_PAUSE_SECONDS", 0)
    return r


@pytest.fixture
def docker(router):
    return router.docker


@pytest.fixture
def binance(router):
    return router.binance


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
    # Ya no hay usuario sembrado: se crea el de la sesión de prueba.
    auth.set_user_password(str(app.config["DATABASE"]), "cliente17", "clave-de-test")
    with sqlite3.connect(str(app.config["DATABASE"])) as conn:
        pw_hash = conn.execute(
            "SELECT password_hash FROM users WHERE username = 'cliente17'"
        ).fetchone()[0]
    with app.app_context():
        fingerprint = auth.password_fingerprint(pw_hash)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["username"] = "cliente17"
        sess[auth.SESSION_FINGERPRINT_KEY] = fingerprint
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


def test_stop_apagar(client, app, env, docker, binance):
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["resumen"]["completo"] is True
    assert data["resumen"]["rondas"] == 0  # no había nada abierto
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


# ---------------------------------------------------------------- config vs accion docker


def state_path(app):
    return pathlib.Path(app.config["DATABASE"]).parent / bot_control.STATE_FILE_NAME


def test_start_container_missing_keeps_config(client, app, env, docker):
    original = env.read_text()
    docker.exists = False
    resp = post(client, "/api/bot/start", {"riesgo": "alto"})
    assert resp.status_code == 502
    assert env.read_text() == original
    assert list(env.parent.glob("new.json.bak-*")) == []
    assert docker.actions() == []
    assert "No se modifico la config" in audit_lines(app)[-1]["detail"]


def test_start_proxy_down_keeps_config(client, env, monkeypatch):
    import requests

    class Broken:
        def request(self, *a, **k):
            raise requests.ConnectionError("boom")

    original = env.read_text()
    monkeypatch.setattr(bot_control.requests, "Session", lambda: Broken())
    assert post(client, "/api/bot/start", {"riesgo": "alto"}).status_code == 502
    assert env.read_text() == original


@pytest.mark.parametrize("running", [True, False])
def test_start_docker_rejects_restores_config(client, app, env, docker, running):
    docker.running = running
    original = env.read_text()
    original_mtime = env.stat().st_mtime
    docker.fail_action = 500
    resp = post(client, "/api/bot/start", {"riesgo": "alto"})
    assert resp.status_code == 502
    assert "Se restauro la config" in resp.get_json()["error"]
    assert env.read_text() == original
    assert env.stat().st_mtime == original_mtime
    entry = audit_lines(app)[-1]
    assert entry["result"] == "error" and "Se restauro" in entry["detail"]


def test_graceful_docker_rejects_restores_config_and_state(client, app, env, docker):
    env.write_text(FORAGER_HJSON.replace("n_longs: 4", "n_longs: 4\n  short_mode: tp_only"))
    original = env.read_text()
    docker.fail_action = 500
    resp = post(client, "/api/bot/stop", {"modo": "graceful"})
    assert resp.status_code == 502
    assert env.read_text() == original
    assert not state_path(app).exists() or "short_mode_before_stop" not in json.loads(state_path(app).read_text())


def test_start_after_graceful_rejected_keeps_saved_short_mode(client, app, env, docker):
    env.write_text(FORAGER_HJSON.replace("n_longs: 4", "n_longs: 4\n  short_mode: tp_only"))
    assert post(client, "/api/bot/stop", {"modo": "graceful"}).status_code == 200
    docker.fail_action = 500
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 502
    # el rollback restaura el estado: short_mode_before_stop sigue guardado
    assert json.loads(state_path(app).read_text())["short_mode_before_stop"] == "tp_only"
    assert hjson.loads(env.read_text())["short_mode"] == "graceful_stop"
    docker.fail_action = None
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 200
    assert hjson.loads(env.read_text())["short_mode"] == "tp_only"


def test_start_docker_timeout_keeps_written_config(client, app, env, docker):
    docker.timeout_action = True
    resp = post(client, "/api/bot/start", {"riesgo": "alto"})
    assert resp.status_code == 502
    error = resp.get_json()["error"]
    assert "Resultado incierto" in error and "new.json.bak-" in error
    data = hjson.loads(env.read_text())
    assert (data["twe_long"], data["twe_short"]) == (8, 3)


# ---------------------------------------------------------------- pendiente de reinicio


def test_parse_docker_time():
    t = bot_control.parse_docker_time("2026-10-01T12:00:00.123456789Z")
    assert t.isoformat() == "2026-10-01T12:00:00.123456+00:00"
    assert bot_control.parse_docker_time("2026-10-01T12:00:00Z").second == 0
    assert bot_control.parse_docker_time("0001-01-01T00:00:00Z") is None
    assert bot_control.parse_docker_time("basura") is None
    assert bot_control.parse_docker_time(None) is None


def test_status_config_pending(client, env, docker):
    import os

    docker.started_at = "2026-10-01T12:00:00.000000000Z"
    started = bot_control.parse_docker_time(docker.started_at).timestamp()
    os.utime(env, (started + 60, started + 60))
    assert client.get("/api/bot/status").get_json()["config_pending"] is True
    os.utime(env, (started - 60, started - 60))
    assert client.get("/api/bot/status").get_json()["config_pending"] is False
    docker.running = False
    assert client.get("/api/bot/status").get_json()["config_pending"] is None


def test_status_without_started_at_pending_unknown(client, env, docker):
    assert client.get("/api/bot/status").get_json()["config_pending"] is None


# ---------------------------------------------------------------- apagar: cierre en Binance


def apagar(client):
    return post(client, "/api/bot/stop", {"modo": "apagar"})


def test_apagar_one_way_cancela_y_cierra(client, app, env, router, docker, binance):
    binance.add_position("BTCUSDT", "0.5")
    binance.add_position("ETHUSDT", "-2")
    binance.orders = [{"symbol": "BTCUSDT"}, {"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]
    binance.algo = [{"symbol": "XRPUSDT", "algoId": 1}]

    resp = apagar(client)
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    r = data["resumen"]
    assert r["completo"] is True and r["rondas"] == 1
    assert {(o["symbol"], o["tipo"], o["cantidad"]) for o in r["ordenes_canceladas"]} == {
        ("BTCUSDT", "normal", 2), ("ETHUSDT", "normal", 1), ("XRPUSDT", "algo", 1)}
    closed = {(p["symbol"], p["lado"], p["modo"], p["cantidad"]) for p in r["posiciones_cerradas"]}
    assert closed == {("BTCUSDT", "LONG", "one-way", "0.5"), ("ETHUSDT", "SHORT", "one-way", "2")}
    orders = binance.market_orders()
    assert {(o["symbol"], o["side"], o["quantity"]) for o in orders} == {("BTCUSDT", "SELL", "0.5"), ("ETHUSDT", "BUY", "2")}
    assert all(o["reduceOnly"] == "true" and "positionSide" not in o and o["type"] == "MARKET" for o in orders)
    assert binance.positions == {} and binance.orders == [] and binance.algo == []
    # Primero se detiene el contenedor; recién después se toca Binance. Órdenes antes que posiciones.
    assert router.log[:2] == ["docker", "docker"] and "docker" not in router.log[2:]
    paths = binance.paths()
    assert paths.index(("DELETE", "/fapi/v1/allOpenOrders")) < paths.index(("POST", "/fapi/v1/order"))


def test_apagar_hedge_ambos_lados_mismo_simbolo(client, env, docker, binance):
    binance.dual = True
    binance.add_position("BTCUSDT", "1.2", "LONG")
    binance.add_position("BTCUSDT", "-0.3", "SHORT")
    resp = apagar(client)
    assert resp.status_code == 200, resp.data
    orders = binance.market_orders()
    assert {(o["side"], o["positionSide"], o["quantity"]) for o in orders} == {
        ("SELL", "LONG", "1.2"), ("BUY", "SHORT", "0.3")}
    assert all("reduceOnly" not in o for o in orders)
    lados = {(p["lado"], p["modo"]) for p in resp.get_json()["resumen"]["posiciones_cerradas"]}
    assert lados == {("LONG", "hedge"), ("SHORT", "hedge")}


def test_apagar_parte_por_max_qty(client, env, docker, binance):
    binance.add_position("DOGEUSDT", "-250")
    binance.filters["DOGEUSDT"] = ("1", "1", "100")
    resp = apagar(client)
    assert resp.status_code == 200, resp.data
    assert [o["quantity"] for o in binance.market_orders()] == ["100", "100", "50"]
    cerrada = resp.get_json()["resumen"]["posiciones_cerradas"][0]
    assert cerrada["cantidad"] == "250" and cerrada["ordenes"] == 3


def test_apagar_usa_lot_size_si_market_lot_size_es_cero(client, env, docker, binance):
    binance.add_position("ETHUSDT", "3")
    binance.filters["ETHUSDT"] = ("0", "0", "0")
    binance.lot_size["ETHUSDT"] = ("0.01", "0.01", "2")
    resp = apagar(client)
    assert resp.status_code == 200, resp.data
    assert [o["quantity"] for o in binance.market_orders()] == ["2", "1"]


def test_apagar_redondea_hacia_abajo_y_reporta_residuo(client, env, docker, binance):
    binance.add_position("BTCUSDT", "0.12345")
    resp = apagar(client)
    assert resp.status_code == 502
    data = resp.get_json()
    assert data["ok"] is False
    assert {o["quantity"] for o in binance.market_orders()} == {"0.123"}
    restante = data["resumen"]["restante"]
    assert restante["posiciones"] == [{"symbol": "BTCUSDT", "lado": "LONG", "modo": "one-way", "cantidad": "0.00045"}]
    assert any("menor al mínimo" in e for e in data["resumen"]["errores"])


def test_apagar_fallo_al_detener_no_cierra_nada(client, app, env, docker, binance):
    binance.add_position("BTCUSDT", "1")
    binance.orders = [{"symbol": "BTCUSDT"}]
    docker.fail_action = 500
    resp = apagar(client)
    assert resp.status_code == 502
    assert binance.calls == []
    assert audit_lines(app)[-1]["result"] == "error"


def test_apagar_timeout_al_detener_no_cierra_nada(client, env, docker, binance):
    binance.add_position("BTCUSDT", "1")
    docker.timeout_action = True
    assert apagar(client).status_code == 502
    assert binance.calls == []


def test_apagar_contenedor_sigue_corriendo_no_cierra(client, env, docker, binance):
    binance.add_position("BTCUSDT", "1")
    orig = docker.request

    def stop_sin_efecto(method, url, **kw):
        resp = orig(method, url, **kw)
        docker.running = True
        return resp

    docker.request = stop_sin_efecto
    resp = apagar(client)
    assert resp.status_code == 502
    assert "sigue corriendo" in resp.get_json()["error"]
    assert binance.calls == []


def test_apagar_contenedor_inexistente_no_cierra(client, env, docker, binance):
    docker.exists = False
    binance.add_position("BTCUSDT", "1")
    assert apagar(client).status_code == 502
    assert binance.calls == []


def test_apagar_cierre_parcial_502(client, app, env, docker, binance):
    binance.add_position("BTCUSDT", "1")
    binance.add_position("ETHUSDT", "-1")
    binance.stuck.add(("ETHUSDT", "BOTH"))
    resp = apagar(client)
    assert resp.status_code == 502
    data = resp.get_json()
    assert "parcial" in data["error"]
    r = data["resumen"]
    assert r["completo"] is False and r["rondas"] == bot_control.CLOSE_ROUNDS
    assert r["restante"]["verificado"] is True
    assert r["restante"]["posiciones"] == [{"symbol": "ETHUSDT", "lado": "SHORT", "modo": "one-way", "cantidad": "1"}]
    assert data["docker_action"] == "stop"
    # BTC se cerró una sola vez; ETH se reintentó en cada ronda.
    eth = [o for o in binance.market_orders() if o["symbol"] == "ETHUSDT"]
    assert len(eth) == bot_control.CLOSE_ROUNDS
    last = audit_lines(app)[-1]
    assert last["result"] == "error" and "ETHUSDT" in last["detail"] and "resumen" in last["detail"]


def test_apagar_reintenta_si_falla_una_orden(client, env, docker, binance):
    binance.add_position("BTCUSDT", "1")
    binance.fail_orders = 1
    resp = apagar(client)
    assert resp.status_code == 200, resp.data
    r = resp.get_json()["resumen"]
    assert r["rondas"] == 2
    assert len(binance.market_orders()) == 2
    assert any("-1001" in e for e in r["errores"])


def test_apagar_reintenta_ordenes_que_reaparecen(client, env, docker, binance):
    binance.orders = [{"symbol": "BTCUSDT"}]
    orig = binance.request
    state = {"n": 0}

    def reaparece(method, url, **kw):
        resp = orig(method, url, **kw)
        if method == "DELETE" and state["n"] == 0:
            state["n"] = 1
            binance.orders.append({"symbol": "BTCUSDT"})
        return resp

    binance.request = reaparece
    resp = apagar(client)
    assert resp.status_code == 200
    assert resp.get_json()["resumen"]["rondas"] == 2


def test_apagar_algo_404_en_produccion_es_502(client, app, env, docker, binance):
    # En producción el endpoint de órdenes algo existe: un 404 es mala config (URL/proxy), no
    # "no hay órdenes condicionales". No se puede anunciar un cierre total.
    binance.algo_status = 404
    binance.add_position("BTCUSDT", "1")
    resp = apagar(client)
    assert resp.status_code == 502, resp.data
    r = resp.get_json()["resumen"]
    assert r["completo"] is False and r["restante"]["verificado"] is False
    assert any("órdenes condicionales" in e for e in r["errores"])
    assert binance.positions == {}  # las posiciones se cierran igual
    # Se reintenta leer en cada ronda (no se "apaga" la verificación tras el primer 404).
    algo_reads = [p for m, p in binance.paths() if p == "/fapi/v1/openAlgoOrders"]
    assert len(algo_reads) == bot_control.CLOSE_ROUNDS + 1
    assert audit_lines(app)[-1]["result"] == "error"


def test_apagar_algo_404_en_testnet_se_omite_y_avisa(client, app, env, docker, binance):
    app.config["BINANCE_TESTNET"] = True
    binance.algo_status = 404
    binance.add_position("BTCUSDT", "1")
    resp = apagar(client)
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["resumen"]["algo_verificado"] is False
    assert "condicionales" in data["warning"]
    assert any("demo/testnet" in e for e in data["resumen"]["errores"])
    assert [p for m, p in binance.paths() if p == "/fapi/v1/openAlgoOrders"] == ["/fapi/v1/openAlgoOrders"]


def test_apagar_normal_algo_verificado(client, env, docker, binance):
    resp = apagar(client)
    assert resp.get_json()["resumen"]["algo_verificado"] is True
    assert "warning" not in resp.get_json()


@pytest.mark.parametrize("path", ["/fapi/v2/positionRisk", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"])
def test_apagar_200_sin_json_no_es_cierre_total(client, env, docker, binance, path):
    binance.add_position("BTCUSDT", "1")
    binance.non_json.add(path)
    resp = apagar(client)
    assert resp.status_code == 502, resp.data
    r = resp.get_json()["resumen"]
    assert r["completo"] is False and r["restante"]["verificado"] is False
    assert any("sin JSON" in e for e in r["errores"])


def test_cliente_200_sin_json_en_todo_no_da_falso_ok():
    class AlwaysHtml:
        def request(self, *a, **k):
            return FakeTextResponse(200)

    client = bot_control.BinanceFuturesClient(BINANCE_BASE, "k", "s", session=AlwaysHtml())
    old = bot_control.ROUND_PAUSE_SECONDS
    bot_control.ROUND_PAUSE_SECONDS = 0
    try:
        r = bot_control.close_all_futures(client)
    finally:
        bot_control.ROUND_PAUSE_SECONDS = old
    assert r["completo"] is False and r["restante"]["verificado"] is False


@pytest.mark.parametrize("path", ["/fapi/v2/positionRisk", "/fapi/v1/openOrders"])
def test_apagar_objeto_en_vez_de_lista_es_502_con_resumen(client, app, env, docker, binance, path):
    binance.orders = [{"symbol": "ETHUSDT"}]
    binance.add_position("BTCUSDT", "1")
    binance.as_dict.add(path)
    resp = apagar(client)
    assert resp.status_code == 502, resp.data
    r = resp.get_json()["resumen"]
    assert r["completo"] is False and r["restante"]["verificado"] is False
    assert any("se esperaba una lista" in e for e in r["errores"])
    assert "resumen" in audit_lines(app)[-1]["detail"]


def test_apagar_error_inesperado_conserva_resumen(client, app, env, docker, binance, monkeypatch):
    binance.orders = [{"symbol": "ETHUSDT"}]
    binance.add_position("BTCUSDT", "1")

    def boom(*a, **k):
        raise AttributeError("'str' object has no attribute 'get'")

    monkeypatch.setattr(bot_control, "split_quantity", boom)
    resp = apagar(client)
    assert resp.status_code == 502, resp.data
    r = resp.get_json()["resumen"]
    assert {(o["symbol"], o["tipo"]) for o in r["ordenes_canceladas"]} == {("ETHUSDT", "normal")}
    assert any("error inesperado (AttributeError)" in e for e in r["errores"])
    assert r["restante"]["posiciones"][0]["symbol"] == "BTCUSDT"
    last = audit_lines(app)[-1]
    assert last["result"] == "error" and "ETHUSDT" in last["detail"]


def test_close_all_futures_red_de_seguridad(monkeypatch):
    binance = FakeBinance()
    binance.orders = [{"symbol": "ETHUSDT"}]
    client = bot_control.BinanceFuturesClient(BINANCE_BASE, "k", "s", session=binance)
    monkeypatch.setattr(bot_control, "ROUND_PAUSE_SECONDS", 0)

    def boom(_):
        raise KeyError("x")

    monkeypatch.setattr(bot_control, "_order_counts", boom)
    r = bot_control.close_all_futures(client)
    assert r["completo"] is False and r["restante"]["verificado"] is False
    assert any("Cierre interrumpido" in e for e in r["errores"])


def test_apagar_sin_poder_verificar_es_502(client, env, docker, binance):
    binance.positions_status = 500
    resp = apagar(client)
    assert resp.status_code == 502
    assert resp.get_json()["resumen"]["restante"]["verificado"] is False


def test_apagar_auditoria_y_sin_secretos(client, app, env, docker, binance):
    app.config["API_KEY"] = "KEYabc123visible"
    app.config["API_SECRET"] = "SECRETxyz789"
    binance.add_position("BTCUSDT", "1")
    binance.fail_orders = 1
    resp = apagar(client)
    assert resp.status_code == 200
    last = audit_lines(app)[-1]
    assert last["action"] == "stop" and last["result"] == "ok"
    assert last["params"] == {"modo": "apagar"}
    assert "resumen" in last["detail"] and "BTCUSDT" in last["detail"]
    audit_text = (pathlib.Path(app.config["DATABASE"]).parent / bot_control.AUDIT_LOG_NAME).read_text()
    for secret in ("KEYabc123visible", "SECRETxyz789", "signature"):
        assert secret not in audit_text
        assert secret not in resp.get_data(as_text=True)
    # Firma HMAC-SHA256 del query string (sin la firma) con el secret, y API key en el header.
    import hashlib
    import hmac as _hmac
    from urllib.parse import urlencode

    method, path, params, headers = binance.calls[0]
    assert headers["X-MBX-APIKEY"] == "KEYabc123visible"
    sig = params.pop("signature")
    expected = _hmac.new(b"SECRETxyz789", urlencode(params).encode(), hashlib.sha256).hexdigest()
    assert sig == expected


def test_apagar_exchange_no_binance_409(client, app, env, docker, binance):
    app.config["EXCHANGE"] = "bybit"
    resp = apagar(client)
    assert resp.status_code == 409
    assert docker.actions() == [] and binance.calls == []


def test_apagar_sin_credenciales_503(client, app, env, docker, binance):
    app.config["API_SECRET"] = ""
    resp = apagar(client)
    assert resp.status_code == 503
    assert docker.actions() == [] and binance.calls == []


def test_split_quantity():
    from decimal import Decimal as D

    assert bot_control.split_quantity(D("250"), D("1"), D("1"), D("100")) == ([D(100), D(100), D(50)], D(0))
    chunks, rest = bot_control.split_quantity(D("0.12345"), D("0.001"), D("0.001"), D("0"))
    assert chunks == [D("0.123")] and rest == D("0.00045")
    assert bot_control.split_quantity(D("0.0004"), D("0.001"), D("0.001"), D("10")) == ([], D("0.0004"))
    # maxQty no múltiplo del step: se alinea hacia abajo.
    chunks, _ = bot_control.split_quantity(D("10"), D("0.5"), D("0.5"), D("4.7"))
    assert chunks == [D("4.5"), D("4.5"), D("1.0")]


def test_lot_filters_fallback():
    info = {"symbols": [{"symbol": "X", "filters": [
        {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "500"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0", "maxQty": "120"},
    ]}]}
    f = bot_control.lot_filters(info, "X")
    assert str(f["step"]) == "0.01" and str(f["max"]) == "120" and str(f["min"]) == "0.01"
    assert bot_control.lot_filters(info, "Y") is None


# ---------------------------------------------------------------- modos: misma tabla que forager


def set_short_mode(path, value):
    """Agrega short_mode al HJSON de prueba como texto crudo (respeta el tipo que parsea hjson)."""
    path.write_text(FORAGER_HJSON.replace("n_longs: 4", f"n_longs: 4\n  short_mode: {value}"))


def test_mode_aliases_match_forager():
    # Copia literal de MODE_ALIASES de forager_modes.py (botbulls/passivbot#2), por codigo corto.
    forager = {
        "n": "n", "normal": "n",
        "gs": "gs", "graceful_stop": "gs", "graceful-stop": "gs",
        "m": "m", "manual": "m",
        "p": "p", "panic": "p",
        "t": "t", "tp_only": "t", "tp-only": "t",
    }
    canonical = {"n": "normal", "gs": "graceful_stop", "m": "manual", "p": "panic", "t": "tp_only"}
    assert bot_control.MODE_ALIASES == {alias: canonical[code] for alias, code in forager.items()}
    assert bot_control.STOP_MODE_VALUES == {"graceful_stop"}


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "normal"), ("", "normal"), ("   ", "normal"), ("n", "normal"), ("NORMAL", "normal"),
        ("GS", "graceful_stop"), (" Graceful_Stop ", "graceful_stop"), ("graceful-stop", "graceful_stop"),
        ("M", "manual"), ("Panic", "panic"), ("t", "tp_only"), ("TP-ONLY", "tp_only"),
    ],
)
def test_normalize_mode(value, expected):
    assert bot_control.normalize_mode(value) == expected


@pytest.mark.parametrize("value", ["off", "stop", "graceful", True, False, 0, 1.5, ["gs"], {"a": 1}])
def test_normalize_mode_rejects_what_forager_rejects(value):
    with pytest.raises(bot_control.InvalidModeError):
        bot_control.normalize_mode(value, "short_mode")


@pytest.mark.parametrize("raw", ["off", "true", "1", "foo"])
def test_start_with_invalid_short_mode_returns_409_without_writing(client, app, env, docker, raw):
    set_short_mode(env, raw)
    original = env.read_text()
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 409, resp.data
    assert "short_mode" in resp.get_json()["error"]
    assert env.read_text() == original
    assert list(env.parent.glob("new.json.bak-*")) == []
    assert docker.actions() == []
    assert audit_lines(app)[-1]["result"] == "error"


@pytest.mark.parametrize("raw", ["GS", "Graceful_Stop", "gs", "graceful-stop"])
def test_start_treats_stop_aliases_as_stop(client, app, env, docker, raw):
    set_short_mode(env, raw)
    state_path(app).write_text(json.dumps({"short_mode_before_stop": "tp_only"}))
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 200
    assert hjson.loads(env.read_text())["short_mode"] == "tp_only"
    assert "short_mode_before_stop" not in json.loads(state_path(app).read_text())


def test_stop_does_not_save_stop_alias_as_previous(client, app, env, docker):
    set_short_mode(env, "GS")
    assert post(client, "/api/bot/stop", {"modo": "graceful"}).status_code == 200
    assert "short_mode_before_stop" not in json.loads(state_path(app).read_text())
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 200
    assert hjson.loads(env.read_text())["short_mode"] == "normal"


def test_stop_saves_normalized_short_mode(client, app, env, docker):
    set_short_mode(env, "TP-Only")
    assert post(client, "/api/bot/stop", {"modo": "graceful"}).status_code == 200
    assert json.loads(state_path(app).read_text())["short_mode_before_stop"] == "tp_only"


def test_stop_graceful_with_invalid_short_mode_not_saved(client, app, env, docker):
    set_short_mode(env, "off")
    state_path(app).write_text(json.dumps({"short_mode_before_stop": "panic"}))
    assert post(client, "/api/bot/stop", {"modo": "graceful"}).status_code == 200
    cfg = hjson.loads(env.read_text())
    assert cfg["long_mode"] == cfg["short_mode"] == "graceful_stop"
    assert "short_mode_before_stop" not in json.loads(state_path(app).read_text())
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 200
    assert hjson.loads(env.read_text())["short_mode"] == "normal"


@pytest.mark.parametrize("saved", ["off", 7, None, "", "gs"])
def test_start_with_invalid_saved_short_mode_falls_back_to_normal(client, app, env, docker, saved):
    set_short_mode(env, "graceful_stop")
    state_path(app).write_text(json.dumps({"short_mode_before_stop": saved}))
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 200
    assert hjson.loads(env.read_text())["short_mode"] == "normal"


def test_status_normalizes_modes(client, env, docker):
    env.write_text(FORAGER_HJSON.replace("n_longs: 4", "n_longs: 4\n  long_mode: GS\n  short_mode: Graceful_Stop"))
    data = client.get("/api/bot/status").get_json()
    assert data["long_mode"] == data["short_mode"] == "graceful_stop"


def test_status_with_invalid_mode_does_not_fail(client, env, docker):
    set_short_mode(env, "off")
    resp = client.get("/api/bot/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["riesgo"] == "medio"
    assert data["long_mode"] == "normal"
    assert data["short_mode"] == "off"
    assert "short_mode" in data["message"] and "rechaza" in data["message"]
