"""Scraper en su propio proceso: loop, SIGTERM, latido, DB en un directorio de datos y estado fijo."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
import requests

from futuresboard import auth
from futuresboard import db
from futuresboard import notifier
from futuresboard import scraper
from futuresboard.app import init_app
from futuresboard.config import Config
from futuresboard.config import ENV_AUTO_SCRAPE_INTERVAL
from futuresboard.config import ENV_DATABASE

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


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
# Loop con Binance simulado
# --------------------------------------------------------------------------------------


class FakeBinance:
    """send_signed_request falso: órdenes, cuenta con posiciones e income paginado."""

    def __init__(self, weight=10, income_pages=1, fail=None):
        self.weight = weight
        self.income_pages = income_pages
        self.fail = fail
        self.calls = []
        self._income_left = 0

    def start_vuelta(self):
        self._income_left = self.income_pages

    def __call__(self, http_method, url_path, payload={}, exchange="binance"):
        self.calls.append(url_path)
        if self.fail is not None:
            exc, self.fail = self.fail, None
            raise exc
        headers = {"X-MBX-USED-WEIGHT-1M": str(self.weight)}
        if url_path == "/fapi/v1/openOrders":
            if self._income_left == 0:
                self.start_vuelta()
            return headers, [{"origQty": "1", "price": "100", "side": "BUY", "positionSide": "LONG",
                              "status": "NEW", "symbol": "BTCUSDT", "time": 1, "type": "LIMIT"}]
        if url_path == "/fapi/v2/account":
            return headers, {"totalWalletBalance": "1000", "totalUnrealizedProfit": "1.5",
                             "totalMarginBalance": "1001.5", "availableBalance": "900",
                             "maxWithdrawAmount": "900",
                             "positions": [{"unrealizedProfit": "1.5", "leverage": "10", "entryPrice": "100",
                                            "positionAmt": "0.1", "symbol": "BTCUSDT",
                                            "positionSide": "LONG"}]}
        if url_path == "/fapi/v1/income":
            if self._income_left <= 0:
                return headers, []
            self._income_left -= 1
            start = payload["startTime"]
            return headers, [{"tranId": str(start), "symbol": "BTCUSDT", "incomeType": "REALIZED_PNL",
                              "income": "0.5", "asset": "USDT", "info": "", "time": start,
                              "tradeId": "7"}]
        raise AssertionError(url_path)


@pytest.fixture
def loop_app(dirs, monkeypatch):
    config_dir, data_dir = dirs
    monkeypatch.setenv(ENV_DATABASE, str(data_dir / "futures.db"))
    app = scraper.build_app(Config.from_config_dir(config_dir))
    fake = FakeBinance()
    monkeypatch.setattr(scraper, "send_signed_request", fake)
    return app, fake, config_dir, data_dir


def test_loop_scrapea_activa_wal_y_deja_latido_en_datos(loop_app):
    app, fake, config_dir, data_dir = loop_app
    assert scraper.run_loop(app, interval=0, detener=threading.Event(), max_vueltas=2) == 0

    database = data_dir / "futures.db"
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM income").fetchone()[0] == 2  # una página por vuelta
    latido = scraper.read_latido(database)
    assert latido["vuelta"] == 2 and latido["intervalo"] == 0 and latido["pid"] == os.getpid()
    assert scraper.read_scrape_state(database)["last_success_at"] <= latido["at"]
    # Nada nuevo junto a config.json: ni la DB, ni el latido, ni scrape_state.
    assert sorted(p.name for p in config_dir.iterdir()) == ["config.json"]


def test_loop_respeta_el_control_de_peso(loop_app, monkeypatch):
    app, fake, _, _ = loop_app
    fake.weight = 900

    class Evento(threading.Event):
        def __init__(self):
            super().__init__()
            self.esperas = []

        def wait(self, timeout=None):
            self.esperas.append(timeout)
            return self.is_set()

    evento = Evento()
    monkeypatch.setattr(scraper, "_DETENER", evento)
    scraper.run_loop(app, interval=0, max_vueltas=1)
    assert 60 in evento.esperas  # peso > 800: pausa de 1 minuto antes de la página de income


def test_vuelta_con_error_no_deja_latido_y_el_loop_sigue(loop_app):
    app, fake, _, data_dir = loop_app
    fake.fail = sqlite3.OperationalError("database is locked")  # antes mataba el hilo del scraper
    scraper.run_loop(app, interval=0, detener=threading.Event(), max_vueltas=1)
    assert scraper.read_latido(data_dir / "futures.db") == {}

    fake.fail = scraper.HTTPRequestError(url="https://x/fapi?signature=abc", code=-1021)
    scraper.run_loop(app, interval=0, detener=threading.Event(), max_vueltas=2)
    latido = scraper.read_latido(data_dir / "futures.db")
    assert latido["vuelta"] == 2  # la 1 falló (HTTP), la 2 dejó latido


# --------------------------------------------------------------------------------------
# Binance colgado: timeout de conexión y lectura
# --------------------------------------------------------------------------------------
# Sin timeout, un Binance que acepta la conexión y no responde dejaba la vuelta trabada para siempre (contenedor
# "Up", sin latido nuevo).


@pytest.fixture
def app_real(dirs, monkeypatch):
    """App del loop con el send_signed_request de verdad (el HTTP se simula más abajo)."""
    config_dir, data_dir = dirs
    monkeypatch.setenv(ENV_DATABASE, str(data_dir / "futures.db"))
    return scraper.build_app(Config.from_config_dir(config_dir)), data_dir


class _RespuestaVacia:
    headers = {"X-MBX-USED-WEIGHT-1M": "1"}

    def json(self):
        return []


def test_pedidos_a_binance_llevan_timeout_de_conexion_y_lectura(app_real, monkeypatch):
    app, _ = app_real
    timeouts = []

    def grabar(self, method, url, **kw):
        timeouts.append(kw.get("timeout"))
        return _RespuestaVacia()

    monkeypatch.setattr(requests.Session, "request", grabar)
    with app.app_context():
        scraper.send_signed_request("GET", "/fapi/v1/openOrders")
        scraper.send_public_request("/fapi/v1/premiumIndex")
    assert timeouts == [(5, 20), (5, 20)]  # (conexión, lectura) en segundos


@pytest.mark.parametrize("exc", [requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout])
def test_timeout_de_binance_es_una_vuelta_fallida_sin_firma(app_real, monkeypatch, exc):
    # Un ReadTimeout salía crudo de scrape(): en el hilo del web (_auto_scrape) eso mataba el hilo.
    app, data_dir = app_real

    def colgado(self, method, url, **kw):
        raise exc("tardó demasiado")

    monkeypatch.setattr(requests.Session, "request", colgado)
    with app.app_context():
        with pytest.raises(scraper.HTTPRequestError) as firmado:
            scraper.send_signed_request("GET", "/fapi/v1/openOrders")
        with pytest.raises(scraper.HTTPRequestError) as publico:
            scraper.send_public_request("/fapi/v1/premiumIndex")
        assert scraper.scrape(app=app) is False
    for error in (firmado.value, publico.value):
        texto = str(error)
        assert "timeout" in texto.lower(), texto
        assert not re.search(r"signature=[0-9a-f]{16}", texto), texto
    estado = scraper.read_scrape_state(data_dir / "futures.db")
    assert "last_success_at" not in estado and estado["last_error"]
    assert scraper.read_latido(data_dir / "futures.db") == {}


def test_binance_colgado_no_traba_el_loop(app_real, monkeypatch, caplog):
    app, data_dir = app_real
    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.bind(("127.0.0.1", 0))
    servidor.listen()
    servidor.settimeout(0.1)
    conexiones, cerrar = [], threading.Event()

    def aceptar():  # acepta y nunca responde: Binance colgado
        while not cerrar.is_set():
            try:
                conexiones.append(servidor.accept()[0])
            except socket.timeout:
                continue
            except OSError:
                return

    threading.Thread(target=aceptar, daemon=True).start()
    app.config["API_BASE_URL"] = f"http://127.0.0.1:{servidor.getsockname()[1]}"
    monkeypatch.setattr(scraper, "BINANCE_TIMEOUT", (0.5, 0.5), raising=False)
    real, fake, pedidos = scraper.send_signed_request, FakeBinance(), []

    def primero_colgado(*a, **kw):  # vuelta 1: el server colgado; vuelta 2: Binance responde
        pedidos.append(a)
        return real(*a, **kw) if len(pedidos) == 1 else fake(*a, **kw)

    monkeypatch.setattr(scraper, "send_signed_request", primero_colgado)
    detener = threading.Event()
    hilo = threading.Thread(target=scraper.run_loop, args=(app,), daemon=True,
                            kwargs={"interval": 0, "detener": detener, "max_vueltas": 2})
    try:
        with caplog.at_level(logging.INFO):
            hilo.start()
            hilo.join(timeout=15)
            assert not hilo.is_alive(), "Binance colgado dejó el loop trabado"
    finally:
        detener.set()
        cerrar.set()
        servidor.close()
        for conexion in conexiones:
            conexion.close()
        hilo.join(timeout=5)
    assert scraper.read_latido(data_dir / "futures.db")["vuelta"] == 2  # la 1 cortó por timeout, sin latido
    mensajes = [r.getMessage() for r in caplog.records]
    assert any("timeout" in m.lower() and "/fapi/v1/openOrders" in m for m in mensajes), mensajes
    assert any("Vuelta 1 sin éxito" in m and "sin latido" in m for m in mensajes), mensajes


def test_cuenta_sin_positions_no_cuenta_como_ok_ni_deja_latido(loop_app, monkeypatch):
    # Binance responde la cuenta sin "positions": cuenta y posiciones quedan como estaban. Antes eso era un scrape
    # OK con latido, y la página decía "datos de hace 5 s" con la cuenta y las posiciones viejas.
    app, fake, _, data_dir = loop_app

    class SinPositions(FakeBinance):
        def __call__(self, http_method, url_path, payload={}, exchange="binance"):
            headers, data = super().__call__(http_method, url_path, payload, exchange)
            if url_path == "/fapi/v2/account":
                data = {k: v for k, v in data.items() if k != "positions"}
            return headers, data

    monkeypatch.setattr(scraper, "send_signed_request", SinPositions())
    scraper.run_loop(app, interval=0, detener=threading.Event(), max_vueltas=1)
    database = data_dir / "futures.db"
    assert scraper.read_latido(database) == {}
    estado = scraper.read_scrape_state(database)
    assert "last_success_at" not in estado and "positions" in estado["last_error"]
    assert scraper.frescura(database)["at"] is None

    monkeypatch.setattr(scraper, "send_signed_request", fake)  # la vuelta siguiente, con la cuenta completa
    scraper.run_loop(app, interval=0, detener=threading.Event(), max_vueltas=1)
    assert scraper.read_latido(database)["vuelta"] == 1


def test_loop_no_escribe_nada_en_el_directorio_de_config(loop_app):
    # El contenedor scraper monta solo config.json en :ro (nunca el directorio ./config, con el estado del panel,
    # rescate y C3). Con el directorio y el archivo sin permiso de escritura, el loop corre igual.
    app, _, config_dir, data_dir = loop_app
    config_json = config_dir / "config.json"
    os.chmod(config_json, 0o444)
    os.chmod(config_dir, 0o555)
    try:
        assert scraper.run_loop(app, interval=0, detener=threading.Event(), max_vueltas=1) == 0
    finally:
        os.chmod(config_dir, 0o755)
        os.chmod(config_json, 0o644)
    assert scraper.read_latido(data_dir / "futures.db")["vuelta"] == 1
    assert sorted(p.name for p in config_dir.iterdir()) == ["config.json"]


def test_detener_antes_de_la_proxima_pagina_corta_el_income(loop_app, monkeypatch):
    app, _, _, data_dir = loop_app
    detener = threading.Event()

    class ConSenal(FakeBinance):
        def __call__(self, http_method, url_path, payload={}, exchange="binance"):
            res = super().__call__(http_method, url_path, payload, exchange)
            if url_path == "/fapi/v1/income":
                detener.set()  # SIGTERM llega mientras se procesa la primera página
            return res

    fake = ConSenal(income_pages=5)
    monkeypatch.setattr(scraper, "send_signed_request", fake)
    monkeypatch.setattr(scraper, "_DETENER", detener)
    scraper.run_loop(app, interval=60)  # sale sin esperar los 60 s: el evento ya está seteado

    with sqlite3.connect(data_dir / "futures.db") as conn:
        # La página en curso se commiteó entera; no se pidió la siguiente.
        assert conn.execute("SELECT COUNT(*) FROM income").fetchone()[0] == 1
    assert fake.calls.count("/fapi/v1/income") == 1
    assert scraper.read_latido(data_dir / "futures.db")["vuelta"] == 1


# --------------------------------------------------------------------------------------
# Proceso real: python -m futuresboard.scraper y SIGTERM
# --------------------------------------------------------------------------------------

_HIJO = """
import pathlib, runpy, sys, time
sys.path.insert(0, __SRC__)
import futuresboard
assert futuresboard.__file__.startswith(__SRC__), futuresboard.__file__
from futuresboard import scraper
marca = pathlib.Path(sys.argv[1])
modo = sys.argv[2]
estado = {"vuelta": 0}

