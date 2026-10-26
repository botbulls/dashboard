"""POST /api/bot/estrategia: plan, confirmación y job (spec bot-estrategia § 3.2 y § 3.3).

La cuenta de prueba es la de dev al 25/10: BNB con una orden de compra, BTC/ETH/XRP con posición. Regla de exposición:
``n_longs ≥ |P ∪ O|`` (P: pares con posición, en la lista o no; O: pares de la lista nueva con órdenes).
"""
from __future__ import annotations

import json
import pathlib

import hjson
import pytest

from futuresboard import bot_control
from futuresboard import estrategia
from futuresboard import manifiesto

from test_bot_control import audit_lines
from test_bot_control import docker  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401
from test_estrategia_api import FORAGER
from test_estrategia_api import app  # noqa: F401
from test_estrategia_api import client  # noqa: F401
from test_estrategia_api import forager  # noqa: F401
from test_estrategia_api import red  # noqa: F401

CUATRO = ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]


def base_hash():
    return bot_control.estrategia_hash(FORAGER)


def pedir(client, pares, n_longs, twe=4, **extra):  # noqa: F811
    body = {"twe_long": twe, "n_longs": n_longs, "pares": pares, "base_hash": base_hash(), **extra}
    return post(client, "/api/bot/estrategia", body)


def confirmar(client, pares, n_longs, twe=4):  # noqa: F811
    plan = pedir(client, pares, n_longs, twe)
    assert plan.status_code == 409 and plan.get_json()["requiere_confirmacion"], plan.get_data(as_text=True)
    return post(client, "/api/bot/estrategia", {"twe_long": twe, "n_longs": n_longs, "pares": pares,
                                                "base_hash": base_hash(), "confirmar": True,
                                                "plan_hash": plan.get_json()["plan_hash"]}), plan.get_json()["plan"]


def leer(forager):  # noqa: F811
    return hjson.loads(pathlib.Path(forager).read_text())


def aviso(plan, clave):
    return next((a for a in plan["avisos"] if a["clave"] == clave), None)


def fila(plan, clave):
    return next(f for f in plan["diff"] if f["clave"] == clave)


# ---------------------------------------------------------------- regla de exposición (spec § 3.2)


def test_quitar_xrp_con_posicion_deja_4_lugares_y_lw_1(client, forager, red, docker):  # noqa: F811
    docker.running = True
    resp = pedir(client, ["BNBUSDT", "BTCUSDT", "ETHUSDT"], 4)
    assert resp.status_code == 409, resp.get_data(as_text=True)
    plan = resp.get_json()["plan"]
    assert resp.get_json()["requiere_confirmacion"]
    assert plan["updates"]["n_longs"] == 4
    assert fila(plan, "lw")["texto_despues"] == "1,00" and not fila(plan, "lw")["cambia"]
    assert "3,00 cuando cierre" in fila(plan, "twe_efectiva")["texto_despues"]
    sale = aviso(plan, "sale:XRPUSDT")
    assert sale["casilla"] and "parada gradual" in sale["texto"] and "144,8 XRP" in sale["titulo"]
    assert aviso(plan, "por_que_no_baja") is not None
    assert plan["requiere_casilla"] and "XRPUSDT sigue operando en parada gradual" in plan["casilla_texto"]
    # Y con 3 a la vez no se puede: la lw subiría a 1,33 también para XRP.
    resp = pedir(client, ["BNBUSDT", "BTCUSDT", "ETHUSDT"], 3)
    assert resp.status_code == 409 and not resp.get_json().get("requiere_confirmacion")
    assert any("Mínimo: 4" in b and "1,33" in b for b in resp.get_json()["bloqueos"])


def test_n_longs_3_con_4_ocupados_es_409(client, forager, red, docker):  # noqa: F811
    resp = pedir(client, CUATRO, 3)
    assert resp.status_code == 409
    d = resp.get_json()
    assert "requiere_confirmacion" not in d and d["bloqueos"]
    assert any("4 lugares ocupados" in b for b in d["bloqueos"])


def test_quitar_bnb_con_solo_una_orden_deja_bajar(client, forager, red, docker):  # noqa: F811
    resp = pedir(client, ["BTCUSDT", "ETHUSDT", "XRPUSDT"], 3)
    assert resp.status_code == 409 and resp.get_json()["requiere_confirmacion"], resp.get_data(as_text=True)
    plan = resp.get_json()["plan"]
    assert plan["lugares"]["minimo"] == 3 and plan["updates"]["n_longs"] == 3
    assert aviso(plan, "sale_orden:BNBUSDT") is not None and aviso(plan, "sale:BNBUSDT") is None


