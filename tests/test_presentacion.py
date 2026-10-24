"""Presentación del dashboard (rediseño): avisos de seguridad a la vista, hooks que usan el JS / los tests / los
scripts de captura, y números en es-AR con el valor crudo intacto en data-value / data-order.

Solo mira el HTML que sale del server; no cambia ni prueba lógica de negocio.
"""
from __future__ import annotations

import pathlib
import re
import sqlite3
import time
from unittest import mock

import pytest
import requests

from test_bot_control import app  # noqa: F401
from test_bot_control import client  # noqa: F401

M = "−"
NB = " "
STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "futuresboard" / "static"


class _Resp:
    def __init__(self, data):
        self._data = data

    def __bool__(self):
        return True

    def json(self):
        return self._data


def _binance(url, *a, **kw):
    if "premiumIndex" in url:
        return _Resp([{"symbol": "BTCUSDT", "markPrice": "81954.1"}, {"symbol": "ETHUSDT", "markPrice": "3907.15"}])
    raise AssertionError(f"llamada inesperada: {url}")


@pytest.fixture
def con_datos(app):  # noqa: F811
    """Cuenta con balance, dos posiciones (una con PnL negativo), órdenes e ingresos de hoy."""
    now = int(time.time() * 1000)
    with sqlite3.connect(str(app.config["DATABASE"])) as con:
        con.execute("UPDATE account SET totalWalletBalance = 5416.8, totalUnrealizedProfit = -2.04 WHERE AID = 1")
        con.executemany(
            "INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, positionAmt) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [("BTCUSDT", -2.25, 10, 82372.0, "LONG", 0.0022), ("ETHUSDT", 0.21, 10, 3905.4, "LONG", 0.12)])
        con.executemany(
            "INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) VALUES (?,?,?,?,?,?,?,?)",
            [(0.0011, 82610.0, "SELL", "LONG", "NEW", "BTCUSDT", now - 60_000, "LIMIT"),
             (0.12, 3949.8, "SELL", "LONG", "NEW", "ETHUSDT", now - 120_000, "LIMIT")])
        con.executemany(
            "INSERT INTO income (tranId, symbol, incomeType, income, asset, info, time, tradeId) VALUES (?,?,?,?,?,?,?,?)",
            [("1", "BTCUSDT", "REALIZED_PNL", 1234.5, "USDT", "", now - 1000, 0),
             ("2", "ETHUSDT", "REALIZED_PNL", -0.69, "USDT", "", now - 2000, 0)])
    return app


def _get(client, url):  # noqa: F811
    with mock.patch.object(requests, "get", _binance):
        resp = client.get(url)
    assert resp.status_code == 200, url
    return resp.get_data(as_text=True)


# ---------------------------------------------------------------- avisos de seguridad (restricción 4)


def test_franja_de_estado_primero_en_la_home(client):  # noqa: F811
    html = _get(client, "/")
    franja = html.index('class="bb-franja"')
    # Estado del bot y último pedido: botones con texto (no solo ícono), antes que los KPIs y el rescate.
    assert re.search(r'<button id="botStatus" type="button" class="badge bot-status-badge[^"]*"', html)
    assert '<span class="bot-status-text">' in html
    assert re.search(r'<button type="button" class="last-order-badge"', html)
    assert "Último pedido" in html
    nota = html.index('role="note">Herramienta, no asesor')
    assert franja < html.index('id="botStatus"') < nota < html.index('class="bb-kpis"') < html.index('id="rescateTarjetas"')
    # El descargo completo sigue en el pie.
    assert 'id="pie"' in html and "No brinda asesoramiento financiero" in html


def test_badge_testnet_sale_de_la_config_aunque_el_rescate_no_tenga_datos(client, app):  # noqa: F811
    assert "testnet/Demo" not in _get(client, "/")
    app.config["BINANCE_TESTNET"] = True
    franja = _get(client, "/").split('class="bb-franja"', 1)[1].split("</section>", 1)[0]
    assert "testnet/Demo" in franja and "Rescate sin datos" in franja


def test_confirmacion_con_casilla_y_boton_en_el_pie_fijo(client):  # noqa: F811
    html = _get(client, "/")
    modal = html.split('id="botActionConfirmModal"', 1)[1].split('id="botJobModal"', 1)[0]
    assert "modal-dialog-scrollable" in modal and "modal-fullscreen-sm-down" in modal
    pie = modal.split('class="modal-footer', 1)[1]
    assert "Operás bajo tu propio riesgo." in pie and "Herramienta, no asesor." in pie
    assert 'data-bot-confirm="check-wrap"' in pie and 'id="botActionConfirmAccept"' in pie


def test_advertencia_de_twe_conserva_las_clases_que_togglea_el_js(client):  # noqa: F811
    html = _get(client, "/")
    assert '<span class="d-none" id="botTweAdvertencia" data-bot-panel="twe-advertencia" role="alert"></span>' in html
    css = (STATIC / "styles" / "bb.css").read_text(encoding="utf-8")
    assert "#botTweAdvertencia.text-warning" in css and "#botTweAdvertencia.text-danger" in css



def _sin_plegados(html):
    return re.sub(r"<details\b.*?</details>", "", html, flags=re.S)


def test_rescate_resultado_de_apagar_y_cierre_a_mercado_a_la_vista(client):  # noqa: F811
    html = _get(client, "/rescate")
    tope = html.split('id="rescateTope"', 1)[1].split('id="rescateConfirmacion"', 1)[0]
    # Lo que hizo Apagar (por ejemplo, un error con posiciones abiertas) se ve antes de la casilla de reconocer.
    assert "<details" not in tope
    assert tope.index('data-r="tope-apagar"') < tope.index('id="rescateTopeCheck"') < tope.index('id="rescateTopeAck"')
    aviso = html.split('class="bb-alerta bb-alerta--aviso" role="note"', 1)[1].split("</div>\n    </div>", 1)[0]
    assert "ejecuta <strong>Apagar</strong>: detiene passivbot y cierra todo a mercado" in _sin_plegados(aviso)



def test_tarjetas_de_la_home_con_errores_y_reinicio_de_c3_a_la_vista(app):  # noqa: F811
    from flask import render_template

    r = {"modo": "real", "testnet": False, "servicio_vivo": True, "habilitado": True,
         "ratio": {"semaforo": "verde", "valor": None, "posiciones": 0},
         "tope": {"estado": "armado", "limite": "4000.00"},
         "capital": {"usdt": None}, "spot": {"usdt": None}, "prestamo": {"usdt": None},
         "upnl": {"estado": "real", "upnl": "-5.00", "x_pct": "70", "paso": "1000", "error": "ERROR DEL UPNL",
                  "upnl_dispara": "-7000.00"},
         "retiro": {"estado": "real", "frecuencia": "anual", "acumulado": "0.00", "acumulado_simulado": "0.00",
                    "base": "10000.00", "error": "ERROR DEL RETIRO"},
         "c3": {"estado": "parado", "parado": True, "proximo_reinicio": "cuando el índice llegue a 2026-10-24",
                "lectura_error": "ERROR DE C3", "ultimo_evento": {"tipo": "corte", "hace": "hace 5 min"}}}
    with app.test_request_context("/"):
        html = render_template("_rescate_tarjetas.html", rescate_resumen=r)
    visible = _sin_plegados(html)
    # Errores y estado del bot (cuándo reinicia C3) sin tocar nada; el resto del detalle sigue plegado.
    for texto in ("ERROR DEL UPNL", "ERROR DEL RETIRO", "error: ERROR DE C3", "reinicio: cuando el índice llegue a 2026-10-24"):
        assert texto in visible, texto
    assert "próximo paso con uPnL" in html and "próximo paso con uPnL" not in visible
    assert "último corte hace 5 min" in html and "último corte" not in visible


# ---------------------------------------------------------------- hooks (restricción 3)

PAGINAS = ["/", "/positions", "/history", "/projection", "/rescate", "/settings", "/coins/BTCUSDT"]


@pytest.mark.parametrize("url", PAGINAS)
def test_hooks_del_shell_en_todas_las_paginas(client, con_datos, url):  # noqa: F811
    html = _get(client, url)
    for hook in ('id="sidebar"', 'id="sidebarToggle"', 'id="sidebarToggleIcon"', 'id="mainContent"',
                 'id="sidebarOverlay"', 'id="activecoins-collapse"', 'id="inactivecoins-collapse"',
                 'id="themeToggleMobile"', 'id="menu_admin"', 'class="icono"', 'name="respuesta"',
                 'id="botActionConfirmModal"', 'id="botActionConfirmCheck"', 'id="botActionConfirmAccept"',
                 'id="botJobModal"', 'id="botPerfilSelect"', 'id="botRiesgoSelect"', 'id="botTweAdvertencia"'):
        assert hook in html, (url, hook)
    # Una sola hamburguesa, en la barra de base.html (antes cada página tenía la suya y Proyección ninguna).
    assert html.count('id="mobileMenuToggle"') == 1, url
    assert len(re.findall(r'class="[^"]*theme-switcher[^"]*"', html)) == 1
    assert len(re.findall(r'data-theme-value="(auto|light|dark)"', html)) == 3
    for k in ("start", "perfil", "twe", "graceful", "apagar"):
        assert f'data-bot-accion="{k}"' in html
    for k in ("titulo", "texto", "check-wrap", "check-label"):
        assert f'data-bot-confirm="{k}"' in html
    assert 'id="botTweInput" name="twe_long" type="number" min="0.5" max="8" step="0.25"' in html
    # Lo que leen los scripts de captura: <head> una vez, </body> una vez, la meta del CSRF con name antes que content.
    assert html.count("<head>") == 1 and html.count("</body>") == 1
    assert re.search(r'<meta name="csrf-token" content="[^"]*">', html)
    assert '<html lang="es-AR"' in html


def test_menu_del_celu_retiene_el_foco(client):  # noqa: F811
    # Tab / Shift+Tab dan la vuelta adentro de #sidebar mientras está abierto; Esc lo cierra (verificado con
    # teclas reales por CDP en la evidencia del deploy). Acá se cuida que el manejador siga en base.html.
    html = _get(client, "/")
    js = html.split("function enfocablesDelMenu()", 1)[1].split("const alCambiarAncho", 1)[0]
    assert "e.key !== 'Tab' || !sidebar.classList.contains('mobile-open')" in js
    assert "ultimo.focus()" in js and "primero.focus()" in js
    assert "if (e.key !== 'Escape') return;" in js


@pytest.mark.parametrize("url,seccion", [("/", "Inicio"), ("/positions", "Posiciones"), ("/history", "Historial"),
                                         ("/rescate", "Rescate (colchón)"), ("/projection", "Proyección"),
                                         ("/settings", "Ajustes de la cuenta")])
def test_sidebar_con_links_reales_y_seccion_activa(client, con_datos, url, seccion):  # noqa: F811
    html = _get(client, url)
    nav = html.split('aria-label="Secciones"', 1)[1].split("</nav>", 1)[0] + html.split('class="bb-sidebar__pie"', 1)[1]
    activos = re.findall(r'<a class="bb-nav__item" href="[^"]+" aria-current="page" title="([^"]+)"', nav)
    assert activos == [seccion]
    for destino in ("/positions", "/history", "/rescate", "/projection"):
        assert f'href="{destino}"' in nav


def test_monedas_activas_legibles_con_crudo(client, con_datos):  # noqa: F811
    html = _get(client, "/")
    lista = html.split('id="activecoins-collapse"', 1)[1].split("</ul>", 1)[0]
    # Sin siglas: par, exposición en % (crudo = pbr en data-value) y órdenes en palabras; cada fila abre la moneda.
    for sigla in ("PBR L", "C/V L", "PBR S", "C/V S"):
        assert sigla not in lista
    assert ">Exposición<" in lista or "Exposición</span>" in html.split('id="activecoins-collapse"', 1)[1]
    filas = re.findall(r'<a class="bb-moneda" href="/coins/([A-Z0-9]+)"', lista)
    assert filas, lista[:500]
    assert re.search(r'class="bb-moneda__expo" data-value="[-0-9.]+"><span class="bb-sr">exposición long </span>[0-9.]+\u00a0%', lista)
    assert re.search(r'\d+ (compra|compras) · \d+ (venta|ventas)', lista)
    assert "bb-moneda--total" in lista and ">Total<" in lista
    # Sin shorts en los datos, no aparece la línea de short.
    assert "bb-moneda__short" not in lista


def test_estaticos_con_huella_de_version(client, con_datos):  # noqa: F811
    html = _get(client, "/")
    for archivo in ("styles/bb.css", "styles/dashboard.css", "js/formato.js"):
        assert re.search(rf'/static/{re.escape(archivo)}\?v=[0-9a-f]{{10}}"', html), archivo


def test_menu_del_celu_con_boton_de_cerrar(client, con_datos):  # noqa: F811
    html = _get(client, "/")
    aside = html.split('id="sidebar"', 1)[1].split("</aside>", 1)[0]
    assert 'id="bbCerrarMenu"' in aside and 'aria-label="Cerrar el menú"' in aside
    assert 'tabindex="-1"' in html.split('id="sidebar"', 1)[0][-200:] + html.split('id="sidebar"', 1)[1][:40]


@pytest.mark.parametrize("url,hooks", [
    ("/", ['id="lineChart"', 'id="barChart"', "function graphClickEvent", 'name="daterange"', 'name="daterangeform"',
           'id="lastOrderModal"', 'id="botStatusModal"', 'id="graficoDiario"']),
    ("/positions", ['id="orders"', "positions_datatable"]),
    ("/history", ['id="orders"', 'id="oldfiles"']),
    ("/projection", ['id="hype"', 'id="lineChart"']),
    ("/coins/BTCUSDT", ['id="orders"', 'id="positionsize"', 'id="positionvalue"', 'id="customsize"', 'id="customvalue"',
                        'id="customaway"', 'id="averageTarget"', 'id="candleChart15m"', 'id="modal1d"']),
    ("/rescate", ['id="rescateTope"', 'id="rescateTopeCheck"', 'id="rescateTopeAck"', 'id="rescateConfirmacion"',
                  'id="rescateForm"', 'id="c3Form"', 'id="c3Corte"', 'id="c3"', 'data-r="tope-texto"',
                  'data-r="tope-apagar"', 'data-c3="historial"', 'data-r="eventos"']),
])
def test_hooks_de_cada_pagina(client, con_datos, url, hooks):  # noqa: F811
    html = _get(client, url)
    for hook in hooks:
        assert hook in html, (url, hook)


# ---------------------------------------------------------------- números es-AR con el crudo intacto (restricción 2)


def test_home_kpis_en_es_ar_con_el_crudo_en_data_value(client, con_datos):  # noqa: F811
    html = _get(client, "/")
    assert 'data-value="5416.80">$5.416,80<' in html          # balance
    assert f'data-value="-2.04">{M}$2,04<' in html               # PnL no realizado: signo menos U+2212
    assert f'data-value="1233.81">+$1.233,81<' in html           # ganancia de hoy con signo y miles
    assert "is-gana" in html and "is-pierde" in html            # color solo para ganancia / pérdida
    assert "$-" not in html.split('class="bb-kpis"', 1)[1].split("</section>", 1)[0]  # formato viejo
    # Las cuentas de Jinja siguen sobre el crudo: % del balance del PnL no realizado = -2.04 / 5416.8.
    assert f"{M}0,04{NB}%" in html


def test_posiciones_ordenan_por_el_crudo(client, con_datos):  # noqa: F811
    html = _get(client, "/positions")
    filas = html.split('id="orders"', 1)[1].split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    # PnL negativo: texto es-AR, data-order crudo (DataTables ordena por número).
    assert f'data-order="-2.25">{M}$2,25<' in filas
    assert 'data-order="0.0022">0,0022<' in filas                # cantidad con su precisión
    assert 'data-order="82372.0">82.372,0<' in filas
    # Toda celda numérica lleva data-order (incluida la que no tiene dato: "-").
    for fila in re.findall(r"<tr>(.*?)</tr>", filas, re.S):
        celdas = re.findall(r"<td[^>]*>", fila)
        assert len(celdas) == 15 and all("data-order=" in c for c in celdas[2:])


def test_coin_conserva_el_valor_crudo_en_los_inputs_que_lee_el_js(client, con_datos):  # noqa: F811
    html = _get(client, "/coins/BTCUSDT")
    assert 'id="positionsize" value="0.0022"' in html and 'id="positionvalue" value="82372.0"' in html


# ---------------------------------------------------------------- tokens y dependencias


def test_tokens_oscuros_iguales_en_auto_y_en_elegido():
    css = (STATIC / "styles" / "bb.css").read_text(encoding="utf-8")
    auto = css.split(':root:not([data-theme="light"]) {', 1)[1].split("}", 1)[0]
    elegido = css.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    norm = lambda s: sorted(d.strip() for d in s.replace("\n", " ").split(";") if d.strip())  # noqa: E731
    assert norm(auto) == norm(elegido)
    assert len(norm(auto)) > 30


def test_foco_adentro_de_lo_que_recorta_y_encabezados_tactiles():
    css = (STATIC / "styles" / "bb.css").read_text(encoding="utf-8")
    # Selector de tema (overflow hidden) y tablas con scroll: el anillo del foco va hacia adentro para que se vea.
    assert ".bb .bb-segmento :focus-visible, .bb .bb-tabla-card :focus-visible, .bb .bb-tabla-scroll :focus-visible { outline-offset: -2px; }" in css
    assert "overflow: hidden" in css.split(".bb-segmento {", 1)[1].split("}", 1)[0]
    celu = css.split("@media (max-width: 767.98px) {\n  .bb-app", 1)[1]
    assert ".bb table.dataTable thead th { height: var(--bb-tap); }" in celu
    # Medidor del semáforo: sin color-mix() (iOS < 16.2 lo ignora y las zonas quedaban sin color).
    medidor = [linea for linea in css.splitlines() if linea.startswith(".bb-medidor .z-")]
    assert len(medidor) == 3 and not any("color-mix" in linea for linea in medidor)


def test_cdns_con_version_fija(client):  # noqa: F811
    for url in ("/", "/login"):
        html = client.get(url).get_data(as_text=True)
        externos = re.findall(r'(?:src|href)="(https://(?:cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com|cdn\.datatables\.net)[^"]+)"', html)
        assert externos
        for u in externos:
            assert "latest" not in u, u
            assert re.search(r"[@/]\d+\.\d+\.\d+", u), u


@pytest.mark.parametrize("url", ["/", "/positions", "/coins/BTCUSDT"])
def test_sin_recarga_automatica_de_la_pagina(client, con_datos, url):  # noqa: F811
    # La página no se recarga entera cada X segundos (molestaba y cerraba lo que estuvieras mirando).
    assert 'http-equiv="refresh"' not in _get(client, url)
