"""Página Bot (diseno/bot-estrategia/spec.md § 2.3): estado ampliado, pares ahora, latidos, acciones, última acción
con sus pasos e historial de acciones paginado (``GET /api/bot/acciones``), que nunca devuelve la IP del log."""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3

import pytest

from futuresboard import acciones
from futuresboard import bot_control

from test_bot_control import app  # noqa: F401
from test_bot_control import binance  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401
from test_bot_control import wait_job

VIGENTE = {
    "user": "binance_01", "twe_long": 4, "twe_short": 1, "n_longs": 4, "n_shorts": 0,
    "long_mode": "normal", "short_mode": "normal",
    "approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
    "approved_symbols_short": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
}
IP = "203.0.113.77"


def escribir(path, **cambios):
    path.write_text(json.dumps({**VIGENTE, **cambios}))


def data_dir(app):  # noqa: F811
    return pathlib.Path(app.config["DATABASE"]).parent


def _db(app, posiciones=(), ordenes=(), balance=5000.0):  # noqa: F811
    with sqlite3.connect(str(app.config["DATABASE"])) as con:
        con.execute("UPDATE account SET totalWalletBalance = ? WHERE AID = 1", (balance,))
        con.executemany(
            "INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, positionAmt) "
            "VALUES (?, ?, ?, ?, ?, ?)", posiciones)
        con.executemany(
            "INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) VALUES (?,?,?,?,?,?,?,?)",
            ordenes)


def _log(app, filas):  # noqa: F811
    path = data_dir(app) / bot_control.AUDIT_LOG_NAME
    with open(path, "a", encoding="utf-8") as fh:
        for f in filas:
            fh.write((f if isinstance(f, str) else json.dumps({"remote_addr": IP, **f})) + "\n")
    return path


def _html(client, url="/bot"):  # noqa: F811
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


# ---------------------------------------------------------------- /api/bot/status ampliado


