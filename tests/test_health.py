from __future__ import annotations

import json
import sqlite3
import time

import pytest

from futuresboard import auth
from futuresboard import bot_control
from futuresboard import health
from futuresboard import scraper
from futuresboard.app import init_app
from futuresboard.config import Config

NOW = time.time()


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeDocker:
    def __init__(self, running=True, exists=True, fail=False):
        self.running = running
        self.exists = exists
        self.fail = fail
        self.calls = []

    def request(self, method, url, **kwargs):
        import requests

        self.calls.append((method, url))
        if self.fail:
            raise requests.ConnectionError("down")
        if not self.exists:
            return FakeResponse(404)
        state = {
            "Status": "running" if self.running else "exited",
            "Running": self.running,
            "StartedAt": "2026-10-01T10:00:00.123456789Z",
        }
        return FakeResponse(200, {"State": state})


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(health.THRESHOLD_DEFAULTS) + [
        health.ENV_METRICS_TOKEN,
        bot_control.ENV_DOCKER_URL,
        bot_control.ENV_CONTAINER,
    ]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setenv(bot_control.ENV_DOCKER_URL, "http://docker-proxy:2375")
    monkeypatch.setattr(bot_control.requests, "Session", lambda: fake)
    return fake


@pytest.fixture
def app(tmp_path):
    cfg = Config(
        CONFIG_DIR=tmp_path,
        DATABASE=tmp_path / "futures.db",
        API_KEY="x",
        API_SECRET="x",
        DISABLE_AUTO_SCRAPE=True,
    )
    application = init_app(cfg)
    application.config["TESTING"] = True
    return application


def seed(app, trade_age=60.0, order_age=30.0, positions=((1.5, 0.01), (-0.5, -0.02), (0.0, 0.0)),
         scrape_age=30.0):
    database = app.config["DATABASE"]
    with sqlite3.connect(database) as conn:
        if trade_age is not None:
            ts = int((NOW - trade_age) * 1000)
            conn.execute(
                "INSERT INTO income (tranId, symbol, incomeType, income, asset, info, time, tradeId)"
                " VALUES ('t1', 'BTCUSDT', 'COMMISSION', -0.01, 'USDT', '', ?, 1)",
                (ts,),
            )
            # Funding y transfers no cuentan como trade aunque sean mas nuevos.
            conn.execute(
                "INSERT INTO income (tranId, symbol, incomeType, income, asset, info, time, tradeId)"
                " VALUES ('f1', 'BTCUSDT', 'FUNDING_FEE', -0.01, 'USDT', '', ?, 0)",
                (int(NOW * 1000),),
            )
        if order_age is not None:
            conn.execute(
                "INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type)"
                " VALUES (1, 100, 'BUY', 'LONG', 'NEW', 'BTCUSDT', ?, 'LIMIT')",
                (int((NOW - order_age) * 1000),),
            )
        for upnl, amt in positions:
            conn.execute(
                "INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide,"
                " positionAmt) VALUES ('X', ?, 10, 1, 'LONG', ?)",
                (upnl, amt),
            )
        conn.commit()
    if scrape_age is not None:
        scraper.scrape_state_path(database).write_text(
            json.dumps({"last_started_at": NOW - scrape_age - 1, "last_success_at": NOW - scrape_age})
        )


def collect(app, **kwargs):
    with app.test_request_context():
        return health.collect_health(now=NOW, **kwargs)


def login(client, app):
    fingerprint = None
    if hasattr(auth, "SESSION_FINGERPRINT_KEY"):
        # Tras mergear login-hardening (#4) la sesion necesita usuario real + huella.
        auth.set_user_password(str(app.config["DATABASE"]), "cliente17", "clave-de-test")
        with sqlite3.connect(str(app.config["DATABASE"])) as conn:
            pw_hash = conn.execute(
                "SELECT password_hash FROM users WHERE username = 'cliente17'"
            ).fetchone()[0]
        with app.app_context():
            fingerprint = auth.password_fingerprint(pw_hash)
    with client.session_transaction() as sess:
        sess["username"] = "cliente17"
        if fingerprint is not None:
            sess[auth.SESSION_FINGERPRINT_KEY] = fingerprint


