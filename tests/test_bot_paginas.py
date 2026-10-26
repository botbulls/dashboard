"""Bot y Estrategia (diseno/bot-estrategia/spec.md § 2): navegación, páginas, píldora del bot con 4 estados.

Sin la rueda flotante (#menu_admin): su JS vive en static/js/bot_acciones.js con los mismos IDs y data-bot-*.
"""
from __future__ import annotations

import pathlib
import re
import sqlite3

import pytest

from futuresboard import bot_control

from test_bot_control import app  # noqa: F401
from test_bot_control import binance  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import router  # noqa: F401

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "futuresboard" / "static"

FORAGER_VIGENTE = """{
  user: binance_01
  twe_long: 4
  twe_short: 1
  n_longs: 4
  n_shorts: 0
  long_mode: normal
  short_mode: normal
  approved_symbols_long: ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]
  approved_symbols_short: ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]
}
"""


def _html(client, url):  # noqa: F811
    resp = client.get(url)
    assert resp.status_code == 200, url
    return resp.get_data(as_text=True)


def _posiciones(app, filas, ordenes=()):  # noqa: F811
    with sqlite3.connect(str(app.config["DATABASE"])) as con:
        con.executemany(
            "INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, positionAmt) "
            "VALUES (?, ?, ?, ?, ?, ?)", filas)
        con.executemany(
            "INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) VALUES (?,?,?,?,?,?,?,?)",
            ordenes)


# ---------------------------------------------------------------- rutas y navegación


@pytest.mark.parametrize("url", ["/bot", "/estrategia"])
def test_paginas_nuevas_requieren_login(app, url):  # noqa: F811
    resp = app.test_client().get(url)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_sidebar_con_grupo_control(client):  # noqa: F811
    html = _html(client, "/")
    nav = html.split('aria-label="Secciones"', 1)[1].split("</nav>", 1)[0]
    assert 'class="bb-nav__grupo sidebar-text"' in nav and ">Control<" in nav
    grupo = nav.split(">Control<", 1)[1]
    assert 'href="/bot"' in grupo and 'href="/estrategia"' in grupo
    # El ítem Bot lleva el punto de estado con texto para lectores (lo completa bot_acciones.js).
    bot = grupo.split('href="/bot"', 1)[1].split("</a>", 1)[0]
    assert "data-bot-punto" in bot and 'class="bb-sr"' in bot


@pytest.mark.parametrize("url,titulo", [("/bot", "Bot"), ("/estrategia", "Estrategia")])
def test_seccion_activa_en_las_paginas_nuevas(client, url, titulo):  # noqa: F811
    html = _html(client, url)
    nav = html.split('aria-label="Secciones"', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r'<a class="bb-nav__item" href="[^"]+" aria-current="page" title="([^"]+)"', nav) == [titulo]


@pytest.mark.parametrize("url", ["/", "/positions", "/history", "/rescate", "/bot", "/estrategia"])
def test_sin_rueda_y_con_el_js_de_acciones(client, url):  # noqa: F811
    html = _html(client, url)
    assert 'id="menu_admin"' not in html and 'class="icono"' not in html and "bbPanelBot" not in html
    # Los modales quedan en base.html (retomar un job desde cualquier página) y el JS en un archivo propio.
    assert 'id="botActionConfirmModal"' in html and 'id="botJobModal"' in html
    m = re.search(r'<script src="/static/js/bot_acciones\.js\?v=[0-9a-f]{10}"', html)
    assert m, url
    assert html.index("bootstrap.bundle.min.js") < m.start()
    # Los avisos de las acciones tienen su región propia (antes vivía adentro de la rueda).
    assert re.search(r'<div class="bb-bot-aviso" data-bot-aviso role="status" aria-live="polite"></div>', html)


@pytest.mark.parametrize("url", ["/positions", "/history", "/rescate", "/bot", "/estrategia"])
def test_pildora_del_bot_fuera_de_inicio(client, url):  # noqa: F811
    html = _html(client, url)
    pildoras = re.findall(r'<a class="bb-pildora-bot[^"]*" href="/bot" data-bot-pildora[^>]*>', html)
    # Una en el encabezado (escritorio) y otra en la barra (celu).
    assert len(pildoras) == 2, url


