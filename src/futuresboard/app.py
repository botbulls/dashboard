from __future__ import annotations

import json
import logging
import os
import pathlib
import secrets
import socket

import requests

from flask import Flask
from flask import redirect
from flask import request
from werkzeug.middleware.proxy_fix import ProxyFix

import futuresboard.scraper
from futuresboard import auth
from futuresboard import blueprint
from futuresboard import db
from futuresboard.config import Config


def clear_trailing():
    rp = request.path
    if rp != "/" and rp.endswith("/"):
        return redirect(rp[:-1])


def _get_server_ip():
    """Get the server's public IP address."""
    # First, try to get from environment variable (useful for Docker/containers)
    public_ip = os.environ.get('FUTURESBOARD_PUBLIC_IP')
    if public_ip:
        return public_ip.strip()
    
    # Try to get public IP from external services
    services = [
        'https://api.ipify.org',
        'https://ifconfig.me/ip',
        'https://icanhazip.com',
        'https://checkip.amazonaws.com',
    ]
    
    for service in services:
        try:
            response = requests.get(service, timeout=3)
            if response.status_code == 200:
                ip = response.text.strip()
                # Validate it's a valid IP address
                try:
                    socket.inet_aton(ip)
                    return ip
                except socket.error:
                    continue
        except Exception:
            continue
    
    # Fallback: try to get from socket (may be private IP in Docker)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = '127.0.0.1'
        finally:
            s.close()
        return ip
    except Exception:
        return '127.0.0.1'


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
    if config is None:
        config = Config.from_config_dir(pathlib.Path.cwd())

    app = Flask(__name__)
    app.config.from_mapping(**json.loads(config.json()))
    _configure_security(app)
    app.url_map.strict_slashes = False
    db.init_app(app)
    app.before_request(clear_trailing)
    auth.init_app(app)
    app.register_blueprint(blueprint.app)

    # Add context processor to pass server IP to all templates
    @app.context_processor
    def inject_server_ip():
        return {'server_ip': _get_server_ip()}

    if config.DISABLE_AUTO_SCRAPE is False:
        futuresboard.scraper.auto_scrape(app)

    app.logger.setLevel(logging.INFO)

    return app
