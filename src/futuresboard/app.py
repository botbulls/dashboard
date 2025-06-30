from __future__ import annotations

import json
import logging
import pathlib
from typing import TYPE_CHECKING

from flask import Flask
from flask import redirect
from flask import request

import futuresboard.scraper
from futuresboard import blueprint
from futuresboard import db
from futuresboard.config import Config

if TYPE_CHECKING:  # pragma: no cover
    from flask_compress import Compress
    from flask_caching import Cache
else:  # runtime lazy import to avoid missing stubs during type-checking
    from importlib import import_module
    Compress = import_module("flask_compress").Compress  # type: ignore
    Cache = import_module("flask_caching").Cache  # type: ignore


def clear_trailing():
    rp = request.path
    if rp != "/" and rp.endswith("/"):
        return redirect(rp[:-1])


def init_app(config: Config | None = None):
    if config is None:
        config = Config.from_config_dir(pathlib.Path.cwd())

    app = Flask(__name__)
    app.config.from_mapping(**json.loads(config.json()))
    app.url_map.strict_slashes = False
    db.init_app(app)
    app.before_request(clear_trailing)
    app.register_blueprint(blueprint.app)

    # Enable gzip / brotli compression for all eligible responses
    Compress(app)

    # Instruct browsers to cache static assets for one year
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

    # Simple in-memory caching; for production swap with Redis/Memcached
    cache = Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 30})
    app.extensions["cache"] = cache  # store for import-time access if needed

    if config.DISABLE_AUTO_SCRAPE is False:
        futuresboard.scraper.auto_scrape(app)

    app.logger.setLevel(logging.INFO)

    return app
