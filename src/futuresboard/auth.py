from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from urllib.parse import urlsplit

import click
from flask import Blueprint
from flask import current_app
from flask import flash
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask.cli import with_appcontext
from flask.helpers import url_for
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash

from futuresboard.blueprint import get_coins


auth = Blueprint("auth", __name__)
log = logging.getLogger(__name__)

# Contraseña que sembraban las versiones anteriores. Un usuario que todavía la tenga
# no puede loguearse hasta fijar una nueva con `flask set-password <usuario>`.
BLOCKED_DEFAULT_PASSWORD = "123456"
ENV_ADMIN_USER = "FUTURESBOARD_ADMIN_USER"
ENV_ADMIN_PASSWORD = "FUTURESBOARD_ADMIN_PASSWORD"
SET_PASSWORD_HINT = "flask set-password <usuario>"

# Rate limit de login: intentos fallidos por (usuario, IP) dentro de la ventana.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_ATTEMPTS_KEY = "futuresboard_login_attempts"


def validate_new_password(password: str) -> str | None:
    """Devuelve un mensaje de error si la contraseña no es aceptable, o None."""
    if not password:
        return "La contraseña no puede estar vacía"
    if password == BLOCKED_DEFAULT_PASSWORD:
        return "Esa contraseña está bloqueada (era la contraseña por defecto)"
    return None


def set_user_password(database_path: str, username: str, password: str) -> bool:
    """Crea el usuario o le cambia la contraseña. Devuelve True si lo creó."""
    with sqlite3.connect(database_path) as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(password), username),
        )
        created = cur.rowcount == 0
        if created:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
        conn.commit()
    return created


def _bootstrap_admin_from_env(conn: sqlite3.Connection) -> None:
    """Alta inicial desde env solo si la tabla users está vacía."""
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return
    username = (os.environ.get(ENV_ADMIN_USER) or "").strip()
    password = os.environ.get(ENV_ADMIN_PASSWORD) or ""
    if not username or not password:
        log.warning(
            "No hay usuarios en la base. Crear uno con `%s` o definir %s/%s.",
            SET_PASSWORD_HINT,
            ENV_ADMIN_USER,
            ENV_ADMIN_PASSWORD,
        )
        return
    error = validate_new_password(password)
    if error:
        log.error("%s rechazada: %s. No se creó el usuario.", ENV_ADMIN_PASSWORD, error)
        return
    conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (username, generate_password_hash(password)),
    )
    log.info("Usuario inicial '%s' creado desde %s.", username, ENV_ADMIN_USER)


def _warn_blocked_default_passwords(database_path: str) -> None:
    with sqlite3.connect(database_path) as conn:
        rows = conn.execute("SELECT username, password_hash FROM users").fetchall()
    for username, password_hash in rows:
        if check_password_hash(password_hash, BLOCKED_DEFAULT_PASSWORD):
            log.warning(
                "El usuario '%s' tiene la contraseña por defecto y no puede loguearse. "
                "Fijar una nueva con `%s`.",
                username,
                SET_PASSWORD_HINT,
            )


def safe_next_url(target: str | None) -> str | None:
    """Acepta solo rutas relativas internas ("/algo"). Cualquier otra cosa -> None."""
    if not target or not isinstance(target, str):
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target:
        return None
    # Los navegadores ignoran tabs/saltos de línea en URLs ("/\t/evil.com" -> "//evil.com").
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


def _client_ip() -> str:
    # Con FUTURESBOARD_PROXY_FIX activo, ProxyFix ya reescribió remote_addr desde
    # X-Forwarded-For. Sin ProxyFix se usa la IP de la conexión y se ignoran los headers.
    return request.remote_addr or "unknown"


def _attempts_store():
    ext = current_app.extensions.setdefault(
        _LOGIN_ATTEMPTS_KEY, {"lock": threading.Lock(), "failures": {}}
    )
    return ext["lock"], ext["failures"]


