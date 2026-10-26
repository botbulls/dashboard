"""Cambiar la API key desde el panel (spec bot-estrategia § 10.2, § 10.4 a § 10.7; commit 13).

Binance está simulado (``FakeBinanceKeys``): ningún test sale a la red. Keys, secrets y contraseñas de prueba se generan
en cada corrida; nunca son reales ni literales de 64 caracteres.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlparse

import pytest
import requests

from futuresboard import acciones
from futuresboard import bot_control
from futuresboard import cambio_key
from futuresboard import credenciales
from futuresboard import jobs
from futuresboard import rescate
from futuresboard import scraper
from futuresboard import telegram_notify

from test_bot_control import FakeResponse
from test_bot_control import app  # noqa: F401
from test_bot_control import audit_lines
from test_bot_control import binance  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401
from test_bot_control import wait_job
from test_credenciales import escribir
from test_credenciales import par

FUT = "https://demo-fapi.binance.com"
SPOT = "https://demo-api.binance.com"
CLAVE = "clave-de-test"  # la contraseña del usuario de test_bot_control.client


def iso(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


class Cuenta:
    def __init__(self, uid, dual=True, sonda=-4059, restricciones=None, income=("9001", "9002"),
                 posiciones=(("BTCUSDT", "LONG", "0.004"),), error_futuros=None, timeout=False, balance="5417.55"):
        self.uid, self.dual, self.sonda, self.restricciones = uid, dual, sonda, restricciones
        self.balance = balance
        self.income, self.posiciones = list(income), list(posiciones)
        self.error_futuros, self.timeout = error_futuros, timeout


class FakeBinanceKeys:
    """Binance Demo/real con varias keys: verifica la firma con el secret de cada una, como Binance."""

    def __init__(self):
        self.keys = {}  # api_key -> (secret, Cuenta)
        self.calls = []  # (method, path, api_key)

    def agregar(self, key, secret, cuenta):
        self.keys[key] = (secret, cuenta)
        return cuenta

    def request(self, method, url, headers=None, timeout=None, **kw):
        u = urlparse(url)
        params = dict(parse_qsl(u.query))
        key = (headers or {}).get("X-MBX-APIKEY")
        self.calls.append((method, u.path, key))
        if key not in self.keys:
            return FakeResponse(401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."})
        secret, c = self.keys[key]
        if c.timeout:
            raise requests.ReadTimeout("tardó")
        firma = params.pop("signature", "")
        if firma != hmac.new(secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest():
            return FakeResponse(400, {"code": -1022, "msg": "Signature for this request is not valid."})
        ruta = (method, u.path)
        if ruta == ("GET", "/fapi/v2/account"):
            if c.error_futuros:
                return FakeResponse(400, {"code": c.error_futuros, "msg": "error"})
            return FakeResponse(200, {"totalWalletBalance": c.balance, "positions": [
                {"symbol": s, "positionSide": ps, "positionAmt": a} for s, ps, a in c.posiciones]
                + [{"symbol": "LTCUSDT", "positionSide": "LONG", "positionAmt": "0"}]})
        if ruta == ("GET", "/api/v3/account"):
            return FakeResponse(200, {"uid": c.uid, "balances": [{"asset": "USDT", "free": "1", "locked": "0"}]})
        if ruta == ("GET", "/fapi/v1/positionSide/dual"):
            return FakeResponse(200, {"dualSidePosition": c.dual})
        if ruta == ("POST", "/fapi/v1/positionSide/dual"):
            assert c.dual, "la sonda solo corre con la cuenta ya en hedge mode"
            if c.sonda == 200:
                return FakeResponse(200, {"code": 200, "msg": "success"})
            return FakeResponse(400, {"code": c.sonda, "msg": "No need to change position side."})
        if ruta == ("GET", "/sapi/v1/account/apiRestrictions"):
            if c.restricciones is None:
                return FakeResponse(404, {})
            return FakeResponse(200, dict(c.restricciones))
        if ruta == ("GET", "/fapi/v1/income"):
            return FakeResponse(200, [{"tranId": int(t), "incomeType": "REALIZED_PNL"} for t in c.income])
        return FakeResponse(404, {"code": -5000, "msg": "Path not found"})

    def posts_sonda(self):
        return [c for c in self.calls if c[:2] == ("POST", "/fapi/v1/positionSide/dual")]


class Entorno:
    def __init__(self, app, d, resp, fake, k0, s0):  # noqa: F811
        self.app, self.dir, self.respaldo, self.fake, self.k0, self.s0 = app, d, resp, fake, k0, s0

    @property
    def data_dir(self):
        return pathlib.Path(self.app.config["DATABASE"]).parent

    def archivo(self):
        return json.loads((self.dir / credenciales.ARCHIVO).read_text())


@pytest.fixture
def cambio(app, env, docker, tmp_path, monkeypatch):  # noqa: F811
    d, resp = tmp_path / "secretos", tmp_path / "secretos-respaldo"
    resp.mkdir()
    k0, s0 = par()
    escribir(d, k0, s0)
    monkeypatch.setenv(cambio_key.ENV_WEB, "1")
    monkeypatch.setenv(credenciales.ENV_RESPALDO_DIR, str(resp))
    monkeypatch.setenv(cambio_key.ENV_ESPERA, "0")
    app.config["BINANCE_TESTNET"] = True
    app.config["API_BASE_URL"] = FUT
    app.extensions[credenciales.EXTENSION] = credenciales.Proveedor(d, usuario=lambda: "binance_01")
    fake = FakeBinanceKeys()
    fake.agregar(k0, s0, Cuenta(uid=1001))
    from futuresboard import blueprint

    app.extensions[blueprint.CAMBIO_EXTENSION] = {"pendientes": cambio_key.Pendientes(),
                                                  "limite": cambio_key.Limitador(),
                                                  "cliente": lambda k, s: cambio_key.Cliente(FUT, SPOT, k, s,
                                                                                             session=fake)}
    # passivbot corriendo; el restart del proxy actualiza StartedAt como Docker.
    docker.started_at = iso(time.time() - 3600)
    original = docker.request

    def request(method, url, **kw):
        r = original(method, url, **kw)
        if method == "POST" and url.endswith("/restart") and r.status_code == 204:
            docker.started_at = iso(time.time() + 1)
        return r

    docker.request = request
    with sqlite3.connect(str(app.config["DATABASE"])) as conn:
        conn.execute("INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, positionAmt) "
                     "VALUES ('BTCUSDT', -1, 10, 80000, 'LONG', 0.004)")
        conn.executemany("INSERT INTO income (tranId, symbol, incomeType, income, asset, info, time, tradeId) "
                         "VALUES (?, 'BTCUSDT', 'REALIZED_PNL', 1, 'USDT', '', 1, 0)", [("9001",), ("9002",)])
    return Entorno(app, d, resp, fake, k0, s0)


def jpost(client, url, body):  # noqa: F811
    return client.post(url, data=json.dumps(body), content_type="application/json", headers={"X-CSRF-Token": "tok"})


def validar(client, k, s):  # noqa: F811
    return jpost(client, "/api/bot/credenciales/validar", {"api_key": k, "api_secret": s})


def plan(client, pid, url="/api/bot/credenciales"):  # noqa: F811
    r = jpost(client, url, {"pendiente_id": pid})
    assert r.status_code == 409 and r.get_json()["requiere_confirmacion"], r.get_json()
    return r.get_json()


def confirmar(client, pid, password=CLAVE, url="/api/bot/credenciales", wait=True):  # noqa: F811
    h = plan(client, pid, url)["plan_hash"]
    return post(client, url, {"pendiente_id": pid, "plan_hash": h, "password": password, "confirmar": True}, wait=wait)


def nueva(cambio, uid=1001, **kw):
    k, s = par()
    cambio.fake.agregar(k, s, Cuenta(uid=uid, **kw))
    return k, s


def textos(chequeos):
    return " | ".join(c["texto"] for c in chequeos)


# ---------------------------------------------------------------- flag, página y CSP


def test_flag_apagado_404_y_bot_lo_dice(client, app, cambio, monkeypatch):  # noqa: F811
    monkeypatch.setenv(cambio_key.ENV_WEB, "0")
    assert client.get("/bot/credenciales").status_code == 404
    k, s = nueva(cambio)
    for url, body in (("/api/bot/credenciales/validar", {"api_key": k, "api_secret": s}),
                      ("/api/bot/credenciales", {"pendiente_id": "x"}), ("/api/bot/credenciales/volver", {})):
        assert jpost(client, url, body).status_code == 404
    assert cambio.fake.calls == []
    assert client.get("/api/bot/credenciales").status_code == 200
    html = client.get("/bot").get_data(as_text=True)
    assert "FUTURESBOARD_CREDENCIALES_WEB=1" in html and "data-bot-cambiar-key" not in html
    assert 'disabled aria-disabled="true"><i class="fas fa-key"' in html


def test_sin_secretos_no_hay_donde_escribir(client, app, cambio, tmp_path):  # noqa: F811
    app.extensions[credenciales.EXTENSION] = credenciales.Proveedor(tmp_path / "no-existe",
                                                                    respaldo=lambda: par())
    resp = validar(client, *nueva(cambio))
    assert resp.status_code == 409 and "/secretos" in resp.get_json()["error"]
    assert "Falta el directorio de credenciales" in client.get("/bot/credenciales").get_data(as_text=True)


def test_pagina_con_csp_sin_scripts_externos_y_campos_fuera_de_un_form(client, cambio):  # noqa: F811
    resp = client.get("/bot/credenciales")
    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == (
        "default-src 'self'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
    assert resp.headers["Cache-Control"] == "no-store"
    html = resp.get_data(as_text=True)
    assert "<form" not in html.lower()
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts and all('src="/static/' in s for s in scripts), scripts
    assert re.findall(r"<script[^>]*>\s*</script>", html) and not re.search(r"<script[^>]*>[^<\s]", html)
    assert not re.search(r"""(href|src)=["']https?://""", html), "nada de terceros"
    assert "style=" not in html and "<style" not in html
    secreto = re.search(r'<input id="bbApiSecret"[^>]*>', html).group(0)
    assert 'type="text"' in secreto and "bb-input-secreto" in secreto and 'type="password"' not in secreto
    for attr in ('name="bb_k2"', "data-1p-ignore", 'data-lpignore="true"', "data-bwignore", 'autocomplete="off"',
                 'autocorrect="off"', 'spellcheck="false"'):
        assert attr in secreto, attr
    assert 'name="bb_k1"' in html and 'autocomplete="current-password"' in html
    assert cambio.k0[:8] not in html