# ---------------------------------------------------------------- /health


def test_liveness_without_login(app):
    resp = app.test_client().get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_liveness_does_not_touch_docker(app, docker):
    app.test_client().get("/health")
    assert docker.calls == []


# ---------------------------------------------------------------- /api/bot/health


def test_bot_health_requires_login(app):
    resp = app.test_client().get("/api/bot/health")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_bot_health_ok(app, docker):
    seed(app)
    client = app.test_client()
    login(client, app)
    resp = client.get("/api/bot/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["bot"]["running"] is True
    assert data["bot"]["started_at"] == "2026-10-01T10:00:00.123456789Z"
    assert data["db"]["positions_open"] == 2
    assert data["db"]["upnl_total"] == pytest.approx(1.0)
    assert data["binance_testnet"] is False
    assert data["checks"]["upnl"]["status"] == "skipped"


def test_collect_values(app, docker):
    seed(app, trade_age=120, order_age=45, scrape_age=10)
    data = collect(app)
    assert data["db"]["last_trade_age_seconds"] == pytest.approx(120, abs=1)
    assert data["db"]["last_order_age_seconds"] == pytest.approx(45, abs=1)
    assert data["scrape"]["age_seconds"] == pytest.approx(10, abs=0.01)
    assert data["checks"]["bot"]["status"] == "ok"


def test_bot_stopped_is_critical(app, docker):
    seed(app)
    docker.running = False
    data = collect(app)
    assert data["checks"]["bot"]["status"] == "critical"
    assert data["status"] == "critical"
    assert data["bot"]["uptime_seconds"] is None


def test_bot_container_missing_is_critical(app, docker):
    seed(app)
    docker.exists = False
    assert collect(app)["checks"]["bot"]["status"] == "critical"


def test_docker_unreachable_is_warn_not_error(app, docker):
    seed(app)
    docker.fail = True
    data = collect(app)
    assert data["checks"]["bot"]["status"] == "warn"
    assert data["bot"]["running"] is None
    assert data["status"] == "warn"


def test_panel_disabled_skips_bot_check(app):
    seed(app)
    data = collect(app)
    assert data["checks"]["bot"]["status"] == "skipped"
    assert data["status"] == "ok"


@pytest.mark.parametrize(
    "scrape_age,expected",
    [(10, "ok"), (1000, "warn"), (4000, "critical")],
)
def test_scrape_age_thresholds_default(app, docker, scrape_age, expected):
    seed(app, scrape_age=scrape_age)
    assert collect(app)["checks"]["scrape"]["status"] == expected


def test_scrape_never_recorded_is_warn(app, docker):
    seed(app, scrape_age=None)
    data = collect(app)
    assert data["scrape"]["age_seconds"] is None
    assert data["checks"]["scrape"]["status"] == "warn"


def test_thresholds_from_env(app, docker, monkeypatch):
    seed(app, trade_age=600, scrape_age=100)
    monkeypatch.setenv("FUTURESBOARD_HEALTH_SCRAPE_WARN_SECONDS", "50")
    monkeypatch.setenv("FUTURESBOARD_HEALTH_SCRAPE_CRIT_SECONDS", "0")  # 0 deshabilita
    monkeypatch.setenv("FUTURESBOARD_HEALTH_TRADE_CRIT_SECONDS", "300")
    data = collect(app)
    assert data["checks"]["scrape"]["status"] == "warn"
    assert data["checks"]["last_trade"]["status"] == "critical"
    assert data["status"] == "critical"
    assert data["thresholds"]["scrape_crit_seconds"] is None


def test_invalid_threshold_uses_default(app, docker, monkeypatch):
    seed(app, scrape_age=1000)
    monkeypatch.setenv("FUTURESBOARD_HEALTH_SCRAPE_WARN_SECONDS", "abc")
    assert collect(app)["checks"]["scrape"]["status"] == "warn"


def test_upnl_and_positions_thresholds(app, docker, monkeypatch):
    seed(app, positions=((-30.0, 1.0), (-25.0, -1.0), (0.0, 1.0)))
    monkeypatch.setenv("FUTURESBOARD_HEALTH_UPNL_WARN", "-20")
    monkeypatch.setenv("FUTURESBOARD_HEALTH_UPNL_CRIT", "-50")
    monkeypatch.setenv("FUTURESBOARD_HEALTH_POSITIONS_WARN", "3")
    data = collect(app)
    assert data["db"]["upnl_total"] == pytest.approx(-55.0)
    assert data["checks"]["upnl"]["status"] == "critical"
    assert data["checks"]["positions"]["status"] == "warn"


def test_no_trades_is_warn(app, docker):
    seed(app, trade_age=None, order_age=None, positions=())
    data = collect(app)
    assert data["db"]["last_trade_at"] is None
    assert data["db"]["positions_open"] == 0
    assert data["checks"]["last_trade"]["status"] == "warn"


def test_bot_health_contains_no_secrets(app, docker, monkeypatch):
    monkeypatch.setenv(health.ENV_METRICS_TOKEN, "s3cr3t-token-value")
    seed(app)
    client = app.test_client()
    login(client, app)
    body = client.get("/api/bot/health").get_data(as_text=True)
    assert "s3cr3t-token-value" not in body
    assert '"x"' not in body  # API_KEY / API_SECRET de la config de prueba


# ---------------------------------------------------------------- /metrics


def parse_metrics(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.split(" ")
            out[name] = float(value)
    return out


def test_metrics_without_auth_is_401(app):
    resp = app.test_client().get("/metrics")
    assert resp.status_code == 401


def test_metrics_with_session(app, docker):
    seed(app)
    client = app.test_client()
    login(client, app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/plain; version=0.0.4")
    m = parse_metrics(resp.get_data(as_text=True))
    assert m["futuresboard_bot_up"] == 1
    assert m["futuresboard_positions_open"] == 2
    assert m["futuresboard_upnl_total"] == pytest.approx(1.0)
    assert m["futuresboard_last_trade_age_seconds"] > 0
    assert m["futuresboard_scrape_age_seconds"] > 0
    assert m["futuresboard_health_status"] == 0
    text = resp.get_data(as_text=True)
    assert "# TYPE futuresboard_bot_up gauge" in text


def test_metrics_with_bearer_token(app, docker, monkeypatch):
    monkeypatch.setenv(health.ENV_METRICS_TOKEN, "tok-123")
    seed(app)
    resp = app.test_client().get("/metrics", headers={"Authorization": "Bearer tok-123"})
    assert resp.status_code == 200
    assert "futuresboard_bot_up 1" in resp.get_data(as_text=True)


def test_metrics_wrong_token_is_401(app, monkeypatch):
    monkeypatch.setenv(health.ENV_METRICS_TOKEN, "tok-123")
    resp = app.test_client().get("/metrics", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_metrics_token_unset_rejects_any_bearer(app):
    resp = app.test_client().get("/metrics", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def test_metrics_token_not_accepted_on_other_endpoints(app, monkeypatch):
    monkeypatch.setenv(health.ENV_METRICS_TOKEN, "tok-123")
    resp = app.test_client().get("/api/bot/health", headers={"Authorization": "Bearer tok-123"})
    assert resp.status_code == 302


def test_metrics_unknown_values_are_nan(app, docker):
    seed(app, trade_age=None, scrape_age=None)
    docker.fail = True
    client = app.test_client()
    login(client, app)
    m = parse_metrics(client.get("/metrics").get_data(as_text=True))
    import math

    assert math.isnan(m["futuresboard_bot_up"])
    assert math.isnan(m["futuresboard_last_trade_age_seconds"])
    assert math.isnan(m["futuresboard_scrape_age_seconds"])
    assert m["futuresboard_health_status"] == 1
