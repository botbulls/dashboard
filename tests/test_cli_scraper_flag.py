from __future__ import annotations

import json
import sys
from unittest import mock

import pytest

import futuresboard.app
from futuresboard import cli
from futuresboard.config import Config


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FUTURESBOARD_DISABLE_AUTO_SCRAPE", raising=False)
    monkeypatch.delenv("FUTURESBOARD_CONFIG_DIR", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"API_KEY": "k", "API_SECRET": "s"}))
    return cfg


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["futuresboard", *argv])
    with mock.patch("futuresboard.scraper.auto_scrape") as auto, mock.patch(
        "flask.Flask.run"
    ) as run, mock.patch("futuresboard.scraper.scrape") as scrape:
        try:
            cli.main()
        except SystemExit:
            pass
    return auto, run, scrape


def test_scraper_runs_by_default(monkeypatch, config_dir):
    auto, run, _ = run_cli(monkeypatch, "-c", str(config_dir))
    assert auto.called
    assert run.called


def test_disable_auto_scraper_flag_has_effect(monkeypatch, config_dir):
    auto, run, _ = run_cli(monkeypatch, "-c", str(config_dir), "--disable-auto-scraper")
    assert not auto.called
    assert run.called


def test_scrape_only_does_not_start_background_thread(monkeypatch, config_dir):
    auto, run, scrape = run_cli(monkeypatch, "-c", str(config_dir), "--scrape-only")
    assert not auto.called
    assert scrape.called
    assert not run.called


def test_disable_auto_scrape_env(monkeypatch, config_dir):
    monkeypatch.setenv("FUTURESBOARD_DISABLE_AUTO_SCRAPE", "1")
    assert Config.from_config_dir(config_dir).DISABLE_AUTO_SCRAPE is True
    auto, _, _ = run_cli(monkeypatch, "-c", str(config_dir))
    assert not auto.called


def test_disable_auto_scrape_config_json(monkeypatch, config_dir):
    (config_dir / "config.json").write_text(
        json.dumps({"API_KEY": "k", "API_SECRET": "s", "DISABLE_AUTO_SCRAPE": True})
    )
    auto, _, _ = run_cli(monkeypatch, "-c", str(config_dir))
    assert not auto.called


def test_default_config_dir_is_cwd_config(monkeypatch, tmp_path):
    monkeypatch.delenv("FUTURESBOARD_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert futuresboard.app.default_config_dir() == tmp_path / "config"


def test_default_config_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUTURESBOARD_CONFIG_DIR", str(tmp_path / "otra"))
    assert futuresboard.app.default_config_dir() == (tmp_path / "otra").resolve()


def test_wsgi_entrypoint_reads_config_subdir(monkeypatch, config_dir):
    """gunicorn importa futuresboard.wsgi desde WORKDIR: debe leer ./config/config.json."""
    monkeypatch.chdir(config_dir.parent)
    monkeypatch.setenv("FUTURESBOARD_DISABLE_AUTO_SCRAPE", "1")
    sys.modules.pop("futuresboard.wsgi", None)
    with mock.patch("futuresboard.scraper.auto_scrape") as auto:
        import futuresboard.wsgi as wsgi
    try:
        assert not auto.called
        assert wsgi.app.config["API_KEY"] == "k"
        assert wsgi.app.config["DATABASE"] == str((config_dir / "futures.db").resolve())
    finally:
        sys.modules.pop("futuresboard.wsgi", None)