def test_bot_ofrece_cambiar_con_el_flag(client, cambio):  # noqa: F811
    html = client.get("/bot").get_data(as_text=True)
    assert 'href="/bot/credenciales" data-bot-cambiar-key' in html and "data-bot-volver-key" not in html


# ---------------------------------------------------------------- validación (§ 10.2)


def test_validar_ok_responde_pendiente_y_nunca_repite_lo_que_llego(client, cambio):  # noqa: F811
    k, s = nueva(cambio)
    resp = validar(client, k, s)
    data = resp.get_json()
    assert resp.status_code == 200 and data["ok"] and len(data["pendiente_id"]) == 32
    assert data["key"] == f"{k[:4]}…{k[-4:]}"
    texto = resp.get_data(as_text=True)
    assert not [w for w in _ventanas(k) | _ventanas(s) if w in texto]
    t = textos(data["chequeos"])
    for parte in ("Formato", "Distinta de la actual", "Firma en Binance Demo", "Misma cuenta: el UID coincide",
                  "Hedge mode activo", "«ya está en hedge» (−4059)", "Spot de Demo responde",
                  "las mismas 1 que ve el panel (BTC long)", "Demo no los informa"):
        assert parte in t, parte
    assert [c["clave"] for c in data["chequeos"]] == ["formato", "distinta", "firma", "cuenta", "hedge", "operar",
                                                     "spot", "posiciones", "permisos"]
    assert not any(c["estado"] == "ok" for c in data["chequeos"] if c["clave"] == "permisos")


def test_la_primera_vez_guarda_la_linea_de_base_sin_el_uid(client, cambio):  # noqa: F811
    assert credenciales.leer_meta(cambio.data_dir) == {}
    validar(client, *nueva(cambio))
    meta = credenciales.leer_meta(cambio.data_dir)
    assert meta["key_id"] == credenciales.key_id(cambio.k0) and meta["hedge"] is True and meta["huella_uid"]
    texto = (cambio.data_dir / credenciales.META_FILE).read_text()
    assert "1001" not in texto and cambio.k0 not in texto
    assert oct((cambio.data_dir / credenciales.META_FILE).stat().st_mode & 0o777) == "0o600"
    assert oct((cambio.dir / cambio_key.HUELLA_ARCHIVO).stat().st_mode & 0o777) == "0o600"


def test_abrir_la_pagina_guarda_la_linea_de_base(client, cambio):  # noqa: F811
    client.get("/bot/credenciales")
    assert credenciales.leer_meta(cambio.data_dir)["key_id"] == credenciales.key_id(cambio.k0)
    assert "UID verificado" in client.get("/bot").get_data(as_text=True)