def test_status_ampliado(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.started_at = "2026-10-23T15:47:48.123456789Z"
    docker.restart_count = 2
    docker.image = "botbulls/passivbot:c3-92dba71"
    _db(app, [("BTCUSDT", -2.0, 10, 100000.0, "LONG", 0.005), ("ETHUSDT", 1.0, 10, 4000.0, "LONG", 0.25),
              ("SOLUSDT", 0.5, 10, 200.0, "LONG", 1.0)],
        [(0.07, 741.54, "BUY", "LONG", "NEW", "BNBUSDT", 1, "LIMIT"),
         (0.25, 4100.0, "SELL", "LONG", "NEW", "ETHUSDT", 2, "LIMIT")])
    st = client.get("/api/bot/status").get_json()
    assert st["started_at"] == "2026-10-23T15:47:48.123456789Z"
    assert st["restart_count"] == 2 and st["container"]["image"] == "botbulls/passivbot:c3-92dba71"
    pa = st["pares_ahora"]
    # Una orden abierta ocupa lugar igual que una posición (forager.py:130): BNB, BTC, ETH y SOL.
    assert (pa["lugares_ocupados"], pa["lugares_total"]) == (4, 4)
    por = {p["symbol"]: p for p in pa["pares"]}
    assert set(por) == {"BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"}
    assert por["BNBUSDT"]["estado"] == "orden" and por["BNBUSDT"]["compras"] == 1
    assert por["BTCUSDT"]["estado"] == "posicion" and por["BTCUSDT"]["pct_balance"] == pytest.approx(10.0)
    assert por["ETHUSDT"]["ventas"] == 1 and por["XRPUSDT"]["estado"] == "libre"
    # Con posición y fuera de approved_symbols_long: en parada gradual hasta cerrar.
    assert por["SOLUSDT"]["fuera_de_lista"] is True and por["BTCUSDT"]["fuera_de_lista"] is False


def test_status_sin_inspect_no_inventa_datos(client, env, docker):  # noqa: F811
    escribir(env)
    st = client.get("/api/bot/status").get_json()
    assert st["restart_count"] is None and st["started_at"] is None


# ---------------------------------------------------------------- historial de acciones


def test_acciones_nunca_devuelve_la_ip(client, app):  # noqa: F811
    _log(app, [
        {"ts": "2026-10-23T15:19:47+00:00", "user": "cliente17", "action": "start", "params": {"twe_long": True},
         "result": "rechazado", "detail": "twe_long tiene que ser un número."},
        "esto no es json",
        '["tampoco", "un objeto"]',
        {"ts": "2026-10-23T15:49:18+00:00", "user": "cliente17", "action": "perfil",
         "params": {"perfil": "con_rescate"}, "result": "ok", "detail": "restart", "remote_addr": IP},
    ])
    resp = client.get("/api/bot/acciones")
    assert resp.status_code == 200
    texto = resp.get_data(as_text=True)
    assert IP not in texto and "remote_addr" not in texto
    d = resp.get_json()
    assert d["total"] == 2 and [a["ts"] for a in d["acciones"]] == ["2026-10-23T15:49:18+00:00",
                                                                     "2026-10-23T15:19:47+00:00"]
    assert set(d["acciones"][0]) == {"ts", "usuario", "accion", "titulo", "detalle", "resultado", "estado",
                                     "estado_texto"}


def test_acciones_marca_los_planes_confirmados(client, app):  # noqa: F811
    base = {"user": "cliente17", "action": "perfil", "params": {"perfil": "con_rescate"}, "detail": "plan"}
    _log(app, [
        {**base, "ts": "2026-10-23T15:29:18+00:00", "result": "requiere_confirmacion"},
        {**base, "ts": "2026-10-23T15:29:35+00:00", "result": "requiere_confirmacion"},
        {**base, "ts": "2026-10-23T15:29:56+00:00", "result": "ok", "detail": "restart"},
        {"ts": "2026-10-23T15:30:00+00:00", "user": "cliente17", "action": "start", "params": {"twe_long": 5},
         "result": "requiere_confirmacion", "detail": "plan"},
        {"ts": "2026-10-23T15:31:00+00:00", "user": "cliente17", "action": "start", "params": {"twe_long": 3},
         "result": "ok", "detail": "restart"},
    ])
    d = client.get("/api/bot/acciones").get_json()
    estados = [(a["ts"][11:19], a["estado"]) for a in d["acciones"]]
    assert estados == [("15:31:00", "aplicado"), ("15:30:00", "sin_confirmar"), ("15:29:56", "aplicado"),
                       ("15:29:35", "plan_mostrado"), ("15:29:18", "sin_confirmar")]
    assert d["acciones"][3]["detalle"] == "Pidió confirmación; confirmado a las 15:29:56."
    assert d["acciones"][1]["titulo"] == "TWE manual 5"
    assert d["acciones"][2]["titulo"] == "Perfil Con rescate"


def test_acciones_paginado_y_filtros(client, app):  # noqa: F811
    filas = []
    for i in range(30):
        filas.append({"ts": f"2026-10-24T10:{i:02d}:00+00:00", "user": "cliente17", "action": "stop",
                      "params": {"modo": "graceful"}, "result": "ok" if i % 3 else "rechazado", "detail": "x"})
    _log(app, filas)
    d = client.get("/api/bot/acciones?desde=0&limite=12").get_json()
    assert len(d["acciones"]) == 12 and d["total"] == 30 and d["siguiente"] == 12
    assert d["acciones"][0]["ts"].startswith("2026-10-24T10:29")
    d2 = client.get("/api/bot/acciones?desde=24&limite=12").get_json()
    assert len(d2["acciones"]) == 6 and d2["siguiente"] is None
    ap = client.get("/api/bot/acciones?filtro=aplicadas&limite=100").get_json()
    re_ = client.get("/api/bot/acciones?filtro=rechazadas&limite=100").get_json()
    assert ap["total"] == 20 and re_["total"] == 10
    assert {a["estado"] for a in ap["acciones"]} == {"aplicado"}
    assert all(a["titulo"] == "Parada gradual" for a in ap["acciones"])


@pytest.mark.parametrize("qs", ["desde=-1", "desde=x", "limite=0", "limite=101", "filtro=otra"])
def test_acciones_valida_la_consulta(client, qs):  # noqa: F811
    assert client.get(f"/api/bot/acciones?{qs}").status_code == 400


def test_acciones_sin_log(client):  # noqa: F811
    d = client.get("/api/bot/acciones").get_json()
    assert d == {"acciones": [], "total": 0, "desde": 0, "limite": acciones.LIMITE_DEFECTO, "siguiente": None,
                 "filtro": "todas"}


def test_titulos_de_acciones_del_sistema():
    assert acciones.titulo("stop", {"modo": "apagar", "origen": "c3_corte"}) == "Apagar por C3"
    assert acciones.titulo("stop", {"modo": "apagar", "origen": "rescate_tope"}) == "Apagar por el tope del rescate"
    assert acciones.titulo("start", {"riesgo": None, "mantener_twe": True, "origen": "c3_reinicio"}) == \
        "Reinicio automático de C3"
    assert acciones.titulo("start", {"reanudar": True, "como": "iniciar"}) == "Iniciar"
    assert acciones.titulo("rescate_config", {"x": 1}) == "Rescate: configuración"


# ---------------------------------------------------------------- página


def test_bot_pagina_completa(client, app, env, docker):  # noqa: F811
    escribir(env)
    docker.started_at = "2026-10-23T15:47:48Z"
    docker.restart_count = 0
    docker.image = "botbulls/passivbot:c3-92dba71"
    _db(app, [("BTCUSDT", -2.0, 10, 100000.0, "LONG", 0.005)])
    _log(app, [{"ts": "2026-10-23T15:49:18+00:00", "user": "cliente17", "action": "perfil",
                "params": {"perfil": "con_rescate"}, "result": "ok", "detail": "restart"}])
    html = _html(client)
    assert IP not in html
    for latido in ("scraper", "rescate", "c3", "acciones"):
        assert f'data-latido="{latido}"' in html, latido
    assert "23/10 15:47 UTC" in html and "botbulls/passivbot:c3-92dba71" in html and "0 reinicios" in html
    assert "1 de 4 lugares ocupados" in html
    # Acciones: operando -> Reiniciar, Parada gradual y Apagar (con qué hace cada una).
    for a in ("reiniciar", "graceful", "apagar"):
        assert f'data-bot-accion="{a}"' in html
    assert 'data-bot-accion="iniciar"' not in html
    # Historial (primera página en el server) y el lugar de "Conexión con Binance" (§ 10, otro commit).
    assert "Perfil Con rescate" in html and 'id="bbHistorial"' in html
    assert "data-bot-conexion-binance" in html
    assert re.search(r'<script src="/static/js/bot_pagina\.js\?v=[0-9a-f]{10}"', html)


@pytest.mark.parametrize("running,modo,esperada,no", [
    (False, "graceful_stop", "iniciar", "reiniciar"),
    (True, "graceful_stop", "reanudar", "graceful"),
])
def test_bot_acciones_segun_estado(client, env, docker, running, modo, esperada, no):  # noqa: F811
    escribir(env, long_mode=modo, short_mode=modo)
    docker.running = running
    html = _html(client)
    assert f'data-bot-accion="{esperada}"' in html
    assert f'data-bot-accion="{no}"' not in html


def test_bot_ultima_accion_con_sus_pasos(client, app, env, docker):  # noqa: F811
    escribir(env)
    resp = post(client, "/api/bot/stop", {"modo": "graceful"})
    assert resp.status_code == 200
    html = _html(client)
    ultima = html.split('id="bbUltimaAccion"', 1)[1].split("</section>", 1)[0]
    assert "Parada gradual" in ultima and "cliente17" in ultima
    for _, titulo in bot_control.GRACEFUL_STEPS:
        assert titulo in ultima
    assert "new.json.bak-" in ultima  # el detalle del backup


def test_bot_latido_de_acciones_sin_tomar_el_lock(client, app, env, docker, monkeypatch):  # noqa: F811
    from test_bot_jobs import Gate

    escribir(env)
    gate = Gate(docker, "restart")
    job_id = post(client, "/api/bot/stop", {"modo": "graceful"}, wait=False).get_json()["job_id"]
    assert gate.entered.wait(5)
    tomas = []
    original = bot_control.Store.try_lock
    monkeypatch.setattr(bot_control.Store, "try_lock", lambda self: tomas.append(1) or original(self))
    try:
        html = _html(client)
    finally:
        monkeypatch.setattr(bot_control.Store, "try_lock", original)
        gate.release.set()
    assert tomas == []  # mirar el estado no compite con un Apagar por el lock
    latido = html.split('data-latido="acciones"', 1)[1].split("</div></div>", 1)[0]
    assert "En curso" in latido
    wait_job(client, job_id)
    latido = _html(client).split('data-latido="acciones"', 1)[1].split("</div></div>", 1)[0]
    assert "Libre" in latido
