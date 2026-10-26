"""Bot → "Conexión con Binance" (spec bot-estrategia § 10.4, paso 1): key enmascarada y quién la usa, sin Binance."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import time

from futuresboard import conexion
from futuresboard import credenciales
from futuresboard import rescate
from futuresboard import scraper

from test_bot_control import app  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import router  # noqa: F401
from test_credenciales import escribir
from test_credenciales import par

AHORA = 1_761_480_000.0  # 26/10/2025 12:00 UTC


def iso(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def vista(**kw):
    base = dict(
        estado={"key_id": "aaaaaaaaaaaa", "key_ok_at": None, "key_error": None, "usuario": "binance_01",
                "testnet": True, "archivo_mtime": AHORA - 86400},
        modo_secretos=True, testnet=True, meta={}, scraper_latido={"key_id": "aaaaaaaaaaaa",
                                                                     "key_ok_at": iso(AHORA - 36)},
        scraper_vivo=True, scraper_intervalo=60,
        rescate_servicio={"key_id": "aaaaaaaaaaaa", "key_ok_at": iso(AHORA - 12)}, rescate_vivo=True,
        container={"status": "running", "running": True, "started_at": iso(AHORA - 3600)},
        ultima_orden_ms=int((AHORA - 60) * 1000), ahora=AHORA, key_enmascarada="Xq7d…f2Ka")
    base.update(kw)
    return conexion.vista(**base)


def por_clave(v):
    return {c["clave"]: c for c in v["consumidores"]}


def test_cuatro_de_cuatro():
    v = vista()
    c = por_clave(v)
    assert [x["clave"] for x in v["consumidores"]] == ["passivbot", "panel", "scraper", "rescate"]
    assert {k: x["estado"] for k, x in c.items()} == dict.fromkeys(c, "ok")
    assert c["passivbot"]["texto"].startswith("orden nueva ")
    assert c["scraper"]["texto"] == "firma OK hace 36 s" and c["rescate"]["texto"] == "firma OK hace 12 s"
    assert (v["con_esta_key"], v["total"]) == (4, 4)


def test_passivbot_sin_orden_nueva_queda_sin_confirmar_en_ambar():
    v = vista(ultima_orden_ms=int((AHORA - 7200) * 1000))  # la última orden es de antes del arranque
    p = por_clave(v)["passivbot"]
    assert p["estado"] == "sin_confirmar" and v["con_esta_key"] == 3
    v = vista(ultima_orden_ms=None)
    assert por_clave(v)["passivbot"]["estado"] == "sin_confirmar"


def test_passivbot_arranco_antes_del_cambio_sigue_con_la_anterior_aunque_haya_ordenes():
    v = vista(estado={"key_id": "aaaaaaaaaaaa", "archivo_mtime": AHORA - 600, "usuario": "binance_01",
                      "testnet": True})
    p = por_clave(v)["passivbot"]
    assert p["estado"] == "atras" and "anterior" in p["texto"]


def test_passivbot_detenido_y_sin_docker():
    assert por_clave(vista(container={"status": "exited", "running": False}))["passivbot"]["estado"] == "detenido"
    assert por_clave(vista(container=None))["passivbot"]["estado"] == "sin_datos"


def test_passivbot_sin_secretos_no_se_puede_verificar():
    v = vista(modo_secretos=False)
    assert por_clave(v)["passivbot"]["estado"] == "sin_confirmar" and v["origen"] == "config"


def test_scraper_y_rescate_con_otra_key_sin_key_id_o_sin_latido():
    v = vista(scraper_latido={"key_id": "bbbbbbbbbbbb"}, rescate_servicio={})
    c = por_clave(v)
    assert c["scraper"]["estado"] == "atras" and c["scraper"]["texto"] == "con otra key"
    assert c["rescate"]["estado"] == "sin_datos"
    assert por_clave(vista(scraper_vivo=False))["scraper"]["estado"] == "sin_datos"
    assert v["con_esta_key"] == 2


def test_sin_firma_ok_posterior_al_archivo_queda_sin_confirmar():
    """El key_id del latido no prueba que haya firmado con esa key: hace falta una firma OK posterior al último cambio
    del archivo. Sin ella, ámbar, y no suma al "N de 4" (revisión, hallazgo 1)."""
    v = vista(rescate_servicio={"key_id": "aaaaaaaaaaaa", "key_ok_at": None,
                                "key_error": "No se pudo leer la cuenta (-2015)"})
    r = por_clave(v)["rescate"]
    assert r["estado"] == "sin_confirmar" and "sin firma OK" in r["texto"] and v["con_esta_key"] == 3
    v = vista(estado={"key_id": "aaaaaaaaaaaa", "archivo_mtime": AHORA - 600, "usuario": "binance_01",
                      "testnet": True},
              scraper_latido={"key_id": "aaaaaaaaaaaa", "key_ok_at": iso(AHORA - 900)})
    assert por_clave(v)["scraper"]["estado"] == "sin_confirmar"
    assert por_clave(v)["rescate"]["estado"] == "ok"  # firmó hace 12 s, después del cambio


def test_rescate_y_c3_en_off_no_firman():
    v = vista(rescate_servicio={"modo": "off", "key_id": "aaaaaaaaaaaa", "key_ok_at": None}, rescate_no_firma=True)
    r = por_clave(v)["rescate"]
    assert r["estado"] == "sin_confirmar" and "off" in r["detalle"] and v["con_esta_key"] == 3
    assert conexion.rescate_no_firma({"servicio": {"modo": "off"}, "c3": {"latido": {"c3_mode": "off"}}})
    assert conexion.rescate_no_firma({"servicio": {"modo": "off"}})
    assert not conexion.rescate_no_firma({"servicio": {"modo": "off"}, "c3": {"latido": {"c3_mode": "dry_run"}}})
    assert not conexion.rescate_no_firma({"servicio": {"modo": "dry_run"}})
    assert not conexion.rescate_no_firma({})


def test_permisos_en_demo_nunca_ok():
    v = vista(meta={"key_id": "aaaaaaaaaaaa", "permisos": {"problemas": []}})
    assert v["permisos"] == {"estado": "demo", "texto": "Demo no los informa"}
    real = vista(testnet=False, meta={"key_id": "aaaaaaaaaaaa", "permisos": {"problemas": ["ipRestrict debería ser true"]}})
    assert real["permisos"]["estado"] == "problemas" and "ipRestrict" in real["permisos"]["texto"]
    assert vista(testnet=False)["permisos"]["estado"] == "sin_verificar"


def test_cuenta_verificada_solo_con_la_huella_de_esta_key():
    meta = {"key_id": "aaaaaaaaaaaa", "huella_uid": "f" * 64, "hedge": True, "configurada_en": iso(AHORA - 100),
            "configurada_por": "cliente17", "validada_en": iso(AHORA - 100)}
    v = vista(meta=meta)
    assert v["cuenta"] == {"verificada": True, "texto": "UID verificado"} and v["hedge"] is True
    assert v["configurada"].endswith("· cliente17") and v["validada"]
    otra = vista(meta={**meta, "key_id": "cccccccccccc"})  # la meta es de otra key (cambio a mano)
    assert otra["cuenta"]["verificada"] is False and otra["hedge"] is None and not otra["validada"]
    assert otra["configurada"].endswith("sin registro de quién")


def test_avisos_archivo_invalido_y_testnet_distinto():
    v = vista(estado={"key_id": "aaaaaaaaaaaa", "key_error": "api-keys.json inválido: no es JSON válido; se sigue "
                      "con la última key válida (aaaaaaaaaaaa).", "testnet": False, "archivo_mtime": AHORA - 86400})
    assert any("inválido" in a for a in v["avisos"]) and any("testnet false" in a for a in v["avisos"])


def test_vista_nunca_lleva_uid_ni_huella():
    meta = {"key_id": "aaaaaaaaaaaa", "huella_uid": "huella-secreta-" + "f" * 50}
    texto = json.dumps(vista(meta=meta))
    assert "huella-secreta" not in texto and "uid" not in texto.replace("UID verificado", "").lower()


# ---------------------------------------------------------------- Bot renderiza la sección


def test_bot_muestra_la_conexion_sin_la_key(client, app, env, docker, tmp_path):  # noqa: F811
    d = tmp_path / "secretos"
    k, s = par()
    escribir(d, k, s)
    app.extensions[credenciales.EXTENSION] = credenciales.Proveedor(d, usuario=lambda: "binance_01")
    docker.started_at = iso(time.time() + 5)  # arrancó después del archivo
    with sqlite3.connect(str(app.config["DATABASE"])) as conn:
        conn.execute("INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) "
                     "VALUES (1, 1, 'BUY', 'LONG', 'NEW', 'BTCUSDT', ?, 'LIMIT')", (int((time.time() + 60) * 1000),))
    datos = pathlib.Path(app.config["DATABASE"]).parent
    (datos / scraper.LATIDO_FILE).write_text(json.dumps({"at": time.time(), "intervalo": 60,
                                                         "key_id": credenciales.key_id(k),
                                                         "key_ok_at": iso(time.time())}))
    estado = rescate.default_state()
    # Los dos firmaron OK después de escribir el archivo (sin eso quedan "sin confirmar").
    estado["servicio"] = {"modo": "dry_run", "heartbeat": dt.datetime.now(dt.timezone.utc).isoformat(),
                          "intervalo": 15, "key_id": credenciales.key_id(k), "key_ok_at": iso(time.time())}
    rescate.save_state(datos, estado)
    html = client.get("/bot").get_data(as_text=True)
    assert "Conexión con Binance" in html and 'id="bbConexionBinance"' in html and "hidden aria-hidden" not in html
    assert f"{k[:4]}<span aria-hidden=\"true\">…</span>" in html and k[-4:] in html
    assert k not in html and s not in html and k[4:-4] not in html
    assert "binance_01" in html and "UID sin verificar" in html and "Demo no los informa" not in html  # config: real
    assert "4 de 4 con esta key" in html
    assert 'data-consumidor="passivbot" data-estado="ok"' in html


def test_bot_sin_secretos_usa_config_y_lo_dice(client, app, env, docker):  # noqa: F811
    k, s = par()
    app.config["API_KEY"], app.config["API_SECRET"] = k, s
    html = client.get("/bot").get_data(as_text=True)
    assert "Por ahora la key sale de config.json" in html
    assert k not in html and s not in html and f"{k[:4]}" in html


def test_bot_con_archivo_roto_no_se_cae(client, app, env, docker, tmp_path):  # noqa: F811
    d = tmp_path / "secretos"
    d.mkdir()
    (d / credenciales.ARCHIVO).write_text("{roto")
    app.extensions[credenciales.EXTENSION] = credenciales.Proveedor(d, usuario=lambda: "binance_01")
    resp = client.get("/bot")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "sin API key" in html and "inválido" in html

