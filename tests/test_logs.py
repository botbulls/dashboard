from __future__ import annotations

import io
import json
import logging

import pytest

from futuresboard import logs
from futuresboard.app import init_app
from futuresboard.config import Config


def _record(msg, *args, exc_info=None):
    return logging.LogRecord("futuresboard.test", logging.WARNING, __file__, 1, msg, args, exc_info)


def test_redacts_signature_bearer_and_registered_secrets():
    logs.register_secret("MY-API-SECRET-VALUE")
    text = logs.redact(
        "GET /fapi?timestamp=1&signature=abcdef0123 Authorization: Bearer tok-xyz MY-API-SECRET-VALUE"
    )
    assert "abcdef0123" not in text
    assert "tok-xyz" not in text
    assert "MY-API-SECRET-VALUE" not in text
    assert text.count(logs.REDACTED) == 3


def test_short_values_are_not_registered():
    logs.register_secret("x")
    assert logs.redact("x marks the spot") == "x marks the spot"


def test_json_formatter_fields_and_redaction():
    logs.register_secret("ANOTHER-SECRET-123")
    try:
        raise ValueError("fallo con ANOTHER-SECRET-123")
    except ValueError:
        import sys

        rec = _record("error %s", "ANOTHER-SECRET-123", exc_info=sys.exc_info())
    out = json.loads(logs.JsonFormatter().format(rec))
    assert set(out) == {"ts", "level", "logger", "msg", "exc"}
    assert out["level"] == "WARNING"
    assert out["logger"] == "futuresboard.test"
    assert out["ts"].endswith("+00:00")
    assert "ANOTHER-SECRET-123" not in out["msg"]
    assert "ANOTHER-SECRET-123" not in out["exc"]


def test_text_formatter_is_consistent():
    line = logs.TextFormatter().format(_record("hola"))
    assert " WARNING [futuresboard.test] hola" in line


@pytest.mark.parametrize("value,expected", [("", "text"), ("json", "json"), ("JSON", "json"), ("xml", "text")])
def test_log_format_env(monkeypatch, value, expected):
    monkeypatch.setenv(logs.ENV_LOG_FORMAT, value)
    assert logs.log_format() == expected


@pytest.mark.parametrize("value,expected", [("", "INFO"), ("debug", "DEBUG"), ("nope", "INFO")])
def test_log_level_env(monkeypatch, value, expected):
    monkeypatch.setenv(logs.ENV_LOG_LEVEL, value)
    assert logs.log_level() == expected


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    for h in list(root.handlers):
        if isinstance(h.formatter, (logs.TextFormatter, logs.JsonFormatter)):
            root.removeHandler(h)
    yield root
    root.handlers[:] = saved[1]
    root.setLevel(saved[0])


def test_configure_logging_is_idempotent(monkeypatch, clean_root):
    monkeypatch.setenv(logs.ENV_LOG_LEVEL, "WARNING")
    logs.configure_logging()
    logs.configure_logging()
    ours = [h for h in clean_root.handlers if isinstance(h.formatter, (logs.TextFormatter, logs.JsonFormatter))]
    assert len(ours) == 1
    assert clean_root.level == logging.WARNING


def test_app_logs_do_not_leak_config_secrets(monkeypatch, tmp_path, clean_root):
    monkeypatch.setenv(logs.ENV_LOG_FORMAT, "json")
    monkeypatch.setenv("FUTURESBOARD_METRICS_TOKEN", "metrics-token-abc")
    cfg = Config(
        CONFIG_DIR=tmp_path,
        DATABASE=tmp_path / "futures.db",
        API_KEY="binance-key-123456",
        API_SECRET="binance-secret-654321",
        DISABLE_AUTO_SCRAPE=True,
    )
    app = init_app(cfg)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.make_formatter())
    app.logger.addHandler(handler)
    try:
        app.logger.error("key=%s secret=%s token=%s", cfg.API_KEY, cfg.API_SECRET, "metrics-token-abc")
    finally:
        app.logger.removeHandler(handler)
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    for secret in ("binance-key-123456", "binance-secret-654321", "metrics-token-abc"):
        assert secret not in line["msg"]


def test_gunicorn_logconfig_dict(monkeypatch):
    import logging.config

    monkeypatch.setenv(logs.ENV_ACCESS_LOG, "0")
    cfg = logs.gunicorn_logconfig_dict()
    assert cfg["loggers"]["gunicorn.access"]["level"] == "CRITICAL"
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    try:
        logging.config.dictConfig(cfg)
        assert any(isinstance(h.formatter, (logs.TextFormatter, logs.JsonFormatter)) for h in root.handlers)
    finally:
        root.handlers[:] = saved[1]
        root.setLevel(saved[0])
