"""Configuracion de gunicorn para produccion.

Uso (es el CMD de la imagen)::

    gunicorn --config python:futuresboard.gunicorn_conf futuresboard.wsgi:app

Por que 1 worker y varios threads (y no N workers):

* ``init_app`` arranca el hilo del scraper automatico dentro del proceso. Con N workers habria
  N scrapers en paralelo pegandole a Binance con la misma API key (rate limit / weight) y
  escribiendo la misma SQLite.
* El rate limit del login (PR de login-hardening) vive en memoria del proceso
  (``threading.Lock`` + dict): con N workers cada uno tendria su propio contador.
* Si ``FUTURESBOARD_SECRET_KEY`` no esta definida, cada proceso generaria su propia clave
  efimera y las sesiones fallarian al caer en otro worker.
* El lock de acciones del panel (``bot_control.Store.lock``) es ``fcntl.flock`` sobre un
  archivo, asi que si funciona entre procesos: no es la razon, pero tampoco se rompe.

``preload_app`` queda en False a proposito: con preload la app (y el hilo del scraper) se
crearia en el master y los hilos no sobreviven al fork, asi que el worker no scrapearia.
El trabajo es I/O (SQLite, Docker API, Binance), por eso alcanza con threads (gthread).

Variables de entorno:

* ``FUTURESBOARD_HOST`` (default 0.0.0.0) / ``FUTURESBOARD_PORT`` (default 5000)
* ``FUTURESBOARD_GUNICORN_THREADS`` (default 8)
* ``FUTURESBOARD_GUNICORN_TIMEOUT`` (default 60 s; una accion del panel puede esperar ~30 s a Docker)
* ``FUTURESBOARD_LOG_LEVEL`` / ``FUTURESBOARD_LOG_FORMAT`` / ``FUTURESBOARD_ACCESS_LOG`` (ver logs.py)
* ``FUTURESBOARD_SECRET_KEY``: la lee la app desde el entorno del worker (gunicorn lo hereda).
"""
from __future__ import annotations

import os

from futuresboard import logs


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


bind = "{}:{}".format(
    os.environ.get("FUTURESBOARD_HOST", "").strip() or "0.0.0.0",
    _int_env("FUTURESBOARD_PORT", 5000),
)

# Fijo en 1 (ignora WEB_CONCURRENCY). Ver docstring.
workers = 1
worker_class = "gthread"
threads = _int_env("FUTURESBOARD_GUNICORN_THREADS", 8)
timeout = _int_env("FUTURESBOARD_GUNICORN_TIMEOUT", 60, minimum=10)
graceful_timeout = 30
keepalive = 5
preload_app = False

access_log_format = logs.ACCESS_LOG_FORMAT
logconfig_dict = logs.gunicorn_logconfig_dict()


def on_starting(server):  # pragma: no cover - lo ejecuta gunicorn
    if server.cfg.workers != 1 or server.cfg.preload_app:
        raise RuntimeError(
            "futuresboard requiere workers=1 y sin preload (scraper y rate limit en proceso); "
            f"recibido workers={server.cfg.workers} preload={server.cfg.preload_app}."
        )