def test_get_conexion_guarda_la_linea_de_base_y_no_devuelve_key_secret_ni_uid(client, cambio):  # noqa: F811
    resp = client.get("/api/bot/credenciales")
    data = resp.get_json()
    assert data["cuenta"] == {"verificada": True, "texto": "UID verificado"} and data["hedge"] is True
    texto = resp.get_data(as_text=True)
    assert cambio.k0 not in texto and cambio.s0 not in texto and "1001" not in texto and "huella" not in texto


def test_formato_invalido_no_llama_a_binance_ni_se_registra(client, cambio, monkeypatch):  # noqa: F811
    from futuresboard import logs

    registrados = []
    monkeypatch.setattr(logs, "register_secret", lambda v: registrados.append(v))
    for k, s in (("corta", "x" * 64), ("a" * 63 + "!", "b" * 64), (None, "b" * 64), ("a" * 64, ["x"])):
        assert validar(client, k, s).status_code == 400
    assert cambio.fake.calls == [] and registrados == []


def test_misma_key_que_la_actual_bloquea(client, cambio):  # noqa: F811
    resp = validar(client, cambio.k0, cambio.s0)
    assert resp.status_code == 422 and "misma API key" in resp.get_json()["error"]
    assert cambio.fake.calls == []


def test_otra_cuenta_bloquea(client, cambio):  # noqa: F811
    client.get("/api/bot/credenciales")  # línea de base con la key actual (uid 1001)
    k, s = nueva(cambio, uid=2002, balance="98765.43")
    resp = validar(client, k, s)
    data = resp.get_json()
    assert resp.status_code == 422 and data["otra_cuenta"] is True
    t = textos(data["chequeos"])
    assert "Es otra cuenta" in t and "quedaría sin bot: 1 posición" in t and "KPIs" in t
    assert "2002" not in resp.get_data(as_text=True)
    # Ni el balance de la cuenta ajena: el panel no es un oráculo de keys robadas (§ 10.6, revisión hallazgo 2).
    assert "balance" not in t and "98.765" not in resp.get_data(as_text=True)
    assert "Firma en Binance Demo: la cuenta de futuros responde" in t
    assert not cambio.fake.posts_sonda()
    assert audit_lines(cambio.app)[-1]["params"]["cuenta"] == "otra"


def test_key_vieja_revocada_y_tranid_en_comun_permite(client, cambio):  # noqa: F811
    del cambio.fake.keys[cambio.k0]  # la actual ya no sirve (revocada) y no hay huella guardada
    k, s = nueva(cambio, uid=1001, income=("5", "9002"))
    resp = validar(client, k, s)
    assert resp.status_code == 200, resp.get_json()
    assert "1 movimientos de Binance coinciden" in textos(resp.get_json()["chequeos"])


def test_sin_huella_key_vieja_revocada_y_sin_tranid_en_comun_bloquea(client, cambio):  # noqa: F811
    del cambio.fake.keys[cambio.k0]
    k, s = nueva(cambio, uid=1001, income=("5", "6"), balance="98765.43")
    resp = validar(client, k, s)
    assert resp.status_code == 422
    assert "No se puede saber si es la misma cuenta" in resp.get_json()["error"]
    assert "la key actual no responde" in resp.get_json()["error"]
    assert "balance" not in textos(resp.get_json()["chequeos"]) and "98.765" not in resp.get_data(as_text=True)


def test_el_balance_se_muestra_recien_con_la_misma_cuenta(client, cambio):  # noqa: F811
    client.get("/api/bot/credenciales")
    resp = validar(client, *nueva(cambio, uid=1001))
    assert resp.status_code == 200
    assert "la cuenta de futuros responde · balance 5.417,55 USDT" in textos(resp.get_json()["chequeos"])
    del cambio.fake.keys[cambio.k0]  # sin huella ni key actual: misma cuenta por tranId
    cambio_key_meta = cambio.data_dir / credenciales.META_FILE
    cambio_key_meta.unlink()
    resp = validar(client, *nueva(cambio, uid=1001, income=("5", "9002")))
    assert resp.status_code == 200 and "balance 5.417,55 USDT" in textos(resp.get_json()["chequeos"])


# ---------------------------------------------------------------- clave de la huella (/secretos/huella.key)


def test_perder_la_clave_de_la_huella_no_convierte_la_misma_cuenta_en_otra(client, cambio):  # noqa: F811
    """Si ./secretos se rehace sin huella.key y la meta sobrevive, la clave nueva da otro HMAC: la misma cuenta salía
    "Es otra cuenta" y quedaba trabada (revisión, hallazgo 3). Ahora la meta guarda el id de la clave y, si cambió, la
    línea de base se recalcula con la key actual."""
    client.get("/api/bot/credenciales")
    meta = credenciales.leer_meta(cambio.data_dir)
    assert meta["huella_clave_id"] and len(meta["huella_clave_id"]) == 12
    (cambio.dir / cambio_key.HUELLA_ARCHIVO).unlink()
    resp = validar(client, *nueva(cambio, uid=1001))
    assert resp.status_code == 200, resp.get_json()
    nuevo = credenciales.leer_meta(cambio.data_dir)
    assert nuevo["huella_clave_id"] != meta["huella_clave_id"] and nuevo["huella_uid"] != meta["huella_uid"]
    # Otra cuenta sigue bloqueando con la clave nueva.
    resp = validar(client, *nueva(cambio, uid=2002))
    assert resp.status_code == 422 and resp.get_json()["otra_cuenta"] is True


def test_sin_la_clave_y_con_la_key_actual_revocada_cae_al_tranid(client, cambio):  # noqa: F811
    client.get("/api/bot/credenciales")
    (cambio.dir / cambio_key.HUELLA_ARCHIVO).unlink()
    del cambio.fake.keys[cambio.k0]  # la actual revocada: la base no se puede recalcular
    resp = validar(client, *nueva(cambio, uid=1001, income=("5", "9002")))
    assert resp.status_code == 200, resp.get_json()
    assert "1 movimientos de Binance coinciden" in textos(resp.get_json()["chequeos"])
    resp = validar(client, *nueva(cambio, uid=1001, income=("5", "6")))
    data = resp.get_json()
    assert resp.status_code == 422 and data["otra_cuenta"] is False
    assert "No se puede saber si es la misma cuenta" in data["error"] and "Es otra cuenta" not in data["error"]


def test_volver_a_la_anterior_sin_la_clave_de_la_huella(client, cambio):  # noqa: F811
    k, s = nueva(cambio, uid=1001)
    assert confirmar(client, validar(client, k, s).get_json()["pendiente_id"]).status_code == 200
    (cambio.dir / cambio_key.HUELLA_ARCHIVO).unlink()
    resp = jpost(client, "/api/bot/credenciales/volver", {})
    data = resp.get_json()
    assert resp.status_code == 409 and data.get("requiere_confirmacion"), data


