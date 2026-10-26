"""Perfiles de riesgo (Conservador / Con rescate) y TWE manual del panel.

Un perfil escribe la TWE de forager con la rutina de START y deja el rescate, el disparo por uPnL y el retiro en el
estado del perfil, en una sola acción (job) con el lock del panel, confirmación previa y auditoría.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import hjson
import pytest

from futuresboard import bot_control
from futuresboard import jobs
from futuresboard import perfiles
from futuresboard import rescate
from futuresboard import telegram_notify

from test_bot_control import FORAGER_HJSON
from test_bot_control import FakeDocker
from test_bot_control import DOCKER_BASE
from test_bot_control import app  # noqa: F401
from test_bot_control import audit_lines
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401
from test_bot_control import wait_job
from test_bot_jobs import Gate
from test_bot_jobs import _app_on
from test_bot_jobs import estados
from test_bot_jobs import registry

SOLO_LONG = FORAGER_HJSON.replace("n_shorts: 2", "n_shorts: 0")


def _data_dir(app):  # noqa: F811
    return pathlib.Path(app.config["DATABASE"]).parent


def _latido_scraper(app, edad_s=0.0):  # noqa: F811
    import time

    from futuresboard import scraper

    scraper.latido_path(app.config["DATABASE"]).write_text(json.dumps({"at": time.time() - edad_s, "intervalo": 60}))


@pytest.fixture(autouse=True)
def _scraper_al_dia(request):
    """Aplicar un perfil corre la regla de exposición, que solo confía en una base del scraper fresca."""
    if "app" in request.fixturenames:
        _latido_scraper(request.getfixturevalue("app"))


@pytest.fixture
def solo_long(env):  # noqa: F811
    env.write_text(SOLO_LONG)
    return env


@pytest.fixture
def manifiesto_medido(env):  # noqa: F811
    """El manifiesto de configs de passivbot (generado con su código): las 4 configs con el md5 medido."""
    from estrategia_fakes import manifiesto_fixture
    from futuresboard import manifiesto

    (env.parent / manifiesto.NOMBRE).write_text(json.dumps(manifiesto_fixture()))
    return env


MEDIDA = {"n_shorts": 0, "n_longs": 4, "approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]}


def _manif():
    from estrategia_fakes import manifiesto_fixture

    return {"usable": True, "datos": manifiesto_fixture()}


def _servicio_vivo(app, modo="real", upnl="real", retiro="real"):  # noqa: F811
    state, _ = rescate.load_state(_data_dir(app))
    state["servicio"] = {"modo": modo, "heartbeat": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                         "intervalo": 15, "testnet": False, "upnl_modo": upnl, "retiro_modo": retiro}
    rescate.save_state(_data_dir(app), state)


def _rescate_cfg(app):  # noqa: F811
    path = _data_dir(app) / rescate.CONFIG_FILE_NAME
    return json.loads(path.read_text()) if path.exists() else None


def _plan(client, perfil):  # noqa: F811
    return post(client, "/api/bot/perfil", {"perfil": perfil})


def _aplicar(client, perfil):  # noqa: F811
    return post(client, "/api/bot/perfil", {"perfil": perfil, "confirmar": True})


# ---------------------------------------------------------------- validación y detección


@pytest.mark.parametrize("body", [None, [], "con_rescate", {}, {"perfil": "agresivo"}, {"perfil": None},
                                  {"perfil": "Con rescate"}, {"perfil": "conservador", "confirmar": "si"},
                                  {"perfil": "conservador", "twe_long": 3}])
def test_body_estricto(body):
    with pytest.raises(ValueError):
        perfiles.validate_body(body)


def test_body_ok():
    assert perfiles.validate_body({"perfil": "con_rescate"}) == {"perfil": "con_rescate", "confirmar": False}
    assert perfiles.validate_body({"perfil": "conservador", "confirmar": True})["confirmar"] is True


def test_definiciones_del_pedido():
    c = perfiles.PERFILES["conservador"]
    assert c["twe_long"] == 3 and c["rescate"] == {"habilitado": False, "upnl_habilitado": False,
                                                   "retiro_frecuencia": "off"}
    r = perfiles.PERFILES["con_rescate"]
    assert r["twe_long"] == 4
    assert r["rescate"] == {"habilitado": True, "reserva_max_usdt": 10000, "paso_usdt": 1000, "umbral_ratio": 8,
                            "umbral_devolucion_ratio": 32, "tope_perdida_pct": 60, "upnl_habilitado": True,
                            "upnl_x_pct": 70, "upnl_paso_usdt": 1000,
                            "retiro_frecuencia": rescate.FRECUENCIA_RECOMENDADA, "retiro_base_usdt": 10000}
    cat = perfiles.catalogo()
    assert set(cat["perfiles"]) == {"conservador", "con_rescate"} and "no una promesa" in cat["fuente"]
    assert cat["perfiles"]["conservador"]["numeros"]["mediana_pct"] == "59,5"
    assert cat["perfiles"]["con_rescate"]["numeros"]["peor_pct"] == "-12"


def test_detectar():
    base = rescate.default_config()
    m = _manif()
    assert perfiles.detectar({**MEDIDA, "twe_long": 3}, base, m) == "conservador"
    assert perfiles.detectar({**MEDIDA, "twe_long": 3.0}, base, m) == "conservador"
    assert perfiles.detectar({**MEDIDA, "twe_long": 3, "n_shorts": 2}, base, m) is None
    assert perfiles.detectar({**MEDIDA, "twe_long": 3.25}, base, m) is None
    assert perfiles.detectar(None, base, m) is None
    cfg = dict(base)
    for k, v in perfiles.PERFILES["con_rescate"]["rescate"].items():
        cfg[k] = v if isinstance(v, (bool, str)) else str(v)
    assert perfiles.detectar({**MEDIDA, "twe_long": 4}, cfg, m) == "con_rescate"
    assert perfiles.detectar({**MEDIDA, "twe_long": 4}, {**cfg, "retiro_frecuencia": "mensual"}, m) == "con_rescate"
    assert perfiles.detectar({**MEDIDA, "twe_long": 4}, {**cfg, "retiro_frecuencia": "off"}, m) is None
    assert perfiles.detectar({**MEDIDA, "twe_long": 4}, {**cfg, "tope_perdida_pct": "50"}, m) is None
    assert perfiles.detectar({**MEDIDA, "twe_long": 3}, cfg, m) is None


def test_detectar_exige_los_4_pares_n_longs_4_y_los_md5_medidos():
    base = rescate.default_config()
    m = _manif()
    con_sol = {**MEDIDA, "twe_long": 3, "approved_symbols_long": MEDIDA["approved_symbols_long"] + ["SOLUSDT"]}
    d = perfiles.detectar_detalle(con_sol, base, m)
    assert d["perfil"] is None and "4 medidos" in d["motivo"] and "Conservador" in d["motivo"]
    d = perfiles.detectar_detalle({**MEDIDA, "twe_long": 3, "n_longs": 5}, base, m)
    assert d["perfil"] is None and "4 a la vez" in d["motivo"]
    otra = _manif()
    otra["datos"]["symbols"]["BTCUSDT"]["md5"] = "0" * 32
    d = perfiles.detectar_detalle({**MEDIDA, "twe_long": 3}, base, otra)
    assert d["perfil"] is None and "BTCUSDT no es la medida" in d["motivo"]
    # Sin manifiesto no se marca como medido, y lo dice.
    for sin in (None, {"usable": False, "motivo": "passivbot todavía no publicó el manifiesto de configs."}):
        d = perfiles.detectar_detalle({**MEDIDA, "twe_long": 3}, base, sin)
        assert d["perfil"] is None and "configs sin verificar" in d["motivo"]
    # Lo que no coincide por TWE o rescate no lleva motivo: es Personalizado sin más.
    assert perfiles.detectar_detalle({**MEDIDA, "twe_long": 3.5}, base, m) == {"perfil": None, "motivo": None}


# ---------------------------------------------------------------- confirmación previa (sin escribir nada)


def test_sin_confirmar_devuelve_el_plan_y_no_escribe(client, app, solo_long, docker, monkeypatch):  # noqa: F811
    monkeypatch.setenv(bot_control.ENV_FORAGER_WARMUP, "90")
    _servicio_vivo(app)
    resp = _plan(client, "con_rescate")
    assert resp.status_code == 409
    d = resp.get_json()
    assert d["requiere_confirmacion"] is True and d["plan"]["corriendo"] is True
    texto = d["error"]
    assert "passivbot está OPERANDO: se reinicia (docker restart) y forager tarda ~90 s" in texto
    assert "twe_long 6 -> 4 (lw 1 con 4 pares)" in texto
    assert "tope de pérdida total 60 %" in texto and "X 70 %, paso 1.000" in texto
    assert f"Retiro periódico: {rescate.FRECUENCIA_RECOMENDADA}" in texto
    assert "mediana 86,3 %" in texto and "no es una promesa" in texto and "Herramienta, no asesor" in texto
    assert "NO PROTEGE" not in texto
    assert docker.actions() == []
    assert solo_long.read_text() == SOLO_LONG
    assert _rescate_cfg(app) is None
    entry = audit_lines(app)[-1]
    assert entry["action"] == "perfil" and entry["result"] == "requiere_confirmacion"
    assert entry["params"] == {"perfil": "con_rescate"}


def test_plan_con_passivbot_detenido(client, app, solo_long, docker):  # noqa: F811
    docker.running = False
    texto = _plan(client, "conservador").get_json()["error"]
    assert "passivbot está DETENIDO (exited): se inicia (docker start)" in texto
    assert "Rescate: se DESHABILITA" in texto and "mediana 59,5 %" in texto


def test_plan_avisa_si_el_servicio_no_protege(client, app, solo_long, docker):  # noqa: F811
    texto = _plan(client, "con_rescate").get_json()["error"]
    assert "NO PROTEGE NADA" in texto  # sin latido del servicio
    _servicio_vivo(app, modo="off")
    assert "FUTURESBOARD_RESCATE_MODE=off" in _plan(client, "con_rescate").get_json()["error"]
    _servicio_vivo(app, modo="real", upnl="off", retiro="off")
    texto = _plan(client, "con_rescate").get_json()["error"]
    assert "FUTURESBOARD_RESCATE_UPNL_MODE=off" in texto and "FUTURESBOARD_RESCATE_RETIRO_MODE=off" in texto
    _servicio_vivo(app, modo="dry_run")
    assert "SIMULADOS" in _plan(client, "con_rescate").get_json()["error"]


def test_plan_vuelve_a_los_4_pares_medidos(client, app, env, docker):  # noqa: F811
    # S2: aplicar un perfil restablece su paquete medido: los 4 pares y 4 a la vez.
    env.write_text(SOLO_LONG.replace("n_longs: 4", "n_longs: 5"))
    d = _plan(client, "conservador").get_json()
    texto = d["error"]
    assert "lw 0,75 con 4 pares" in texto
    assert "Pares: BNBUSDT, BTCUSDT, ETHUSDT, XRPUSDT, 4 a la vez (antes: BTCUSDT, 5 a la vez)" in texto
    assert d["plan"]["n_longs"] == 4


def _con_posiciones(app, filas, ordenes=()):  # noqa: F811
    import sqlite3

    with sqlite3.connect(str(app.config["DATABASE"])) as con:
        con.executemany("INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, "
                        "positionAmt) VALUES (?, ?, ?, ?, ?, ?)", filas)
        con.executemany("INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) "
                        "VALUES (?,?,?,?,?,?,?,?)", ordenes)


def test_perfil_con_un_quinto_par_con_posicion_queda_en_5_lugares(client, app, solo_long, docker):  # noqa: F811
    # Regla de exposición (spec § 3.2): SOL con posición sale de la lista; n_longs no baja de |P ∪ O| = 5.
    _con_posiciones(app, [("SOLUSDT", 0, 10, 100.0, "LONG", 2.0), ("BTCUSDT", 0, 10, 80000.0, "LONG", 0.001)],
                    [(0.07, 741.54, "BUY", "LONG", "NEW", "BNBUSDT", 1, "LIMIT"),
                     (1, 1.3, "BUY", "LONG", "NEW", "XRPUSDT", 1, "LIMIT"),
                     (1, 2400, "BUY", "LONG", "NEW", "ETHUSDT", 1, "LIMIT")])
    d = _plan(client, "con_rescate").get_json()
    assert d["plan"]["n_longs"] == 5
    assert "SOLUSDT sale de la lista con posición" in d["error"] and "queda en 5 a la vez" in d["error"]
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 200, resp.get_json()
    cfg = hjson.loads(solo_long.read_text())
    assert cfg["n_longs"] == 5 and cfg["approved_symbols_long"] == perfiles.PARES == cfg["approved_symbols_short"]


def test_perfil_con_la_base_del_scraper_vieja_no_se_aplica(client, app, solo_long, docker):  # noqa: F811
    _latido_scraper(app, edad_s=7200)
    d = _plan(client, "con_rescate").get_json()
    assert "Los datos del scraper tienen 2 h" in d["error"] and "El perfil no se aplica" in d["error"]
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code != 200 and "Los datos del scraper tienen" in json.dumps(resp.get_json())


def test_plan_avisa_prestamo_pendiente_al_pasar_a_conservador(client, app, solo_long, docker):  # noqa: F811
    _servicio_vivo(app)
    state, _ = rescate.load_state(_data_dir(app))
    state["prestamo"]["real"] = "1000.00"
    rescate.save_state(_data_dir(app), state)
    texto = _plan(client, "conservador").get_json()["error"]
    assert "préstamo pendiente de 1000.00 USDT" in texto


# ---------------------------------------------------------------- aplicar de punta a punta


def test_con_rescate_de_punta_a_punta(client, app, solo_long, manifiesto_medido, docker):  # noqa: F811
    _servicio_vivo(app)
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 200, resp.get_json()
    job = resp.job
    assert [p["clave"] for p in job["pasos"]] == ["validar", "rescate", "backup", "escribir", "docker", "verificar",
                                                  "forager"]
    assert set(estados(job).values()) == {"ok"}
    cfg = hjson.loads(solo_long.read_text())
    assert (cfg["twe_long"], cfg["twe_short"]) == (4, 1)
    assert cfg["long_mode"] == "normal" and cfg["n_longs"] == 4 and cfg["n_shorts"] == 0
    # S2: el perfil escribe también los 4 pares medidos (y la lista short igual).
    assert cfg["approved_symbols_long"] == cfg["approved_symbols_short"] == ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]
    assert docker.actions() == ["restart"]
    versiones = (_data_dir(app) / "estrategia_versiones.jsonl").read_text().splitlines()
    v = json.loads(versiones[-1])
    assert v["accion"] == "perfil" and v["perfil"] == "con_rescate" and v["usuario"] == "cliente17" and v["backup"]
    r = _rescate_cfg(app)
    assert r["habilitado"] is True and r["generacion"] == 1
    assert (r["reserva_max_usdt"], r["paso_usdt"], r["umbral_ratio"], r["umbral_devolucion_ratio"],
            r["tope_perdida_pct"]) == ("10000", "1000", "8", "32", "60")
    assert (r["upnl_habilitado"], r["upnl_x_pct"], r["upnl_paso_usdt"]) == (True, "70", "1000")
    assert r["retiro_frecuencia"] == rescate.FRECUENCIA_RECOMENDADA and r["retiro_base_usdt"] == "10000.00"
    assert r["retiro_desde"] and r["perfil"]["nombre"] == "con_rescate" and r["perfil"]["aplicado_por"] == "cliente17"
    data = resp.get_json()
    assert data["perfil"] == "con_rescate" and data["riesgo"] == "bajo" and data["rescate"]["habilitado"] is True
    entry = audit_lines(app)[-1]
    assert (entry["action"], entry["params"], entry["result"]) == ("perfil", {"perfil": "con_rescate"}, "ok")
    status = client.get("/api/bot/status").get_json()
    assert status["perfil_vigente"] == "con_rescate" and status["perfil_ultimo"]["nombre"] == "con_rescate"
    # Aplicarlo otra vez no abre una generación nueva (no cambia la base del tope) ni re-ancla el retiro.
    assert _aplicar(client, "con_rescate").status_code == 200
    r2 = _rescate_cfg(app)
    assert r2["generacion"] == 1 and r2["retiro_desde"] == r["retiro_desde"]


def test_conservador_deshabilita_y_conserva_los_valores(client, app, solo_long, manifiesto_medido, docker):  # noqa: F811
    body = {"habilitado": True, "reserva_max_usdt": 5000, "paso_usdt": 500, "umbral_ratio": 6,
            "umbral_devolucion_ratio": 24, "tope_perdida_pct": 40, "upnl_habilitado": True, "upnl_x_pct": 50,
            "upnl_paso_usdt": 500, "retiro_frecuencia": "mensual", "retiro_base_usdt": 8000}
    rescate.save_config(_data_dir(app), rescate.validate_config_body(body), "cliente17")
    resp = _aplicar(client, "conservador")
    assert resp.status_code == 200, resp.get_json()
    cfg = hjson.loads(solo_long.read_text())
    assert (cfg["twe_long"], cfg["twe_short"]) == (3, 1)
    r = _rescate_cfg(app)
    assert r["habilitado"] is False and r["upnl_habilitado"] is False and r["retiro_frecuencia"] == "off"
    assert (r["reserva_max_usdt"], r["paso_usdt"], r["umbral_ratio"], r["tope_perdida_pct"]) == ("5000", "500", "6", "40")
    assert (r["upnl_x_pct"], r["retiro_base_usdt"]) == ("50", "8000.00")
    status = client.get("/api/bot/status").get_json()
    assert status["perfil_vigente"] == "conservador" and status["riesgo"] == "personalizado"
    # Un guardado del panel borra la marca del último perfil aplicado.
    rescate.save_config(_data_dir(app), rescate.validate_config_body({**body, "habilitado": False}), "cliente17")
    assert _rescate_cfg(app)["perfil"] is None


def test_con_shorts_no_se_aplica(client, app, env, docker):  # noqa: F811
    resp = _plan(client, "con_rescate")
    assert resp.status_code == 409 and "n_shorts" in resp.get_json()["error"]
    assert "requiere_confirmacion" not in resp.get_json()
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 409 and "n_shorts" in resp.get_json()["error"]
    assert estados(resp.job)["validar"] == "error" and estados(resp.job)["rescate"] == "omitido"
    assert env.read_text() == FORAGER_HJSON and _rescate_cfg(app) is None
    assert docker.actions() == []


def test_tope_sin_reconocer_bloquea(client, app, solo_long, docker):  # noqa: F811
    state, _ = rescate.load_state(_data_dir(app))
    state["tope"] = {"id": "t1", "disparado_en": "2026-10-23T10:00:00+00:00", "modo": "real"}
    rescate.save_state(_data_dir(app), state)
    for resp in (_plan(client, "con_rescate"), _aplicar(client, "conservador")):
        assert resp.status_code == 409 and "START bloqueado" in resp.get_json()["error"]
    assert docker.actions() == [] and _rescate_cfg(app) is None and solo_long.read_text() == SOLO_LONG


def test_short_mode_invalido_no_escribe_nada(client, app, env, docker):  # noqa: F811
    env.write_text(SOLO_LONG.replace("n_longs: 4", "n_longs: 4\n  short_mode: cualquiera"))
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 409 and _rescate_cfg(app) is None
    assert docker.actions() == []


def test_docker_rechaza_restaura_forager_y_rescate(client, app, solo_long, docker):  # noqa: F811
    docker.fail_action = 500
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 502
    err = resp.get_json()["error"]
    assert "Se restauro la config de forager" in err and "Se restauró también la config del rescate" in err
    assert hjson.loads(solo_long.read_text())["twe_long"] == 6
    r = _rescate_cfg(app)
    # Vuelve al contenido anterior (no existía: default deshabilitado) sin bajar la generación que se abrió.
    assert r["habilitado"] is False and r["retiro_frecuencia"] == "off" and r["generacion"] == 1
    assert audit_lines(app)[-1]["result"] == "error"
    docker.fail_action = None
    assert _aplicar(client, "con_rescate").status_code == 200
    assert _rescate_cfg(app)["generacion"] == 2  # generación nueva: base nueva


def test_docker_sin_respuesta_deja_las_dos_escritas(client, app, solo_long, docker):  # noqa: F811
    docker.timeout_action = True
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 502
    assert "quedó guardada, igual que la de forager" in resp.get_json()["error"]
    assert hjson.loads(solo_long.read_text())["twe_long"] == 4
    assert _rescate_cfg(app)["habilitado"] is True


def _despues_de_guardar_el_rescate(monkeypatch, accion):
    """Corre ``accion()`` justo después de que el perfil guarda el rescate (antes del START)."""
    save = rescate.save_config

    def save_config(*a, **kw):
        out = save(*a, **kw)
        accion()
        return out

    monkeypatch.setattr(rescate, "save_config", save_config)


def test_timeout_del_inspect_previo_a_escribir_forager_restaura_el_rescate(client, app, solo_long, docker,  # noqa: F811
                                                                          monkeypatch):
    """El START vuelve a consultar el contenedor antes de escribir forager. Un timeout ahí es "incierto" para Docker,
    pero forager no se tocó: el rescate tiene que volver atrás (quedaba el del perfil con la TWE vieja)."""
    import requests

    request = docker.request
    corte = {"activo": False}

    def request_con_timeout(method, url, **kw):
        if corte["activo"] and method == "GET" and url.endswith("/json"):
            raise requests.ReadTimeout("timeout")
        return request(method, url, **kw)

    monkeypatch.setattr(docker, "request", request_con_timeout)
    _despues_de_guardar_el_rescate(monkeypatch, lambda: corte.update(activo=True))
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 502
    err = resp.get_json()["error"]
    assert "Se restauró también la config del rescate" in err and "igual que la de forager" not in err
    assert solo_long.read_text() == SOLO_LONG and docker.actions() == []
    r = _rescate_cfg(app)
    assert r["habilitado"] is False and r["upnl_habilitado"] is False and r["retiro_frecuencia"] == "off"


def test_docker_rechaza_y_forager_no_se_restaura_deja_el_rescate_del_perfil(client, app, solo_long,  # noqa: F811
                                                                            docker,  # noqa: F811
                                                                            monkeypatch):
    """Docker rechaza el restart y falla la vuelta atrás de forager: forager quedó con la TWE del perfil (se carga en el
    próximo reinicio). Restaurar el rescate viejo dejaba TWE 4 sin rescate; queda el del perfil y el mensaje lo dice."""
    docker.fail_action = 500

    def restore(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bot_control, "restore_forager_config", restore)
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 502
    err = resp.get_json()["error"]
    assert "no se pudo restaurar" in err and "no se aplicó el perfil" not in err
    assert "TWE del perfil" in err and "también quedó guardada" in err
    assert hjson.loads(solo_long.read_text())["twe_long"] == 4
    r = _rescate_cfg(app)
    assert r["habilitado"] is True and r["retiro_frecuencia"] == rescate.FRECUENCIA_RECOMENDADA


def test_falla_al_guardar_el_estado_restaura_forager_y_rescate(client, app, solo_long, docker,  # noqa: F811
                                                               monkeypatch):
    """Un error de disco al guardar el estado local, con forager ya escrito y antes de Docker: se vuelve atrás forager
    (y con él el rescate) en vez de dejar las dos escritas sin reiniciar."""
    save_state = bot_control.Store.save_state
    fallas = {"n": 1}

    def save_state_falla(self, state):
        if fallas["n"]:
            fallas["n"] -= 1
            raise OSError(28, "No space left on device")
        return save_state(self, state)

    monkeypatch.setattr(bot_control.Store, "save_state", save_state_falla)
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 500
    err = resp.get_json()["error"]
    assert "Se restauró también la config del rescate" in err
    assert solo_long.read_text() == SOLO_LONG and docker.actions() == []
    assert _rescate_cfg(app)["habilitado"] is False


def test_no_pisa_un_guardado_del_rescate_hecho_durante_el_reinicio(client, app, solo_long, docker,  # noqa: F811
                                                                  monkeypatch):
    """Alguien guarda el rescate desde otra pestaña mientras Docker reinicia y el reinicio falla: ese guardado no se
    pierde (la vuelta atrás solo restaura si la config sigue siendo la que escribió el perfil)."""
    request = docker.request
    otro = {"habilitado": True, "reserva_max_usdt": 5000, "paso_usdt": 500, "umbral_ratio": 6,
            "umbral_devolucion_ratio": 24, "tope_perdida_pct": 40}

    def request_con_guardado(method, url, **kw):
        if method == "POST" and url.endswith("/restart"):
            rescate.save_config(_data_dir(app), rescate.validate_config_body(otro), "otra-pestaña")
        return request(method, url, **kw)

    monkeypatch.setattr(docker, "request", request_con_guardado)
    docker.fail_action = 500
    resp = _aplicar(client, "con_rescate")
    assert resp.status_code == 502
    err = resp.get_json()["error"]
    assert "cambió mientras se aplicaba el perfil" in err
    assert solo_long.read_text() == SOLO_LONG
    r = _rescate_cfg(app)
    assert r["actualizado_por"] == "otra-pestaña" and r["reserva_max_usdt"] == "5000" and r["perfil"] is None


def test_un_job_a_la_vez(client, app, solo_long, docker):  # noqa: F811
    gate = Gate(docker, "restart")
    try:
        first = post(client, "/api/bot/start", {"riesgo": "alto"}, wait=False)
        job_id = first.get_json()["job_id"]
        assert gate.entered.wait(5)
        resp = _aplicar(client, "con_rescate")
        assert resp.status_code == 409 and resp.get_json()["job_id"] == job_id
    finally:
        gate.release.set()
    wait_job(client, job_id)
    assert _rescate_cfg(app) is None


def test_lock_tomado_por_otro_proceso(client, app, solo_long, docker):  # noqa: F811
    handle = bot_control.Store(_data_dir(app)).try_lock()
    try:
        resp = _aplicar(client, "conservador")
        assert resp.status_code == 409
    finally:
        handle.release()
    assert _rescate_cfg(app) is None and docker.actions() == []


def test_requiere_login_y_csrf(app, client, solo_long, docker):  # noqa: F811
    assert app.test_client().post("/api/bot/perfil", json={"perfil": "conservador"}).status_code in (302, 403)
    assert post(client, "/api/bot/perfil", {"perfil": "conservador", "confirmar": True}, token="otro").status_code == 403
    assert client.get("/api/bot/perfil").status_code == 405
    assert docker.actions() == [] and _rescate_cfg(app) is None


def test_job_de_perfil_interrumpido_se_audita_como_perfil(client, app, solo_long, docker, monkeypatch):  # noqa: F811
    notificaciones = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action", lambda *a, **k: notificaciones.append(a))
    gate = Gate(docker, "restart")
    path = _data_dir(app) / jobs.LAST_JOB_FILE_NAME
    try:
        first = post(client, "/api/bot/perfil", {"perfil": "con_rescate", "confirmar": True}, wait=False)
        job_id = first.get_json()["job_id"]
        assert gate.entered.wait(5)
        snapshot = path.read_text()
    finally:
        gate.release.set()
    wait_job(client, job_id)
    path.write_text(snapshot)
    notificaciones.clear()
    app2 = _app_on(_data_dir(app))
    job = registry(app2).get(job_id)
    assert job["estado"] == "interrumpido"
    # Sin vuelta atrás automática: el mensaje dice que el perfil puede haber quedado a medias y qué revisar.
    assert "a medias" in job["resultado"]["error"] and "Rescate" in job["resultado"]["error"]
    entry = audit_lines(app2)[-1]
    assert (entry["action"], entry["result"], entry["params"]) == ("perfil", "interrumpido", {"perfil": "con_rescate"})
    assert "a medias" in entry["detail"]
    assert notificaciones[0][0] == "perfil"


def test_telegram_titulos():
    txt = telegram_notify.format_bot_action("perfil", {"perfil": "con_rescate"}, "ok", {"docker_action": "restart"}, "u")
    assert "PERFIL con_rescate" in txt and "OK" in txt
    txt = telegram_notify.format_bot_action("start", {"twe_long": 3.25}, "ok", {"warning": "w"}, "u")
    assert "START TWE manual 3.25" in txt and "w" in txt


def test_panel_muestra_perfiles_y_twe_manual(client, app, solo_long, docker):  # noqa: F811
    html = client.get("/estrategia").get_data(as_text=True)
    assert 'id="botPerfilSelect"' in html and 'data-bot-accion="perfil"' in html
    assert 'id="botTweInput"' in html and 'min="0.5" max="8" step="0.25"' in html
    status = client.get("/api/bot/status").get_json()
    assert status["n_shorts"] == 0 and status["n_longs"] == 4
    assert status["twe_manual"] == {"min": "0.5", "max": "8", "paso": "0.25", "advertencia": "4"}
    assert set(status["perfiles"]) == {"conservador", "con_rescate"} and status["perfil_vigente"] is None


# ---------------------------------------------------------------- TWE manual


@pytest.mark.parametrize("body", [{"twe_long": "3"}, {"twe_long": True}, {"twe_long": None}, {"twe_long": 0.25},
                                  {"twe_long": 8.25}, {"twe_long": 3.3}, {"twe_long": -1}, {"twe_long": [3]},
                                  {"twe_long": 3, "riesgo": "bajo"},
                                  {"twe_long": 3, "confirmar_twe_alta": "si"}])
def test_twe_manual_validacion_estricta(client, app, env, docker, body):  # noqa: F811
    resp = post(client, "/api/bot/start", body)
    assert resp.status_code == 400
    assert docker.actions() == [] and env.read_text() == FORAGER_HJSON
    assert audit_lines(app)[-1]["result"] == "rechazado"


def test_twe_manual_nan_rechazado(client, env, docker):  # noqa: F811
    resp = client.post("/api/bot/start", data='{"twe_long": NaN}', content_type="application/json",
                       headers={"X-CSRF-Token": "tok"})
    assert resp.status_code == 400 and docker.actions() == []


def test_twe_manual_con_shorts_conserva_twe_short(client, app, env, docker):  # noqa: F811
    resp = post(client, "/api/bot/start", {"twe_long": 3.25})
    assert resp.status_code == 200, resp.get_json()
    cfg = hjson.loads(env.read_text())
    assert (cfg["twe_long"], cfg["twe_short"]) == (3.25, 2)
    assert resp.get_json()["riesgo"] == "personalizado" and "warning" not in resp.get_json()
    assert audit_lines(app)[-1]["params"] == {"twe_long": 3.25}
    assert docker.actions() == ["restart"]


def test_twe_manual_solo_long_escribe_twe_short_1(client, solo_long, docker):  # noqa: F811
    assert post(client, "/api/bot/start", {"twe_long": 3}).status_code == 200
    cfg = hjson.loads(solo_long.read_text())
    assert (cfg["twe_long"], cfg["twe_short"]) == (3, 1) and isinstance(cfg["twe_long"], int)
    assert post(client, "/api/bot/start", {"twe_long": 4.0}).get_json()["riesgo"] == "bajo"


def test_twe_manual_alta_pide_confirmacion(client, app, env, docker):  # noqa: F811
    resp = post(client, "/api/bot/start", {"twe_long": 6})
    assert resp.status_code == 409 and resp.get_json()["requiere_confirmacion"] is True
    assert "liquidaciones desde TWE 6" in resp.get_json()["error"]
    assert docker.actions() == [] and env.read_text() == FORAGER_HJSON
    assert audit_lines(app)[-1]["result"] == "requiere_confirmacion"
    assert post(client, "/api/bot/start", {"twe_long": 4}).status_code == 200  # 4 no pide confirmación
    resp = post(client, "/api/bot/start", {"twe_long": 6.5, "confirmar_twe_alta": True})
    assert resp.status_code == 200
    assert hjson.loads(env.read_text())["twe_long"] == 6.5
    assert "por encima de 4" in resp.get_json()["warning"]


def test_twe_manual_bloqueado_por_tope(client, app, env, docker):  # noqa: F811
    state, _ = rescate.load_state(_data_dir(app))
    state["tope"] = {"id": "t1", "disparado_en": "2026-10-23T10:00:00+00:00", "modo": "real"}
    rescate.save_state(_data_dir(app), state)
    assert post(client, "/api/bot/start", {"twe_long": 3}).status_code == 409
    assert docker.actions() == []


def test_reinicio_de_c3_conserva_una_twe_manual(tmp_path):
    path = tmp_path / "new.json"
    path.write_text(SOLO_LONG.replace("twe_long: 6", "twe_long: 3.25").replace("twe_short: 2", "twe_short: 1"))
    settings = bot_control.Settings()
    settings.docker_url, settings.container, settings.forager_config = DOCKER_BASE, "client17-passivbot", path
    fake = FakeDocker(running=False)
    out = bot_control.start_bot(settings, bot_control.Store(tmp_path / "data"), None,
                                docker=bot_control.DockerClient(DOCKER_BASE, session=fake), mantener_twe=True)
    cfg = hjson.loads(path.read_text())
    assert (cfg["twe_long"], cfg["twe_short"]) == (3.25, 1) and out["riesgo"] == "personalizado"
    assert fake.actions() == ["start"]


def test_sin_manifiesto_el_perfil_aplicado_no_figura_como_medido(client, app, solo_long, docker):  # noqa: F811
    _servicio_vivo(app)
    assert _aplicar(client, "con_rescate").status_code == 200
    status = client.get("/api/bot/status").get_json()
    assert status["perfil_vigente"] is None
    assert "configs sin verificar" in status["perfil_motivo"] and "passivbot todavía no publicó" in status["perfil_motivo"]
    html = client.get("/estrategia").get_data(as_text=True)
    assert "Personalizado: TWE y rescate de Con rescate, pero configs sin verificar" in html
