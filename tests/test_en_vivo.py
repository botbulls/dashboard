"""Números en vivo sin recargar: /api/vivo/* (futuresboard/vivo.py) y los hooks que actualiza static/js/en_vivo.js.

Lo central: lo que devuelve el JSON es exactamente lo que pinta la página (crudo en data-value, texto es-AR, clase de
color, HTML de los bloques y filas de las tablas), en las dos direcciones: todo hook de la página tiene su clave en el
JSON y toda clave del JSON tiene dónde aplicarse.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import time
from html.parser import HTMLParser
from unittest import mock

import pytest
import requests

from futuresboard import blueprint as bp
from futuresboard.config import state_dir

from test_bot_control import app  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_presentacion import con_datos  # noqa: F401

M = "−"
STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "futuresboard" / "static"
URLS = {"/": "/api/vivo/inicio", "/positions": "/api/vivo/posiciones", "/coins/BTCUSDT": "/api/vivo/moneda/BTCUSDT"}
# Claves del JSON que son solo para quien consuma la API: en la página el % va dentro de un bloque (con su ícono).
SOLO_API = {"hoy_pct", "semana_pct", "mes_pct", "total_pct"}


class _Resp:
    def __init__(self, data):
        self._data = data

    def __bool__(self):
        return True

    def json(self):
        return self._data


PRECIOS = {"BTCUSDT": "81954.1", "ETHUSDT": "3907.15"}


def _binance(url, *a, **kw):
    if "premiumIndex?symbol=" in url:
        simbolo = url.split("symbol=", 1)[1]
        return _Resp({"symbol": simbolo, "markPrice": PRECIOS[simbolo]})
    if "premiumIndex" in url:
        return _Resp([{"symbol": s, "markPrice": p} for s, p in PRECIOS.items()])
    if "klines" in url:
        return _Resp([])
    raise AssertionError(f"llamada inesperada: {url}")


def _sin_binance(url, *a, **kw):
    """Binance caído: premiumIndex (y las velas) fallan."""
    raise requests.ConnectionError("Binance no responde")


def _get(client, url, binance=_binance):  # noqa: F811
    with mock.patch.object(requests, "get", binance):
        resp = client.get(url)
    assert resp.status_code == 200, (url, resp.status_code)
    return resp


# ---------------------------------------------------------------- lector de hooks del HTML

VACIOS = {"input", "br", "img", "meta", "link", "hr", "source", "wbr", "col", "area", "base", "embed", "track"}


class _Hooks(HTMLParser):
    """Junta [data-vivo] (atributos y texto), [data-vivo-title] y el HTML fuente de cada [data-vivo-bloque]."""

    def __init__(self, fuente):
        super().__init__(convert_charrefs=True)
        self.fuente = fuente
        self.lineas = [0]
        for linea in fuente.splitlines(True):
            self.lineas.append(self.lineas[-1] + len(linea))
        self.pila = []  # (tag, attrs, inicio, juntar_texto)
        self.valores = []  # (clave, attrs, texto)
        self.titulos = []
        self.bloques = {}
        self.tbody = {}

    def _pos(self):
        linea, col = self.getpos()
        return self.lineas[linea - 1] + col

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-vivo-title" in a:
            self.titulos.append((a["data-vivo-title"], a))
        if tag in VACIOS:
            if "data-vivo" in a:
                self.valores.append((a["data-vivo"], a, None))
            return
        self.pila.append([tag, a, self._pos(), [] if "data-vivo" in a else None])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        for item in self.pila:
            if item[3] is not None:
                item[3].append(data)

    def handle_endtag(self, tag):
        while self.pila:
            t, a, inicio, texto = self.pila.pop()
            if t == tag:
                break
        else:
            return
        fin = self.fuente.index(">", self._pos()) + 1
        if texto is not None:
            self.valores.append((a["data-vivo"], a, "".join(texto)))
        if "data-vivo-bloque" in a:
            self.bloques[a["data-vivo-bloque"]] = self.fuente[inicio:fin]
        if t == "table" and "id" in a:
            html = self.fuente[inicio:fin]
            self.tbody[a["id"]] = html.split("<tbody>", 1)[1].rsplit("</tbody>", 1)[0]


def _hooks(html):
    h = _Hooks(html)
    h.feed(html)
    h.close()
    return h


def _norm(html):
    return re.sub(r"\s+", " ", re.sub(r">\s+<", "><", html)).strip()


# ---------------------------------------------------------------- auth, CSRF, forma

@pytest.mark.parametrize("url", list(URLS.values()))
def test_sin_sesion_redirige_al_login_sin_tocar_binance(app, url):  # noqa: F811
    boom = mock.Mock(side_effect=AssertionError("no debe llamar a Binance sin sesión"))
    with mock.patch.object(requests, "get", boom):
        resp = app.test_client().get(url)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert not boom.called


@pytest.mark.parametrize("url", list(URLS.values()))
def test_get_sin_csrf_y_forma_del_json(client, con_datos, url):  # noqa: F811
    # GET de lectura: no pide X-CSRF-Token (client no lo manda) ni body.
    resp = _get(client, url)
    assert resp.mimetype == "application/json" and resp.headers["Cache-Control"] == "no-store"
    d = resp.get_json()
    assert d["ok"] is True and d["pagina"] in ("inicio", "posiciones", "moneda")
    assert set(d["datos"]) == {"fuente", "at", "edad_segundos", "intervalo_segundos"}
    for clave, v in d["valores"].items():
        assert set(v) == {"crudo", "texto", "tono"}, clave
        assert all(isinstance(x, str) for x in v.values()), clave
        assert v["tono"] in ("", "gana", "pierde", "cero"), clave
    assert all(isinstance(h, str) and h.strip().startswith("<") for h in d["bloques"].values())
    for k in ("monedas_activas", "monedas_inactivas", "nav_posiciones"):
        assert k in d["bloques"]


def test_inicio_trae_graficos_bot_y_fuentes(client, con_datos):  # noqa: F811
    d = _get(client, "/api/vivo/inicio").get_json()
    assert d["graficos"]["por_moneda"] == {"monedas": ["BTCUSDT", "ETHUSDT"], "valores": [1234.5, -0.69], "alto": 240}
    assert d["graficos"]["diario"]["valores"] == [1233.81]
    assert d["graficos"]["balance"]["valores"] == [5416.8]
    assert d["bot"]["http"] == 200 and d["bot"]["datos"]["enabled"] is False  # panel sin Docker en los tests
    assert d["fuentes"]["balance"]["origen"] == "base" and d["fuentes"]["upnl"]["origen"] == "base"
    hoy = dt.date.today()
    assert d["periodo"] == {"start": (hoy - dt.timedelta(days=hoy.weekday())).isoformat(), "end": hoy.isoformat()}


def test_inicio_con_periodo_y_fechas_invalidas(client, con_datos):  # noqa: F811
    d = _get(client, "/api/vivo/inicio?start=2020-01-01&end=2030-12-31").get_json()
    assert d["periodo"] == {"start": "2020-01-01", "end": "2030-12-31"}
    assert d["valores"]["periodo"]["texto"] == "del 2020-01-01 al 2030-12-31"
    assert d["valores"]["hoy"]["crudo"] == "1233.81"
    for q in ("?start=2020-01-01", "?end=2020-01-01", "?start=ayer&end=hoy"):
        resp = client.get("/api/vivo/inicio" + q)
        assert resp.status_code == 400 and resp.get_json()["ok"] is False, q


def test_moneda_inexistente_404_sin_pedir_a_binance(client, con_datos):  # noqa: F811
    boom = mock.Mock(side_effect=AssertionError("no debe llamar a Binance con una moneda que no existe"))
    with mock.patch.object(requests, "get", boom):
        resp = client.get("/api/vivo/moneda/NOEXISTE")
    assert resp.status_code == 404 and resp.get_json() == {"ok": False, "error": "Moneda inexistente."}
    assert not boom.called


def test_moneda_no_pide_velas(client, con_datos):  # noqa: F811
    urls = []

    def espia(url, *a, **kw):
        urls.append(url)
        return _binance(url)

    with mock.patch.object(requests, "get", espia):
        assert client.get("/api/vivo/moneda/BTCUSDT").status_code == 200
    assert urls and all("premiumIndex?symbol=BTCUSDT" in u for u in urls) and not any("klines" in u for u in urls)


def test_error_del_bot_no_corta_los_numeros(client, con_datos, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bp, "bot_status_payload", mock.Mock(side_effect=RuntimeError("docker caído")))
    d = _get(client, "/api/vivo/inicio").get_json()
    assert d["ok"] is True and d["bot"]["ok"] is False and d["valores"]["balance"]["crudo"] == "5416.80"


def test_docker_colgado_no_frena_los_numeros(client, con_datos, monkeypatch):  # noqa: F811
    # Con el docker-proxy colgado, /api/vivo/inicio corta el inspect a los 3 s (el JS corta a los 10 s): los números
    # llegan igual y el bot queda sin datos. /api/bot/status sigue con los 10 s de siempre.
    from futuresboard import bot_control
    from futuresboard import vivo

    monkeypatch.setenv(bot_control.ENV_DOCKER_URL, "http://docker-proxy:2375")
    timeouts = []

    def colgado(self, method, url, **kw):
        timeouts.append(kw.get("timeout"))
        raise requests.ReadTimeout("colgado")

    monkeypatch.setattr(requests.Session, "request", colgado)
    d = _get(client, "/api/vivo/inicio").get_json()
    assert d["ok"] is True and d["valores"]["balance"]["crudo"] == "5416.80"
    assert d["bot"]["ok"] is False and timeouts == [vivo.BOT_DOCKER_TIMEOUT_S] and vivo.BOT_DOCKER_TIMEOUT_S < 10
    client.get("/api/bot/status")
    assert timeouts[-1] == bot_control.HTTP_TIMEOUT_SECONDS


def test_moneda_con_la_posicion_cerrada_manda_la_calculadora_en_cero(client, con_datos, app):  # noqa: F811
    html = _get(client, "/coins/BTCUSDT").get_data(as_text=True)
    assert 'data-vivo="pos_cantidad_0"' in html  # la página cargó con la posición
    with sqlite3.connect(str(app.config["DATABASE"])) as con:
        con.execute("DELETE FROM positions WHERE symbol = 'BTCUSDT'")
    v = _get(client, "/api/vivo/moneda/BTCUSDT").get_json()["valores"]
    assert v["pos_cantidad_0"]["crudo"] == "0" and v["pos_precio_0"]["crudo"] == "0"
    # La calculadora recalcula el promedio cuando en_vivo.js aplica los valores nuevos.
    assert "window.addEventListener('fb-vivo-aplicado'" in html


def test_pagina_con_el_hilo_del_web_usa_su_intervalo(client, con_datos, app):  # noqa: F811
    # Imagen nueva con el compose viejo (sin servicio scraper ni latido): el indicador usa los 300 s del hilo.
    from futuresboard import scraper

    app.config["DISABLE_AUTO_SCRAPE"] = False  # el hilo no arranca: init_app ya corrió
    app.config["AUTO_SCRAPE_INTERVAL"] = 300
    scraper.scrape_state_path(app.config["DATABASE"]).write_text(json.dumps({"last_success_at": time.time() - 200}))
    html = _get(client, "/").get_data(as_text=True)
    assert re.search(r'data-vivo-edad="20[01]" data-vivo-intervalo="300"', html)
    assert _get(client, "/api/vivo/inicio").get_json()["datos"]["intervalo_segundos"] == 300


def test_base_bloqueada_responde_503_json(client, con_datos, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bp, "_datos_inicio", mock.Mock(side_effect=sqlite3.OperationalError("database is locked")))
    resp = client.get("/api/vivo/inicio")
    assert resp.status_code == 503 and resp.mimetype == "application/json" and resp.get_json()["ok"] is False


# ---------------------------------------------------------------- crudo == lo que muestra el template

@pytest.mark.parametrize("binance", [_binance, _sin_binance], ids=["con_precio", "sin_binance"])
@pytest.mark.parametrize("pagina", list(URLS))
def test_json_igual_a_lo_que_pinta_la_pagina_en_las_dos_direcciones(client, con_datos, pagina, binance):  # noqa: F811
    html = _get(client, pagina, binance).get_data(as_text=True)
    d = _get(client, URLS[pagina], binance).get_json()
    h = _hooks(html)

    # Página -> JSON: cada número del HTML tiene su valor, con el mismo crudo, texto y color.
    assert h.valores, pagina
    for clave, attrs, texto in h.valores:
        assert clave in d["valores"], (pagina, clave)
        v = d["valores"][clave]
        if texto is None:  # <input>: el crudo en value
            assert attrs.get("value") == v["crudo"], (pagina, clave)
            continue
        assert texto == v["texto"], (pagina, clave, texto, v)
        if "data-value" in attrs:
            assert attrs["data-value"] == v["crudo"], (pagina, clave)
        if "data-vivo-tono" in attrs:
            assert f"is-{v['tono'] or 'cero'}" in attrs["class"].split(), (pagina, clave)
    for clave, attrs in h.titulos:
        assert attrs["title"] == d["valores"][clave]["texto"]
    # JSON -> página: cada valor tiene dónde aplicarse (salvo los que son solo de la API).
    en_pagina = {c for c, _, _ in h.valores} | {c for c, _ in h.titulos}
    assert set(d["valores"]) - en_pagina - SOLO_API == set(), pagina

    # Bloques: el mismo HTML (mismo macro, mismos argumentos), y ninguno de más ni de menos.
    assert set(h.bloques) == set(d["bloques"]), pagina
    for clave, fragmento in d["bloques"].items():
        assert _norm(fragmento) == _norm(h.bloques[clave]), (pagina, clave)

    # Tablas: las filas que recibe la DataTable son las del HTML.
    for tabla, filas in d.get("tablas", {}).items():
        assert _norm(filas) == _norm(h.tbody[tabla]), (pagina, tabla)
        assert re.findall(r'data-fila="([^"]+)"', filas), (pagina, tabla)


# ---------------------------------------------------------------- sin precio de Binance

RAYA = "—"
AVISO = "sin precio de Binance"


def _crudos(html):
    return re.findall(r'data-(?:value|order)="([^"]*)"', html)


def _filas(html):
    """Celdas (data-order, texto) de cada <tr data-fila> de la tabla de Posiciones."""
    filas = {}
    for clave, cuerpo in re.findall(r'<tr data-fila="([^"]+)">(.*?)</tr>', html, re.S):
        filas[clave] = [(o, re.sub(r"<[^>]+>", "", t).strip())
                        for o, t in re.findall(r'<td class="bb-n[^"]*" data-order="([^"]*)">(.*?)</td>', cuerpo, re.S)]
    return filas


def _sin_eth(url, *a, **kw):
    """premiumIndex responde pero sin ETHUSDT (par nuevo o deslistado)."""
    if url.endswith("/fapi/v1/premiumIndex"):
        return _Resp([{"symbol": "BTCUSDT", "markPrice": PRECIOS["BTCUSDT"]}])
    return _binance(url, *a, **kw)


@pytest.mark.parametrize("url", ["/positions", "/api/vivo/posiciones", "/coins/BTCUSDT", "/api/vivo/moneda/BTCUSDT"])
def test_sin_precio_de_binance_responde_200_sin_none_ni_nan(client, con_datos, url):  # noqa: F811
    # Antes /positions y /api/vivo/posiciones daban 500 (markprices[coin] indefinido en el template).
    resp = _get(client, url, _sin_binance)
    if resp.is_json:
        d = resp.get_json()
        texto = "".join(list(d["bloques"].values()) + list(d.get("tablas", {}).values()))
        crudos = _crudos(texto) + [v["crudo"] for v in d["valores"].values()]
    else:
        texto = resp.get_data(as_text=True)
        crudos = _crudos(texto)
    assert crudos, url
    assert not {c.lower() for c in crudos} & {"none", "nan", "inf", "-inf", "undefined"}, url
    assert AVISO in texto, url


def test_posiciones_sin_precio_muestra_raya_y_crudo_vacio(client, con_datos):  # noqa: F811
    html = _get(client, "/positions", _sin_binance).get_data(as_text=True)
    filas = _filas(html)
    assert set(filas) == {"BTCUSDT-LONG", "ETHUSDT-LONG"}
    for celdas in filas.values():
        assert len(celdas) == 13
        assert celdas[3][0] in ("-2.25", "0.21")  # el PnL no realizado sale de la base: no depende del precio
        assert celdas[4] == ("", RAYA)  # precio actual
        # Sin compras: "-" de siempre (no depende del precio). Con venta: distancia y % en "—" con el crudo vacío.
        assert celdas[5:9] == [("0", "0"), ("-", "-"), ("-", "-"), ("-", "")]
        assert celdas[9] == ("1", "1") and celdas[10][0] in ("82610.0", "3949.8")  # próxima venta: sale de la base
        assert celdas[11:13] == [("", RAYA), ("", RAYA)]
    assert AVISO in html.split('data-vivo-bloque="aviso_precio"', 1)[1].split("</p>", 1)[0]
    # La DataTable sigue ordenando por data-order en las columnas numéricas.
    assert "window.BB.columnasDataOrder(2, 14)" in html

    d = _get(client, "/api/vivo/posiciones", _sin_binance).get_json()
    assert AVISO in d["bloques"]["aviso_precio"] and _filas(d["tablas"]["orders"]) == filas


def test_posiciones_con_precio_no_avisa(client, con_datos):  # noqa: F811
    html = _get(client, "/positions").get_data(as_text=True)
    assert AVISO not in html and RAYA not in "".join(t for c in _filas(html).values() for _, t in c)
    assert _filas(html)["BTCUSDT-LONG"][4] == ("81954.1", "81.954,1")
    assert AVISO not in _get(client, "/api/vivo/posiciones").get_json()["bloques"]["aviso_precio"]


def test_posiciones_con_un_par_sin_precio(client, con_datos):  # noqa: F811
    # premiumIndex responde sin uno de los pares: ese par va en "—", el otro con su precio.
    html = _get(client, "/positions", _sin_eth).get_data(as_text=True)
    filas = _filas(html)
    assert filas["BTCUSDT-LONG"][4] == ("81954.1", "81.954,1") and filas["BTCUSDT-LONG"][11][1] != RAYA
    assert filas["ETHUSDT-LONG"][4] == ("", RAYA) and filas["ETHUSDT-LONG"][11:13] == [("", RAYA), ("", RAYA)]
    assert AVISO in html


def test_moneda_sin_precio_muestra_raya_y_avisa(client, con_datos):  # noqa: F811
    html = _get(client, "/coins/BTCUSDT", _sin_binance).get_data(as_text=True)
    assert html.count('data-vivo="precio" data-value="">' + RAYA + "<") == 4
    for k in range(3):
        assert f'data-vivo="objetivo_{k}" data-value="">{RAYA}<' in html
    bajada = html.split('data-vivo-bloque="bajada_moneda"', 1)[1].split("</p>", 1)[0]
    assert AVISO in bajada and "precio actual" not in bajada
    v = _get(client, "/api/vivo/moneda/BTCUSDT", _sin_binance).get_json()["valores"]
    for k in ("precio", "objetivo_0", "objetivo_1", "objetivo_2"):
        assert (v[k]["crudo"], v[k]["texto"]) == ("", RAYA), k


def test_moneda_sin_posicion_con_binance_ok_no_avisa(client, con_datos, app):  # noqa: F811
    # Sin posición, average_down_target(allpositions[0]...) tiraba IndexError en el mismo try que el pedido y el precio
    # quedaba en "-": con el aviso, eso diría "sin precio de Binance" con Binance respondiendo bien.
    with sqlite3.connect(str(app.config["DATABASE"])) as con:
        con.execute("DELETE FROM positions WHERE symbol = 'BTCUSDT'")
    html = _get(client, "/coins/BTCUSDT").get_data(as_text=True)
    bajada = html.split('data-vivo-bloque="bajada_moneda"', 1)[1].split("</p>", 1)[0]
    assert AVISO not in bajada and 'precio actual <span class="bb-num" data-value="81954.1">81.954,1</span>' in bajada
    v = _get(client, "/api/vivo/moneda/BTCUSDT").get_json()
    assert v["valores"]["precio"]["crudo"] == "81954.1" and AVISO not in v["bloques"]["bajada_moneda"]


def _calc_target(html, precio):
    """Corre calcTarget() de la página con node, con un DOM mínimo y el data-value del precio dado."""
    fuente = html.split("function calcTarget()", 1)[1].split("function averageDown()", 1)[0]
    script = (
        "var els = {positionsize: {value: '0.0022'}, positionvalue: {value: '82372.0'}, customaway: {value: '1'},"
        " averageTarget: {value: 'previo'}};"
        "var document = {getElementById: function (id) { return els[id]; },"
        " querySelector: function () { return {getAttribute: function () { return " + json.dumps(precio) + "; }}; }};"
        "function calcTarget()" + fuente + "calcTarget(); console.log(String(els.averageTarget.value));"
    )
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout.strip()


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_calculadora_sin_precio_no_muestra_nan(client, con_datos):  # noqa: F811
    html = _get(client, "/coins/BTCUSDT", _sin_binance).get_data(as_text=True)
    assert _calc_target(html, "") == ""  # antes: NaN en "Cantidad para esa distancia"
    assert re.fullmatch(r"-?\d+", _calc_target(html, "81954.1"))


def test_dashboard_con_fechas_pide_su_periodo(client, con_datos):  # noqa: F811
    html = _get(client, "/dashboard/2020-01-01/2030-12-31").get_data(as_text=True)
    assert 'data-vivo-url="/api/vivo/inicio?start=2020-01-01&amp;end=2030-12-31"' in html
    d = _get(client, "/api/vivo/inicio?start=2020-01-01&end=2030-12-31").get_json()
    h = _hooks(html)
    assert set(h.bloques) == set(d["bloques"])
    for clave, fragmento in d["bloques"].items():
        assert _norm(fragmento) == _norm(h.bloques[clave]), clave


def test_valores_cambian_con_la_base(client, con_datos):  # noqa: F811
    antes = _get(client, "/api/vivo/inicio").get_json()
    with sqlite3.connect(str(con_datos.config["DATABASE"])) as con:
        con.execute("UPDATE account SET totalWalletBalance = 6000.5 WHERE AID = 1")
        con.execute("UPDATE positions SET unrealizedProfit = 3.5 WHERE symbol = 'BTCUSDT'")
        con.execute("INSERT INTO income (tranId, symbol, incomeType, income, asset, info, time, tradeId) "
                    "VALUES ('9', 'ETHUSDT', 'REALIZED_PNL', 10, 'USDT', '', ?, 0)", (int(time.time() * 1000) - 500,))
    despues = _get(client, "/api/vivo/inicio").get_json()
    assert antes["valores"]["balance"]["texto"] == "$5.416,80"
    assert despues["valores"]["balance"] == {"crudo": "6000.50", "texto": "$6.000,50", "tono": ""}
    assert despues["valores"]["upnl"] == {"crudo": "3.71", "texto": "+$3,71", "tono": ""}
    assert despues["valores"]["hoy"]["texto"] == "+$1.243,81"
    assert 'is-gana' in despues["bloques"]["upnl_pct"] and 'is-pierde' in antes["bloques"]["upnl_pct"]


# ---------------------------------------------------------------- balance y PnL: lectura fresca del rescate

def _estado_rescate(app, modo, edad, **ultimo):  # noqa: F811
    ahora = time.time()
    ts = dt.datetime.fromtimestamp(ahora - edad, dt.timezone.utc).isoformat(timespec="seconds")
    latido = dt.datetime.fromtimestamp(ahora, dt.timezone.utc).isoformat(timespec="seconds")
    estado = {"prestamo": {"real": "0.00", "dry_run": "1000.00" if modo == "dry_run" else "0.00"},
              "servicio": {"modo": modo, "heartbeat": latido, "intervalo": 15},
              "ultimo": {"ts": ts, "error": None, "simulado": modo == "dry_run", **ultimo}}
    (state_dir(app.config) / "rescate_state.json").write_text(json.dumps(estado), encoding="utf-8")


def test_dry_run_con_prestamo_no_infla_el_balance(client, con_datos):  # noqa: F811
    # En dry_run wallet_balance de la lectura trae el préstamo simulado (1000): la wallet real es margin - upnl.
    _estado_rescate(con_datos, "dry_run", 5, wallet_balance="7419.76", margin_balance="6417.72", upnl="-2.04")
    d = _get(client, "/api/vivo/inicio").get_json()
    assert d["valores"]["balance"]["crudo"] == "6419.76"
    assert d["valores"]["upnl"]["crudo"] == "-2.04"
    assert d["fuentes"]["balance"]["origen"] == "rescate" and d["fuentes"]["balance"]["derivado"] is True
    assert d["valores"]["fuente"]["texto"] == "Balance y PnL: lectura del rescate"
    # La página dice lo mismo que el JSON (sin salto en el primer poll).
    html = _get(client, "/").get_data(as_text=True)
    assert 'data-vivo="balance" class="bb-kpi__valor bb-num" data-value="6419.76">$6.419,76<' in html
    assert ">Balance y PnL: lectura del rescate<" in html


def test_real_usa_wallet_balance_y_lectura_vieja_o_con_error_usa_la_base(client, con_datos):  # noqa: F811
    _estado_rescate(con_datos, "real", 3, wallet_balance="5420.00", margin_balance="5425.00", upnl="5.00")
    d = _get(client, "/api/vivo/inicio").get_json()
    assert (d["valores"]["balance"]["crudo"], d["valores"]["upnl"]["crudo"]) == ("5420.00", "5.00")
    assert d["fuentes"]["balance"].get("derivado") is False

    _estado_rescate(con_datos, "real", 75, wallet_balance="5420.00", upnl="5.00")  # más de 60 s
    d = _get(client, "/api/vivo/inicio").get_json()
    assert (d["valores"]["balance"]["crudo"], d["valores"]["upnl"]["crudo"]) == ("5416.80", "-2.04")
    assert d["valores"]["fuente"]["texto"] == "Balance y PnL: base del scraper"

    _estado_rescate(con_datos, "real", 3, wallet_balance="5420.00", upnl="5.00", error="timeout")
    assert _get(client, "/api/vivo/inicio").get_json()["fuentes"]["balance"]["origen"] == "base"

    _estado_rescate(con_datos, "off", 3, wallet_balance="5420.00", upnl="5.00")
    assert _get(client, "/api/vivo/inicio").get_json()["fuentes"]["upnl"]["origen"] == "base"


def test_lectura_sin_wallet_usa_solo_el_upnl(client, con_datos):  # noqa: F811
    _estado_rescate(con_datos, "real", 3, upnl="-7.10")
    d = _get(client, "/api/vivo/inicio").get_json()
    assert (d["valores"]["balance"]["crudo"], d["valores"]["upnl"]["crudo"]) == ("5416.80", "-7.10")
    assert d["valores"]["fuente"]["texto"] == "Balance: base del scraper · PnL: lectura del rescate"


# ---------------------------------------------------------------- hooks del JS en las páginas

@pytest.mark.parametrize("pagina", list(URLS))
def test_paginas_en_vivo_tienen_el_indicador_y_el_script(client, con_datos, pagina):  # noqa: F811
    html = _get(client, pagina).get_data(as_text=True)
    assert f'id="bbVivo" class="bb-vivo" data-vivo-url="{URLS[pagina]}"' in html
    assert "data-vivo-reloj" in html and "data-vivo-datos" in html
    # Un solo aviso de estado (role="status") para conexión / sesión; el reloj que cambia cada segundo no es live.
    assert html.count("data-vivo-aviso") == 1
    assert re.search(r'<div class="bb-vivo__aviso" role="status" data-vivo-aviso></div>', html)
    vivo = html.split('id="bbVivo"', 1)[1].split("</div>", 1)[0]
    assert "aria-live" not in vivo and "role=" not in vivo
    assert re.search(r'<script src="/static/js/en_vivo\.js\?v=[0-9a-f]{10}" defer></script>', html)


@pytest.mark.parametrize("url", ["/history", "/projection", "/rescate", "/settings"])
def test_las_demas_paginas_no_piden_en_vivo(client, con_datos, url):  # noqa: F811
    html = _get(client, url).get_data(as_text=True)
    assert 'id="bbVivo"' not in html


def test_edad_de_los_datos_del_latido_en_la_pagina(client, con_datos):  # noqa: F811
    from futuresboard import scraper

    scraper.latido_path(con_datos.config["DATABASE"]).write_text(
        json.dumps({"at": time.time() - 42, "intervalo": 60, "vuelta": 1}), encoding="utf-8")
    html = _get(client, "/positions").get_data(as_text=True)
    assert re.search(r'data-vivo-edad="4[23]" data-vivo-intervalo="60"', html)
    assert re.search(r"Datos de hace 4[23] s", html)
    d = _get(client, "/api/vivo/posiciones").get_json()
    assert d["datos"]["fuente"] == "latido" and d["datos"]["intervalo_segundos"] == 60


def test_js_en_vivo_contrato():
    js = (STATIC / "js" / "en_vivo.js").read_text(encoding="utf-8")
    # setTimeout encadenado (no setInterval para pedir), pausa con la pestaña oculta, sesión vencida por redirect.
    assert "setTimeout(tick" in js and "setInterval(tick" not in js
    assert "visibilitychange" in js and "document.hidden" in js
    assert "redirect: 'manual'" in js and "opaqueredirect" in js
    assert "location.protocol === 'file:'" in js  # las capturas abren el HTML guardado: no pide nada
    assert ".modal.show" in js and "[data-bot-controles]" in js and "menu_admin" not in js
    assert "prefers-reduced-motion" in js
    # Una respuesta vieja no pisa a una más nueva; la calculadora recalcula; "hace 89 s" -> "hace 2 min" no es otro
    # aviso; al reemplazar un bloque entero, los avisos que ya estaban no se vuelven a anunciar.
    assert "mio !== pedido" in js and "fb-vivo-aplicado" in js
    assert "(\\s*(s|min|h|d)\\b)?" in js and "silenciarRepetidos(viejo, nuevo);" in js
    css = (STATIC / "styles" / "bb.css").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce) { .bb-vivo-cambio { animation: none; } }" in css
    assert ".bb-vivo-envoltura { display: contents; }" in css


def test_home_actualiza_graficos_sin_recrearlos(client, con_datos):  # noqa: F811
    html = _get(client, "/").get_data(as_text=True)
    assert "window.BB.vivoGraficos = function (g)" in html and "c.update('none')" in html
    # El estado del bot (franja y control rápido) llega como bloques de /api/vivo/inicio: Inicio no tiene un poll propio
    # contra Docker (spec bot-estrategia § 2.2).
    assert "setInterval(updateBotStatus" not in html and "updateBotStatus" not in html
    assert 'data-vivo-bloque="franja_bot"' in html and 'data-vivo-bloque="control_bot"' in html