def test_clave_de_la_huella_truncada_o_vacia_se_regenera(client, cambio, tmp_path):  # noqa: F811
    client.get("/api/bot/credenciales")
    p = cambio.dir / cambio_key.HUELLA_ARCHIVO
    p.write_text(p.read_text()[:10])
    resp = validar(client, *nueva(cambio, uid=1001))
    assert resp.status_code == 200, resp.get_json()
    assert len(p.read_text()) == 64 and oct(p.stat().st_mode & 0o777) == "0o600"
    vacia = tmp_path / "vacia"
    vacia.mkdir()
    (vacia / cambio_key.HUELLA_ARCHIVO).write_text("")
    clave = cambio_key.huella_clave(vacia)
    assert len(clave) == 64 and cambio_key.huella_clave(vacia) == clave
    assert not [x for x in vacia.iterdir() if x.name != cambio_key.HUELLA_ARCHIVO]  # sin temporales


def test_clave_de_la_huella_creada_por_dos_hilos_es_una_sola(tmp_path):
    barrera = threading.Barrier(8)
    claves = []

    def crear():
        barrera.wait()
        claves.append(cambio_key.huella_clave(tmp_path))

    hilos = [threading.Thread(target=crear) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(10)
    assert len(set(claves)) == 1 and len(claves[0]) == 64
    assert (tmp_path / cambio_key.HUELLA_ARCHIVO).read_text().encode("ascii") == claves[0]
    assert [x.name for x in tmp_path.iterdir()] == [cambio_key.HUELLA_ARCHIVO]


def test_one_way_bloquea_y_la_sonda_no_corre(client, cambio):  # noqa: F811
    resp = validar(client, *nueva(cambio, dual=False))
    assert resp.status_code == 422 and "one-way" in resp.get_json()["error"]
    assert cambio.fake.posts_sonda() == []


@pytest.mark.parametrize("codigo,texto", [(-2015, "no tiene permiso de operar futuros"),
                                          (-1099, "respondió -1099: no se puede usar"),
                                          (200, "respondió 200: no se puede usar")])
def test_sonda_del_permiso_de_operar(client, cambio, codigo, texto):  # noqa: F811
    resp = validar(client, *nueva(cambio, sonda=codigo))
    assert resp.status_code == 422 and texto in resp.get_json()["error"]
    assert len(cambio.fake.posts_sonda()) == 1


def test_errores_de_binance_con_motivo(client, cambio, app):  # noqa: F811
    k = secrets.token_hex(32)
    resp = validar(client, k, secrets.token_hex(32))  # Binance no la conoce: -2015
    assert "Binance Demo no reconoce esta key: ¿es de Binance real?" in resp.get_json()["error"]
    k2, s2 = nueva(cambio)
    resp = validar(client, k2, secrets.token_hex(32))  # secret de otra
    assert resp.get_json()["error"] == "El secret no corresponde a la key."
    resp = validar(client, *nueva(cambio, timeout=True))
    assert resp.get_json()["error"] == "Binance no respondió; no se guardó nada."
    app.config["BINANCE_TESTNET"] = False
    resp = validar(client, secrets.token_hex(32), secrets.token_hex(32))
    assert "Binance real no reconoce esta key" in resp.get_json()["error"]


def test_permisos_en_real(client, cambio, app):  # noqa: F811
    app.config["BINANCE_TESTNET"] = False
    malos = {"enableReading": True, "enableFutures": True, "enableWithdrawals": True, "enableInternalTransfer": False,
             "ipRestrict": False}
    resp = validar(client, *nueva(cambio, restricciones=malos))
    error = resp.get_json()["error"]
    assert resp.status_code == 422 and "enableWithdrawals debería ser false" in error and "ipRestrict" in error
    buenos = {**malos, "enableWithdrawals": False, "ipRestrict": True}
    resp = validar(client, *nueva(cambio, restricciones=buenos))
    assert resp.status_code == 200
    assert "sin retiros" in textos(resp.get_json()["chequeos"])
    resp = validar(client, *nueva(cambio, restricciones=None))  # en real, sin apiRestrictions no se puede
    assert resp.status_code == 422 and "permisos" in resp.get_json()["error"]


def test_limite_de_validaciones_429(client, cambio):  # noqa: F811
    for _ in range(cambio_key.VALIDACIONES_MAX):
        assert validar(client, *nueva(cambio)).status_code == 200
    llamadas = len(cambio.fake.calls)
    resp = validar(client, *nueva(cambio))
    assert resp.status_code == 429 and "5 validaciones cada 15 minutos" in resp.get_json()["error"]
    assert len(cambio.fake.calls) == llamadas  # el 6.º no llega a Binance


def test_limitador_y_pendientes_en_el_tiempo():
    t = [0.0]
    lim = cambio_key.Limitador(maximo=2, ventana=60, clock=lambda: t[0])
    assert lim.intentar("u") is None and lim.intentar("u") is None and lim.intentar("u") == 60
    assert lim.intentar("otro") is None
    t[0] = 61
    assert lim.intentar("u") is None
    pend = cambio_key.Pendientes(clock=lambda: t[0], ttl=300)
    k, s = par()
    p = pend.crear(usuario="u", api_key=k, api_secret=s, key_id="x", enmascarada="…", huella=None,
                   archivo_sha="a")
    assert k not in repr(p) and s not in repr(p)
    with pytest.raises(cambio_key.PendienteError):
        pend.ver(p.id, "otro")
    assert pend.ver(p.id, "u") is p
    t[0] += 301
    with pytest.raises(cambio_key.PendienteError) as exc:
        pend.ver(p.id, "u")
    assert "venció" in str(exc.value)


# ---------------------------------------------------------------- plan, re-auth y pendiente


def test_plan_409_con_diff_y_avisos(client, cambio):  # noqa: F811
    k, s = nueva(cambio)
    pid = validar(client, k, s).get_json()["pendiente_id"]
    data = plan(client, pid)
    p = data["plan"]
    assert p["cambios"][0] == {"que": "API key", "nota": "primeros y últimos 4",
                               "antes": f"{cambio.k0[:4]}…{cambio.k0[-4:]}", "despues": f"{k[:4]}…{k[-4:]}",
                               "clase": "key"}
    assert "se reinicia" in p["avisos"][0]["titulo"] and "No borres la key vieja" in p["avisos"][-1]["titulo"]
    assert data["pendiente_id"] == pid and len(data["plan_hash"]) == 32
    assert audit_lines(cambio.app)[-1]["result"] == "requiere_confirmacion"


def test_sin_casilla_o_con_plan_viejo_no_aplica(client, cambio):  # noqa: F811
    pid = validar(client, *nueva(cambio)).get_json()["pendiente_id"]
    h = plan(client, pid)["plan_hash"]
    resp = jpost(client, "/api/bot/credenciales", {"pendiente_id": pid, "plan_hash": h, "password": CLAVE})
    assert resp.status_code == 409 and resp.get_json()["requiere_confirmacion"]
    resp = jpost(client, "/api/bot/credenciales", {"pendiente_id": pid, "plan_hash": "x" * 32, "password": CLAVE,
                                                   "confirmar": True})
    assert resp.status_code == 409 and "ya no corresponde" in resp.get_json()["error"]
    assert cambio.archivo()["binance_01"]["key"] == cambio.k0


def test_reauth_comparte_el_limitador_del_login_y_avisa(client, cambio, monkeypatch):  # noqa: F811
    avisos = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action", lambda *a, **kw: avisos.append(a))
    pid = validar(client, *nueva(cambio)).get_json()["pendiente_id"]
    for _ in range(5):
        resp = confirmar(client, pid, password="no-es-" + secrets.token_hex(4))
        assert resp.status_code == 403 and resp.get_json()["error"] == "Contraseña incorrecta."
    assert sum(1 for a in avisos if a[0] == "credenciales" and a[2] == "error") == 5
    resp = confirmar(client, pid)  # la buena, pero el limitador ya está lleno
    assert resp.status_code == 429
    # El mismo limitador que el login (usuario + IP): el login también queda bloqueado.
    login = client.post("/login", data={"username": "cliente17", "password": CLAVE})
    assert login.status_code == 429
    assert cambio.archivo()["binance_01"]["key"] == cambio.k0


def test_reauth_ok_limpia_los_fallos_como_el_login(client, cambio):  # noqa: F811
    pid = validar(client, *nueva(cambio)).get_json()["pendiente_id"]
    for _ in range(3):
        assert confirmar(client, pid, password="mala").status_code == 403
    assert confirmar(client, pid).status_code == 200


def test_pendiente_vencido_o_reusado_409(client, cambio):  # noqa: F811
    from futuresboard import blueprint

    t = [1000.0]
    cambio.app.extensions[blueprint.CAMBIO_EXTENSION]["pendientes"] = cambio_key.Pendientes(clock=lambda: t[0])
    pid = validar(client, *nueva(cambio)).get_json()["pendiente_id"]
    t[0] += cambio_key.PENDIENTE_TTL_S + 1
    resp = jpost(client, "/api/bot/credenciales", {"pendiente_id": pid})
    assert resp.status_code == 409 and resp.get_json()["vencido"] and "venció" in resp.get_json()["error"]
    pid = validar(client, *nueva(cambio)).get_json()["pendiente_id"]
    h = plan(client, pid)["plan_hash"]
    ok = post(client, "/api/bot/credenciales", {"pendiente_id": pid, "plan_hash": h, "password": CLAVE,
                                                "confirmar": True})
    assert ok.status_code == 200
    resp = jpost(client, "/api/bot/credenciales", {"pendiente_id": pid, "plan_hash": h, "password": CLAVE,
                                                   "confirmar": True})
    assert resp.status_code == 409 and resp.get_json()["vencido"]


def test_con_otra_accion_en_curso_el_pendiente_no_se_pierde(client, cambio, app):  # noqa: F811
    pid = validar(client, *nueva(cambio)).get_json()["pendiente_id"]
    store = bot_control.Store(cambio.data_dir)
    handle = store.try_lock()
    try:
        resp = confirmar(client, pid, wait=False)
        assert resp.status_code == 409
    finally:
        handle.release()
    assert confirmar(client, pid).status_code == 200


# ---------------------------------------------------------------- job


def test_cambio_completo(client, cambio, docker, monkeypatch):  # noqa: F811
    antes = cambio.archivo()
    k, s = nueva(cambio)
    pid = validar(client, k, s).get_json()["pendiente_id"]
    res = confirmar(client, pid)
    assert res.status_code == 200, res.job
    job = res.job
    assert job["estado"] == "ok" and job["params"] == {"key": f"{k[:4]}…{k[-4:]}", "cuenta": "misma",
                                                       "entorno": "demo"}
    estados = {p["clave"]: p["estado"] for p in job["pasos"]}
    assert estados == {"revalidar": "ok", "respaldo": "ok", "escribir": "ok", "docker": "ok", "verificar": "ok",
                       "consumidores": "error"}  # espera 0 s: termina con avisos
    assert "No borres la key vieja" in job["resultado"]["warning"]
    despues = cambio.archivo()
    assert despues["binance_01"]["key"] == k and despues["binance_01"]["secret"] == s
    assert despues["binance_01"]["testnet"] is True and despues["binance_01"]["exchange"] == "binance"
    assert {x: v for x, v in despues.items() if x != "binance_01"} == {x: v for x, v in antes.items() if x != "binance_01"}
    assert oct((cambio.dir / credenciales.ARCHIVO).stat().st_mode & 0o777) == "0o600"
    respaldos = list(cambio.respaldo.glob("api-keys.json.bak-*"))
    assert len(respaldos) == 1 and json.loads(respaldos[0].read_text()) == antes
    assert oct(respaldos[0].stat().st_mode & 0o777) == "0o600"
    assert not list(cambio.dir.glob(".*.tmp"))
    assert docker.actions() == ["restart"]
    meta = credenciales.leer_meta(cambio.data_dir)
    assert meta["key_id"] == credenciales.key_id(k) and meta["configurada_por"] == "cliente17"
    assert meta["anterior"]["key"] == f"{cambio.k0[:4]}…{cambio.k0[-4:]}"
    # El panel ya firma con la nueva.
    assert credenciales.de_app(cambio.app).estado()["key_id"] == credenciales.key_id(k)
    linea = audit_lines(cambio.app)[-1]
    assert linea["action"] == "credenciales" and linea["result"] == "ok" and linea["params"] == job["params"]
    html = client.get("/bot").get_data(as_text=True)
    assert "data-bot-volver-key" in html and f"{cambio.k0[:4]}…{cambio.k0[-4:]}" in html


def test_detenido_escribe_y_no_reinicia(client, cambio, docker):  # noqa: F811
    docker.running = False
    k, s = nueva(cambio)
    res = confirmar(client, validar(client, k, s).get_json()["pendiente_id"])
    assert res.status_code == 200
    estados = {p["clave"]: p["estado"] for p in res.job["pasos"]}
    assert estados["docker"] == "omitido" and estados["verificar"] == "omitido"
    assert docker.actions() == [] and cambio.archivo()["binance_01"]["key"] == k


def test_docker_4xx_restaura_copiando_dentro_de_secretos(client, cambio, docker, monkeypatch):  # noqa: F811
    antes = (cambio.dir / credenciales.ARCHIVO).read_bytes()
    k, s = nueva(cambio)
    pid = validar(client, k, s).get_json()["pendiente_id"]
    reemplazos = []
    real = os.replace

    def replace(src, dst, *a, **kw):
        reemplazos.append((pathlib.Path(src).parent, pathlib.Path(dst).parent))
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", replace)
    docker.fail_action = 500
    res = confirmar(client, pid)
    assert res.status_code == 502 and "Se restauró la key anterior" in res.get_json()["error"]
    assert (cambio.dir / credenciales.ARCHIVO).read_bytes() == antes
    archivo = [(a, b) for a, b in reemplazos if b == cambio.dir]
    assert len(archivo) == 2 and all(a == b for a, b in archivo)  # escribir y restaurar, sin rename entre montajes
    assert not any(a == cambio.respaldo for a, _ in reemplazos)
    assert credenciales.de_app(cambio.app).estado()["key_id"] == credenciales.key_id(cambio.k0)


def test_docker_sin_respuesta_no_revierte(client, cambio, docker):  # noqa: F811
    k, s = nueva(cambio)
    pid = validar(client, k, s).get_json()["pendiente_id"]
    docker.timeout_action = True
    res = confirmar(client, pid)
    assert res.status_code == 502 and "Resultado incierto" in res.get_json()["error"]
    assert cambio.archivo()["binance_01"]["key"] == k


def test_archivo_cambiado_desde_la_validacion_no_se_pisa(client, cambio):  # noqa: F811
    k, s = nueva(cambio)
    pid = validar(client, k, s).get_json()["pendiente_id"]
    otra_k, otra_s = par()
    escribir(cambio.dir, otra_k, otra_s)  # alguien lo cambió a mano mientras tanto
    res = confirmar(client, pid)
    assert res.status_code == 409 and "cambió desde la validación" in res.get_json()["error"]
    assert cambio.archivo()["binance_01"]["key"] == otra_k
    assert list(cambio.respaldo.iterdir()) == []


def test_espera_sin_el_lock_y_un_apagar_la_corta(client, cambio, docker, binance, monkeypatch):  # noqa: F811
    monkeypatch.setenv(cambio_key.ENV_ESPERA, "30")
    monkeypatch.setattr(cambio_key, "ESPERA_TICK_S", 0.05)
    k, s = nueva(cambio)
    pid = validar(client, k, s).get_json()["pendiente_id"]
    resp = confirmar(client, pid, wait=False)
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]
    deadline = time.monotonic() + 10
    while True:
        job = client.get(f"/api/bot/jobs/{job_id}").get_json()
        if job["cancelable"]:
            break
        assert time.monotonic() < deadline, job
        time.sleep(0.02)
    handle = bot_control.Store(cambio.data_dir).try_lock()
    assert handle is not None, "la espera a los consumidores no puede tener el lock de acciones"
    handle.release()
    apagar = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert apagar.accepted.status_code == 202 and apagar.status_code == 200
    final = wait_job(client, job_id)
    paso = next(p for p in final["pasos"] if p["clave"] == "consumidores")
    assert paso["estado"] == "omitido" and "interrumpida" in paso["detalle"]
    assert final["estado"] == "ok" and cambio.archivo()["binance_01"]["key"] == k


