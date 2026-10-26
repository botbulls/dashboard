"""GET /api/bot/estrategia y /api/bot/estrategia/candidatos (spec bot-estrategia § 4), chequeos de forager por ciclo en
Bot (§ 2.3). Binance público falso (``estrategia_fakes``), sin red.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from estrategia_fakes import Red
from estrategia_fakes import manifiesto_fixture
from futuresboard import auth
from futuresboard import binance_publico
from futuresboard import bot_control
from futuresboard import chequeos_forager
from futuresboard import manifiesto
from futuresboard.app import init_app
from futuresboard.config import Config

from test_bot_control import docker  # noqa: F401
from test_bot_control import router  # noqa: F401

FORAGER = {
    "user": "binance_01", "twe_long": 4, "twe_short": 1, "n_longs": 4, "n_shorts": 0, "leverage": 10,
    "max_min_cost": 150, "market_age_threshold": "2023-01", "update_interval_minutes": 15,
    "ohlcv_interval": "15m", "price_distance_threshold": 0.07,
    "volume_clip_threshold": 0.51, "unilateralness_clip_threshold": 0.49, "noisiness_clip_threshold": 0.52,
    "symbols_to_ignore": ["BAKEUSDT", "INJUSDT"],
    "approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
    "approved_symbols_short": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
    "no_long": ["SOLUSDT"], "no_short": ["SOLUSDT"],
    "live_configs_dir_long": "configs/live/single_symbol_optimizations/clock/",
    "default_config_path": "configs/live/cargaydescarga.json",
    "long_mode": "normal", "short_mode": "normal",
}
# Lo de dev al 25/10: BNB con una orden de compra, BTC/ETH/XRP con posición (ETH y XRP con una venta).
POSICIONES = [("BTCUSDT", -1.1, 10, 80827.91, "LONG", 0.0043), ("ETHUSDT", -3.53, 10, 2365.03, "LONG", 0.226),
              ("XRPUSDT", -0.07, 10, 1.29998, "LONG", 144.8)]
ORDENES = [(0.07, 741.54, "BUY", "LONG", "NEW", "BNBUSDT", 1, "LIMIT"),
           (0.226, 2415.24, "SELL", "LONG", "NEW", "ETHUSDT", 1, "LIMIT"),
           (144.8, 1.368, "SELL", "LONG", "NEW", "XRPUSDT", 1, "LIMIT")]


@pytest.fixture
def red(monkeypatch):
    r = Red()
    monkeypatch.setattr(binance_publico, "_get", r)
    return r


@pytest.fixture
def forager(tmp_path, monkeypatch):
    d = tmp_path / "forager"
    d.mkdir()
    path = d / "new.json"
    path.write_text(json.dumps(FORAGER, indent=2))
    (d / manifiesto.NOMBRE).write_text(json.dumps(manifiesto_fixture()))
    monkeypatch.setenv(bot_control.ENV_DOCKER_URL, "http://docker-proxy:2375")
    monkeypatch.setenv(bot_control.ENV_CONTAINER, "client17-passivbot")
    monkeypatch.setenv(bot_control.ENV_FORAGER_CONFIG, str(path))
    monkeypatch.setenv(bot_control.ENV_MODES_SUPPORTED, "1")
    monkeypatch.setenv(bot_control.ENV_FORAGER_WARMUP, "0")
    return path


@pytest.fixture
def app(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = Config(CONFIG_DIR=tmp_path, DATABASE=data_dir / "futures.db", API_KEY="x", API_SECRET="x",
                 DISABLE_AUTO_SCRAPE=True, BINANCE_TESTNET=True)
    application = init_app(cfg)
    application.config["TESTING"] = True
    with sqlite3.connect(str(application.config["DATABASE"])) as con:
        con.executemany("INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, "
                        "positionAmt) VALUES (?, ?, ?, ?, ?, ?)", POSICIONES)
        con.executemany("INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) "
                        "VALUES (?,?,?,?,?,?,?,?)", ORDENES)
        con.execute("INSERT OR REPLACE INTO account (AID, totalWalletBalance) VALUES (1, 5417.55)")
    latido_fresco(application)
    yield application


def latido_fresco(application, edad_s=0.0, intervalo=60):
    """Latido del scraper: la regla de exposición solo confía en una base que el scraper actualizó hace ≤ 3 vueltas."""
    import time

    from futuresboard import scraper

    scraper.latido_path(application.config["DATABASE"]).write_text(
        json.dumps({"at": time.time() - edad_s, "intervalo": intervalo}))


@pytest.fixture
def client(app):
    auth.set_user_password(str(app.config["DATABASE"]), "cliente17", "clave-de-test")
    with sqlite3.connect(str(app.config["DATABASE"])) as conn:
        pw_hash = conn.execute("SELECT password_hash FROM users WHERE username = 'cliente17'").fetchone()[0]
    with app.app_context():
        fingerprint = auth.password_fingerprint(pw_hash)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["username"] = "cliente17"
        sess[auth.SESSION_FINGERPRINT_KEY] = fingerprint
        sess["csrf_token"] = "tok"
    return c


def _par(data, sym):
    return next(p for p in data["pares"] if p["symbol"] == sym)


def _chequeo(par, clave):
    return next(c for c in par["chequeos"] if c["clave"] == clave)


# ---------------------------------------------------------------- GET /api/bot/estrategia


@pytest.mark.parametrize("metodo,url", [
    ("get", "/api/bot/estrategia"), ("get", "/api/bot/estrategia/candidatos?q=SOL"),
    ("get", "/api/bot/estrategia/versiones"), ("get", "/api/bot/estrategia/rotacion?pares=BTCUSDT&n=1"),
    ("post", "/api/bot/estrategia"), ("post", "/api/bot/estrategia/volver")])
def test_requieren_login(app, metodo, url):
    resp = getattr(app.test_client(), metodo)(url, json={})
    assert resp.status_code in (302, 401)


def test_estrategia_vigente_con_chequeos(client, forager, red, docker):  # noqa: F811
    resp = client.get("/api/bot/estrategia")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    d = resp.get_json()
    assert d["vigente"]["pares_long"] == FORAGER["approved_symbols_long"] and d["vigente"]["n_longs"] == 4
    assert d["base_hash"] == bot_control.estrategia_hash(FORAGER)
    assert d["endpoint"] == "Demo"
    assert d["manifiesto"]["usable"] is True
    assert d["balance"]["para_dimensionar"] == pytest.approx(5417.55)
    # Lugares: BNB (orden) + BTC, ETH, XRP (posiciones): 4 de 4; hoy n_longs queda fijo en 4.
    assert d["lugares"]["ocupados"] == ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]
    assert d["lugares"]["minimo"] == 4 and d["lugares"]["maximo"] == 4
    btc = _par(d, "BTCUSDT")
    assert not btc["bloquea"]
    assert "Mínimo 50,00 de 150" in _chequeo(btc, "minimo")["texto"]
    assert _chequeo(btc, "medido")["estado"] == "ok" and _chequeo(btc, "config_medida")["estado"] == "ok"
    assert btc["ahora"]["posicion"] == pytest.approx(0.0043)
    bnb = _par(d, "BNBUSDT")
    assert bnb["ahora"]["compras"] == 1 and bnb["ahora"]["posicion"] == 0
    assert d["derivados"]["lw"] == pytest.approx(1.0)
    assert d["derivados"]["max_total"] == pytest.approx(21670.2)
    assert d["derivados"]["caida_liquida_pct"] == pytest.approx(24.0)
    assert d["derivados"]["sin_margen_upnl_pct"] == pytest.approx(-60.0)
    claves = [a["clave"] for a in d["avanzado"]]
    assert "max_min_cost" in claves and "no_long / no_short" in claves
    assert "no tienen efecto" in next(a for a in d["avanzado"] if a["clave"] == "no_long / no_short")["efecto"]


def test_estrategia_sin_manifiesto_lo_dice(client, forager, red, docker):  # noqa: F811
    (forager.parent / manifiesto.NOMBRE).unlink()
    d = client.get("/api/bot/estrategia").get_json()
    assert d["manifiesto"]["usable"] is False and "passivbot todavía no publicó" in d["manifiesto"]["motivo"]
    # Los pares que ya están no se bloquean por eso (se pueden quitar y cambiar TWE): solo "sin verificar".
    assert _chequeo(_par(d, "BTCUSDT"), "config_medida")["estado"] == "info"


def test_estrategia_sin_config_de_forager(client, monkeypatch, red):
    monkeypatch.delenv(bot_control.ENV_FORAGER_CONFIG, raising=False)
    monkeypatch.setenv(bot_control.ENV_DOCKER_URL, "http://docker-proxy:2375")
    resp = client.get("/api/bot/estrategia")
    assert resp.status_code == 503 and resp.get_json()["ok"] is False


# ---------------------------------------------------------------- candidatos


def test_candidatos_sol(client, forager, red, docker):  # noqa: F811
    resp = client.get("/api/bot/estrategia/candidatos?q=sol")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    d = resp.get_json()
    syms = [r["symbol"] for r in d["resultados"]]
    assert syms[0] == "SOLUSDT" and "SOLVUSDT" in syms and len(syms) <= 10
    sol = d["resultados"][0]
    assert not sol["bloquea"] and sol["sin_backtest"]
    solv = next(r for r in d["resultados"] if r["symbol"] == "SOLVUSDT")
    assert solv["bloquea"]
    assert any("cargaydescarga.json" in m for m in solv["motivos"])
    assert any("03/2025" in m for m in solv["motivos"])


def test_candidatos_marca_los_que_ya_estan(client, forager, red, docker):  # noqa: F811
    d = client.get("/api/bot/estrategia/candidatos?q=BTC").get_json()
    btc = next(r for r in d["resultados"] if r["symbol"] == "BTCUSDT")
    assert btc["ya_esta"] is True


@pytest.mark.parametrize("q", ["", "SO L", "<script>", "x" * 30])
def test_candidatos_q_invalido(client, forager, red, docker, q):  # noqa: F811
    resp = client.get("/api/bot/estrategia/candidatos", query_string={"q": q})
    assert resp.status_code == 400


def test_estrategia_con_manifiesto_viejo_no_deja_agregar(client, forager, red, docker):  # noqa: F811
    docker.running = True
    docker.started_at = "2026-10-26T12:35:53.000000000Z"
    data = client.get("/api/bot/estrategia").get_json()
    assert data["manifiesto"]["estado"] == "viejo" and not data["manifiesto"]["usable"]
    cand = client.get("/api/bot/estrategia/candidatos?q=SOL").get_json()
    assert cand["manifiesto"]["estado"] == "viejo"
    sol = next(r for r in cand["resultados"] if r["symbol"] == "SOLUSDT")
    assert sol["bloquea"]


def test_candidatos_sin_manifiesto_bloquea_con_motivo(client, forager, red, docker):  # noqa: F811
    (forager.parent / manifiesto.NOMBRE).unlink()
    d = client.get("/api/bot/estrategia/candidatos?q=SOL").get_json()
    sol = d["resultados"][0]
    assert sol["bloquea"] and any("passivbot todavía no publicó" in m for m in sol["motivos"])


def test_candidatos_con_binance_caido(client, forager, red, docker):  # noqa: F811
    red.demo.fallar.add("/fapi/v1/exchangeInfo")
    resp = client.get("/api/bot/estrategia/candidatos?q=SOL")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["error"] and "Binance" in d["error"]


# ---------------------------------------------------------------- Bot: chequeos de forager por ciclo


def test_bot_muestra_los_chequeos_del_ciclo_sin_llamar_a_binance(client, app, forager, docker):  # noqa: F811
    # Sin Red: si /bot llamara a Binance, el guard de conftest lo haría fallar.
    estado = {"ts": "2026-10-25T22:00:00+00:00", "endpoint": "Demo", "intervalo_s": 900, "error": None, "pares": {
        "BTCUSDT": {"estado": "ok", "conocido": "ok", "motivos": [], "chequeos": [
            {"clave": "minimo", "estado": "info", "texto": "Mínimo 50,00 de 150 (real 80,02: forager lo saca si BTC pasa de 150.000)"}]},
        "XRPUSDT": {"estado": "descarta", "conocido": "descarta", "motivos": ["Binance (Demo) lo tiene en SETTLING: no se opera"],
                    "chequeos": []},
    }}
    data_dir = app.config["DATABASE"]
    (pathlib_parent(data_dir) / chequeos_forager.ESTADO_FILE).write_text(json.dumps(estado))
    html = client.get("/bot").get_data(as_text=True)
    bloque = html.split("data-bot-chequeos-forager", 1)[1].split("</ul>", 1)[0]
    assert "XRPUSDT" in bloque and "forager lo descartaría" in bloque and "SETTLING" in bloque
    assert "Mínimo 50,00 de 150" in bloque
    assert "hidden" not in html.split("data-bot-chequeos-forager", 1)[1][:40]


def test_bot_sin_chequeos_todavia(client, forager, docker):  # noqa: F811
    html = client.get("/bot").get_data(as_text=True)
    assert "Todavía no hay chequeos de forager" in html


def pathlib_parent(p):
    import pathlib

    return pathlib.Path(p).parent