def test_inicio_sin_pildora(client):  # noqa: F811
    assert "data-bot-pildora" not in _html(client, "/")


@pytest.mark.parametrize("url", ["/", "/positions", "/bot", "/estrategia"])
def test_presets_fuera_de_la_interfaz(client, url):  # noqa: F811
    html = _html(client, url)
    assert 'id="botRiesgoSelect"' not in html and 'data-bot-accion="start"' not in html
    assert "Iniciar / aplicar riesgo" not in html


def test_api_de_presets_se_conserva(client, env, docker):  # noqa: F811
    from test_bot_control import post

    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 200 and resp.get_json()["riesgo"] == "bajo"


# ---------------------------------------------------------------- Bot y Estrategia


def test_bot_tiene_parada_gradual_y_apagar(client, env, docker):  # noqa: F811
    docker.running = True
    html = _html(client, "/bot")
    for accion in ("graceful", "apagar"):
        assert f'data-bot-accion="{accion}"' in html
    assert "Herramienta, no asesor" in html


def test_estrategia_muestra_lo_vigente_en_solo_lectura(client, env, docker):  # noqa: F811
    env.write_text(FORAGER_VIGENTE)
    html = _html(client, "/estrategia")
    vigente = html.split('id="bbEstrategiaVigente"', 1)[1].split("</section>", 1)[0]
    for par in ("BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"):
        assert par in vigente
    assert re.search(r'data-value="4"[^>]*>4<', vigente)  # TWE long
    assert "4 a la vez" in vigente
    assert "lw 1,00" in vigente
    # Vigente es solo lectura: lo que escribe la estrategia es el editor de abajo (POST /api/bot/estrategia).
    assert "/api/bot/estrategia" not in vigente and "<form" not in vigente
    # Perfil sigue disponible (mismo endpoint, mismos IDs que tenía la rueda).
    assert 'id="botPerfilSelect"' in html and 'data-bot-accion="perfil"' in html
    # Sin el atajo "Iniciar con esta TWE…": arrancaba passivbot sin el resumen ni la casilla de Iniciar (spec § 2.6:
    # la TWE se edita con el flujo de Estrategia; revisión, hallazgo 19).
    assert 'data-bot-accion="twe"' not in html and 'id="botTweInput"' not in html
    # Editor de la estrategia (borrador, exposición, pares, avanzado): lo arma static/js/estrategia.js con la API.
    assert 'data-estrategia-editor' in html and 'data-url="/api/bot/estrategia"' in html
    assert 'id="bbAgregarParModal"' in html and "js/estrategia.js" in html
    assert 'id="bbTwe"' in html and 'id="bbNLongs"' in html