def _latidos(cambio, kid, ok_at, modo_rescate="dry_run", c3_mode=None):
    """Lo que publican scraper y rescate: ``key_id`` y su última firma OK (``key_ok_at``, ISO truncado a segundos)."""
    datos = cambio.data_dir
    (pathlib.Path(cambio.app.config["DATABASE"]).parent / scraper.LATIDO_FILE).write_text(
        json.dumps({"at": time.time(), "intervalo": 60, "key_id": kid, "key_ok_at": ok_at}))
    estado = rescate.default_state()
    estado["servicio"] = {"modo": modo_rescate, "heartbeat": dt.datetime.now(dt.timezone.utc).isoformat(),
                          "key_id": kid, "key_ok_at": ok_at}
    if c3_mode is not None:
        estado["c3"] = {"latido": {"ts": None, "c3_mode": c3_mode, "error": None, "suspendido": None}}
    rescate.save_state(datos, estado)


def _cuando_se_escribe(cambio, k, accion):
    """Corre ``accion`` apenas el job escribió la key nueva (los consumidores la toman después, como en la vida)."""
    def correr():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if cambio.archivo()["binance_01"]["key"] == k:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        time.sleep(0.05)
        accion()

    hilo = threading.Thread(target=correr)
    hilo.start()
    return hilo


