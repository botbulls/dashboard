from __future__ import annotations

import logging
import sqlite3
import time

import pytest
from werkzeug.security import generate_password_hash

from futuresboard import auth
from futuresboard.app import init_app
from futuresboard.config import Config



@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "FUTURESBOARD_SECRET_KEY",
        "FUTURESBOARD_COOKIE_SECURE",
        "FUTURESBOARD_PROXY_FIX",
        auth.ENV_ADMIN_USER,
        auth.ENV_ADMIN_PASSWORD,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FUTURESBOARD_SECRET_KEY", "test-secret")


@pytest.fixture
def make_app(tmp_path):
    def _make():
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        cfg = Config(
            CONFIG_DIR=tmp_path,
            DATABASE=data_dir / "futures.db",
            API_KEY="x",
            API_SECRET="x",
            DISABLE_AUTO_SCRAPE=True,
        )
        application = init_app(cfg)
        application.config["TESTING"] = True
        return application

    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def db_path(app):
    return str(app.config["DATABASE"])


def users(app):
    with sqlite3.connect(db_path(app)) as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT username, password_hash FROM users")}


def login(client, username, password, next_url=None, **kwargs):
    url = "/login" if next_url is None else f"/login?next={next_url}"
    return client.post(url, data={"username": username, "password": password}, **kwargs)


def logged_in(client):
    with client.session_transaction() as sess:
        return "username" in sess


# ---------------------------------------------------------------- (1) sin seed / alta por CLI


def test_no_default_user_seeded(app):
    assert users(app) == {}


def test_bootstrap_admin_from_env(monkeypatch, make_app):
    monkeypatch.setenv(auth.ENV_ADMIN_USER, "admin")
    monkeypatch.setenv(auth.ENV_ADMIN_PASSWORD, "una-clave-larga")
    application = make_app()
    assert list(users(application)) == ["admin"]
    c = application.test_client()
    assert login(c, "admin", "una-clave-larga").status_code == 302
    assert logged_in(c)


def test_bootstrap_only_when_users_empty(monkeypatch, make_app):
    application = make_app()
    auth.set_user_password(db_path(application), "existente", "clave-existente")
    monkeypatch.setenv(auth.ENV_ADMIN_USER, "admin")
    monkeypatch.setenv(auth.ENV_ADMIN_PASSWORD, "una-clave-larga")
    application = make_app()
    assert list(users(application)) == ["existente"]


def test_bootstrap_rejects_default_password(monkeypatch, make_app):
    monkeypatch.setenv(auth.ENV_ADMIN_USER, "admin")
    monkeypatch.setenv(auth.ENV_ADMIN_PASSWORD, "123456")
    application = make_app()
    assert users(application) == {}


def test_cli_set_password_prompt_creates_and_updates(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["set-password", "admin"], input="clave-uno\nclave-uno\n")
    assert result.exit_code == 0, result.output
    assert "creado" in result.output
    assert "clave-uno" not in result.output  # sin eco

    result = runner.invoke(args=["set-password", "admin"], input="clave-dos\nclave-dos\n")
    assert result.exit_code == 0, result.output
    assert "actualizado" in result.output

    c = app.test_client()
    assert login(c, "admin", "clave-uno").status_code == 200
    assert login(c, "admin", "clave-dos").status_code == 302


def test_cli_set_password_from_env(monkeypatch, app):
    monkeypatch.setenv(auth.ENV_ADMIN_PASSWORD, "clave-desde-env")
    result = app.test_cli_runner().invoke(args=["set-password", "ops"])
    assert result.exit_code == 0, result.output
    c = app.test_client()
    assert login(c, "ops", "clave-desde-env").status_code == 302


def test_cli_set_password_rejects_default(app):
    result = app.test_cli_runner().invoke(args=["set-password", "admin"], input="123456\n123456\n")
    assert result.exit_code != 0
    assert users(app) == {}


# ---------------------------------------------------------------- (2) contraseña por defecto heredada


def _insert_legacy_default_user(app, username="cliente17"):
    with sqlite3.connect(db_path(app)) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash("123456")),
        )


def test_legacy_default_password_cannot_login(app, client, caplog):
    _insert_legacy_default_user(app)
    with caplog.at_level(logging.WARNING):
        resp = login(client, "cliente17", "123456")
    assert resp.status_code == 403
    assert b"set-password" in resp.data
    assert not logged_in(client)
    assert any("contraseña por defecto" in r.getMessage() for r in caplog.records)


