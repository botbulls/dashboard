"""Decisión 2 (diseno/bot-estrategia/spec.md § 3.4 y § 12): Apagar deja forager en parada gradual antes de detener
(el del panel, el del tope del rescate y el de C3) sin frenar nunca el Apagar, y C3 no reinicia solo si la estrategia
cambió en el corte.

Riesgos con test explícito:
  (a) el hash de C3 excluye long_mode / short_mode: si no, después de su propio Apagar C3 no reiniciaría nunca;
  (b) el START automático de C3 (preset o mantener_twe), después de su Apagar y después de un Apagar del tope, vuelve
      los modos a normal: si no, el bot "arranca" y no abre nada.
"""
from __future__ import annotations

import json
import os
import pathlib

import hjson
import pytest

from futuresboard import bot_control
from futuresboard import c3
from futuresboard import jobs
from futuresboard import rescate
from futuresboard import telegram_notify

from test_bot_control import app  # noqa: F401
from test_bot_control import binance  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401
from test_c3 import C3Harness
from test_c3 import cortar
from test_c3 import operando

DOCKER = "http://docker-proxy:2375"


def leer(path):
    return hjson.loads(path.read_text())


def backups(path):
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


def paso(job, clave):
    return next(p for p in job["pasos"] if p["clave"] == clave)