def _orden_nueva(cambio):
    with sqlite3.connect(str(cambio.app.config["DATABASE"])) as conn:
        conn.execute("INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) "
                     "VALUES (1, 1, 'BUY', 'LONG', 'NEW', 'BTCUSDT', ?, 'LIMIT')", (int((time.time() + 5) * 1000),))


def test_espera_termina_bien_con_los_cuatro(client, cambio, docker, monkeypatch):  # noqa: F811
    monkeypatch.setenv(cambio_key.ENV_ESPERA, "20")
    monkeypatch.setattr(cambio_key, "ESPERA_TICK_S", 0.05)
    k, s = nueva(cambio)
    kid = credenciales.key_id(k)
    pid = validar(client, k, s).get_json()["pendiente_id"]

    def consumidores():  # scraper y rescate firman con la key nueva y passivbot crea una orden
        _latidos(cambio, kid, iso(time.time()))
        _orden_nueva(cambio)

    hilo = _cuando_se_escribe(cambio, k, consumidores)
    res = confirmar(client, pid)
    hilo.join()
    paso = next(p for p in res.job["pasos"] if p["clave"] == "consumidores")
    assert paso["estado"] == "ok" and "4 de 4 con la key nueva" in paso["detalle"]
    assert "ya podés borrar la vieja" in paso["detalle"]
    assert not res.job["resultado"].get("warning")


def test_key_id_en_el_latido_sin_firma_ok_con_la_nueva_no_cuenta(client, cambio, docker, monkeypatch):  # noqa: F811
    """El rescate escribe el key_id nuevo en el latido aunque todo su tick haya fallado (-2015), y el scraper publica
    el key_id que relee al escribir el latido aunque la vuelta haya firmado con la vieja. Sin una firma OK posterior a
    la escritura no cuentan (revisión, hallazgo 1)."""
    monkeypatch.setenv(cambio_key.ENV_ESPERA, "1")
    monkeypatch.setattr(cambio_key, "ESPERA_TICK_S", 0.05)
    k, s = nueva(cambio)
    kid = credenciales.key_id(k)
    pid = validar(client, k, s).get_json()["pendiente_id"]

    def consumidores():
        _latidos(cambio, kid, None)
        _orden_nueva(cambio)

    hilo = _cuando_se_escribe(cambio, k, consumidores)
    res = confirmar(client, pid)
    hilo.join()
    paso = next(p for p in res.job["pasos"] if p["clave"] == "consumidores")
    assert paso["estado"] == "error" and "Sin confirmar a tiempo: scraper, rescate" in paso["detalle"]
    assert "ya podés borrar" not in paso["detalle"]
    assert "Sin confirmar a tiempo" in res.job["resultado"]["warning"]


