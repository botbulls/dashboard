from __future__ import annotations

import importlib

import pytest


def load(monkeypatch, **env):
    for name in ("FUTURESBOARD_HOST", "FUTURESBOARD_PORT", "FUTURESBOARD_GUNICORN_THREADS",
                 "FUTURESBOARD_GUNICORN_TIMEOUT", "WEB_CONCURRENCY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    import futuresboard.gunicorn_conf as conf

    return importlib.reload(conf)


def test_defaults(monkeypatch):
    conf = load(monkeypatch)
    assert conf.bind == "0.0.0.0:5000"
    assert conf.workers == 1
    assert conf.worker_class == "gthread"
    assert conf.threads == 8
    assert conf.preload_app is False
    assert "gunicorn.access" in conf.logconfig_dict["loggers"]


def test_env_overrides_but_workers_fixed(monkeypatch):
    conf = load(monkeypatch, FUTURESBOARD_HOST="127.0.0.1", FUTURESBOARD_PORT="8080",
                FUTURESBOARD_GUNICORN_THREADS="4", WEB_CONCURRENCY="4")
    assert conf.bind == "127.0.0.1:8080"
    assert conf.threads == 4
    assert conf.workers == 1


def test_invalid_values_fall_back(monkeypatch):
    conf = load(monkeypatch, FUTURESBOARD_GUNICORN_THREADS="abc", FUTURESBOARD_GUNICORN_TIMEOUT="0")
    assert conf.threads == 8
    assert conf.timeout == 10


@pytest.mark.parametrize("workers,preload", [(2, False), (1, True)])
def test_on_starting_rejects_unsafe_settings(monkeypatch, workers, preload):
    conf = load(monkeypatch)

    class Cfg:
        pass

    class Server:
        cfg = Cfg()

    Server.cfg.workers = workers
    Server.cfg.preload_app = preload
    with pytest.raises(RuntimeError):
        conf.on_starting(Server())
