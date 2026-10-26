"""Versiones de la estrategia: listar, diferencias y Volver (spec bot-estrategia § 2.4 paso 6 y § 3.3).

Un backup ``new.json.bak-<sello>`` guarda la config de **antes** de la escritura de ese momento: la versión que creó esa
escritura es la del backup siguiente (o la vigente). Volver restaura solo TWE, pares a la vez y pares, con el mismo plan,
confirmación y regla de exposición que un cambio nuevo.
"""
from __future__ import annotations

import json
import pathlib

import hjson
import pytest

from futuresboard import bot_control
from futuresboard import estrategia

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


def _cfg(**cambios):
    return json.dumps({**FORAGER, **cambios}, indent=2)


@pytest.fixture
def historia(app, forager):  # noqa: F811
    """Cinco escrituras (la más vieja primero) y la config vigente:

    13:12 preset bajo     TWE 8 -> 4      (backup con TWE 8; en el log de auditoría)
    15:16 perfil          TWE 4 -> 3      (backup con TWE 4; en estrategia_versiones.jsonl)
    15:20 parada gradual  solo modos      (backup con TWE 3)
    15:29 estrategia      +SOL, 5 a la vez (backup con TWE 3 en graceful)
    vigente: TWE 3, 5 a la vez, con SOL
    """
    d = forager.parent
    (d / "new.json.bak-20261023-131235-134741").write_text(_cfg(twe_long=8, twe_short=3))
    (d / "new.json.bak-20261023-151612-188356").write_text(_cfg(twe_long=4))
    (d / "new.json.bak-20261023-152036-545483").write_text(_cfg(twe_long=3))
    (d / "new.json.bak-20261023-152942-076822").write_text(_cfg(twe_long=3, long_mode="graceful_stop",
                                                                short_mode="graceful_stop"))
    # Ruido que no es un backup con fecha: no aparece ni se puede usar.
    (d / "new.json.bak-mejoras-20261001").write_text(_cfg(twe_long=6))
    forager.write_text(_cfg(twe_long=3, n_longs=5, approved_symbols_long=CUATRO + ["SOLUSDT"],
                            approved_symbols_short=CUATRO + ["SOLUSDT"], long_mode="graceful_stop",
                            short_mode="graceful_stop"))
    data = pathlib.Path(app.config["DATABASE"]).parent
    with open(data / bot_control.AUDIT_LOG_NAME, "a") as fh:
        fh.write(json.dumps({"ts": "2026-10-23T13:14:06+00:00", "user": "cliente17", "action": "start",
                             "params": {"riesgo": "bajo"}, "result": "ok", "detail": "restart"}) + "\n")
    estrategia.registrar_version(data, {"ts": "2026-10-23T15:17:42+00:00", "usuario": "cliente17", "accion": "perfil",
                                        "perfil": "conservador", "backup": "new.json.bak-20261023-151612-188356"})
    estrategia.registrar_version(data, {"ts": "2026-10-23T15:30:00+00:00", "usuario": "otra", "accion": "estrategia",
                                        "backup": "new.json.bak-20261023-152942-076822"})
    return forager


def test_lista_agrupada_con_quien_y_que(client, historia, red, docker):  # noqa: F811
    resp = client.get("/api/bot/estrategia/versiones")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    vs = resp.get_json()["versiones"]
    # 4 versiones: la vigente, la de TWE 3 (con su parada gradual agrupada), la de TWE 4 y la más vieja (TWE 8).
    assert [v["resumen"]["twe_long"] for v in vs] == [3, 3, 4, 8]
    vig, tres, cuatro, ocho = vs
    assert vig["vigente"] and vig["backup"] is None and vig["titulo"] == "Estrategia" and vig["usuario"] == "otra"
    assert vig["desde"].startswith("2026-10-23T15:29:42") and "+SOL" in vig["cambios_texto"]
    assert "Pares a la vez 4 → 5" in vig["cambios_texto"]
    # TWE 3: la creó el perfil Conservador de las 15:16; su backup más nuevo (con la parada gradual) es el de las 15:29.
    assert tres["titulo"] == "Perfil Conservador" and tres["usuario"] == "cliente17"
    assert tres["backup"] == "new.json.bak-20261023-152942-076822" and "TWE long 4 → 3" in tres["cambios_texto"]
    # TWE 4: la escribió el preset bajo de las 13:12 (sacado del log de auditoría).
    assert cuatro["titulo"] == "Iniciar con riesgo bajo" and cuatro["fuente"] == "auditoria"
    assert cuatro["backup"] == "new.json.bak-20261023-151612-188356"
    assert ocho["titulo"] == "Versión más vieja guardada" and ocho["desde"] is None
    assert ocho["backup"] == "new.json.bak-20261023-131235-134741"
    # Diferencias con la vigente.
    claves = {c["clave"]: c for c in cuatro["diferencias"]}
    assert claves["twe_long"]["antes"] == 4 and claves["twe_long"]["ahora"] == 3
    assert claves["n_longs"]["antes"] == 4 and claves["n_longs"]["ahora"] == 5
    assert all("mejoras" not in (v["backup"] or "") for v in vs)