def test_firma_ok_vieja_de_la_misma_key_no_cuenta(cambio):
    """Volver a una key ya usada: el key_ok_at de antes (en memoria del proceso, indexado por key_id) no prueba que
    firme ahora."""
    from types import SimpleNamespace

    k, _s = par()
    kid = credenciales.key_id(k)
    escrito_en = time.time()
    _latidos(cambio, kid, iso(escrito_en - 3600))
    env_ = SimpleNamespace(proveedor=SimpleNamespace(estado=lambda: {"key_id": kid}), data_dir=cambio.data_dir,
                           db_path=str(cambio.app.config["DATABASE"]))
    out = cambio_key.consumidores_con(env_, kid, None, escrito_en)
    assert out == {"panel": True, "scraper": False, "rescate": False}
    _latidos(cambio, kid, iso(escrito_en))
    assert cambio_key.consumidores_con(env_, kid, None, escrito_en) == {"panel": True, "scraper": True,
                                                                         "rescate": True}


def test_detenido_no_dice_que_ya_se_puede_borrar_la_vieja(client, cambio, docker, monkeypatch):  # noqa: F811
    """Con passivbot detenido, el plan dice "borrala con 4 de 4" y Bot muestra 3 de 4: el job no puede cerrar con "ya
    podés borrar la vieja" (revisión, hallazgo 4)."""
    monkeypatch.setenv(cambio_key.ENV_ESPERA, "20")
    monkeypatch.setattr(cambio_key, "ESPERA_TICK_S", 0.05)
    docker.running = False
    k, s = nueva(cambio)
    kid = credenciales.key_id(k)
    pid = validar(client, k, s).get_json()["pendiente_id"]
    hilo = _cuando_se_escribe(cambio, k, lambda: _latidos(cambio, kid, iso(time.time())))
    res = confirmar(client, pid)
    hilo.join()
    paso = next(p for p in res.job["pasos"] if p["clave"] == "consumidores")
    assert paso["estado"] == "ok" and paso["detalle"].startswith("3 de 3 con la key nueva")
    assert "ya podés borrar" not in paso["detalle"] and "passivbot está detenido" in paso["detalle"]
    aviso = res.job["resultado"]["warning"]
    assert "4 de 4" in aviso and "Iniciar" in aviso and "No borres la vieja" in aviso


def test_rescate_y_c3_en_off_no_firman_y_no_se_esperan(client, cambio, docker, monkeypatch):  # noqa: F811
    """Con rescate y C3 en off el servicio no llama a Binance: no se espera su firma hasta el timeout, queda
    "omitido" y el job no dice "ya podés borrar"."""
    monkeypatch.setenv(cambio_key.ENV_ESPERA, "20")
    monkeypatch.setattr(cambio_key, "ESPERA_TICK_S", 0.05)
    k, s = nueva(cambio)
    kid = credenciales.key_id(k)
    pid = validar(client, k, s).get_json()["pendiente_id"]

    def consumidores():
        _latidos(cambio, kid, None, modo_rescate="off", c3_mode="off")
        (pathlib.Path(cambio.app.config["DATABASE"]).parent / scraper.LATIDO_FILE).write_text(
            json.dumps({"at": time.time(), "intervalo": 60, "key_id": kid, "key_ok_at": iso(time.time())}))
        _orden_nueva(cambio)

    hilo = _cuando_se_escribe(cambio, k, consumidores)
    inicio = time.monotonic()
    res = confirmar(client, pid)
    hilo.join()
    assert time.monotonic() - inicio < 15  # no esperó el timeout por el rescate
    paso = next(p for p in res.job["pasos"] if p["clave"] == "consumidores")
    assert paso["estado"] == "ok" and paso["detalle"].startswith("3 de 3 con la key nueva")
    assert "ya podés borrar" not in paso["detalle"] and "off" in paso["detalle"]
    assert "4 de 4" in res.job["resultado"]["warning"]
    html = client.get("/bot").get_data(as_text=True)
    assert 'data-consumidor="rescate" data-estado="sin_confirmar"' in html


def test_volver_a_la_key_anterior(client, cambio):  # noqa: F811
    k, s = nueva(cambio)
    assert confirmar(client, validar(client, k, s).get_json()["pendiente_id"]).status_code == 200
    resp = jpost(client, "/api/bot/credenciales/volver", {})
    data = resp.get_json()
    assert resp.status_code == 409 and data["requiere_confirmacion"], data
    assert data["key"] == f"{cambio.k0[:4]}…{cambio.k0[-4:]}" and data["plan"]["origen"] == "volver"
    res = post(client, "/api/bot/credenciales/volver", {"pendiente_id": data["pendiente_id"],
                                                       "plan_hash": data["plan_hash"], "password": CLAVE,
                                                       "confirmar": True})
    assert res.status_code == 200 and res.job["params"]["volver"] is True
    assert cambio.archivo()["binance_01"]["key"] == cambio.k0
    # El pendiente de "volver" no sirve para el cambio normal.
    resp = jpost(client, "/api/bot/credenciales/volver", {})
    assert resp.status_code == 409
    html = client.get("/bot/credenciales?volver=1").get_data(as_text=True)
    assert "Volver a la API key anterior" in html and k not in html


