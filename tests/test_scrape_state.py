from __future__ import annotations

from unittest import mock

import pytest

from futuresboard import scraper
from futuresboard.app import init_app
from futuresboard.config import Config


@pytest.fixture
def app(tmp_path):
    cfg = Config(
        CONFIG_DIR=tmp_path,
        DATABASE=tmp_path / "futures.db",
        API_KEY="x",
        API_SECRET="x",
        DISABLE_AUTO_SCRAPE=True,
    )
    return init_app(cfg)


def test_successful_scrape_records_timestamps(app):
    with app.app_context(), mock.patch.object(scraper, "_scrape"):
        scraper.scrape(app=app)
        state = scraper.read_scrape_state(app.config["DATABASE"])
    assert state["last_success_at"] >= state["last_started_at"]
    assert "last_error_at" not in state


def test_http_error_recorded_without_signature(app):
    err = scraper.HTTPRequestError(url="https://x/fapi?timestamp=1&signature=deadbeef", code=-1021)
    with app.app_context(), mock.patch.object(scraper, "_scrape", side_effect=err):
        scraper.scrape(app=app)
        state = scraper.read_scrape_state(app.config["DATABASE"])
    assert "last_success_at" not in state
    assert state["last_error"] == "HTTP error code -1021"
    assert "deadbeef" not in str(err)


def test_unexpected_error_recorded_and_reraised(app):
    with app.app_context(), mock.patch.object(scraper, "_scrape", side_effect=KeyError("x")):
        with pytest.raises(KeyError):
            scraper.scrape(app=app)
        state = scraper.read_scrape_state(app.config["DATABASE"])
    assert state["last_error"] == "KeyError"


def test_success_after_error_keeps_both(app):
    with app.app_context():
        with mock.patch.object(scraper, "_scrape", side_effect=scraper.HTTPRequestError("u", -1)):
            scraper.scrape(app=app)
        with mock.patch.object(scraper, "_scrape"):
            scraper.scrape(app=app)
        state = scraper.read_scrape_state(app.config["DATABASE"])
    assert state["last_success_at"] >= state["last_error_at"]


def test_read_state_missing_or_corrupt(tmp_path):
    db = tmp_path / "futures.db"
    assert scraper.read_scrape_state(db) == {}
    scraper.scrape_state_path(db).write_text("{no json")
    assert scraper.read_scrape_state(db) == {}