def _recent_failures(failures: dict, key, now: float) -> list:
    recent = [t for t in failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    if recent:
        failures[key] = recent
    else:
        failures.pop(key, None)
    return recent


def _reserve_attempt(key) -> float | None:
    """Cuenta el intento de forma atómica ANTES de verificar la contraseña.

    Bajo el candado: si ya hay LOGIN_MAX_FAILURES intentos en la ventana devuelve None
    (rate limited); si no, registra el intento ahora y devuelve su timestamp. Así N
    requests concurrentes no pueden pasar todas el chequeo antes de que se registre
    ninguna falla (el hash scrypt tarda decenas de ms).
    """
    lock, failures = _attempts_store()
    now = time.monotonic()
    with lock:
        recent = _recent_failures(failures, key, now)
        if len(recent) >= LOGIN_MAX_FAILURES:
            return None
        recent.append(now)
        failures[key] = recent
        if len(failures) > 10000:
            for other in list(failures):
                _recent_failures(failures, other, now)
    return now


def _release_attempt(key, stamp: float) -> None:
    """Devuelve un intento reservado que no debe contar como fallo."""
    lock, failures = _attempts_store()
    with lock:
        entries = failures.get(key)
        if entries and stamp in entries:
            entries.remove(stamp)
            if not entries:
                failures.pop(key, None)


def _clear_failures(key) -> None:
    lock, failures = _attempts_store()
    with lock:
        failures.pop(key, None)


def password_fingerprint(password_hash: str) -> str:
    """Huella del hash de contraseña que se guarda en la sesión.

    Si la contraseña cambia (set-password, /settings) la huella deja de coincidir y las
    sesiones emitidas antes quedan inválidas. Es un HMAC con la secret key: la cookie es
    legible por el cliente y no tiene por qué exponer nada derivable del hash.
    """
    key = str(current_app.secret_key or "").encode()
    return hmac.new(key, password_hash.encode(), hashlib.sha256).hexdigest()[:32]


SESSION_FINGERPRINT_KEY = "pw_fp"

# Endpoints sin login (liveness; no exponen datos).
LOGIN_EXEMPT_ENDPOINTS = {"ops.liveness"}
# Endpoints que ademas del login aceptan "Authorization: Bearer <FUTURESBOARD_METRICS_TOKEN>".
BEARER_TOKEN_ENDPOINTS = {"ops.metrics"}


def _ensure_users_columns(conn: sqlite3.Connection) -> None:
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "theme_default" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN theme_default text NOT NULL DEFAULT 'auto'")


def _ensure_database_schema(database_path: str) -> None:
    """
    Ensure the minimal database schema exists for both the dashboard and login.
    This keeps first-run behavior working with an empty DB.
    """
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS income (
                    IID integer PRIMARY KEY AUTOINCREMENT,
                    tranId text,
                    symbol text,
                    incomeType text,
                    income real,
                    asset text,
                    info text,
                    time integer,
                    tradeId integer,
                    UNIQUE(tranId, incomeType) ON CONFLICT REPLACE
                ); """
        )
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS positions (
                    PID integer PRIMARY KEY AUTOINCREMENT,
                    symbol text,
                    unrealizedProfit real,
                    leverage integer,
                    entryPrice real,
                    positionSide text,
                    positionAmt real
                ); """
        )
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS account (
                    AID integer PRIMARY KEY,
                    totalWalletBalance real,
                    totalUnrealizedProfit real,
                    totalMarginBalance real,
                    availableBalance real,
                    maxWithdrawAmount real
                ); """
        )
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS orders (
                    OID integer PRIMARY KEY AUTOINCREMENT,
                    origQty real,
                    price real,
                    side text,
                    positionSide text,
                    status text,
                    symbol text,
                    time integer,
                    type text
                ); """
        )
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS users (
                    UID integer PRIMARY KEY AUTOINCREMENT,
                    username text UNIQUE NOT NULL,
                    password_hash text NOT NULL,
                    theme_default text NOT NULL DEFAULT 'auto'
                ); """
        )
        _ensure_users_columns(conn)
        _bootstrap_admin_from_env(conn)

        # Seed required baseline rows for first-run.
        conn.execute(
            """
            INSERT OR IGNORE INTO account (
                AID,
                totalWalletBalance,
                totalUnrealizedProfit,
                totalMarginBalance,
                availableBalance,
                maxWithdrawAmount
            ) VALUES (1, 0.0, 0.0, 0.0, 0.0, 0.0)
            """
        )
        conn.commit()


@auth.route("/login", methods=["GET", "POST"])
def login_page():
    next_url = safe_next_url(request.args.get("next")) or url_for("main.index_page")

    def _render(status=200):
        return (
            render_template("login.html", next=next_url, custom=current_app.config["CUSTOM"]),
            status,
        )

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        ip = _client_ip()
        key = (username.lower(), ip)

        stamp = _reserve_attempt(key)
        if stamp is None:
            current_app.logger.warning(
                "Login bloqueado por rate limit: usuario=%r ip=%s", username, ip
            )
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return _render(429)

        with sqlite3.connect(current_app.config["DATABASE"]) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT username, password_hash, theme_default FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        valid = bool(row) and check_password_hash(row["password_hash"], password)

        if row and check_password_hash(row["password_hash"], BLOCKED_DEFAULT_PASSWORD):
            # Contraseña por defecto heredada: no se permite el login aunque sea correcta.
            current_app.logger.warning(
                "Login rechazado: el usuario %r tiene la contraseña por defecto; "
                "fijar una nueva con `%s` (ip=%s)",
                row["username"],
                SET_PASSWORD_HINT,
                ip,
            )
            if valid:
                # La cuenta igual no es usable: este intento no suma al rate limit.
                _release_attempt(key, stamp)
                flash(
                    "This account still uses the default password and is locked. "
                    f"An administrator must set a new one with `{SET_PASSWORD_HINT}`.",
                    "error",
                )
                return _render(403)
            valid = False

        if valid:
            _clear_failures(key)
            session["username"] = row["username"]
            session[SESSION_FINGERPRINT_KEY] = password_fingerprint(row["password_hash"])
            session["theme_default"] = row["theme_default"] or "auto"
            return redirect(next_url)

        # El intento ya quedó registrado en _reserve_attempt.
        current_app.logger.warning("Login fallido: usuario=%r ip=%s", username, ip)
        flash("Invalid username or password", "error")

    return _render()


@auth.route("/logout", methods=["GET"])
def logout_page():
    session.clear()
    return redirect(url_for("auth.login_page"))


@auth.route("/settings", methods=["GET", "POST"])
def settings_page():
    if "username" not in session:
        return redirect(url_for("auth.login_page", next=url_for("auth.settings_page")))

    current_username = session["username"]

    with sqlite3.connect(current_app.config["DATABASE"]) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT username, password_hash, theme_default FROM users WHERE username = ?",
            (current_username,),
        ).fetchone()

        if row is None:
            session.clear()
            return redirect(url_for("auth.login_page"))

        if request.method == "POST":
            new_username = (request.form.get("username") or "").strip()
            theme_default = (request.form.get("theme_default") or "").strip().lower()
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""

            if theme_default not in {"light", "dark"}:
                theme_default = row["theme_default"] or "auto"

            # Update username (if changed)
            if new_username and new_username != row["username"]:
                try:
                    conn.execute(
                        "UPDATE users SET username = ? WHERE username = ?",
                        (new_username, row["username"]),
                    )
                    session["username"] = new_username
                    current_username = new_username
                    row = dict(row)
                    row["username"] = new_username
                except sqlite3.IntegrityError:
                    flash("Username already exists", "error")

            # Update password (if provided)
            password_error = validate_new_password(new_password) if new_password else None
            if password_error:
                flash(password_error, "error")
            elif new_password:
                if check_password_hash(row["password_hash"], current_password):
                    new_hash = generate_password_hash(new_password)
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE username = ?",
                        (new_hash, current_username),
                    )
                    # Esta sesión sigue válida; las demás del usuario quedan revocadas.
                    session[SESSION_FINGERPRINT_KEY] = password_fingerprint(new_hash)
                else:
                    flash("Current password is incorrect", "error")

            # Update default theme
            conn.execute(
                "UPDATE users SET theme_default = ? WHERE username = ?",
                (theme_default, current_username),
            )
            session["theme_default"] = theme_default

            conn.commit()
            flash("Settings saved", "success")

    current_theme_default = session.get("theme_default") or (row["theme_default"] or "auto")
    if current_theme_default not in {"light", "dark"}:
        current_theme_default = "dark"

    return render_template(
        "settings.html",
        coin_list=get_coins(),
        custom=current_app.config["CUSTOM"],
        username=session.get("username") or "",
        theme_default=current_theme_default,
    )


def _require_login():
    # Skip auth for static and auth endpoints.
    if request.endpoint is None:
        return None
    if request.endpoint.startswith("static"):
        return None
    if request.endpoint in LOGIN_EXEMPT_ENDPOINTS:
        return None
    if request.endpoint in BEARER_TOKEN_ENDPOINTS and (
        request.headers.get("Authorization") or "username" not in session
    ):
        # Con token: se valida el token. Sin token ni sesion: 401 (no redirect al login,
        # Prometheus no lo seguiria). Con sesion: sigue el chequeo normal de abajo.
        from futuresboard.health import bearer_token_response

        return bearer_token_response()
    if request.endpoint == "auth.login_page":
        return None

    if "username" not in session or not _session_is_current():
        session.clear()
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.login_page", next=next_url))
    return None


def _session_is_current() -> bool:
    """La sesión sigue valiendo solo si el usuario existe y su contraseña no cambió.

    Las sesiones sin huella (emitidas antes de este cambio, p. ej. con `123456`) se
    rechazan. No se recalcula scrypt por request: se compara la huella del hash guardado.
    """
    fingerprint = session.get(SESSION_FINGERPRINT_KEY)
    if not fingerprint:
        return False
    with sqlite3.connect(current_app.config["DATABASE"]) as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (session["username"],)
        ).fetchone()
    if row is None:
        return False
    return hmac.compare_digest(fingerprint, password_fingerprint(row[0]))


@click.command("set-password")
@click.argument("username")
@with_appcontext
def set_password_command(username: str) -> None:
    """Crea el usuario USERNAME o le fija una nueva contraseña.

    La contraseña se pide por prompt sin eco. Si está definida la variable
    FUTURESBOARD_ADMIN_PASSWORD se usa esa (solo en este comando).
    """
    username = username.strip()
    if not username:
        raise click.UsageError("El usuario no puede estar vacío")
    password = os.environ.get(ENV_ADMIN_PASSWORD)
    if not password:
        password = click.prompt(
            f"Nueva contraseña para '{username}'", hide_input=True, confirmation_prompt=True
        )
    error = validate_new_password(password)
    if error:
        raise click.ClickException(error)
    created = set_user_password(str(current_app.config["DATABASE"]), username, password)
    click.echo(f"Usuario '{username}' {'creado' if created else 'actualizado'}.")


def init_app(app) -> None:
    app.register_blueprint(auth)
    app.cli.add_command(set_password_command)
    with app.app_context():
        _ensure_database_schema(str(current_app.config["DATABASE"]))
        _warn_blocked_default_passwords(str(current_app.config["DATABASE"]))
    app.before_request(_require_login)



