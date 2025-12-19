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


def init_app(config: Config | None = None):
    if config is None:
        config = Config.from_config_dir(pathlib.Path.cwd())

    app = Flask(__name__)
    app.secret_key = os.environ.get("FUTURESBOARD_SECRET_KEY") or secrets.token_hex(32)
    app.config.from_mapping(**json.loads(config.json()))
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