def test_cinco_pares_y_cuatro_lugares_rota(client, forager, red, docker):  # noqa: F811
    resp = pedir(client, CUATRO + ["SOLUSDT"], 4)
    assert resp.status_code == 409 and resp.get_json()["requiere_confirmacion"], resp.get_data(as_text=True)
    plan = resp.get_json()["plan"]
    assert aviso(plan, "rotacion") is not None
    assert aviso(plan, "sin_backtest")["casilla"] and "SOLUSDT" in aviso(plan, "sin_backtest")["titulo"]


def test_mas_lugares_que_pares_es_409(client, forager, red, docker):  # noqa: F811
    resp = pedir(client, CUATRO, 5)
    assert resp.status_code == 409 and any("Máximo: 4" in b for b in resp.get_json()["bloqueos"])


def test_base_hash_viejo_es_409(client, forager, red, docker):  # noqa: F811
    resp = pedir(client, CUATRO + ["SOLUSDT"], 5, base_hash="0" * 64)
    assert resp.status_code == 409
    assert estrategia.BASE_VIEJA in resp.get_json()["bloqueos"]


def test_par_que_entra_y_no_pasa_los_chequeos_bloquea(client, forager, red, docker):  # noqa: F811
    resp = pedir(client, CUATRO + ["SUIUSDT"], 5)
    assert resp.status_code == 409
    assert any(b.startswith("SUIUSDT:") and "06/2023" in b for b in resp.get_json()["bloqueos"])


def test_sin_manifiesto_no_agrega_pero_quita_y_cambia_twe(client, forager, red, docker):  # noqa: F811
    (forager.parent / manifiesto.NOMBRE).unlink()
    resp = pedir(client, CUATRO + ["SOLUSDT"], 5)
    assert resp.status_code == 409 and any("passivbot todavía no publicó" in b for b in resp.get_json()["bloqueos"])
    resp = pedir(client, ["BTCUSDT", "ETHUSDT", "XRPUSDT"], 3, twe=3)
    assert resp.status_code == 409 and resp.get_json()["requiere_confirmacion"], resp.get_data(as_text=True)
    assert aviso(resp.get_json()["plan"], "manifiesto") is not None


def test_sin_cambios_es_409(client, forager, red, docker):  # noqa: F811
    resp = pedir(client, CUATRO, 4)
    assert resp.status_code == 409 and any("No hay cambios" in b for b in resp.get_json()["bloqueos"])


def test_n_shorts_distinto_de_0_bloquea(client, forager, red, docker):  # noqa: F811
    cfg = dict(FORAGER, n_shorts=2)
    forager.write_text(json.dumps(cfg))
    body = {"twe_long": 4, "n_longs": 4, "pares": CUATRO + ["SOLUSDT"], "base_hash": bot_control.estrategia_hash(cfg)}
    resp = post(client, "/api/bot/estrategia", body)
    assert resp.status_code == 409 and any("n_shorts" in b for b in resp.get_json()["bloqueos"])


@pytest.mark.parametrize("body,msg", [
    ({"twe_long": 9, "n_longs": 4, "pares": CUATRO}, "twe_long"),
    ({"twe_long": 4, "n_longs": True, "pares": CUATRO}, "n_longs"),
    ({"twe_long": 4, "n_longs": 4, "pares": "BTCUSDT"}, "pares"),
    ({"twe_long": 4, "n_longs": 4, "pares": CUATRO, "riesgo": "alto"}, "desconocidos"),
])
def test_body_invalido_es_400(client, forager, red, docker, body, msg):  # noqa: F811
    resp = post(client, "/api/bot/estrategia", {"base_hash": base_hash(), **body})
    assert resp.status_code == 400 and msg in resp.get_json()["error"]


def test_requiere_csrf(client, forager, red, docker):  # noqa: F811
    resp = post(client, "/api/bot/estrategia", {"twe_long": 4, "n_longs": 4, "pares": CUATRO}, token="otro")
    assert resp.status_code == 403


# ---------------------------------------------------------------- avisos con casilla


