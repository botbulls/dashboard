from __future__ import annotations

import json
import logging
import os
import pathlib
import secrets

from flask import Flask
from flask import redirect
from flask import request
from werkzeug.middleware.proxy_fix import ProxyFix

import futuresboard.scraper
from futuresboard import auth
from futuresboard import blueprint
from futuresboard import db
from futuresboard import health
from futuresboard import logs
from futuresboard import telegram_notify
from futuresboard.config import Config


def clear_trailing():
    rp = request.path
    if rp != "/" and rp.endswith("/"):
        return redirect(rp[:-1])


def default_config_dir() -> pathlib.Path:
    """Directorio de config: env FUTURESBOARD_CONFIG_DIR o ``./config`` (igual que el CLI).

    Lo usan el CLI y el entrypoint WSGI (gunicorn), asi ambos leen el mismo config.json y
    la misma DB.
    """
    env_dir = os.environ.get("FUTURESBOARD_CONFIG_DIR", "").strip()
    if env_dir:
        return pathlib.Path(env_dir).resolve()
    return pathlib.Path.cwd() / "config"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _configure_security(app: Flask) -> None:
    log = logging.getLogger(__name__)

    secret_key = os.environ.get("FUTURESBOARD_SECRET_KEY")
    if not secret_key:
        log.warning(
            "FUTURESBOARD_SECRET_KEY no está definida: se usa una clave efímera. "
            "Las sesiones se invalidan en cada reinicio. Definirla en producción."
        )
        secret_key = secrets.token_hex(32)
    app.secret_key = secret_key

    # Secure por defecto: la app se sirve detrás de HTTPS (Cloudflare Tunnel). Para
    # acceder por HTTP plano (dev local sin TLS) definir FUTURESBOARD_COOKIE_SECURE=0.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_bool("FUTURESBOARD_COOKIE_SECURE", True),
    )

    # Opt-in: solo si la app es alcanzable únicamente a través del proxy (ej. cloudflared).
    # Valor = cantidad de proxies de confianza delante de la app.
    proxies = os.environ.get("FUTURESBOARD_PROXY_FIX", "").strip()
    if proxies:
        try:
            count = int(proxies)
        except ValueError:
            count = 0
            log.error("FUTURESBOARD_PROXY_FIX=%r no es un entero; ProxyFix desactivado.", proxies)
        if count > 0:
            app.wsgi_app = ProxyFix(app.wsgi_app, x_for=count, x_proto=count)  # type: ignore[method-assign]


def init_app(config: Config | None = None):
    logs.configure_logging()
    if config is None:
        config = Config.from_config_dir(default_config_dir())

    app = Flask(__name__)
    app.config.from_mapping(**json.loads(config.json()))
    _configure_security(app)
    app.url_map.strict_slashes = False
    db.init_app(app)
    app.before_request(clear_trailing)
    auth.init_app(app)
    app.register_blueprint(blueprint.app)
    app.register_blueprint(health.ops)

    _register_log_secrets(app, config)

    if config.DISABLE_AUTO_SCRAPE is False:
        futuresboard.scraper.auto_scrape(app)

    # Nivel: lo fija logs.configure_logging (FUTURESBOARD_LOG_LEVEL) en el root logger.
    telegram_notify.log_startup_state(app.logger)

    return app


def _register_log_secrets(app: Flask, config: Config) -> None:
    """Valores que nunca deben aparecer en los logs (ver futuresboard.logs.redact)."""
    for value in (
        config.API_KEY,
        config.API_SECRET,
        app.secret_key,
        os.environ.get("FUTURESBOARD_SECRET_KEY"),
        os.environ.get(health.ENV_METRICS_TOKEN),
    ):
        logs.register_secret(value)