def test_respaldos_quedan_2_y_vencen_a_los_7_dias(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    ahora = time.time()
    for i in range(4):
        p = d / f"{cambio_key.RESPALDO_PREFIJO}2026010{i}-000000"
        p.write_text("{}")
        os.utime(p, (ahora - (10 - i) * 86400 if i == 0 else ahora, ahora - (10 - i) * 86400 if i == 0 else ahora))
    cambio_key.podar_respaldos(d, ahora)
    assert sorted(p.name[-15:] for p in d.iterdir()) == ["20260102-000000", "20260103-000000"]


def test_solo_cambian_key_y_secret():
    k, s = par()
    original = json.dumps({"referrals": {"binance": "x"}, "binance_01": {"exchange": "binance", "key": "a" * 64,
                                                                         "secret": "b" * 64, "testnet": True},
                           "okx_01": {"exchange": "okx", "key": "k", "secret": "s", "passphrase": "p"}}).encode()
    nuevo = cambio_key.contenido_nuevo(original, "binance_01", k, s)
    cambio_key.verificar_contenido(original, nuevo, "binance_01", k, s)
    roto = json.loads(nuevo)
    roto["binance_01"]["testnet"] = False
    with pytest.raises(cambio_key.CambioError):
        cambio_key.verificar_contenido(original, json.dumps(roto).encode(), "binance_01", k, s)


# ---------------------------------------------------------------- títulos e interrupción


def test_historial_dice_validada_y_telegram_avisa_cada_intento(client, cambio, monkeypatch):  # noqa: F811
    avisos = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action", lambda *a, **kw: avisos.append((a[0], a[2])))
    validar(client, "corta", "x")  # no llega a Binance, pero avisa
    validar(client, *nueva(cambio))
    assert avisos == [("credenciales_validar", "error"), ("credenciales_validar", "ok")]
    ultima = acciones.historial(bot_control.Store(cambio.data_dir).audit_path)["acciones"][0]
    assert ultima["estado"] == "validada" and ultima["estado_texto"] == "Validada"
    assert ultima["titulo"].startswith("API key nueva: validación (")


def test_titulos_y_telegram_con_la_key_enmascarada():
    p = {"key": "Lm3p…9zQe", "cuenta": "misma", "entorno": "demo"}
    assert acciones.titulo("credenciales", p) == "Cambiar la API key (Lm3p…9zQe)"
    assert acciones.titulo("credenciales", {**p, "volver": True}) == "Volver a la API key anterior (Lm3p…9zQe)"
    assert acciones.titulo("credenciales_validar", p) == "API key nueva: validación (Lm3p…9zQe)"
    texto = telegram_notify.format_bot_action("credenciales", p, "ok", {"docker_action": "restart"}, "cliente17")
    assert "CAMBIAR la API key" in texto and "Key: Lm3p…9zQe · demo" in texto


def test_job_interrumpido_se_audita_como_cambio_de_key(tmp_path):
    store = bot_control.Store(tmp_path)
    registry = jobs.JobRegistry(store)
    job = registry.create("credenciales", {"key": "Lm3p…9zQe", "cuenta": "misma", "entorno": "demo"}, "cliente17")
    job.release_alive()
    data = jobs.recover_interrupted(registry, logging.getLogger("t"))
    assert data["estado"] == "interrumpido" and "Conexión con Binance" in data["resultado"]["error"]
    linea = json.loads(store.audit_path.read_text().splitlines()[-1])
    assert linea["action"] == "credenciales" and linea["params"]["key"] == "Lm3p…9zQe"


# ---------------------------------------------------------------- la key y el secret nunca salen del servidor


def _ventanas(valor, n=8):
    return {valor[i:i + n] for i in range(len(valor) - n + 1)}


def test_los_64_caracteres_nunca_aparecen(client, cambio, docker, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(telegram_notify.ENV_TOKEN, "123:abc")
    monkeypatch.setenv(telegram_notify.ENV_CHAT_ID, "1")
    enviados = []
    monkeypatch.setattr(telegram_notify, "send_async", lambda text, cfg=None: enviados.append(text))
    salidas, errores = [], []
    clave_mala = "clave-mala-" + secrets.token_hex(8)

    def guardar(resp):
        salidas.append(resp.get_data(as_text=True) if hasattr(resp, "get_data") else json.dumps(resp))
        return resp

    k, s = nueva(cambio)
    # Formato inválido con el secret bueno, y una validación bloqueada (otra cuenta).
    guardar(validar(client, k + "!", s))
    guardar(client.get("/api/bot/credenciales"))
    ko, so = nueva(cambio, uid=918273645)
    guardar(validar(client, ko, so))
    # Validación OK, plan, contraseña mala, Docker que rechaza el restart (vuelta atrás) y de nuevo, bien.
    pid = guardar(validar(client, k, s)).get_json()["pendiente_id"]
    guardar(jpost(client, "/api/bot/credenciales", {"pendiente_id": pid}))
    guardar(confirmar(client, pid, password=clave_mala))
    docker.fail_action = 500
    falla = confirmar(client, pid)
    salidas.append(json.dumps(falla.job))
    docker.fail_action = None
    pid = guardar(validar(client, k, s)).get_json()["pendiente_id"]
    ok = confirmar(client, pid)
    salidas.append(json.dumps(ok.job))
    salidas.append(json.dumps(client.get(f"/api/bot/jobs/{ok.job['id']}").get_json()))
    # Volver a la anterior (la key vieja, también sintética) y las páginas.
    v = guardar(jpost(client, "/api/bot/credenciales/volver", {})).get_json()
    salidas.append(json.dumps(post(client, "/api/bot/credenciales/volver", {
        "pendiente_id": v["pendiente_id"], "plan_hash": v["plan_hash"], "password": CLAVE, "confirmar": True}).job))
    for url in ("/bot/credenciales", "/bot/credenciales?volver=1", "/bot", "/api/bot/credenciales",
                "/api/bot/acciones?limite=100"):
        guardar(client.get(url))
    # Errores forzados: Binance que responde mal, sin respuesta y Docker.
    cli = cambio_key.Cliente(FUT, SPOT, k, s, session=cambio.fake)
    cambio.fake.keys[k][1].timeout = True
    for llamada in (cli.futuros, cli.spot):
        try:
            llamada()
        except cambio_key.ErrorBinance as exc:
            errores.append(repr(exc) + str(exc) + repr(exc.__cause__) + repr(exc.__context__))
    try:
        cambio_key.Cliente(FUT, SPOT, secrets.token_hex(32), s, session=cambio.fake).futuros()
    except cambio_key.ErrorBinance as exc:
        errores.append(str(exc))

    datos = cambio.data_dir
    artefactos = {
        "respuestas": "\n".join(salidas),
        "bot_actions.log": (datos / bot_control.AUDIT_LOG_NAME).read_text(),
        "bot_job_last.json": (datos / jobs.LAST_JOB_FILE_NAME).read_text(),
        "credenciales_meta.json": (datos / credenciales.META_FILE).read_text(),
        "telegram": "\n".join(enviados),
        "logs": caplog.text + "\n".join(r.getMessage() for r in caplog.records),
        "excepciones": "\n".join(errores),
    }
    assert len(enviados) >= 4  # validaciones, contraseña mala y los cambios: Telegram sí avisó
    for nombre, texto in artefactos.items():
        for valor, que in ((s, "secret"), (so, "secret de otra cuenta"), (cambio.s0, "secret anterior"),
                           (k, "key"), (ko, "key de otra cuenta"), (cambio.k0, "key anterior")):
            filtradas = [w for w in _ventanas(valor) if w in texto]
            assert not filtradas, f"{que} en {nombre}: {len(filtradas)} ventanas de 8 caracteres"
        assert clave_mala not in texto and CLAVE not in texto, f"contraseña en {nombre}"
        assert "918273645" not in texto, f"uid en {nombre}"
    # La key aparece, pero solo enmascarada.
    assert f"{k[:4]}…{k[-4:]}" in artefactos["bot_actions.log"] and f"{k[:4]}…{k[-4:]}" in artefactos["telegram"]