def test_volver_restaura_solo_la_estrategia(client, app, historia, red, docker):  # noqa: F811
    docker.running = True
    base = bot_control.estrategia_hash(hjson.loads(historia.read_text()))
    body = {"backup": "new.json.bak-20261023-151612-188356", "base_hash": base}  # TWE 4, los 4, 4 a la vez
    plan = post(client, "/api/bot/estrategia/volver", body)
    assert plan.status_code == 409 and plan.get_json()["requiere_confirmacion"], plan.get_data(as_text=True)
    p = plan.get_json()["plan"]
    assert p["titulo"] == "Volver a la versión del 23/10 15:16 UTC" and p["origen"] == "volver"
    assert p["updates"] == {"twe_long": 4, "twe_short": 1, "n_longs": 4, "approved_symbols_long": CUATRO,
                            "approved_symbols_short": CUATRO}
    res = post(client, "/api/bot/estrategia/volver", {**body, "confirmar": True, "plan_hash": plan.get_json()["plan_hash"]})
    assert res.status_code == 200, res.get_json()
    cfg = hjson.loads(historia.read_text())
    assert cfg["twe_long"] == 4 and cfg["n_longs"] == 4 and cfg["approved_symbols_long"] == CUATRO
    # Los modos y el resto quedan como estaban (Volver no cambia el estado de marcha).
    assert cfg["long_mode"] == "graceful_stop" and cfg["max_min_cost"] == 150
    assert docker.actions() == ["restart"]
    data = pathlib.Path(app.config["DATABASE"]).parent
    v = estrategia.leer_registro_versiones(data)[-1]
    assert v["accion"] == "volver" and v["volver_a"] == body["backup"] and v["usuario"] == "cliente17"
    linea = [x for x in audit_lines(app) if x["action"] == "estrategia"][-1]
    assert linea["params"]["volver"] == body["backup"] and linea["result"] == "ok"
    hist = client.get("/api/bot/acciones").get_json()["acciones"]
    assert hist[0]["titulo"].startswith("Volver a una versión: TWE 4, 4 a la vez")


@pytest.mark.parametrize("nombre", ["../new.json", "new.json", "new.json.bak-mejoras-20261001", "/etc/passwd",
                                    "new.json.bak-20261023-151612-188356/..", 3, None])
def test_volver_con_un_nombre_invalido_es_400(client, historia, red, docker, nombre):  # noqa: F811
    resp = post(client, "/api/bot/estrategia/volver", {"backup": nombre, "base_hash": "0" * 64})
    assert resp.status_code == 400


def test_volver_a_un_backup_que_ya_no_esta(client, historia, red, docker):  # noqa: F811
    resp = post(client, "/api/bot/estrategia/volver", {"backup": "new.json.bak-20200101-000000-000000",
                                                       "base_hash": "0" * 64})
    assert resp.status_code == 409 and "No está el backup" in resp.get_json()["error"]


def test_volver_bloquea_si_un_par_ya_no_pasa_los_chequeos(client, historia, red, docker):  # noqa: F811
    red.demo.set_status("XRPUSDT", "SETTLING")
    base = bot_control.estrategia_hash(hjson.loads(historia.read_text()))
    resp = post(client, "/api/bot/estrategia/volver", {"backup": "new.json.bak-20261023-151612-188356", "base_hash": base})
    assert resp.status_code == 409 and "requiere_confirmacion" not in resp.get_json()
    assert any(b.startswith("XRPUSDT ya no pasa los chequeos") for b in resp.get_json()["bloqueos"])


def test_volver_respeta_la_regla_de_exposicion(client, historia, red, docker):  # noqa: F811
    # Una versión con 3 a la vez cuando hoy hay 4 lugares ocupados (BNB por su orden y BTC, ETH, XRP).
    (historia.parent / "new.json.bak-20261023-100000-000000").write_text(_cfg(n_longs=3))
    base = bot_control.estrategia_hash(hjson.loads(historia.read_text()))
    resp = post(client, "/api/bot/estrategia/volver", {"backup": "new.json.bak-20261023-100000-000000",
                                                       "base_hash": base})
    assert resp.status_code == 409 and any("Mínimo: 4" in b for b in resp.get_json()["bloqueos"])


def test_volver_requiere_csrf(client, historia, red, docker):  # noqa: F811
    resp = post(client, "/api/bot/estrategia/volver", {"backup": "new.json.bak-20261023-151612-188356"}, token="x")
    assert resp.status_code == 403