def test_twe_alta_y_personalizado_con_rescate_piden_casilla(client, app, forager, red, docker):  # noqa: F811
    from futuresboard import rescate

    data_dir = pathlib.Path(app.config["DATABASE"]).parent
    cfg = rescate.validate_config_body({"habilitado": True, "reserva_max_usdt": 10000, "paso_usdt": 1000,
                                        "umbral_ratio": 8, "umbral_devolucion_ratio": 32, "tope_perdida_pct": 60,
                                        "upnl_habilitado": True, "upnl_x_pct": 70, "upnl_paso_usdt": 1000,
                                        "retiro_frecuencia": rescate.FRECUENCIA_RECOMENDADA, "retiro_base_usdt": 10000})
    rescate.save_config(data_dir, cfg, "cliente17")
    plan = pedir(client, CUATRO, 4, twe=5).get_json()["plan"]
    assert aviso(plan, "twe_alta")["casilla"]
    assert aviso(plan, "rescate_personalizado")["casilla"] and "70 %" in aviso(plan, "rescate_personalizado")["texto"]
    assert "TWE 5 está por encima" in plan["casilla_texto"]
    # Con rescate (TWE 4, los 4 pares, configs medidas) → Personalizado.
    assert fila(plan, "perfil")["texto_antes"] == "Con rescate" and fila(plan, "perfil")["texto_despues"] == "Personalizado"
    assert aviso(plan, "personalizado")["casilla"]


# ---------------------------------------------------------------- job: escritura, reinicio y vuelta atrás


def test_aplica_operando_reinicia_y_sincroniza_short(client, app, forager, red, docker):  # noqa: F811
    docker.running = True
    res, plan = confirmar(client, CUATRO + ["SOLUSDT"], 5)
    assert res.accepted.status_code == 202
    assert res.status_code == 200, res.get_json()
    assert res.job["accion"] == "estrategia"
    cfg = leer(forager)
    assert cfg["approved_symbols_long"] == CUATRO + ["SOLUSDT"]
    assert cfg["approved_symbols_short"] == CUATRO + ["SOLUSDT"]
    assert cfg["n_longs"] == 5 and cfg["twe_long"] == 4 and cfg["twe_short"] == 1
    # No toca los modos ni el resto.
    assert cfg["long_mode"] == "normal" and cfg["max_min_cost"] == 150 and cfg["symbols_to_ignore"] == ["BAKEUSDT", "INJUSDT"]
    assert docker.actions() == ["restart"]
    assert [p["clave"] for p in res.job["pasos"]] == [c for c, _ in estrategia.ESTRATEGIA_STEPS]
    assert all(p["estado"] == "ok" for p in res.job["pasos"])
    # Arranque registrado con la estrategia nueva (lo usa Iniciar y C3).
    ultimo = bot_control.ultimo_arranque(bot_control.Store(pathlib.Path(app.config["DATABASE"]).parent))
    assert ultimo["origen"] == "estrategia" and ultimo["hash"] == bot_control.estrategia_hash(cfg)
    # Auditoría con el diff resumido y versión con quién y qué.
    lineas = [x for x in audit_lines(app) if x["action"] == "estrategia"]
    assert [x["result"] for x in lineas] == ["requiere_confirmacion", "ok"]
    assert lineas[0]["params"] == lineas[1]["params"] == {"twe_long": 4, "n_longs": 5, "pares": CUATRO + ["SOLUSDT"]}
    assert "+SOLUSDT" in lineas[1]["detail"] and "pares a la vez 4 → 5" in lineas[1]["detail"]
    versiones = [json.loads(x) for x in (pathlib.Path(app.config["DATABASE"]).parent / estrategia.VERSIONES_FILE)
                 .read_text().splitlines()]
    assert versiones[-1]["usuario"] == "cliente17" and versiones[-1]["accion"] == "estrategia"
    assert versiones[-1]["backup"] == res.get_json()["backup"] and versiones[-1]["resultado"] == "ok"
    assert versiones[-1]["hash_antes"] == base_hash() and versiones[-1]["hash_despues"] == bot_control.estrategia_hash(cfg)
    # El historial de Bot lo muestra en palabras.
    hist = client.get("/api/bot/acciones").get_json()["acciones"]
    assert hist[0]["titulo"].startswith("Estrategia: TWE 4, 5 a la vez") and hist[0]["estado"] == "aplicado"
    assert hist[1]["estado"] == "plan_mostrado"


def test_en_parada_gradual_reinicia_y_queda_en_parada_gradual(client, forager, red, docker):  # noqa: F811
    forager.write_text(json.dumps(dict(FORAGER, long_mode="graceful_stop", short_mode="graceful_stop")))
    docker.running = True
    res, _plan = confirmar(client, CUATRO + ["SOLUSDT"], 5)
    assert res.status_code == 200
    cfg = leer(forager)
    assert cfg["long_mode"] == "graceful_stop" and cfg["short_mode"] == "graceful_stop"
    assert docker.actions() == ["restart"]