@pytest.fixture
def h(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return C3Harness(d, tmp_path)


@pytest.fixture
def solo_lectura(env):  # noqa: F811
    """new.json y su directorio en solo lectura (con el archivo solo, el dueño igual puede hacer os.replace)."""
    if os.geteuid() == 0:
        pytest.skip("root ignora los permisos")
    carpeta = env.parent
    os.chmod(env, 0o444)
    os.chmod(carpeta, 0o555)
    try:
        yield env
    finally:
        os.chmod(carpeta, 0o755)
        os.chmod(env, 0o644)


# ---------------------------------------------------------------- Apagar del panel


def test_apagar_deja_forager_en_parada_gradual_antes_del_stop(client, app, env, docker, binance):  # noqa: F811
    binance.add_position("BTCUSDT", "0.5")
    vistos = {}
    original = docker.request

    def request(method, url, **kw):
        if method == "POST" and url.endswith("/stop"):
            vistos["al_stop"] = (leer(env).get("long_mode"), leer(env).get("short_mode"))
        return original(method, url, **kw)

    docker.request = request
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    # Escrito ANTES del docker stop, con backup, y el resto del Apagar igual.
    assert vistos["al_stop"] == ("graceful_stop", "graceful_stop")
    assert len(backups(env)) == 1
    assert [c for c, _ in bot_control.APAGAR_STEPS][:2] == ["parada_gradual", "detener"]
    assert paso(resp.job, "parada_gradual")["estado"] == "ok"
    assert "backup" in paso(resp.job, "parada_gradual")["detalle"]
    assert resp.get_json()["resumen"]["completo"] is True and binance.positions == {}
    cfg = leer(env)
    assert (cfg["twe_long"], cfg["twe_short"], cfg["n_longs"]) == (6, 2, 4)  # la estrategia no se toca


def test_apagar_con_new_json_de_solo_lectura_sigue_igual(client, app, env, docker, binance, solo_lectura,  # noqa: F811
                                                        monkeypatch):
    binance.orders = [{"symbol": "ETHUSDT"}]
    binance.add_position("BTCUSDT", "0.5")
    antes = env.read_bytes()
    avisados = []
    monkeypatch.setattr(telegram_notify, "notify_bot_action",
                        lambda action, params, outcome, payload, user: avisados.append(
                            telegram_notify.format_bot_action(action, params, outcome, payload, user)))
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    # Stop, cancelar y cerrar corren igual: el cierre es completo y el job termina ok.
    assert resp.status_code == 200 and resp.job["estado"] == "ok"
    assert docker.actions() == ["stop"]
    assert binance.positions == {} and binance.orders == []
    for clave in ("detener", "verificar_detenido", "cancelar_ordenes", "cerrar_posiciones", "verificacion"):
        assert paso(resp.job, clave)["estado"] == "ok", clave
    # El fallo queda en los pasos, en el resultado y en Telegram; new.json no cambió.
    p = paso(resp.job, "parada_gradual")
    assert p["estado"] == "error" and "No se pudo dejar forager en parada gradual" in p["detalle"]
    assert "parada gradual" in resp.get_json()["warning"]
    assert env.read_bytes() == antes and backups(env) == []
    assert len(avisados) == 1 and "No se pudo dejar forager en parada gradual" in avisados[0]


def test_apagar_ya_en_parada_gradual_no_escribe(client, env, docker, binance):  # noqa: F811
    env.write_text(env.read_text().replace("n_shorts: 2", "n_shorts: 2\n  long_mode: graceful_stop\n"
                                                           "  short_mode: graceful_stop"))
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200
    assert paso(resp.job, "parada_gradual")["estado"] == "ok" and backups(env) == []


def test_apagar_con_hjson_roto_sigue_igual(client, env, docker, binance):  # noqa: F811
    env.write_text("{ esto no es: [hjson")
    binance.add_position("BTCUSDT", "0.5")
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    assert resp.status_code == 200 and binance.positions == {}
    assert paso(resp.job, "parada_gradual")["estado"] == "error"


def test_apagar_guarda_el_short_mode_para_iniciar(client, app, env, docker, binance):  # noqa: F811
    env.write_text(env.read_text().replace("n_shorts: 2", "n_shorts: 2\n  short_mode: tp_only"))
    post(client, "/api/bot/stop", {"modo": "apagar"})
    docker.running = False
    resp = post(client, "/api/bot/start", {"riesgo": "medio"})
    assert resp.status_code == 200
    assert (leer(env)["long_mode"], leer(env)["short_mode"]) == ("normal", "tp_only")


def _stop_que_no_detiene(docker, monkeypatch):  # noqa: F811
    original = docker.request

    def request(method, url, **kw):
        r = original(method, url, **kw)
        if method == "POST" and url.endswith("/stop"):
            docker.running = True  # respondió 204, pero el contenedor sigue corriendo
        return r

    monkeypatch.setattr(docker, "request", request)


@pytest.mark.parametrize("falla", ["http500", "sigue_corriendo"])
def test_apagar_con_stop_fallido_vuelve_la_config_a_lo_que_corre(client, app, env, docker, binance, falla,  # noqa: F811
                                                                  monkeypatch):
    """Si el docker stop falla de forma definida, passivbot sigue corriendo con la config de antes (normal): la
    escritura de graceful_stop se deshace, si no la píldora e Inicio mostraban "Parada gradual" con forager operando
    normal (revisión, hallazgo 17)."""
    env.write_text(env.read_text().replace("n_shorts: 2", "n_shorts: 2\n  short_mode: normal"))
    os.utime(env, (1, 1))  # config en efecto: más vieja que el arranque de passivbot
    antes = env.read_bytes()
    docker.running = True
    docker.started_at = "2026-10-01T12:00:00.000000000Z"
    estado_antes = bot_control.Store(pathlib.Path(app.config["DATABASE"]).parent).load_state()
    if falla == "http500":
        docker.fail_action = 500
    else:
        _stop_que_no_detiene(docker, monkeypatch)
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    docker.fail_action = None
    assert resp.job["estado"] == "error" and docker.running is True
    assert env.read_bytes() == antes  # la config vuelve a la que corre forager
    despues = bot_control.Store(pathlib.Path(app.config["DATABASE"]).parent).load_state()
    assert despues.get("short_mode_before_stop") == estado_antes.get("short_mode_before_stop")
    assert "Se volvió a la config de forager de antes" in resp.get_json()["error"]
    assert "volvió a la config" in paso(resp.job, "parada_gradual")["detalle"]
    assert not [c for c in binance.calls if c[0] in ("POST", "DELETE")]  # sin tocar Binance
    st = client.get("/api/bot/status").get_json()
    assert st["estado_bot"] == "operando" and st["config_pending"] is False


def test_apagar_con_stop_incierto_deja_graceful_y_lo_dice(client, app, env, docker, binance):  # noqa: F811
    docker.running = True
    docker.timeout_action = True
    resp = post(client, "/api/bot/stop", {"modo": "apagar"})
    docker.timeout_action = False
    assert resp.job["estado"] == "error"
    assert leer(env)["long_mode"] == "graceful_stop"  # resultado incierto: no se adivina
    assert "quedó en graceful_stop" in resp.get_json()["error"]


def test_apagar_sin_config_de_forager_sigue_y_avisa(tmp_path, env, docker, binance, monkeypatch):  # noqa: F811
    monkeypatch.delenv(bot_control.ENV_FORAGER_CONFIG)
    binance.add_position("BTCUSDT", "0.5")
    rep = jobs.Job("apagar", {"modo": "apagar"}, bot_control.APAGAR_STEPS, "u")
    out = bot_control.stop_bot(bot_control.Settings(), bot_control.Store(tmp_path), "apagar",
                               binance=bot_control.BinanceFuturesClient("https://fapi.binance.com", "k", "s"),
                               reporter=rep)
    assert out["ok"] is True and binance.positions == {}
    assert paso(rep.to_dict(), "parada_gradual")["estado"] == "omitido"
    assert bot_control.ENV_FORAGER_CONFIG in out["warning"]


# ---------------------------------------------------------------- Apagar del tope y de C3 (make_apagar)


@pytest.mark.parametrize("origen,usuario", [("rescate_tope", rescate.APAGAR_USUARIO),
                                            ("c3_corte", c3.USUARIO_APAGAR)])
def test_apagar_del_tope_y_de_c3_dejan_parada_gradual(tmp_path, env, docker, binance, origen, usuario):  # noqa: F811
    binance.add_position("BTCUSDT", "0.5")
    apagar = rescate.make_apagar(tmp_path, bot_control.Settings(),
                                 bot_control.BinanceFuturesClient("https://fapi.binance.com", "k", "s"),
                                 usuario=usuario, origen=origen)
    outcome, payload = apagar()
    assert outcome == "ok" and binance.positions == {}
    assert (leer(env)["long_mode"], leer(env)["short_mode"]) == ("graceful_stop", "graceful_stop")


def test_apagar_del_tope_con_solo_lectura_cierra_igual_y_lo_dice_en_telegram(tmp_path, env, docker, binance,  # noqa: F811
                                                                          solo_lectura):
    binance.add_position("BTCUSDT", "0.5")
    apagar = rescate.make_apagar(tmp_path, bot_control.Settings(),
                                 bot_control.BinanceFuturesClient("https://fapi.binance.com", "k", "s"))
    outcome, payload = apagar()
    assert outcome == "ok" and binance.positions == {} and docker.actions() == ["stop"]
    texto = telegram_notify.format_bot_action("stop", {"modo": "apagar"}, outcome, payload, rescate.APAGAR_USUARIO)
    assert "No se pudo dejar forager en parada gradual" in texto


# ---------------------------------------------------------------- (b) START de C3 después de un Apagar


@pytest.mark.parametrize("origen", ["c3_corte", "rescate_tope"])
@pytest.mark.parametrize("plan", [{"riesgo": "medio", "mantener_twe": False},
                                  {"riesgo": None, "mantener_twe": True}])
def test_start_de_c3_despues_de_un_apagar_vuelve_los_modos_a_normal(tmp_path, env, docker, binance, origen,  # noqa: F811
                                                                    plan):
    hash_antes = bot_control.estrategia_hash(leer(env))
    apagar = rescate.make_apagar(tmp_path, bot_control.Settings(),
                                 bot_control.BinanceFuturesClient("https://fapi.binance.com", "k", "s"),
                                 origen=origen)
    assert apagar()[0] == "ok"
    assert leer(env)["long_mode"] == "graceful_stop" and docker.running is False
    # (a) en archivos reales: el Apagar cambió los modos pero no la estrategia.
    assert bot_control.estrategia_hash(leer(env)) == hash_antes
    start = c3.make_start(tmp_path, bot_control.Settings(), docker=bot_control.DockerClient(DOCKER))
    outcome, payload = start(plan)
    assert outcome == "ok", payload
    cfg = leer(env)
    assert (cfg["long_mode"], cfg["short_mode"]) == ("normal", "normal")
    assert docker.running is True


# ---------------------------------------------------------------- C3: hash de la estrategia al corte


def _apagar_real(h):
    """Doble de Apagar que hace lo mismo que el real con la config: deja los modos en graceful_stop."""
    original = h.apagar

    def apagar():
        h.forager_cfg = {**h.forager_cfg, "long_mode": "graceful_stop", "short_mode": "graceful_stop"}
        return original()

    h.apagar = apagar


ESTRATEGIA = {"twe_long": 4, "twe_short": 1, "n_longs": 4, "long_mode": "normal", "short_mode": "normal",
              "approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
              "approved_symbols_short": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]}


