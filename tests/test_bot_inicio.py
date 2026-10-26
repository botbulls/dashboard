"""Inicio: control rápido con los 4 estados, Iniciar / Reanudar / Reiniciar con la estrategia guardada
(``POST /api/bot/start {"reanudar": true}``, camino ``mantener_twe``) y el registro de cada arranque.

diseno/bot-estrategia/spec.md § 2.2, § 3.3 (registro) y § 4.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from unittest import mock

import hjson
import pytest
import requests

from futuresboard import bot_control
from futuresboard import c3
from futuresboard import rescate
from futuresboard import telegram_notify

from test_bot_control import app  # noqa: F401
from test_bot_control import audit_lines
from test_bot_control import binance  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401

VIGENTE = {
    "user": "binance_01", "twe_long": 4, "twe_short": 1, "n_longs": 4, "n_shorts": 0, "leverage": 10,
    "long_mode": "graceful_stop", "short_mode": "graceful_stop",
    "approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
    "approved_symbols_short": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
}


def escribir(path, **cambios):
    path.write_text(json.dumps({**VIGENTE, **cambios}, indent=2))


def leer(path):
    return hjson.loads(path.read_text())


def data_dir(app):  # noqa: F811
    return pathlib.Path(app.config["DATABASE"]).parent


def arranques(app):  # noqa: F811
    p = data_dir(app) / bot_control.ARRANQUES_FILE_NAME
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def plan(client, **extra):  # noqa: F811
    return post(client, "/api/bot/start", {"reanudar": True, **extra})


def iniciar(client):  # noqa: F811
    """Pide el plan y lo confirma con su plan_hash (lo que hace el modal de Iniciar)."""
    resp = plan(client)
    assert resp.status_code == 409, resp.get_data(as_text=True)
    return post(client, "/api/bot/start", {"reanudar": True, "confirmar": True,
                                           "plan_hash": resp.get_json()["plan_hash"]})


# ---------------------------------------------------------------- hash de la estrategia


def test_hash_de_estrategia_ignora_modos_orden_y_formato():
    base = bot_control.estrategia_hash(VIGENTE)
    assert bot_control.estrategia_hash({**VIGENTE, "long_mode": "normal", "short_mode": "normal"}) == base
    assert bot_control.estrategia_hash({**VIGENTE, "twe_long": 4.0, "twe_short": 1.0}) == base
    assert bot_control.estrategia_hash(
        {**VIGENTE, "approved_symbols_long": list(reversed(VIGENTE["approved_symbols_long"]))}) == base
    # Lo que cambia la estrategia sí cambia el hash.
    for cambio in ({"twe_long": 4.25}, {"twe_short": 2}, {"n_longs": 5},
                   {"approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]},
                   {"approved_symbols_short": []}):
        assert bot_control.estrategia_hash({**VIGENTE, **cambio}) != base, cambio


# ---------------------------------------------------------------- Iniciar (detenido): plan y confirmación


def test_iniciar_detenido_pide_plan_sin_tocar_nada(client, app, env, docker):  # noqa: F811
    escribir(env)
    antes = env.read_text()
    docker.running = False
    resp = plan(client)
    assert resp.status_code == 409
    d = resp.get_json()
    assert d["requiere_confirmacion"] is True and d["plan_hash"] == d["plan"]["plan_hash"]
    p = d["plan"]
    assert p["como"] == "iniciar" and p["vigente"]["pares_long"] == VIGENTE["approved_symbols_long"]
    assert p["vigente"]["twe_long"] == 4 and p["vigente"]["n_longs"] == 4
    # Sin registro del último arranque no se puede mostrar qué cambió: pide la casilla.
    assert p["ultimo"] is None and p["requiere_casilla"] is True
    assert any("último arranque" in a["texto"] for a in p["avisos"])
    assert docker.actions() == [] and env.read_text() == antes
    assert audit_lines(app)[-1]["result"] == "requiere_confirmacion"
    assert audit_lines(app)[-1]["params"] == {"reanudar": True, "como": "iniciar"}


def test_iniciar_confirmado_usa_mantener_twe_y_registra_el_arranque(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = False
    resp = iniciar(client)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.accepted.status_code == 202 and resp.job["accion"] == "start"
    assert docker.actions() == ["start"]
    cfg = leer(env)
    # Camino mantener_twe: modos a normal, TWE y pares tal cual.
    assert (cfg["long_mode"], cfg["short_mode"]) == ("normal", "normal")
    assert (cfg["twe_long"], cfg["twe_short"], cfg["n_longs"]) == (4, 1, 4)
    assert cfg["approved_symbols_long"] == VIGENTE["approved_symbols_long"]
    (reg,) = arranques(app)
    assert reg["usuario"] == "cliente17" and reg["origen"] == "iniciar" and reg["docker_action"] == "start"
    assert reg["hash"] == bot_control.estrategia_hash(cfg)
    assert reg["resumen"]["pares_long"] == VIGENTE["approved_symbols_long"] and reg["resumen"]["twe_long"] == 4
    assert audit_lines(app)[-1]["result"] == "ok"


def test_sin_cambios_desde_el_ultimo_arranque_no_pide_casilla(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = False
    iniciar(client)
    docker.running = False  # se detuvo (Apagar, docker stop)
    escribir(env)  # Apagar deja los modos en parada gradual: no es un cambio de estrategia
    p = plan(client).get_json()["plan"]
    assert p["ultimo"]["origen"] == "iniciar" and p["cambios"] == []
    assert p["requiere_casilla"] is False


def test_iniciar_con_cambios_sin_arrancar_pide_casilla(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = False
    iniciar(client)
    docker.running = False
    # Estrategia guardada con el bot detenido: + SOL y 5 a la vez.
    escribir(env, n_longs=5, approved_symbols_long=VIGENTE["approved_symbols_long"] + ["SOLUSDT"],
             approved_symbols_short=VIGENTE["approved_symbols_long"] + ["SOLUSDT"])
    p = plan(client).get_json()["plan"]
    assert p["requiere_casilla"] is True
    claves = {c["clave"]: c for c in p["cambios"]}
    assert set(claves) >= {"pares_long", "n_longs"}
    assert claves["n_longs"]["antes"] == 4 and claves["n_longs"]["ahora"] == 5
    textos = " ".join(a["texto"] for a in p["avisos"])
    assert "cambió desde el último arranque" in textos and "SOLUSDT" in textos and "backtest" in textos


def test_iniciar_con_twe_alta_pide_casilla(client, app, env, docker):  # noqa: F811
    escribir(env, twe_long=6)
    docker.running = False
    iniciar(client)
    docker.running = False
    p = plan(client).get_json()["plan"]
    assert p["cambios"] == [] and p["requiere_casilla"] is True
    assert any("TWE 6" in a["texto"] for a in p["avisos"])


def test_plan_hash_viejo_no_arranca(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = False
    h = plan(client).get_json()["plan_hash"]
    escribir(env, twe_long=3)  # alguien cambió la estrategia después de ver el resumen
    resp = post(client, "/api/bot/start", {"reanudar": True, "confirmar": True, "plan_hash": h})
    assert resp.status_code == 409 and resp.get_json()["requiere_confirmacion"] is True
    assert resp.get_json()["plan_hash"] != h
    assert docker.actions() == [] and leer(env)["long_mode"] == "graceful_stop"
    # Sin plan_hash tampoco arranca.
    assert post(client, "/api/bot/start", {"reanudar": True, "confirmar": True}).status_code == 409
    assert docker.actions() == []


def test_el_job_vuelve_a_mirar_el_plan_con_el_lock(client, app, env, docker, monkeypatch):  # noqa: F811
    escribir(env)
    docker.running = False
    h = plan(client).get_json()["plan_hash"]
    original = bot_control.plan_reanudar
    llamadas = []

    def plan_que_cambia(settings, store, docker=None):
        llamadas.append(1)
        if len(llamadas) == 2:  # dentro del job: la estrategia cambió entre el POST y el lock
            escribir(env, twe_long=3)
        return original(settings, store, docker=docker)

    monkeypatch.setattr(bot_control, "plan_reanudar", plan_que_cambia)
    resp = post(client, "/api/bot/start", {"reanudar": True, "confirmar": True, "plan_hash": h})
    assert resp.status_code == 409 and "cambió" in resp.get_json()["error"]
    assert docker.actions() == [] and arranques(app) == []


# ---------------------------------------------------------------- Reanudar / Reiniciar (de un toque)


def test_reanudar_en_parada_gradual_es_de_un_toque(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = True
    resp = plan(client)
    assert resp.status_code == 200 and resp.accepted.status_code == 202
    assert resp.job["params"] == {"reanudar": True, "como": "reanudar"}
    assert docker.actions() == ["restart"]
    cfg = leer(env)
    assert (cfg["long_mode"], cfg["short_mode"], cfg["twe_long"]) == ("normal", "normal", 4)
    assert arranques(app)[-1]["origen"] == "reanudar"


def test_reiniciar_operando(client, app, env, docker):  # noqa: F811
    escribir(env, long_mode="normal", short_mode="normal")
    docker.running = True
    resp = plan(client)
    assert resp.status_code == 200 and resp.job["params"]["como"] == "reiniciar"
    assert docker.actions() == ["restart"]


def test_corriendo_con_config_pendiente_pide_plan(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = True
    docker.started_at = "2020-01-01T00:00:00Z"  # la config cambió después del arranque
    resp = plan(client)
    assert resp.status_code == 409 and resp.get_json()["plan"]["como"] == "reanudar"
    assert docker.actions() == []


@pytest.mark.parametrize("body", [{"reanudar": True, "riesgo": "bajo"}, {"reanudar": True, "twe_long": 4},
                                  {"reanudar": False}, {"reanudar": "si"},
                                  {"reanudar": True, "confirmar": "si"}, {"reanudar": True, "plan_hash": 3}])
def test_reanudar_valida_el_body(client, env, docker, body):  # noqa: F811
    resp = post(client, "/api/bot/start", body)
    assert resp.status_code == 400
    assert docker.actions() == []


def test_reanudar_respeta_el_tope_del_rescate(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = False
    st = rescate.default_state()
    st["tope"] = {"id": "abc", "disparado_en": "2026-10-22T10:00:00+00:00", "modo": "real"}
    rescate.save_state(data_dir(app), st)
    resp = plan(client)
    assert resp.status_code == 409 and "START bloqueado" in resp.get_json()["error"]
    assert docker.actions() == []


def test_telegram_nombra_iniciar_y_reanudar():
    txt = telegram_notify.format_bot_action("start", {"reanudar": True, "como": "iniciar"}, "ok",
                                            {"docker_action": "start"}, "u")
    assert "INICIAR" in txt and "estrategia guardada" in txt and "None" not in txt
    txt = telegram_notify.format_bot_action("start", {"reanudar": True, "como": "reanudar"}, "ok", {}, "u")
    assert "REANUDAR" in txt


# ---------------------------------------------------------------- registro de arranques: todos los caminos


def test_start_con_preset_y_twe_manual_tambien_registran(client, app, env, docker):  # noqa: F811
    escribir(env)
    post(client, "/api/bot/start", {"riesgo": "bajo"})
    post(client, "/api/bot/start", {"twe_long": 3})
    regs = arranques(app)
    assert [r["origen"] for r in regs] == ["start", "start"]
    assert regs[0]["resumen"]["twe_long"] == 4 and regs[1]["resumen"]["twe_long"] == 3


def test_reinicio_automatico_de_c3_registra_su_arranque(tmp_path, env, docker):  # noqa: F811
    escribir(env, long_mode="normal", short_mode="normal")
    docker.running = False
    start = c3.make_start(tmp_path, bot_control.Settings(), docker=bot_control.DockerClient("http://docker-proxy:2375"))
    assert start({"riesgo": None, "mantener_twe": True})[0] == "ok"
    (reg,) = [json.loads(x) for x in (tmp_path / bot_control.ARRANQUES_FILE_NAME).read_text().splitlines()]
    assert reg["usuario"] == c3.USUARIO_START and reg["origen"] == "c3_reinicio"
    assert reg["hash"] == bot_control.estrategia_hash(leer(env))
    # El Iniciar siguiente compara contra ese arranque.
    store = bot_control.Store(tmp_path)
    assert bot_control.ultimo_arranque(store)["id"] == reg["id"]


def test_registro_que_falla_no_frena_el_arranque(client, app, env, docker, monkeypatch):  # noqa: F811
    escribir(env)
    docker.running = False

    def roto(*a, **kw):
        raise OSError("disco lleno")

    monkeypatch.setattr(bot_control, "_append_arranque", roto)
    resp = iniciar(client)
    assert resp.status_code == 200 and docker.actions() == ["start"]


def test_registro_ilegible_se_ignora(tmp_path):
    p = tmp_path / bot_control.ARRANQUES_FILE_NAME
    p.write_text('{"id": "a1", "hash": "x", "resumen": {}}\nesto no es json\n')
    assert bot_control.ultimo_arranque(bot_control.Store(tmp_path))["id"] == "a1"


# ---------------------------------------------------------------- Cerrar todo con passivbot detenido


def test_cerrar_todo_con_passivbot_detenido(client, app, env, docker, binance):  # noqa: F811
    docker.running = False
    binance.orders = [{"symbol": "ETHUSDT"}]
    binance.add_position("BTCUSDT", "0.5")
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["resumen"]["completo"] is True
    assert binance.positions == {} and binance.orders == []
    assert docker.actions() == ["stop"]  # el stop sobre uno detenido responde 304 y sigue


# ---------------------------------------------------------------- Inicio: control rápido


def _con_posiciones(app):  # noqa: F811
    with sqlite3.connect(str(app.config["DATABASE"])) as con:
        con.execute("UPDATE account SET totalWalletBalance = 5417.55 WHERE AID = 1")
        con.executemany(
            "INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, positionAmt) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [("BTCUSDT", -3.12, 10, 112000.0, "LONG", 0.0043), ("XRPUSDT", -0.44, 10, 2.62, "LONG", 144.8)])
        con.execute("INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) "
                    "VALUES (0.07, 741.54, 'BUY', 'LONG', 'NEW', 'BNBUSDT', 1, 'LIMIT')")


def _inicio(client):  # noqa: F811
    with mock.patch.object(requests, "get", side_effect=Exception("offline")):
        resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    return html.split('data-vivo-bloque="control_bot"', 1)[1].split("</section>", 1)[0], html


def _acciones(control):
    import re

    return re.findall(r'data-bot-accion="([a-z_]+)"', control)


def test_control_operando(client, app, env, docker):  # noqa: F811
    escribir(env, long_mode="normal", short_mode="normal")
    docker.running = True
    control, html = _inicio(client)
    assert _acciones(control) == ["graceful", "apagar"]
    assert "Personalizado" in control or "Perfil" in control
    assert "TWE 4" in control and "4 pares a la vez" in control and 'href="/estrategia"' in control
    assert "BNB, BTC, ETH y XRP" in control
    # La franja sigue arriba; el estado del bot es un link a Bot.
    assert '<a id="botStatus" class="badge bot-status-badge' in html and 'href="/bot"' in html


def test_control_parada_gradual(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = True
    control, _ = _inicio(client)
    assert _acciones(control) == ["reanudar", "apagar"]
    assert "bb-control__botones--separados" in control


def _latido_scraper(app, edad_s=0.0):  # noqa: F811
    import time

    from futuresboard import scraper

    scraper.latido_path(app.config["DATABASE"]).write_text(json.dumps({"at": time.time() - edad_s, "intervalo": 60}))


def test_control_detenido_limpio(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = False
    _latido_scraper(app)  # "sin posiciones ni órdenes" exige una base del scraper al día
    control, _ = _inicio(client)
    assert _acciones(control) == ["iniciar"]
    assert "Sin posiciones ni órdenes abiertas" in control


@pytest.mark.parametrize("como", ["base_vieja", "sin_registro", "base_ilegible"])
def test_control_detenido_sin_poder_verificar_la_exposicion(client, app, env, docker, monkeypatch, como):  # noqa: F811
    """Con la base del scraper vieja, sin registro de una vuelta OK o ilegible, "Sin posiciones ni órdenes abiertas"
    no tiene evidencia y solo ofrecía Iniciar…: puede haber posiciones sin bot. Ahora es "exposición sin verificar"
    (ámbar) con Iniciar… y Cerrar todo… (revisión, hallazgo 18)."""
    from futuresboard import blueprint

    escribir(env)
    docker.running = False
    if como == "base_vieja":
        _latido_scraper(app, edad_s=2 * 3600)
    elif como == "base_ilegible":
        _latido_scraper(app)
        original = blueprint.db.query

        def query(sql, *a, **kw):
            if "FROM positions" in sql or "FROM orders" in sql:
                raise RuntimeError("database is locked")
            return original(sql, *a, **kw)

        monkeypatch.setattr(blueprint.db, "query", query)
    st = client.get("/api/bot/status").get_json()
    assert st["estado_bot"] == "sin_verificar"
    if como == "base_ilegible":
        return  # Inicio entero lee la base: la página es de los otros dos casos
    control, html = _inicio(client)
    assert "Sin posiciones ni órdenes abiertas" not in control
    assert "No se pudo verificar si hay posiciones u órdenes" in control
    assert _acciones(control) == ["iniciar", "cerrar_todo"]
    if como == "base_vieja":
        assert "datos del scraper de hace 2 h" in control
    franja = html.split('id="botStatus"', 1)[1].split("</a>", 1)[0]
    assert "Detenido · exposición sin verificar" in franja and "bg-warning" in franja
    pildora = client.get("/bot").get_data(as_text=True).split("data-bot-pildora", 1)[1].split("</a>", 1)[0]
    assert 'data-bot-estado="sin_verificar"' in pildora and "sin verificar" in pildora
    # /api/vivo/inicio rearma el mismo bloque.
    assert "No se pudo verificar si hay posiciones u órdenes" in json.dumps(client.get("/api/vivo/inicio").get_json(),
                                                                          ensure_ascii=False)
    # Bot ofrece Cerrar todo con passivbot detenido.
    assert 'data-bot-accion="cerrar_todo"' in client.get("/bot").get_data(as_text=True)


def test_control_detenido_con_exposicion(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = False
    _con_posiciones(app)
    control, html = _inicio(client)
    assert "bb-control--expuesto" in control
    assert _acciones(control) == ["iniciar", "cerrar_todo"]
    assert "2 posiciones y 1 orden sin bot" in control
    assert "bb-control__botones--separados" in control
    assert "Detenido · 2 posiciones sin bot" in html.split('id="botStatus"', 1)[1].split("</a>", 1)[0]


def test_control_sin_datos_de_docker(client, app, env, docker):  # noqa: F811
    from test_bot_control import FakeResponse

    docker.request = lambda method, url, **kw: FakeResponse(500)
    control, _ = _inicio(client)
    assert _acciones(control) == []
    assert "No se pudo leer el estado del bot" in control


def test_control_sin_panel(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv(bot_control.ENV_DOCKER_URL, raising=False)
    control, _ = _inicio(client)
    assert _acciones(control) == [] and "Panel del bot no configurado" in control


def test_vivo_inicio_trae_el_control(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.running = False
    _con_posiciones(app)
    with mock.patch.object(requests, "get", side_effect=Exception("offline")):
        d = client.get("/api/vivo/inicio").get_json()
    bloque = d["bloques"]["control_bot"]
    assert bloque.startswith('<section data-vivo-bloque="control_bot"') and "cerrar_todo" in bloque
