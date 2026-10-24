"""DB en su propio directorio de datos (FUTURESBOARD_DATABASE) sin mover el estado del panel/rescate."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import types

import pydantic
import pytest

from futuresboard import blueprint
from futuresboard.app import init_app
from futuresboard.config import Config
from futuresboard.config import ENV_AUTO_SCRAPE_INTERVAL
from futuresboard.config import ENV_DATABASE
from futuresboard.config import state_dir


@pytest.fixture(autouse=True)
def _env_limpio(monkeypatch):
    for name in (ENV_DATABASE, ENV_AUTO_SCRAPE_INTERVAL, "FUTURESBOARD_DISABLE_AUTO_SCRAPE",
                 "FUTURESBOARD_CONFIG_DIR", "FUTURESBOARD_BINANCE_TESTNET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def dirs(tmp_path):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"API_KEY": "k" * 12, "API_SECRET": "s" * 12, "AUTO_SCRAPE_INTERVAL": 300})
    )
    return config_dir, data_dir


# --------------------------------------------------------------------------------------
# Config: la DB se mueve por env, el estado (panel, rescate, C3, jobs) no
# --------------------------------------------------------------------------------------


def test_sin_env_todo_queda_como_antes(dirs):
    config_dir, _ = dirs
    cfg = Config.from_config_dir(config_dir)
    assert cfg.DATABASE == config_dir / "futures.db"
    assert cfg.STATE_DIR == config_dir
    assert state_dir(cfg) == config_dir
    assert cfg.AUTO_SCRAPE_INTERVAL == 300


def test_env_database_mueve_solo_la_db(dirs, monkeypatch):
    config_dir, data_dir = dirs
    monkeypatch.setenv(ENV_DATABASE, str(data_dir / "futures.db"))
    cfg = Config.from_config_dir(config_dir)
    assert cfg.DATABASE == data_dir / "futures.db"
    assert cfg.STATE_DIR == config_dir
    assert state_dir(json.loads(cfg.json())) == config_dir  # como lo ve app.config


def test_database_en_config_json_mantiene_el_comportamiento_historico(dirs):
    config_dir, data_dir = dirs
    (config_dir / "config.json").write_text(
        json.dumps({"API_KEY": "k", "API_SECRET": "s", "DATABASE": str(data_dir / "x.db")})
    )
    cfg = Config.from_config_dir(config_dir)
    assert cfg.DATABASE == data_dir / "x.db"
    assert cfg.STATE_DIR == data_dir  # estado junto a la DB, como siempre


def test_state_dir_de_configs_armadas_a_mano(tmp_path):
    assert state_dir({"DATABASE": str(tmp_path / "a" / "f.db")}) == tmp_path / "a"
    assert state_dir(types.SimpleNamespace(DATABASE=tmp_path / "b" / "f.db")) == tmp_path / "b"


def test_intervalo_por_env_gana_y_respeta_el_rango(dirs, monkeypatch):
    config_dir, _ = dirs
    monkeypatch.setenv(ENV_AUTO_SCRAPE_INTERVAL, "60")
    assert Config.from_config_dir(config_dir).AUTO_SCRAPE_INTERVAL == 60
    for malo in ("59", "3601", "rápido"):
        monkeypatch.setenv(ENV_AUTO_SCRAPE_INTERVAL, malo)
        with pytest.raises(pydantic.ValidationError):
            Config.from_config_dir(config_dir)


def test_web_con_db_en_datos_deja_panel_y_rescate_en_config(dirs, monkeypatch):
    config_dir, data_dir = dirs
    monkeypatch.setenv(ENV_DATABASE, str(data_dir / "futures.db"))
    monkeypatch.setenv("FUTURESBOARD_DISABLE_AUTO_SCRAPE", "1")
    app = init_app(Config.from_config_dir(config_dir))
    assert pathlib.Path(app.config["DATABASE"]).parent == data_dir
    with app.test_request_context():
        assert blueprint._bot_store().data_dir == config_dir
    # El esquema (usuarios incluidos) se crea en la DB nueva; config.json queda solo.
    assert (data_dir / "futures.db").exists()
    assert not (config_dir / "futures.db").exists()
    # Con el web apagado como escritor de WAL: nunca cambia el journal_mode por su cuenta.
    with sqlite3.connect(data_dir / "futures.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