def test_c3_guarda_el_hash_de_la_estrategia_al_cortar(h):
    h.forager_cfg = dict(ESTRATEGIA)
    svc = operando(h, politica="cooldown", cooldown=1)
    corte = cortar(h, svc)
    assert corte["config_al_corte"]["estrategia_hash"] == bot_control.estrategia_hash(ESTRATEGIA)


def test_a_c3_reinicia_despues_de_su_propio_apagar(h):
    """(a) El Apagar de C3 deja los modos en graceful_stop: como el hash no los mira, el reinicio sigue."""
    h.forager_cfg = dict(ESTRATEGIA)
    _apagar_real(h)
    svc = operando(h, politica="cooldown", cooldown=1)
    cortar(h, svc)
    assert h.forager_cfg["long_mode"] == "graceful_stop"
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.starts == [{"riesgo": "bajo", "mantener_twe": False, "descripcion": "el preset bajo",
                         "estrategia_hash": bot_control.estrategia_hash(ESTRATEGIA)}]
    assert h.c3()["c3_parado"] is False


@pytest.mark.parametrize("cambio", [
    {"approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]},
    {"n_longs": 5},
    {"approved_symbols_short": ["BTCUSDT"]},
])
def test_c3_no_reinicia_solo_si_cambio_la_estrategia_en_el_corte(h, cambio):
    h.forager_cfg = dict(ESTRATEGIA)
    _apagar_real(h)
    svc = operando(h, politica="cooldown", cooldown=1)
    cortar(h, svc)
    h.forager_cfg = {**h.forager_cfg, **cambio}  # Estrategia guardada con C3 parado (TWE igual)
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.starts == []
    corte = h.c3()["corte"]
    assert corte["auto"] is False and "estrategia" in corte["motivo_manual"]
    assert "Iniciar" in corte["motivo_manual"]
    assert any("Sin reinicio automático" in t and "estrategia" in t for t in h.sent)
    # Queda esperando: más tiempo no lo reinicia.
    h.avanzar(86400)
    svc.tick()
    assert h.starts == []


def test_c3_corte_viejo_sin_hash_usa_la_comparacion_de_twe(h):
    h.forager_cfg = dict(ESTRATEGIA)
    svc = operando(h, politica="cooldown", cooldown=1)
    cortar(h, svc)
    # Un corte guardado antes de este cambio no tiene estrategia_hash.
    state, _ = rescate.load_state(h.data_dir)
    del state["c3"]["corte"]["config_al_corte"]["estrategia_hash"]
    rescate.save_state(h.data_dir, state)
    h.avanzar(86400 + 60)
    svc.tick()
    assert len(h.starts) == 1


# ---------------------------------------------------------------- el hash se vuelve a mirar con el lock tomado

_CON_SOL = {"n_longs": 5,
            "approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"],
            "approved_symbols_short": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]}