def test_estrategia_sin_config_de_forager_no_rompe(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv(bot_control.ENV_FORAGER_CONFIG, raising=False)
    html = _html(client, "/estrategia")
    assert "No se pudo leer la estrategia" in html


def test_js_de_acciones_conserva_el_contrato():
    js = (STATIC / "js" / "bot_acciones.js").read_text(encoding="utf-8")
    for hook in ("botActionConfirmModal", "botActionConfirmCheck", "botActionConfirmAccept", "botJobModal",
                 "[data-bot-accion]", "data-bot-confirm", "data-bot-job", "fbBotJobId", "/api/bot/jobs/activo",
                 "job.cancelable", "fb-bot-status", "data-bot-pildora", "data-bot-punto", "botPerfilSelect",
                 "/api/bot/perfil", "confirmar: true"):
        assert hook in js, hook
    # Sin el atajo de TWE: la TWE se guarda con el flujo de Estrategia y el arranque va por Iniciar… (hallazgo 19).
    assert "botTweInput" not in js and "twe: () =>" not in js
    # Los presets no se ofrecen (S3): no hay acción "start" con riesgo en el JS.
    assert "riesgo:" not in js and "botRiesgoSelect" not in js


# ---------------------------------------------------------------- estado del bot (4 estados)


def _st(running=True, long_mode="normal", modes=True, enabled=True, error=None):
    st = {"enabled": enabled, "modes_supported": modes, "long_mode": long_mode, "short_mode": long_mode,
          "container": {"running": running, "status": "running" if running else "exited"}}
    if error:
        st["error"] = error
    return st


SIN = {"posiciones": 0, "ordenes": 0}


@pytest.mark.parametrize("status,expo,esperado", [
    (_st(), SIN, "operando"),
    (_st(long_mode="graceful_stop"), SIN, "gradual"),
    (_st(long_mode="graceful_stop", modes=False), SIN, "operando"),
    (_st(running=False), SIN, "detenido"),
    (_st(running=False, long_mode="graceful_stop"), SIN, "detenido"),
    (_st(running=False), {"posiciones": 3, "ordenes": 3}, "expuesto"),
    (_st(running=False), {"posiciones": 0, "ordenes": 1}, "expuesto"),
    # Sin evidencia de que no haya nada: base ilegible o vieja (revisión, hallazgo 18). Con algo, expuesto igual.
    (_st(running=False), {**SIN, "error": "No se pudo leer la base del scraper."}, "sin_verificar"),
    (_st(running=False), {**SIN, "fresco": False}, "sin_verificar"),
    (_st(running=False), {**SIN, "fresco": True}, "detenido"),
    (_st(running=False), {"posiciones": 1, "ordenes": 0, "fresco": False}, "expuesto"),
    (_st(), {**SIN, "fresco": False}, "operando"),
    (_st(enabled=False), SIN, "sin_datos"),
    (_st(error="Docker respondio 500"), SIN, "sin_datos"),
    ({"enabled": True, "error": "proxy caído"}, SIN, "sin_datos"),
])
def test_estado_del_bot(status, expo, esperado):
    assert bot_control.estado_del_bot(status, expo) == esperado


def test_status_con_estado_y_exposicion_del_scraper(client, app, env, docker):  # noqa: F811
    _posiciones(app, [("BTCUSDT", -2.25, 10, 82372.0, "LONG", 0.0022), ("ETHUSDT", 0.21, 10, 3905.4, "LONG", 0.12),
                      ("XRPUSDT", 0.0, 10, 1.3, "LONG", 0.0)],
                [(0.07, 741.54, "BUY", "LONG", "NEW", "BNBUSDT", 1, "LIMIT")])
    docker.running = False
    st = client.get("/api/bot/status").get_json()
    assert st["estado_bot"] == "expuesto"
    expo = st["exposicion"]
    assert (expo["posiciones"], expo["ordenes"]) == (2, 1)
    assert expo["pares"] == ["BNBUSDT", "BTCUSDT", "ETHUSDT"]
    assert expo["upnl"] == pytest.approx(-2.04)
    docker.running = True
    assert client.get("/api/bot/status").get_json()["estado_bot"] == "operando"


def test_status_con_docker_caido_es_sin_datos(client, env, docker):  # noqa: F811
    docker.fail_action = None
    docker.exists = True

    def roto(method, url, **kwargs):
        from test_bot_control import FakeResponse

        return FakeResponse(500)

    docker.request = roto
    resp = client.get("/api/bot/status")
    assert resp.status_code == 502
    assert resp.get_json()["estado_bot"] == "sin_datos"


def test_status_rapido_usa_timeout_corto(client, env, docker, monkeypatch):  # noqa: F811
    vistos = []
    original = bot_control.DockerClient.__init__

    def init(self, base_url, session=None, timeout=bot_control.HTTP_TIMEOUT_SECONDS):
        vistos.append(timeout)
        original(self, base_url, session=session, timeout=timeout)

    monkeypatch.setattr(bot_control.DockerClient, "__init__", init)
    client.get("/api/bot/status?rapido=1")
    assert vistos and max(vistos) <= 3
