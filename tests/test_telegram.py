from __future__ import annotations

import datetime as dt
import logging
import os
import pathlib
import sqlite3

import pytest
import requests

from futuresboard import bot_control
from futuresboard import notifier
from futuresboard import telegram_notify
from futuresboard.app import init_app

# Fixtures y fakes del panel (Docker + Binance simulados).
from test_bot_control import FakeDocker  # noqa: F401
from test_bot_control import FakeResponse
from test_bot_control import app  # noqa: F401
from test_bot_control import binance  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401

TOKEN = "123456:SECRETO-de-prueba-AAAA"
CHAT = "-1001234"


class FakeTelegram:
    def __init__(self, status=200, exc=None):
        self.status = status
        self.exc = exc
        self.calls = []

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return FakeResponse(self.status, {"ok": self.status == 200, "description": "Bad Request: chat not found"})

    @property
    def texts(self):
        return [c["json"]["text"] for c in self.calls]


@pytest.fixture
def tg_env(monkeypatch):
    monkeypatch.setenv(telegram_notify.ENV_TOKEN, TOKEN)
    monkeypatch.setenv(telegram_notify.ENV_CHAT_ID, CHAT)
    monkeypatch.setenv(telegram_notify.ENV_PREFIX, "[DEV client17]")


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(telegram_notify, "_post", fake)
    # Envío síncrono en los tests de acciones: determinista, sin esperar hilos.
    monkeypatch.setattr(telegram_notify, "_start_thread", lambda fn: fn())
    return fake


# ---------------------------------------------------------------- envío básico


def test_send_formato_html_prefijo_y_timeout(tg_env, tg):
    assert telegram_notify.send("hola <b>x</b>") is True
    call = tg.calls[0]
    assert call["url"] == f"{telegram_notify.API_BASE}/bot{TOKEN}/sendMessage"
    assert call["timeout"] == telegram_notify.TIMEOUT_SECONDS
    body = call["json"]
    assert body["chat_id"] == CHAT and body["parse_mode"] == "HTML"
    assert body["text"].startswith("<b>[DEV client17]</b> ")


def test_prefijo_se_escapa(monkeypatch, tg):
    monkeypatch.setenv(telegram_notify.ENV_TOKEN, TOKEN)
    monkeypatch.setenv(telegram_notify.ENV_CHAT_ID, CHAT)
    monkeypatch.setenv(telegram_notify.ENV_PREFIX, "<cliente&17>")
    telegram_notify.send("x")
    assert tg.texts[0].startswith("<b>&lt;cliente&amp;17&gt;</b>")


def test_mensaje_largo_se_recorta(tg_env, tg):
    telegram_notify.send("linea\n" * 2000)
    assert len(tg.texts[0]) <= telegram_notify.MAX_MESSAGE_LEN