class _HarnessConCarrera(C3Harness):
    """forager lee el new.json real y el START es el make_start real. Entre el plan de C3 (hash leído sin el lock) y
    el lock, un job de Estrategia con el bot detenido toma el lock, escribe otra estrategia y lo suelta."""

    def __init__(self, data_dir, tmp_path, cfg_path, docker_fake, nueva):
        super().__init__(data_dir, tmp_path)
        self.cfg_path, self.docker_fake, self.nueva = cfg_path, docker_fake, nueva
        self.planes = []

    def forager(self):
        return bot_control.read_forager_config(self.cfg_path)

    def contenedor(self):
        return bool(self.docker_fake.running)

    def apagar(self):
        bot_control.write_forager_config(self.cfg_path, {"long_mode": "graceful_stop", "short_mode": "graceful_stop"})
        self.docker_fake.running = False
        self.apagados.append(self.clock.t)
        return self.apagar_result

    def start(self, plan):
        import threading
        import time

        self.planes.append(dict(plan))
        real = c3.make_start(self.data_dir, bot_control.Settings(), docker=bot_control.DockerClient(DOCKER),
                             lock_timeout=10)
        job = bot_control.Store(self.data_dir).try_lock()  # el job de Estrategia ganó el lock
        assert job is not None
        out = {}
        t = threading.Thread(target=lambda: out.update(r=real(plan)))
        t.start()
        time.sleep(1.2)  # make_start sondea el lock cada 0,5 s
        assert "r" not in out
        bot_control.write_forager_config(self.cfg_path, self.nueva)  # detenido: escribe y no arranca
        job.release()
        t.join(15)
        return out["r"]


