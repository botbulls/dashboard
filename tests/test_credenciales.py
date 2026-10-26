"""Credenciales con recarga en caliente (spec bot-estrategia § 10.3, commit 11).

Keys y secrets de prueba: se generan en cada corrida (``secrets.token_hex``); nunca son keys reales ni literales.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import pathlib
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlparse

import pytest
import requests

from futuresboard import bot_control
from futuresboard import credenciales
from futuresboard import logs
from futuresboard import rescate
from futuresboard import scraper
from futuresboard.config import Config

from test_bot_control import BINANCE_BASE
from test_bot_control import FakeResponse
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import binance  # noqa: F401
from test_bot_control import router  # noqa: F401


def par():
    return secrets.token_hex(32), secrets.token_hex(32)


def escribir(dir_secretos: pathlib.Path, key: str, secret: str, usuario: str = "binance_01", testnet=True,
             atomico: bool = True) -> pathlib.Path:
    """api-keys.json con el formato de passivbot: el usuario y otras entradas que no se tocan."""
    dir_secretos.mkdir(parents=True, exist_ok=True)
    data = {
        "referrals": {"binance": "https://example.invalid/ref"},
        usuario: {"exchange": "binance", "key": key, "secret": secret, "testnet": testnet},
        "bybit_01": {"exchange": "bybit", "key": "key", "secret": "secret"},
    }
    path = dir_secretos / credenciales.ARCHIVO
    texto = json.dumps(data, indent=4)
    if atomico:
        tmp = dir_secretos / ".tmp-test"
        tmp.write_text(texto)
        os.replace(tmp, path)  # inodo nuevo, como el job del panel
    else:
        path.write_text(texto)
    return path


@pytest.fixture
def secretos(tmp_path, monkeypatch):
    d = tmp_path / "secretos"
    monkeypatch.setenv(credenciales.ENV_DIR, str(d))
    return d


def prov(d, respaldo=("", ""), usuario="binance_01"):
    return credenciales.Proveedor(d, respaldo=lambda: respaldo, usuario=lambda: usuario)


# ---------------------------------------------------------------- el proveedor


def test_sin_directorio_usa_config_json(tmp_path):
    k, s = par()
    p = prov(tmp_path / "no-existe", respaldo=(k, s))
    c = p.actuales()
    assert (c.api_key, c.api_secret, c.origen) == (k, s, credenciales.ORIGEN_CONFIG)
    assert c.key_id == hashlib.sha256(k.encode()).hexdigest()[:12]
    assert p.estado()["key_error"] is None


def test_sin_directorio_ni_config_no_hay_credenciales(tmp_path):
    p = prov(tmp_path / "no-existe")
    with pytest.raises(credenciales.SinCredenciales):
        p.actuales()
    assert not p.disponible()


def test_lee_el_archivo_y_no_la_config(secretos):
    k, s = par()
    escribir(secretos, k, s)
    p = prov(secretos, respaldo=par())
    c = p.actuales()
    assert (c.api_key, c.api_secret, c.origen) == (k, s, credenciales.ORIGEN_SECRETOS)
    e = p.estado()
    assert e["usuario"] == "binance_01" and e["testnet"] is True and e["archivo_mtime"]


def test_recarga_por_cambio_de_inodo(secretos):
    k1, s1 = par()
    k2, s2 = par()
    escribir(secretos, k1, s1)
    p = prov(secretos)
    assert p.actuales().api_key == k1
    escribir(secretos, k2, s2)
    c = p.actuales()
    assert (c.api_key, c.api_secret) == (k2, s2)
    assert c.key_id == credenciales.key_id(k2)


def test_recarga_reescrito_en_el_lugar(secretos):
    k1, s1 = par()
    escribir(secretos, k1, s1)
    p = prov(secretos)
    p.actuales()
    k2, s2 = par()
    path = escribir(secretos, k2, s2, atomico=False)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # aunque el reloj del FS sea grueso
    assert p.actuales().api_key == k2


def test_no_relee_si_no_cambio(secretos, monkeypatch):
    k, s = par()
    escribir(secretos, k, s)
    p = prov(secretos)
    p.actuales()
    leidos = []
    real = credenciales.leer_entrada
    monkeypatch.setattr(credenciales, "leer_entrada", lambda *a: leidos.append(1) or real(*a))
    for _ in range(5):
        p.actuales()
    assert leidos == []


@pytest.mark.parametrize("contenido", ["{roto", "[]", json.dumps({"otro": {}}),
                                       json.dumps({"binance_01": {"exchange": "binance", "key": "corta", "secret": "x"}}),
                                       json.dumps({"binance_01": {"exchange": "bybit", "key": "a" * 64,
                                                                  "secret": "b" * 64}})])
def test_archivo_invalido_sigue_con_la_ultima_key_valida(secretos, contenido, caplog):
    k, s = par()
    escribir(secretos, k, s)
    p = prov(secretos)
    assert p.actuales().api_key == k
    tmp = secretos / ".tmp"
    tmp.write_text(contenido)
    os.replace(tmp, secretos / credenciales.ARCHIVO)
    with caplog.at_level(logging.DEBUG):
        c = p.actuales()
    assert c.api_key == k and c.api_secret == s
    e = p.estado()
    assert e["key_id"] == credenciales.key_id(k)
    assert "inválido" in e["key_error"] and "última key válida" in e["key_error"]
    assert k not in e["key_error"] and s not in e["key_error"]
    assert k not in caplog.text and s not in caplog.text
    # Se arregla el archivo: vuelve a leer y se va el error.
    k2, s2 = par()
    escribir(secretos, k2, s2)
    assert p.actuales().api_key == k2 and p.estado()["key_error"] is None


def test_archivo_invalido_al_arrancar_usa_config_y_lo_dice(secretos):
    secretos.mkdir()
    (secretos / credenciales.ARCHIVO).write_text("{roto")
    k, s = par()
    p = prov(secretos, respaldo=(k, s))
    c = p.actuales()
    assert c.api_key == k and c.origen == credenciales.ORIGEN_CONFIG
    assert "inválido" in p.estado()["key_error"]


def test_falta_el_archivo_sin_config(secretos):
    secretos.mkdir()
    p = prov(secretos)
    with pytest.raises(credenciales.SinCredenciales) as exc:
        p.actuales()
    assert "falta api-keys.json" in str(exc.value)


def test_falta_el_archivo_despues_de_leerlo_sigue_con_la_ultima(secretos):
    k, s = par()
    path = escribir(secretos, k, s)
    p = prov(secretos)
    p.actuales()
    path.unlink()
    assert p.actuales().api_key == k
    assert "falta" in p.estado()["key_error"]


def test_register_secret_antes_del_primer_uso(secretos, monkeypatch):
    registrados = []
    monkeypatch.setattr(logs, "register_secret", lambda v: registrados.append(v))
    k1, s1 = par()
    escribir(secretos, k1, s1)
    p = prov(secretos)
    c = p.actuales()
    assert k1 in registrados and s1 in registrados
    k2, s2 = par()
    escribir(secretos, k2, s2)
    registrados.clear()
    c = p.actuales()
    assert c.api_key == k2 and registrados == [k2, s2]


def test_formato_invalido_no_se_registra(secretos, monkeypatch):
    registrados = []
    monkeypatch.setattr(logs, "register_secret", lambda v: registrados.append(v))
    secretos.mkdir()
    (secretos / credenciales.ARCHIVO).write_text(json.dumps(
        {"binance_01": {"exchange": "binance", "key": "con espacios no sirve", "secret": "x" * 64}}))
    with pytest.raises(credenciales.SinCredenciales):
        prov(secretos).actuales()
    assert registrados == []


def test_usuario_de_new_json_env_y_default(tmp_path, monkeypatch):
    cfg = tmp_path / "new.json"
    cfg.write_text("{\n user: binance_07\n twe_long: 4\n}\n")
    monkeypatch.delenv(credenciales.ENV_USUARIO, raising=False)
    assert credenciales.usuario_passivbot(cfg) == "binance_07"
    assert credenciales.usuario_passivbot(tmp_path / "no-esta.json") == "binance_01"
    monkeypatch.setenv(credenciales.ENV_USUARIO, "binance_03")
    assert credenciales.usuario_passivbot(tmp_path / "no-esta.json") == "binance_03"
    assert credenciales.usuario_passivbot(cfg) == "binance_07"  # new.json manda donde se puede leer
    monkeypatch.setenv(credenciales.ENV_FORAGER_CONFIG, str(cfg))
    assert credenciales.usuario_passivbot() == "binance_07"


def test_lee_el_usuario_de_passivbot(secretos):
    k, s = par()
    escribir(secretos, k, s, usuario="binance_07")
    assert prov(secretos, usuario="binance_07").actuales().api_key == k
    with pytest.raises(credenciales.SinCredenciales):
        prov(secretos, usuario="binance_01").actuales()


def test_key_ok_at_solo_de_la_key_actual(secretos):
    k1, s1 = par()
    escribir(secretos, k1, s1)
    reloj = [1_700_000_000.0]
    p = credenciales.Proveedor(secretos, usuario=lambda: "binance_01", clock=lambda: reloj[0])
    c1 = p.actuales()
    assert p.estado()["key_ok_at"] is None
    p.firma_ok(c1)
    assert p.estado()["key_ok_at"] == "2023-11-14T22:13:20+00:00"
    k2, s2 = par()
    escribir(secretos, k2, s2)
    p.actuales()
    p.firma_ok(c1)  # un pedido viejo, firmado con la anterior, que terminó tarde
    e = p.estado()
    assert e["key_id"] == credenciales.key_id(k2) and e["key_ok_at"] is None


def test_repr_y_enmascarar_sin_valores():
    k, s = par()
    c = credenciales.Credencial(k, s, "x")
    assert k not in repr(c) and s not in repr(c) and k not in str(c)
    m = credenciales.enmascarar(k)
    assert m == f"{k[:4]}…{k[-4:]}" and len(m) == 9
    assert credenciales.enmascarar("corta") == "…" and credenciales.enmascarar(None) is None


def test_config_json_sin_keys_carga(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"BINANCE_TESTNET": True}))
    cfg = Config.from_config_dir(tmp_path)
    assert cfg.API_KEY == "" and cfg.API_SECRET == ""


# ---------------------------------------------------------------- clientes: piden la key en cada request


def _firma_ok(params, secret):
    params = dict(params)
    sig = params.pop("signature")
    return sig == hmac.new(secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()


def test_cliente_de_futuros_firma_con_la_key_nueva_sin_recrearse(secretos, binance, router):  # noqa: F811
    k1, s1 = par()
    escribir(secretos, k1, s1)
    p = prov(secretos)
    cliente = bot_control.BinanceFuturesClient(BINANCE_BASE, credenciales=p)
    cliente.open_orders()
    k2, s2 = par()
    escribir(secretos, k2, s2)
    cliente.open_orders()
    (_, _, p1, h1), (_, _, p2, h2) = binance.calls
    assert h1["X-MBX-APIKEY"] == k1 and _firma_ok(p1, s1)
    assert h2["X-MBX-APIKEY"] == k2 and _firma_ok(p2, s2)
    assert p.estado()["key_ok_at"] is not None


def test_cliente_de_futuros_sin_credenciales_es_disabled(tmp_path):
    with pytest.raises(bot_control.DisabledError):
        bot_control.BinanceFuturesClient(BINANCE_BASE, credenciales=prov(tmp_path / "no"))
    with pytest.raises(bot_control.DisabledError):
        bot_control.BinanceFuturesClient(BINANCE_BASE, "", "s")


def test_cliente_de_futuros_firma_ok_solo_si_respondio_bien(secretos, binance, router):  # noqa: F811
    k, s = par()
    escribir(secretos, k, s)
    p = prov(secretos)
    binance.positions_status = 500
    cliente = bot_control.BinanceFuturesClient(BINANCE_BASE, credenciales=p)
    with pytest.raises(bot_control.BinanceError):
        cliente.positions()
    assert p.estado()["key_ok_at"] is None


def test_rescate_binance_firma_con_la_key_nueva(secretos):
    k1, s1 = par()
    escribir(secretos, k1, s1)
    p = prov(secretos)
    vistos = []

    class Sesion:
        def request(self, method, url, headers=None, timeout=None):
            vistos.append((headers["X-MBX-APIKEY"], dict(parse_qsl(urlparse(url).query))))
            return FakeResponse(200, {"balances": []})

    cliente = rescate.RescateBinance("https://fapi.binance.com", "https://api.binance.com", credenciales=p,
                                     session=Sesion())
    cliente.spot_usdt_free()
    k2, s2 = par()
    escribir(secretos, k2, s2)
    cliente.spot_usdt_free()
    assert vistos[0][0] == k1 and _firma_ok(vistos[0][1], s1)
    assert vistos[1][0] == k2 and _firma_ok(vistos[1][1], s2)


# ---------------------------------------------------------------- rescate: Apagar del tope, de C3 y velas


@pytest.fixture
def servicio(secretos, tmp_path, env, router, monkeypatch):  # noqa: F811
    from test_bot_control import DOCKER_BASE

    k1, s1 = par()
    escribir(secretos, k1, s1)
    monkeypatch.setenv(bot_control.ENV_DOCKER_URL, DOCKER_BASE)
    data = tmp_path / "data"
    data.mkdir()
    config = SimpleNamespace(API_KEY="", API_SECRET="", DATABASE=str(data / "futures.db"), EXCHANGE="binance",
                             BINANCE_TESTNET=False, API_BASE_URL=BINANCE_BASE)
    settings = rescate.RescateSettings({rescate.ENV_MODE: "off", rescate.ENV_LOAN_STATE: str(tmp_path / "loan.json")})
    svc = rescate.build_service(settings, config)
    return svc, (k1, s1)


def _apagar_y_headers(apagar, binance):  # noqa: F811
    binance.calls.clear()
    outcome, _payload = apagar()
    assert outcome == "ok", _payload
    return {h.get("X-MBX-APIKEY") for _m, _p, _params, h in binance.calls if h.get("X-MBX-APIKEY")}


def test_despues_de_recargar_el_apagar_del_tope_y_el_de_c3_firman_con_la_key_nueva(servicio, secretos, binance,
                                                                                    docker):  # noqa: F811
    svc, (k1, _s1) = servicio
    assert _apagar_y_headers(svc._apagar, binance) == {k1}
    docker.running = True
    assert _apagar_y_headers(svc.c3._apagar, binance) == {k1}
    k2, s2 = par()
    escribir(secretos, k2, s2)
    docker.running = True
    assert _apagar_y_headers(svc._apagar, binance) == {k2}
    docker.running = True
    assert _apagar_y_headers(svc.c3._apagar, binance) == {k2}
    for _m, _p, params, h in binance.calls:
        assert _firma_ok(params, s2)


def test_despues_de_recargar_las_velas_usan_el_proveedor(servicio, secretos):
    svc, _ = servicio
    cliente_velas = svc.c3._klines.__self__
    assert cliente_velas._cred is svc.credenciales and svc.binance._cred is svc.credenciales
    k2, s2 = par()
    escribir(secretos, k2, s2)
    # Las velas son públicas (sin firma): lo que se prueba es que el cliente no guardó una key propia.
    assert cliente_velas._cred.actuales().key_id == credenciales.key_id(k2)


def test_rescate_publica_key_id_y_key_ok_at_en_su_latido(servicio, secretos):
    svc, (k1, _) = servicio
    svc.tick()
    serv = rescate.load_state(svc.data_dir)[0]["servicio"]
    assert serv["key_id"] == credenciales.key_id(k1) and serv["key_error"] is None
    assert "key_ok_at" in serv
    k2, s2 = par()
    escribir(secretos, k2, s2)
    svc.tick()
    assert rescate.load_state(svc.data_dir)[0]["servicio"]["key_id"] == credenciales.key_id(k2)
    assert k1 not in (svc.data_dir / rescate.STATE_FILE_NAME).read_text()


def test_rescate_reinicia_los_permisos_cuando_cambia_la_key(secretos, tmp_path):
    from test_rescate import FakeRescateBinance
    from test_rescate import FUT
    from test_rescate import SPOT

    k1, s1 = par()
    escribir(secretos, k1, s1)
    p = prov(secretos)
    fake = FakeRescateBinance()
    fake.restrictions["ipRestrict"] = False  # la key vieja no cumple: queda bloqueado hasta que cambie
    data = tmp_path / "data"
    data.mkdir()
    settings = rescate.RescateSettings({rescate.ENV_MODE: "real", rescate.ENV_LOAN_STATE: str(tmp_path / "l.json")})
    svc = rescate.RescateService(data, settings, rescate.RescateBinance(FUT, SPOT, credenciales=p, session=fake),
                                 apagar=lambda: ("ok", {}), send=lambda t: True, credenciales=p)

    def consultas():
        return fake.paths("GET").count("/sapi/v1/account/apiRestrictions")

    svc.tick()
    svc.tick()
    assert consultas() == 1 and svc._restricciones
    fake.restrictions["ipRestrict"] = True  # la key nueva sí cumple
    k2, s2 = par()
    escribir(secretos, k2, s2)
    svc.tick()
    assert consultas() == 2 and svc._restricciones == []
    eventos = rescate.load_state(data)[0]["eventos"]
    assert any("API key cambió" in e["texto"] for e in eventos)


# ---------------------------------------------------------------- scraper


@pytest.fixture
def app_scraper(secretos, tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"BINANCE_TESTNET": False}))
    monkeypatch.setenv("FUTURESBOARD_DATABASE", str(tmp_path / "futures.db"))
    k1, s1 = par()
    escribir(secretos, k1, s1)
    return scraper.build_app(Config.from_config_dir(cfg_dir)), (k1, s1)


class _Resp:
    headers = {"X-MBX-USED-WEIGHT-1M": "1"}

    def json(self):
        return []


def test_scraper_una_misma_key_para_firma_y_header_y_recarga(app_scraper, secretos, monkeypatch):
    app, (k1, s1) = app_scraper
    pedidos = []

    def grabar(self, method, url, **kw):
        pedidos.append((self.headers.get("X-MBX-APIKEY"), dict(parse_qsl(urlparse(url).query))))
        return _Resp()

    monkeypatch.setattr(requests.Session, "request", grabar)
    with app.app_context():
        scraper.send_signed_request("GET", "/fapi/v1/openOrders")
        k2, s2 = par()
        escribir(secretos, k2, s2)
        scraper.send_signed_request("GET", "/fapi/v1/openOrders")
    assert pedidos[0][0] == k1 and _firma_ok(pedidos[0][1], s1)
    assert pedidos[1][0] == k2 and _firma_ok(pedidos[1][1], s2)
    assert credenciales.de_app(app).estado()["key_ok_at"] is not None


def test_scraper_recarga_entre_firma_y_header_no_las_mezcla(app_scraper, secretos, monkeypatch):
    """Si la key cambia en medio de un pedido, el pedido sale entero con la que tomó al empezar."""
    app, (k1, s1) = app_scraper
    pedidos = []
    real_hashing = scraper.hashing
    cambiada = []

    def hashing_y_cambio(*a, **kw):
        out = real_hashing(*a, **kw)
        if not cambiada:
            cambiada.append(1)
            escribir(secretos, *par())
        return out

    def grabar(self, method, url, **kw):
        pedidos.append((self.headers.get("X-MBX-APIKEY"), dict(parse_qsl(urlparse(url).query))))
        return _Resp()

    monkeypatch.setattr(scraper, "hashing", hashing_y_cambio)
    monkeypatch.setattr(requests.Session, "request", grabar)
    with app.app_context():
        scraper.send_signed_request("GET", "/fapi/v1/openOrders")
    assert pedidos[0][0] == k1 and _firma_ok(pedidos[0][1], s1)


def test_scraper_latido_con_key_id(app_scraper, monkeypatch):
    app, (k1, _) = app_scraper
    monkeypatch.setattr(scraper, "scrape", lambda app=None: True)
    scraper.run_loop(app, interval=0, max_vueltas=1)
    latido = scraper.read_latido(app.config["DATABASE"])
    assert latido["key_id"] == credenciales.key_id(k1) and latido["key_error"] is None and "key_ok_at" in latido
    assert k1 not in json.dumps(latido)


# ---------------------------------------------------------------- panel: Apagar con la key del archivo


def test_panel_sin_keys_en_config_apaga_con_la_del_archivo(secretos, tmp_path, env, docker, binance):  # noqa: F811
    from futuresboard.app import init_app
    from test_bot_control import post
    import sqlite3
    from futuresboard import auth

    k, s = par()
    escribir(secretos, k, s)
    data = tmp_path / "data"
    data.mkdir()
    app = init_app(Config(CONFIG_DIR=tmp_path, DATABASE=data / "futures.db", DISABLE_AUTO_SCRAPE=True))
    auth.set_user_password(str(app.config["DATABASE"]), "cliente17", "clave-de-test")
    with sqlite3.connect(str(app.config["DATABASE"])) as conn:
        pw = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    with app.app_context():
        fp = auth.password_fingerprint(pw)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess.update(username="cliente17", csrf_token="tok", **{auth.SESSION_FINGERPRINT_KEY: fp})
    binance.add_position("BTCUSDT", "1")
    resp = post(c, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200
    assert {h.get("X-MBX-APIKEY") for _m, _p, q, h in binance.calls if "signature" in q} == {k}
    assert credenciales.de_app(app).estado()["key_ok_at"] is not None


# ---------------------------------------------------------------- credenciales_meta.json con hilos (gthread)

_META_LARGA = {"key": "Lm3p…9zQe", "key_id": "k" * 12, "huella_uid": "h" * 64, "linea_base_en": "2026-10-26T10:00:00Z",
               "configurada_en": "2026-10-26T10:05:00Z", "configurada_por": "marcos",
               "validada_en": "2026-10-26T10:05:00Z", "hedge": True, "entorno": "demo",
               "anterior": {"key": "Ab12…cd34", "key_id": "a" * 12}}
_META_CORTA = {"key": "Lm3p…9zQe", "key_id": "k" * 12, "huella_uid": "h" * 64, "linea_base_en": "2026-10-26T10:05:01Z",
               "validada_en": "2026-10-26T10:05:01Z", "hedge": True, "entorno": "demo"}


def test_guardar_meta_de_dos_hilos_a_la_vez_deja_un_json_valido(tmp_path, monkeypatch):
    """gunicorn corre 8 hilos: el GET de la línea de base y el job pueden guardar la meta a la vez. Con un temporal fijo
    por pid, los dos lo abrían con O_TRUNC, el archivo quedaba mezclado y el segundo os.replace daba
    FileNotFoundError (revisión, hallazgo 7)."""
    import threading

    real_dump, real_replace = json.dump, os.replace
    ambos_escribiendo = threading.Barrier(2, timeout=5)
    largo_escrito = threading.Event()
    largo_reemplazo = threading.Event()

    def dump(obj, fh, *a, **k):
        ambos_escribiendo.wait()  # los dos tienen su temporal abierto antes de escribir
        if threading.current_thread().name == "get":
            largo_escrito.wait(5)  # el corto escribe después del largo
        real_dump(obj, fh, *a, **k)
        if threading.current_thread().name == "job":
            fh.flush()
            largo_escrito.set()

    def replace(src, dst):
        if threading.current_thread().name == "get":
            largo_reemplazo.wait(5)
        try:
            return real_replace(src, dst)
        finally:
            if threading.current_thread().name == "job":
                largo_reemplazo.set()

    monkeypatch.setattr(json, "dump", dump)
    monkeypatch.setattr(os, "replace", replace)
    errores = []

    def correr(meta, nombre):
        try:
            credenciales.guardar_meta(tmp_path, meta)
        except OSError as exc:
            errores.append((nombre, type(exc).__name__))

    hilos = [threading.Thread(target=correr, args=(_META_LARGA, "job"), name="job"),
             threading.Thread(target=correr, args=(_META_CORTA, "get"), name="get")]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(10)
    monkeypatch.undo()
    assert errores == []
    final = json.loads((tmp_path / credenciales.META_FILE).read_text(encoding="utf-8"))
    assert final in (_META_LARGA, _META_CORTA)
    assert oct((tmp_path / credenciales.META_FILE).stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob(".*.tmp"))


def test_guardar_meta_sin_temporales_huerfanos_si_falla(tmp_path, monkeypatch):
    def dump(*a, **k):
        raise OSError("disco lleno")

    monkeypatch.setattr(json, "dump", dump)
    with pytest.raises(OSError):
        credenciales.guardar_meta(tmp_path, _META_CORTA)
    monkeypatch.undo()
    assert not list(tmp_path.glob(".*"))