def falso(http_method, url_path, payload={}, exchange="binance"):
    peso = "900" if modo == "peso" else "10"
    h = {"X-MBX-USED-WEIGHT-1M": peso}
    if url_path == "/fapi/v1/openOrders":
        estado["vuelta"] += 1
        estado["income"] = 0
        return h, []
    if url_path == "/fapi/v2/account":
        if modo == "peso":
            marca.write_text("pausa")  # justo antes de la pausa de 60 s por peso
        return h, {"totalWalletBalance": "1", "totalUnrealizedProfit": "0", "totalMarginBalance": "1",
                   "availableBalance": "1", "maxWithdrawAmount": "1",
                   "positions": [{"unrealizedProfit": "0", "leverage": "1", "entryPrice": "1",
                                  "positionAmt": "1", "symbol": "BTCUSDT", "positionSide": "LONG"}]}
    estado["income"] += 1
    if estado["income"] == 1:
        if modo == "escritura":
            marca.write_text("en vuelta")
            time.sleep(1.5)  # SIGTERM llega acá: la vuelta tiene que terminar igual
        start = payload["startTime"]
        return h, [{"tranId": str(start), "symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "1",
                    "asset": "USDT", "info": "", "time": start, "tradeId": "1"}]
    if modo == "espera":
        marca.write_text("esperando")
    return h, []