@pytest.mark.parametrize("nueva", [_CON_SOL, {"twe_long": 3}], ids=["pares_nuevos", "twe_guardada"])
def test_c3_no_arranca_una_estrategia_guardada_mientras_esperaba_el_lock(tmp_path, env, docker, nueva):  # noqa: F811
    """C3 comparaba el hash fuera del lock y make_start arrancaba con lo que hubiera al tomarlo: pares nuevos sin que
    nadie los viera, o el preset pisando la TWE guardada (revisión, hallazgos 13 y 16)."""
    env.write_text(hjson.dumps({**ESTRATEGIA, "n_shorts": 0}))
    d = tmp_path / "data"
    d.mkdir()
    h = _HarnessConCarrera(d, tmp_path, env, docker, nueva)
    docker.running = True
    svc = operando(h, politica="cooldown", cooldown=1)
    corte = cortar(h, svc)
    assert docker.running is False
    h.avanzar(86400 + 60)
    svc.tick()
    assert len(h.planes) == 1 and h.planes[0]["estrategia_hash"] == corte["config_al_corte"]["estrategia_hash"]
    assert "start" not in docker.actions() and docker.running is False
    cfg = leer(env)
    assert cfg["long_mode"] == "graceful_stop"  # nadie lo arrancó
    st = h.c3()
    assert st["c3_parado"] is True and st["corte"]["reinicio"]["intentos"] == 0  # bloqueado: no gasta intento
    # En el tick siguiente ve el cambio y pasa al camino manual, con el aviso.
    h.avanzar(60)
    svc.tick()
    assert len(h.planes) == 1 and "start" not in docker.actions()
    st = h.c3()
    assert st["corte"]["auto"] is False and "estrategia" in st["corte"]["motivo_manual"]
    assert any("Sin reinicio automático" in t for t in h.sent)