def test_detenido_escribe_y_no_arranca(client, app, forager, red, docker):  # noqa: F811
    docker.running = False
    res, plan = confirmar(client, CUATRO + ["SOLUSDT"], 5)
    assert plan["como"] == "escribe"
    assert res.status_code == 200, res.get_json()
    assert docker.actions() == [] and docker.running is False
    pasos = {p["clave"]: p for p in res.job["pasos"]}
    assert pasos["docker"]["estado"] == "omitido" and "se aplica al iniciar" in pasos["docker"]["detalle"].lower()
    assert pasos["verificar"]["estado"] == "omitido" and pasos["forager"]["estado"] == "omitido"
    assert leer(forager)["n_longs"] == 5
    # Sin arranque: Iniciar va a mostrar este cambio.
    assert bot_control.ultimo_arranque(bot_control.Store(pathlib.Path(app.config["DATABASE"]).parent)) is None


@pytest.mark.parametrize("codigo", [409, 500])
def test_docker_rechaza_el_reinicio_y_se_restaura(client, forager, red, docker, codigo):  # noqa: F811
    docker.running = True
    antes = forager.read_text()
    docker.fail_action = codigo
    res, _plan = confirmar(client, CUATRO + ["SOLUSDT"], 5)
    assert res.status_code == 502
    assert "Se restauró la config de forager" in res.get_json()["error"]
    assert forager.read_text() == antes


def test_docker_timeout_deja_la_estrategia_escrita(client, forager, red, docker):  # noqa: F811
    docker.running = True
    docker.timeout_action = True
    res, _plan = confirmar(client, CUATRO + ["SOLUSDT"], 5)
    assert res.status_code == 502 and "Resultado incierto" in res.get_json()["error"]
    assert leer(forager)["n_longs"] == 5


def test_plan_hash_viejo_no_escribe(client, forager, red, docker):  # noqa: F811
    antes = forager.read_text()
    resp = post(client, "/api/bot/estrategia", {"twe_long": 4, "n_longs": 5, "pares": CUATRO + ["SOLUSDT"],
                                                "base_hash": base_hash(), "confirmar": True, "plan_hash": "a" * 64})
    assert resp.status_code == 409 and resp.get_json()["requiere_confirmacion"]
    assert "ya no corresponde" in resp.get_json()["error"]
    assert forager.read_text() == antes and docker.actions() == []


def test_si_cambia_la_estrategia_entre_el_plan_y_el_job_no_escribe(client, forager, red, docker, monkeypatch):  # noqa: F811
    docker.running = True
    plan = pedir(client, CUATRO + ["SOLUSDT"], 5).get_json()
    original = estrategia.aplicar

    def cambia_antes(settings, store, armar, plan_hash, usuario, **kw):
        forager.write_text(json.dumps(dict(FORAGER, twe_long=3)))  # alguien aplicó otra cosa en el medio
        return original(settings, store, armar, plan_hash, usuario, **kw)

    monkeypatch.setattr(estrategia, "aplicar", cambia_antes)
    res = post(client, "/api/bot/estrategia", {"twe_long": 4, "n_longs": 5, "pares": CUATRO + ["SOLUSDT"],
                                               "base_hash": base_hash(), "confirmar": True,
                                               "plan_hash": plan["plan_hash"]})
    assert res.status_code == 409
    assert leer(forager)["twe_long"] == 3 and leer(forager)["n_longs"] == 4
    assert docker.actions() == []


def test_otra_accion_en_curso_es_409(client, app, forager, red, docker):  # noqa: F811
    from futuresboard import bot_control as bc

    plan = pedir(client, CUATRO + ["SOLUSDT"], 5).get_json()
    handle = bc.Store(pathlib.Path(app.config["DATABASE"]).parent).try_lock()
    try:
        resp = post(client, "/api/bot/estrategia", {"twe_long": 4, "n_longs": 5, "pares": CUATRO + ["SOLUSDT"],
                                                    "base_hash": base_hash(), "confirmar": True,
                                                    "plan_hash": plan["plan_hash"]}, wait=False)
    finally:
        handle.release()
    assert resp.status_code == 409 and "en curso" in resp.get_json()["error"]


def test_telegram_de_la_estrategia(monkeypatch):
    from futuresboard import telegram_notify

    texto = telegram_notify.format_bot_action("estrategia", {"twe_long": 4, "n_longs": 5, "pares": CUATRO}, "ok",
                                              {"docker_action": "restart", "cambios_texto": "+SOLUSDT · pares a la vez 4 → 5"},
                                              "cliente17")
    assert "ESTRATEGIA" in texto and "+SOLUSDT" in texto