def test_legacy_default_wrong_password_is_generic(app, client):
    _insert_legacy_default_user(app)
    resp = login(client, "cliente17", "otra")
    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data
    assert b"set-password" not in resp.data
    assert not logged_in(client)


def test_legacy_default_unlocked_after_set_password(app, client):
    _insert_legacy_default_user(app)
    result = app.test_cli_runner().invoke(
        args=["set-password", "cliente17"], input="nueva-clave\nnueva-clave\n"
    )
    assert result.exit_code == 0, result.output
    assert login(client, "cliente17", "nueva-clave").status_code == 302
    assert logged_in(client)


def test_startup_warns_about_legacy_default(make_app, caplog):
    application = make_app()
    _insert_legacy_default_user(application)
    with caplog.at_level(logging.WARNING):
        make_app()
    assert any("cliente17" in r.getMessage() for r in caplog.records)


def test_settings_rejects_default_password(app, client):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    login(client, "admin", "clave-actual")
    client.post(
        "/settings",
        data={"username": "admin", "current_password": "clave-actual", "new_password": "123456"},
    )
    c2 = app.test_client()
    assert login(c2, "admin", "clave-actual").status_code == 302


# ---------------------------------------------------------------- (3) open redirect


@pytest.mark.parametrize(
    "target",
    [
        "//evil.com",
        "///evil.com",
        "https://evil.com",
        "http:evil.com",
        "/\\evil.com",
        "\\\\evil.com",
        "/%09/evil.com",
        "javascript:alert(1)",
        "evil.com",
        "",
    ],
)
def test_next_rejects_external(app, client, target):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    resp = login(client, "admin", "clave-actual", next_url=target)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