@pytest.mark.parametrize("missing", [telegram_notify.ENV_TOKEN, telegram_notify.ENV_CHAT_ID])
def test_deshabilitado_sin_env(tg_env, tg, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert telegram_notify.TelegramConfig.from_env().enabled is False
    assert telegram_notify.send("x") is False
    assert telegram_notify.send_async("x") is None
    assert telegram_notify.notify_bot_action("start", {"riesgo": "bajo"}, "ok", {}, "u") is None
    assert tg.calls == []


def test_log_de_arranque_deshabilitado(caplog):
    with caplog.at_level(logging.INFO):
        assert telegram_notify.log_startup_state() is False
    assert "deshabilitadas" in caplog.text


def test_app_loguea_estado_al_arrancar(tmp_path, caplog):
    from futuresboard.config import Config

    cfg = Config(CONFIG_DIR=tmp_path, DATABASE=tmp_path / "futures.db", API_KEY="x", API_SECRET="x",
                 DISABLE_AUTO_SCRAPE=True)
    with caplog.at_level(logging.INFO):
        init_app(cfg)
    assert "Notificaciones Telegram deshabilitadas" in caplog.text


@pytest.mark.parametrize("exc", [requests.ConnectionError(f"Max retries with url: /bot{TOKEN}/sendMessage"),
                                 requests.ReadTimeout(f"/bot{TOKEN}/sendMessage timed out")])
def test_fallo_de_red_no_lanza_ni_loguea_token(tg_env, monkeypatch, caplog, exc):
    monkeypatch.setattr(telegram_notify, "_post", FakeTelegram(exc=exc))
    with caplog.at_level(logging.DEBUG):
        assert telegram_notify.send("x") is False
    assert caplog.text and TOKEN not in caplog.text
    assert type(exc).__name__ in caplog.text


def test_respuesta_no_200_no_loguea_token(tg_env, monkeypatch, caplog):
    fake = FakeTelegram(status=400)
    monkeypatch.setattr(telegram_notify, "_post", fake)
    with caplog.at_level(logging.DEBUG):
        assert telegram_notify.send("x") is False
    assert "400" in caplog.text and TOKEN not in caplog.text


def test_repr_de_config_no_expone_token(tg_env):
    assert TOKEN not in repr(telegram_notify.TelegramConfig.from_env())


def test_send_async_no_bloquea(tg_env, monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(telegram_notify, "_post", fake)
    t = telegram_notify.send_async("hola")
    assert t is not None
    t.join(5)
    assert fake.texts and "hola" in fake.texts[0]


# ---------------------------------------------------------------- eventos del panel


def test_evento_start(client, env, docker, tg_env, tg):  # noqa: F811
    resp = post(client, "/api/bot/start", {"riesgo": "medio"})
    assert resp.status_code == 200
    assert len(tg.texts) == 1
    text = tg.texts[0]
    assert "START riesgo medio" in text and "OK" in text and "cliente17" in text


def test_evento_graceful_stop(client, env, docker, tg_env, tg):  # noqa: F811
    resp = post(client, "/api/bot/stop", {"modo": "graceful"})
    assert resp.status_code == 200
    assert "Graceful stop" in tg.texts[0] and "OK" in tg.texts[0]


def test_evento_apagar_con_resumen(client, env, docker, binance, tg_env, tg):  # noqa: F811
    binance.add_position("BTCUSDT", "0.5")
    binance.orders = [{"symbol": "BTCUSDT"}, {"symbol": "BTCUSDT"}]
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200
    text = tg.texts[0]
    assert "APAGAR" in text and "OK" in text
    assert "Posiciones cerradas: 1" in text and "BTCUSDT LONG 0.5" in text
    assert "Órdenes canceladas: 2" in text and "Errores: 0" in text


def test_evento_apagar_parcial_falla(client, env, docker, binance, tg_env, tg):  # noqa: F811
    binance.add_position("ETHUSDT", "-1")
    binance.stuck.add(("ETHUSDT", "BOTH"))
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 502
    text = tg.texts[0]
    assert "❌" in text and "FALLÓ" in text and "parcial" in text
    assert "Quedó abierto: 1 posiciones" in text


def test_evento_fallo_docker(client, env, docker, tg_env, tg):  # noqa: F811
    docker.fail_action = 500
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 502
    assert "FALLÓ" in tg.texts[0] and "500" in tg.texts[0]


def test_validacion_400_no_notifica(client, env, docker, tg_env, tg):  # noqa: F811
    resp = post(client, "/api/bot/start", {"riesgo": "loco"})
    assert resp.status_code == 400
    assert tg.calls == []


def test_texto_del_evento_se_escapa():
    text = telegram_notify.format_bot_action("stop", {"modo": "graceful"}, "error",
                                             {"error": "<script>&"}, "<u>")
    assert "&lt;script&gt;&amp;" in text and "&lt;u&gt;" in text


@pytest.mark.parametrize("exc", [requests.ConnectionError("x"), RuntimeError("boom")])
def test_fallo_de_telegram_no_rompe_la_accion(client, env, docker, tg_env, monkeypatch, exc):  # noqa: F811
    monkeypatch.setattr(telegram_notify, "_post", FakeTelegram(exc=exc))
    monkeypatch.setattr(telegram_notify, "_start_thread", lambda fn: fn())
    resp = post(client, "/api/bot/start", {"riesgo": "alto"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True


def test_fallo_al_programar_hilo_no_rompe_la_accion(client, env, docker, tg_env, monkeypatch):  # noqa: F811
    def boom(fn):
        raise RuntimeError("no threads")

    monkeypatch.setattr(telegram_notify, "_start_thread", boom)
    resp = post(client, "/api/bot/stop", {"modo": "graceful"})
    assert resp.status_code == 200


# ---------------------------------------------------------------- notifier: salud y dedup

TZ = notifier.TZ


def make_db(path: pathlib.Path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE income (IID integer PRIMARY KEY AUTOINCREMENT, tranId text, symbol text, "
                 "incomeType text, income real, asset text, info text, time integer, tradeId integer)")
    conn.execute("CREATE TABLE positions (PID integer PRIMARY KEY AUTOINCREMENT, symbol text, "
                 "unrealizedProfit real, leverage integer, entryPrice real, positionSide text, positionAmt real)")
    conn.execute("CREATE TABLE account (AID integer PRIMARY KEY, totalWalletBalance real, "
                 "totalUnrealizedProfit real, totalMarginBalance real, availableBalance real, "
                 "maxWithdrawAmount real)")
    conn.commit()
    return conn


class Clock:
    def __init__(self, when):
        self.when = when

    def __call__(self):
        return self.when


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "futures.db"
    make_db(p).close()
    return p


@pytest.fixture
def notifier_env(monkeypatch):
    monkeypatch.setenv("NOTIFIER_FAIL_THRESHOLD", "2")
    monkeypatch.setenv("NOTIFIER_SCRAPE_MAX_AGE", "900")
    monkeypatch.delenv("NOTIFIER_TRADE_MAX_AGE", raising=False)
    monkeypatch.setenv("NOTIFIER_DAILY_SUMMARY", "off")
    monkeypatch.setenv(bot_control.ENV_DOCKER_URL, "http://docker-proxy:2375")
    monkeypatch.setenv(bot_control.ENV_CONTAINER, "client17-passivbot")


def make_monitor(db_path, fake_docker, sent, clock=None, send_ok=True):
    def send(text):
        sent.append(text)
        return send_ok

    clock = clock or Clock(dt.datetime.now(TZ))
    dc = bot_control.DockerClient("http://docker-proxy:2375", session=fake_docker)
    return notifier.Monitor(db_path, docker=dc, send=send, now=clock)


def test_dedup_caido_y_recuperado(db_path, notifier_env):
    fd = FakeDocker(running=True)
    sent = []
    m = make_monitor(db_path, fd, sent)
    assert m.tick() == []                       # todo ok, sin mensajes
    fd.running = False
    assert m.tick() == []                       # 1er fallo: bajo el umbral
    out = m.tick()                              # 2do fallo: avisa caído
    assert len(out) == 1 and "caído" in out[0] and "passivbot" in out[0]
    for _ in range(5):                          # sigue caído: no spamea
        assert m.tick() == []
    fd.running = True
    out = m.tick()
    assert len(out) == 1 and "recuperado" in out[0]
    assert m.tick() == []
    assert len(sent) == 2


def test_flap_bajo_umbral_no_avisa(db_path, notifier_env):
    fd = FakeDocker(running=True)
    sent = []
    m = make_monitor(db_path, fd, sent)
    for _ in range(4):
        fd.running = False
        m.tick()
        fd.running = True
        m.tick()
    assert sent == []


def test_estado_persistente_no_repite_alerta_tras_reinicio(db_path, notifier_env):
    fd = FakeDocker(running=False)
    sent = []
    m = make_monitor(db_path, fd, sent)
    m.tick()
    m.tick()
    assert len(sent) == 1
    m2 = make_monitor(db_path, fd, sent)        # reinicio del notifier
    m2.tick()
    m2.tick()
    assert len(sent) == 1


def test_si_telegram_falla_reintenta_el_aviso(db_path, notifier_env):
    fd = FakeDocker(running=False)
    sent = []
    m = make_monitor(db_path, fd, sent, send_ok=False)
    m.tick()
    m.tick()
    m.tick()
    assert len(sent) == 2                       # se reintenta mientras no se pudo avisar
    assert m.state["checks"]["passivbot"]["status"] == "ok"


def test_proxy_docker_caido_cuenta_como_fallo(db_path, notifier_env):
    class Down:
        def request(self, *a, **k):
            raise requests.ConnectionError("no route")

    sent = []
    m = make_monitor(db_path, Down(), sent)
    m.tick()
    m.tick()
    assert len(sent) == 1 and "ConnectionError" in sent[0]


def test_scrape_viejo_avisa(db_path, notifier_env):
    sent = []
    m = make_monitor(db_path, FakeDocker(), sent)
    old = dt.datetime.now(TZ).timestamp() - 3 * 3600
    os.utime(db_path, (old, old))
    m.tick()
    m.tick()
    assert len(sent) == 1 and "Scrape" in sent[0] and "3.0 h (máx 15 min)" in sent[0]


def test_trade_viejo_opcional(db_path, notifier_env, monkeypatch):
    conn = sqlite3.connect(db_path)
    old_ms = int((dt.datetime.now(TZ).timestamp() - 7200) * 1000)
    conn.execute("INSERT INTO income (incomeType, income, asset, time) VALUES ('REALIZED_PNL', 1, 'USDT', ?)",
                 (old_ms,))
    conn.commit()
    conn.close()
    sent = []
    m = make_monitor(db_path, FakeDocker(), sent)
    m.tick()
    m.tick()
    assert sent == []                           # deshabilitado por default
    monkeypatch.setenv("NOTIFIER_TRADE_MAX_AGE", "3600")
    m = make_monitor(db_path, FakeDocker(), sent)
    m.tick()
    m.tick()
    assert len(sent) == 1 and "Último trade" in sent[0]


def test_sin_docker_url_no_chequea_contenedor(db_path, notifier_env, monkeypatch):
    monkeypatch.delenv(bot_control.ENV_DOCKER_URL)
    m = notifier.Monitor(db_path, send=lambda t: True)
    assert m.docker is None
    assert "passivbot" not in m.run_checks()


# ---------------------------------------------------------------- resumen diario


def seed_day(db_path, day):
    start, end = notifier.day_bounds_ms(day)
    conn = sqlite3.connect(db_path)
    rows = [
        ("REALIZED_PNL", 10.5, "USDT", start + 1000),
        ("COMMISSION", -0.5, "USDT", start + 2000),
        ("FUNDING_FEE", -1.0, "USDT", end - 1000),
        ("TRANSFER", 500.0, "USDT", start + 3000),     # excluido
        ("COMMISSION", -0.1, "BNB", start + 4000),     # excluido (BNB)
        ("REALIZED_PNL", 99.0, "USDT", start - 1),     # día anterior
        ("REALIZED_PNL", 77.0, "USDT", end + 1),       # día siguiente
    ]
    conn.executemany("INSERT INTO income (incomeType, income, asset, time) VALUES (?,?,?,?)", rows)
    conn.execute("INSERT INTO positions (symbol, unrealizedProfit, positionSide, positionAmt) "
                 "VALUES ('BTCUSDT', 3.25, 'LONG', 0.01)")
    conn.execute("INSERT INTO positions (symbol, unrealizedProfit, positionSide, positionAmt) "
                 "VALUES ('ETHUSDT', 0, 'SHORT', 0)")
    conn.execute("INSERT INTO account (AID, totalWalletBalance, totalUnrealizedProfit) VALUES (1, 1000, 3.25)")
    conn.commit()
    conn.close()


def test_resumen_diario_datos_y_zona(db_path):
    day = dt.date(2026, 10, 5)
    seed_day(db_path, day)
    start, _ = notifier.day_bounds_ms(day)
    # Medianoche de Buenos Aires = 03:00 UTC.
    assert dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).hour == 3
    data = notifier.daily_summary_data(db_path, day)
    assert data["ingreso"] == pytest.approx(9.0)
    assert set(data["por_tipo"]) == {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
    assert [p["symbol"] for p in data["posiciones"]] == ["BTCUSDT"]
    assert data["upnl"] == pytest.approx(3.25)
    text = notifier.format_daily_summary(data)
    assert "Resumen diario 2026-10-05" in text and "+9.00 USDT" in text
    assert "Posiciones abiertas: 1" in text and "BTCUSDT LONG" in text and "UPNL total: +3.25" in text


def test_resumen_diario_una_vez_por_dia_a_la_hora(db_path, notifier_env, monkeypatch):
    monkeypatch.setenv("NOTIFIER_DAILY_SUMMARY", "21:00")
    monkeypatch.setenv("NOTIFIER_SCRAPE_MAX_AGE", "0")  # reloj simulado: aislar el resumen
    seed_day(db_path, dt.date(2026, 10, 5))
    clock = Clock(dt.datetime(2026, 10, 5, 20, 59, tzinfo=TZ))
    sent = []
    m = make_monitor(db_path, FakeDocker(), sent, clock=clock)
    m.tick()
    assert sent == []                           # antes de la hora
    clock.when = dt.datetime(2026, 10, 5, 21, 0, tzinfo=TZ)
    m.tick()
    assert len(sent) == 1 and "Resumen diario 2026-10-05" in sent[0]
    clock.when = dt.datetime(2026, 10, 5, 23, 0, tzinfo=TZ)
    m.tick()
    m2 = make_monitor(db_path, FakeDocker(), sent, clock=clock)   # reinicio: no repite
    m2.tick()
    assert len(sent) == 1
    clock.when = dt.datetime(2026, 10, 6, 21, 5, tzinfo=TZ)
    m2.tick()
    assert len(sent) == 2 and "2026-10-06" in sent[1]


def test_resumen_diario_sin_db_no_rompe(tmp_path, notifier_env, monkeypatch):
    monkeypatch.setenv("NOTIFIER_DAILY_SUMMARY", "00:00")
    sent = []
    m = make_monitor(tmp_path / "no-existe.db", FakeDocker(), sent)
    m.maybe_daily_summary()
    assert len(sent) == 1 and "No se pudo leer la DB" in sent[0]


def test_resumen_usa_telegram_real_con_prefijo(db_path, notifier_env, monkeypatch, tg_env, tg):
    monkeypatch.setenv("NOTIFIER_DAILY_SUMMARY", "00:00")
    m = notifier.Monitor(db_path, docker=bot_control.DockerClient("http://docker-proxy:2375",
                                                                  session=FakeDocker()))
    m.maybe_daily_summary()
    assert tg.texts and tg.texts[0].startswith("<b>[DEV client17]</b> 📊")


def test_main_once_deshabilitado_sale_sin_enviar(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        assert notifier.main(["--once", "--db", str(tmp_path / "futures.db")]) == 0
    assert "deshabilitadas" in caplog.text


def test_main_once_habilitado_sin_config_json(tmp_path, tg_env, tg, notifier_env, monkeypatch):
    # Sin config.json (ni API keys de Binance): el notifier solo necesita la ruta de la DB.
    make_db(tmp_path / "futures.db").close()
    monkeypatch.delenv(bot_control.ENV_DOCKER_URL)
    assert notifier.main(["--once", "--db", str(tmp_path / "futures.db")]) == 0
    assert not (tmp_path / "config.json").exists()
    assert (tmp_path / notifier.STATE_FILE_NAME).exists()
    assert tg.calls == []                       # todo sano, sin resumen: nada que avisar


def test_main_no_carga_config(tmp_path, tg_env, tg, notifier_env, monkeypatch):
    from futuresboard import config as fb_config

    def boom(*a, **k):
        raise AssertionError("el notifier no debe cargar Config")

    monkeypatch.setattr(fb_config.Config, "from_config_dir", boom)
    make_db(tmp_path / "futures.db").close()
    monkeypatch.delenv(bot_control.ENV_DOCKER_URL)
    monkeypatch.setenv(notifier.ENV_DB_PATH, str(tmp_path / "futures.db"))
    assert notifier.main(["--once"]) == 0
    assert (tmp_path / notifier.STATE_FILE_NAME).exists()


def test_main_db_solo_lectura_y_estado_aparte(tmp_path, tg_env, tg, notifier_env, monkeypatch):
    # DB en un directorio de solo lectura (como un montaje :ro) y estado en otro volumen rw.
    data = tmp_path / "data"
    data.mkdir()
    make_db(data / "futures.db").close()
    state = tmp_path / "state" / "notifier_state.json"
    state.parent.mkdir()
    monkeypatch.delenv(bot_control.ENV_DOCKER_URL)
    monkeypatch.setenv(notifier.ENV_STATE_PATH, str(state))
    monkeypatch.setenv("NOTIFIER_DAILY_SUMMARY", "00:00")
    os.chmod(data, 0o555)
    try:
        assert notifier.main(["--once", "--db", str(data / "futures.db")]) == 0
    finally:
        os.chmod(data, 0o755)
    assert state.exists() and not (data / notifier.STATE_FILE_NAME).exists()
    assert len(tg.texts) == 1 and "📊" in tg.texts[0]   # leyó la DB en solo lectura para el resumen


def test_resumen_suma_ordenes_restantes_por_simbolo():
    resumen = {
        "posiciones_cerradas": [],
        "ordenes_canceladas": [],
        "errores": ["x"],
        "restante": {
            "posiciones": [{"symbol": "ETHUSDT", "lado": "SHORT", "cantidad": "1"}],
            "ordenes": [{"symbol": "BTCUSDT", "cantidad": 12}, {"symbol": "ETHUSDT", "cantidad": 3}],
            "ordenes_algo": [{"symbol": "BTCUSDT", "cantidad": 2}],
            "verificado": True,
        },
    }
    text = telegram_notify.format_resumen(resumen)
    assert "Quedó abierto: 1 posiciones, 15 órdenes, 2 condicionales" in text
    assert "órdenes BTCUSDT: 12" in text and "órdenes ETHUSDT: 3" in text
    assert "condicionales BTCUSDT: 2" in text and "posición ETHUSDT SHORT 1" in text


def test_resumen_sin_restante_no_muestra_quedo_abierto():
    resumen = {"restante": {"posiciones": [], "ordenes": [], "ordenes_algo": [], "verificado": True}}
    assert "Quedó abierto" not in telegram_notify.format_resumen(resumen)


def test_evento_apagar_parcial_cuenta_ordenes_restantes(client, env, docker, binance, tg_env, tg):  # noqa: F811
    binance.add_position("ETHUSDT", "-1")
    binance.stuck.add(("ETHUSDT", "BOTH"))
    binance.orders = [{"symbol": "BTCUSDT", "orderId": i} for i in range(12)]
    real_request = binance.request

    def cancel_sin_efecto(method, url, **kwargs):  # Binance confirma la cancelación pero quedan abiertas
        if method == "DELETE" and "/fapi/v1/allOpenOrders" in url:
            return FakeResponse(200, {"code": 200, "msg": "done"})
        return real_request(method, url, **kwargs)

    binance.request = cancel_sin_efecto
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 502
    assert "Quedó abierto: 1 posiciones, 12 órdenes" in tg.texts[0]