scraper.send_signed_request = falso
sys.argv = ["futuresboard.scraper", "-c", sys.argv[3]]
runpy.run_module("futuresboard.scraper", run_name="__main__")
"""


@pytest.mark.parametrize("modo", ["escritura", "espera", "peso"])
def test_sigterm_apaga_limpio_sin_cortar_la_vuelta(dirs, tmp_path, modo):
    config_dir, data_dir = dirs
    script = tmp_path / "hijo.py"
    script.write_text(_HIJO.replace("__SRC__", repr(str(SRC))))
    marca = tmp_path / "marca"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({ENV_DATABASE: str(data_dir / "futures.db"), ENV_AUTO_SCRAPE_INTERVAL: "60",
                "FUTURESBOARD_LOG_FORMAT": "text"})
    proc = subprocess.Popen([sys.executable, "-I", str(script), str(marca), modo, str(config_dir)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        limite = time.time() + 20
        while not marca.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.time() < limite, "el hijo nunca llegó al punto de la señal"
            time.sleep(0.05)
        if modo == "espera":
            time.sleep(0.3)  # que termine la vuelta y entre en la espera de 60 s
        enviada = time.time()
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert time.time() - enviada < 10, "no esperó a los 60 s del intervalo ni de la pausa por peso"
    assert "Scraper detenido tras 1 vueltas" in err
    assert "k" * 12 not in err and "s" * 12 not in err

    database = data_dir / "futures.db"
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        income = conn.execute("SELECT COUNT(*) FROM income").fetchone()[0]
    latido = scraper.read_latido(database)
    if modo == "peso":
        assert income == 0  # la pausa se cortó y no se pidió la página
    else:
        assert income == 1  # la vuelta en curso terminó y quedó commiteada
    assert latido["vuelta"] == 1
    assert sorted(p.name for p in config_dir.iterdir()) == ["config.json"]


# --------------------------------------------------------------------------------------
# Latido y "datos de hace X s"
# --------------------------------------------------------------------------------------


def test_frescura_usa_lo_mas_nuevo_entre_latido_y_scrape_state(tmp_path):
    database = tmp_path / "futures.db"
    assert scraper.frescura(database, now=1000)["edad_segundos"] is None

    scraper.scrape_state_path(database).write_text(json.dumps({"last_success_at": 900}))
    assert scraper.frescura(database, now=1000) == {
        "fuente": "scrape_state", "at": 900.0, "edad_segundos": 100, "intervalo_segundos": None}

    scraper.latido_path(database).write_text(json.dumps({"at": 950, "intervalo": 60, "vuelta": 3}))
    assert scraper.frescura(database, now=1000) == {
        "fuente": "latido", "at": 950.0, "edad_segundos": 50, "intervalo_segundos": 60}

    # scrape_state más nuevo que el latido (otro scraper anduvo después): fuente e intervalo son los suyos.
    scraper.scrape_state_path(database).write_text(json.dumps({"last_success_at": 990}))
    assert scraper.frescura(database, now=1000) == {
        "fuente": "scrape_state", "at": 990.0, "edad_segundos": 10, "intervalo_segundos": None}
    assert scraper.frescura(database, now=1000, intervalo_defecto=300)["intervalo_segundos"] == 300
    # Empate: el latido.
    scraper.scrape_state_path(database).write_text(json.dumps({"last_success_at": 950}))
    assert scraper.frescura(database, now=1000, intervalo_defecto=300)["fuente"] == "latido"
    assert scraper.frescura(database, now=1000, intervalo_defecto=300)["intervalo_segundos"] == 60

    scraper.latido_path(database).write_text("{roto")
    assert scraper.read_latido(database) == {}


def test_frescura_web_con_el_hilo_del_web_usa_su_intervalo(tmp_path):
    # Compose sin servicio scraper (prod hoy): sin latido, el umbral de "datos viejos" sale del intervalo del hilo
    # (300 s) y no de los 180 s por defecto, que lo ponían amarillo 2 de cada 5 minutos.
    database = tmp_path / "futures.db"
    scraper.scrape_state_path(database).write_text(json.dumps({"last_success_at": 900}))
    cfg = {"DATABASE": database, "AUTO_SCRAPE_INTERVAL": 300, "DISABLE_AUTO_SCRAPE": False}
    assert scraper.frescura_web(cfg, now=1000)["intervalo_segundos"] == 300
    cfg["DISABLE_AUTO_SCRAPE"] = True  # el web no scrapea: no se sabe cada cuánto corre el otro (cron)
    assert scraper.frescura_web(cfg, now=1000)["intervalo_segundos"] is None


def _login(client, app):
    auth.set_user_password(str(app.config["DATABASE"]), "cliente17", "clave-de-test")
    with sqlite3.connect(str(app.config["DATABASE"])) as conn:
        pw_hash = conn.execute("SELECT password_hash FROM users WHERE username = 'cliente17'").fetchone()[0]
    with app.app_context():
        fingerprint = auth.password_fingerprint(pw_hash)
    with client.session_transaction() as sess:
        sess["username"] = "cliente17"
        sess[auth.SESSION_FINGERPRINT_KEY] = fingerprint


def test_endpoint_latido_requiere_login(tmp_path):
    cfg = Config(CONFIG_DIR=tmp_path, DATABASE=tmp_path / "futures.db", API_KEY="x", API_SECRET="x",
                 DISABLE_AUTO_SCRAPE=True)
    app = init_app(cfg)
    app.config["TESTING"] = True
    client = app.test_client()
    assert client.get("/api/scraper/latido").status_code == 302

    _login(client, app)
    scraper.latido_path(app.config["DATABASE"]).write_text(
        json.dumps({"at": time.time() - 5, "intervalo": 60, "vuelta": 1}))
    resp = client.get("/api/scraper/latido")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    data = resp.get_json()
    assert data["fuente"] == "latido" and data["intervalo_segundos"] == 60
    assert 4 <= data["edad_segundos"] <= 10 and data["at"].endswith("+00:00")


# --------------------------------------------------------------------------------------
# Notifier: por qué el directorio de datos se monta escribible (y el código abre mode=ro)
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignora los permisos del directorio")
def test_lector_ro_de_wal_necesita_poder_crear_el_shm(tmp_path):
    datos = tmp_path / "data"
    datos.mkdir()
    database = datos / "futures.db"
    scraper.db_setup(str(database))
    assert db.ensure_wal(database) == "wal"
    assert sorted(p.name for p in datos.iterdir()) == ["futures.db"]  # sin conexiones: no hay -wal/-shm

    # Directorio escribible: el notifier lee en mode=ro aunque no haya ningún escritor conectado.
    ro = notifier._connect_ro(database)
    assert ro.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM positions")
    ro.close()

    # Directorio de solo lectura (equivalente a montarlo :ro) y sin escritor conectado: no abre.
    for sufijo in ("-wal", "-shm"):
        (datos / f"futures.db{sufijo}").unlink(missing_ok=True)
    datos.chmod(0o555)
    try:
        with pytest.raises(sqlite3.OperationalError):
            notifier._connect_ro(database).execute("SELECT 1 FROM positions").fetchone()
    finally:
        datos.chmod(0o755)