@pytest.mark.parametrize("target", ["/positions", "/coin/BTCUSDT?x=1"])
def test_next_accepts_internal(app, client, target):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    resp = client.post(
        "/login", query_string={"next": target}, data={"username": "admin", "password": "clave-actual"}
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == target


@pytest.mark.parametrize(
    "target,expected",
    [
        ("/ok", "/ok"),
        ("/a\tb", None),
        ("/a\nb", None),
        ("//x", None),
        ("/\\x", None),
        ("ftp://x/y", None),
        (None, None),
    ],
)
def test_safe_next_url_unit(target, expected):
    assert auth.safe_next_url(target) == expected


def test_login_form_does_not_echo_external_next(client):
    resp = client.get("/login?next=//evil.com")
    assert b"evil.com" not in resp.data


# ---------------------------------------------------------------- (4) cookies / secret key


def _session_cookie_header(app):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    c = app.test_client()
    resp = login(c, "admin", "clave-actual")
    return [h for h in resp.headers.getlist("Set-Cookie") if h.startswith("session=")][0]


def test_session_cookie_flags_default(app):
    header = _session_cookie_header(app)
    assert "Secure" in header
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header


def test_session_cookie_secure_can_be_disabled(monkeypatch, make_app):
    monkeypatch.setenv("FUTURESBOARD_COOKIE_SECURE", "0")
    header = _session_cookie_header(make_app())
    assert "Secure" not in header
    assert "HttpOnly" in header


def test_secret_key_from_env(app):
    assert app.secret_key == "test-secret"


def test_missing_secret_key_warns(monkeypatch, make_app, caplog):
    monkeypatch.delenv("FUTURESBOARD_SECRET_KEY")
    with caplog.at_level(logging.WARNING):
        application = make_app()
    assert application.secret_key and len(application.secret_key) >= 32
    assert any("FUTURESBOARD_SECRET_KEY" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- (5) rate limit


def test_rate_limit_after_five_failures(app, client, caplog):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        assert login(client, "admin", "mal").status_code == 200
    with caplog.at_level(logging.WARNING):
        resp = login(client, "admin", "clave-actual")
    assert resp.status_code == 429
    assert not logged_in(client)
    assert any("rate limit" in r.getMessage() for r in caplog.records)


def test_rate_limit_is_per_user_and_ip(app, client):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    auth.set_user_password(db_path(app), "otro", "clave-otro")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        login(client, "admin", "mal")
    # otro usuario desde la misma IP: no bloqueado
    assert login(client, "otro", "clave-otro").status_code == 302
    # mismo usuario desde otra IP: no bloqueado
    c2 = app.test_client()
    resp = login(c2, "admin", "clave-actual", environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert resp.status_code == 302


def test_rate_limit_window_expires(app, client, monkeypatch):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    for _ in range(auth.LOGIN_MAX_FAILURES):
        login(client, "admin", "mal")
    assert login(client, "admin", "clave-actual").status_code == 429
    now[0] += auth.LOGIN_WINDOW_SECONDS + 1
    assert login(client, "admin", "clave-actual").status_code == 302


def test_success_resets_failures(app, client):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        login(client, "admin", "mal")
    assert login(client, "admin", "clave-actual").status_code == 302
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        login(client, "admin", "mal")
    assert login(client, "admin", "clave-actual").status_code == 302


def test_rate_limit_holds_under_concurrent_requests(app, monkeypatch):
    """Ráfaga concurrente: no se evalúan más de LOGIN_MAX_FAILURES contraseñas."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    auth.set_user_password(db_path(app), "admin", "clave-actual")
    real_check = auth.check_password_hash
    evaluated = []
    count_lock = threading.Lock()

    def slow_check(pwhash, password):
        if password != auth.BLOCKED_DEFAULT_PASSWORD:
            with count_lock:
                evaluated.append(password)
            time.sleep(0.05)  # simula el costo de scrypt y abre la ventana de carrera
        return real_check(pwhash, password)

    monkeypatch.setattr(auth, "check_password_hash", slow_check)
    burst = 20
    barrier = threading.Barrier(burst)

    def attempt(i):
        c = app.test_client()
        barrier.wait()
        return login(c, "admin", f"mal{i}").status_code

    with ThreadPoolExecutor(max_workers=burst) as pool:
        codes = list(pool.map(attempt, range(burst)))

    assert len(evaluated) <= auth.LOGIN_MAX_FAILURES
    assert codes.count(200) <= auth.LOGIN_MAX_FAILURES
    assert codes.count(429) >= burst - auth.LOGIN_MAX_FAILURES
    assert login(app.test_client(), "admin", "clave-actual").status_code == 429


def test_xff_ignored_without_proxy_fix(app, client):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    for i in range(auth.LOGIN_MAX_FAILURES):
        login(client, "admin", "mal", headers={"X-Forwarded-For": f"1.2.3.{i}"})
    resp = login(client, "admin", "clave-actual", headers={"X-Forwarded-For": "9.9.9.9"})
    assert resp.status_code == 429


def test_xff_used_with_proxy_fix(monkeypatch, make_app):
    monkeypatch.setenv("FUTURESBOARD_PROXY_FIX", "1")
    application = make_app()
    auth.set_user_password(db_path(application), "admin", "clave-actual")
    c = application.test_client()
    for _ in range(auth.LOGIN_MAX_FAILURES):
        login(c, "admin", "mal", headers={"X-Forwarded-For": "1.2.3.4"})
    assert login(c, "admin", "clave-actual", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 429
    assert login(c, "admin", "clave-actual", headers={"X-Forwarded-For": "5.6.7.8"}).status_code == 302


# ---------------------------------------------------------------- revocación de sesiones


def test_session_revoked_after_set_password(app, client):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    assert login(client, "admin", "clave-actual").status_code == 302
    assert client.get("/settings").status_code == 200
    auth.set_user_password(db_path(app), "admin", "clave-nueva")
    resp = client.get("/settings")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert not logged_in(client)


def test_session_revoked_when_user_deleted(app, client):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    login(client, "admin", "clave-actual")
    with sqlite3.connect(db_path(app)) as conn:
        conn.execute("DELETE FROM users WHERE username = 'admin'")
    assert client.get("/").status_code == 302


def test_legacy_session_without_fingerprint_rejected(app, client):
    """Cookie emitida antes del cambio (p. ej. con 123456): solo trae username."""
    _insert_legacy_default_user(app)
    with client.session_transaction() as sess:
        sess["username"] = "cliente17"
    resp = client.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert not logged_in(client)


def test_settings_password_change_keeps_own_session_revokes_others(app):
    auth.set_user_password(db_path(app), "admin", "clave-actual")
    c1, c2 = app.test_client(), app.test_client()
    login(c1, "admin", "clave-actual")
    login(c2, "admin", "clave-actual")
    c1.post(
        "/settings",
        data={
            "username": "admin",
            "theme_default": "dark",
            "current_password": "clave-actual",
            "new_password": "clave-nueva",
        },
    )
    assert c1.get("/settings").status_code == 200
    assert c2.get("/settings").status_code == 302
