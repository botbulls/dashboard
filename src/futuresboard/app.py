from __future__ import annotations

import json
import logging
import os
import pathlib
import secrets

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


def default_config_dir() -> pathlib.Path:
    """Directorio de config: env FUTURESBOARD_CONFIG_DIR o ``./config`` (igual que el CLI).

    Lo usan el CLI y el entrypoint WSGI (gunicorn), asi ambos leen el mismo config.json y
    la misma DB.
    """
    env_dir = os.environ.get("FUTURESBOARD_CONFIG_DIR", "").strip()
    if env_dir:
        return pathlib.Path(env_dir).resolve()
    return pathlib.Path.cwd() / "config"


def init_app(config: Config | None = None):
    if config is None:
        config = Config.from_config_dir(default_config_dir())

    app = Flask(__name__)
    app.secret_key = os.environ.get("FUTURESBOARD_SECRET_KEY") or secrets.token_hex(32)
    app.config.from_mapping(**json.loads(config.json()))
    app.url_map.strict_slashes = False
    db.init_app(app)
    app.before_request(clear_trailing)
    auth.init_app(app)
    app.register_blueprint(blueprint.app)

    if config.DISABLE_AUTO_SCRAPE is False:
        futuresboard.scraper.auto_scrape(app)

    app.logger.setLevel(logging.INFO)

    return app
