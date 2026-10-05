"""Logging del dashboard: formato consistente (texto o JSON) y redaccion de secretos.

Variables de entorno:

* ``FUTURESBOARD_LOG_LEVEL``  DEBUG/INFO/WARNING/ERROR (default INFO).
* ``FUTURESBOARD_LOG_FORMAT`` ``text`` (default) o ``json`` (una linea JSON por evento:
  ``ts``, ``level``, ``logger``, ``msg`` y ``exc`` si hay traceback).
* ``FUTURESBOARD_ACCESS_LOG`` ``0`` desactiva el access log de gunicorn (default activo).

Todo mensaje pasa por :func:`redact` antes de escribirse: oculta firmas HMAC (``signature=``),
tokens Bearer y los valores registrados con :func:`register_secret` (API key/secret de la
config, secret key de Flask, token de metricas).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import threading
from typing import Any
from typing import Dict
from typing import Set

ENV_LOG_LEVEL = "FUTURESBOARD_LOG_LEVEL"
ENV_LOG_FORMAT = "FUTURESBOARD_LOG_FORMAT"
ENV_ACCESS_LOG = "FUTURESBOARD_ACCESS_LOG"

TEXT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
TEXT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

# Formato del access log de gunicorn (el timestamp lo pone el formatter).
ACCESS_LOG_FORMAT = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'

REDACTED = "<redacted>"
_MIN_SECRET_LEN = 6
_PATTERNS = (
    re.compile(r"(signature=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[^\s'\"]+", re.IGNORECASE),
    re.compile(r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+", re.IGNORECASE),
    re.compile(r"(X-BAPI-API-KEY['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+", re.IGNORECASE),
)

_secrets: Set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: Any) -> None:
    """Agrega un valor literal a ocultar en los logs (se ignoran valores muy cortos)."""
    if isinstance(value, str) and len(value.strip()) >= _MIN_SECRET_LEN:
        with _secrets_lock:
            _secrets.add(value.strip())


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    with _secrets_lock:
        values = sorted(_secrets, key=len, reverse=True)
    for value in values:
        if value in text:
            text = text.replace(value, REDACTED)
    return text


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(fmt=TEXT_FORMAT, datefmt=TEXT_DATEFMT)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exc"] = redact(record.exc_text)
        return json.dumps(payload, ensure_ascii=False)


def log_level() -> str:
    level = os.environ.get(ENV_LOG_LEVEL, "").strip().upper() or "INFO"
    return level if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO"


def log_format() -> str:
    fmt = os.environ.get(ENV_LOG_FORMAT, "").strip().lower()
    return "json" if fmt == "json" else "text"


def make_formatter() -> logging.Formatter:
    return JsonFormatter() if log_format() == "json" else TextFormatter()


def _is_ours(handler: logging.Handler) -> bool:
    return isinstance(handler.formatter, (TextFormatter, JsonFormatter))


def configure_logging() -> None:
    """Instala un handler en el root logger con el formato elegido (idempotente).

    Si el root ya tiene un handler con nuestros formatters (por ejemplo, configurado por
    gunicorn via ``logconfig_dict``) solo ajusta el nivel, para no duplicar lineas.
    """
    root = logging.getLogger()
    root.setLevel(log_level())
    if any(_is_ours(h) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(make_formatter())
    root.addHandler(handler)


def access_log_enabled() -> bool:
    return os.environ.get(ENV_ACCESS_LOG, "1").strip().lower() not in ("0", "false", "no", "off")


def gunicorn_logconfig_dict() -> Dict[str, Any]:
    """``logconfig_dict`` para gunicorn: mismo formato para la app, gunicorn.error y access."""
    level = log_level()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"futuresboard": {"()": "futuresboard.logs.make_formatter"}},
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "futuresboard",
                "stream": "ext://sys.stderr",
            }
        },
        "root": {"level": level, "handlers": ["console"]},
        "loggers": {
            "gunicorn.error": {"level": level, "handlers": ["console"], "propagate": False},
            "gunicorn.access": {
                "level": "INFO" if access_log_enabled() else "CRITICAL",
                "handlers": ["console"],
                "propagate": False,
            },
        },
    }
