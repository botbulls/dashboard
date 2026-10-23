"""Filtros de formato es-AR (futuresboard.formato) y su gemelo de JS (static/js/formato.js).

Solo presentación: el valor crudo no cambia (signo, decimales, unidades) y lo que no es un número limpio vuelve
tal cual. La paridad Python/JS se prueba con node si está instalado.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
from decimal import Decimal

import pytest

from futuresboard import formato
from futuresboard.formato import num
from futuresboard.formato import pct
from futuresboard.formato import usd

M = "−"
NB = " "

CASOS = [
    (usd, ("5416.80",), {}, "$5.416,80"),
    (usd, ("-0.69",), {}, M + "$0,69"),
    (usd, ("-2.04",), {}, M + "$2,04"),
    (usd, ("5.63",), {"signo": True}, "+$5,63"),
    (usd, ("0.00",), {"signo": True}, "$0,00"),
    (usd, ("-0.00",), {}, M + "$0,00"),  # una pérdida redondeada a cero conserva el signo
    (num, ("1852.77",), {}, "1.852,77"),
    (num, ("12.50",), {}, "12,50"),
    (num, ("0.0022",), {}, "0,0022"),  # cantidad de moneda: precisión del crudo
    (num, ("81310.68",), {}, "81.310,68"),
    (num, (131.5,), {}, "131,5"),
    (pct, ("4.98",), {}, "4,98" + NB + "%"),
    (pct, ("-0.04",), {"signo": True}, M + "0,04" + NB + "%"),
    (pct, ((-2.04 / 5416.80) * 100,), {"dec": 2}, M + "0,04" + NB + "%"),  # mismo redondeo que "%.2f"
    (usd, ("19000.00",), {}, "$19.000,00"),
    (usd, ("1000",), {}, "$1.000"),
    (num, ("1234567.5",), {}, "1.234.567,5"),
    (num, ("7",), {"dec": 2}, "7,00"),
    (num, (0.1 + 0.2,), {"dec": 2}, "0,30"),
    (num, (-0.004,), {"dec": 2}, M + "0,00"),
    (usd, (-27.43,), {"dec": 2, "signo": True}, M + "$27,43"),
    (num, ("+500.00",), {"signo": True}, "+500,00"),  # variación del rescate con + explícito
    (num, ("+0.00",), {"signo": True}, "0,00"),
    (pct, ("+2.27",), {"signo": True}, "+2,27" + NB + "%"),
    (num, ("+500.00",), {}, "500,00"),
]


@pytest.mark.parametrize("f,args,kw,esperado", CASOS)
def test_formato_es_ar(f, args, kw, esperado):
    assert f(*args, **kw) == esperado


@pytest.mark.parametrize("crudo", [None, "-", "", "n/a", "5,416.80", "1e-05", float("nan"), True, "+", "++1", "+-1"])
def test_lo_que_no_es_un_numero_limpio_vuelve_tal_cual(crudo):
    for f in (num, usd, pct):
        assert f(crudo) is crudo


def test_decimal_y_signo_del_crudo():
    assert num(Decimal("-500.00")) == M + "500,00"
    assert usd(Decimal("0.005"), dec=2) == "$0,00"  # ROUND_HALF_EVEN, como format_dp
    assert usd("-0.004", dec=2) == M + "$0,00"


def test_no_redondea_sin_dec():
    assert num("0.00012345") == "0,00012345"
    assert usd("12345.678901") == "$12.345,678901"


@pytest.mark.parametrize("crudo,clase", [("5.63", "gana"), ("-0.69", "pierde"), ("-0.00", "pierde"),
                                         ("0.00", "cero"), (0, "cero"), ("-", ""), (None, ""), (-2.5, "pierde")])
def test_tendencia(crudo, clase):
    assert formato.tendencia(crudo) == clase


@pytest.mark.parametrize("mes,es", [("October", "Octubre"), ("may", "Mayo"), ("Diciembre", "Diciembre"), (None, None)])
def test_mes_es(mes, es):
    assert formato.mes_es(mes) == es


def test_filtros_registrados_en_la_app(tmp_path):
    from futuresboard.app import init_app
    from futuresboard.config import Config

    app = init_app(Config(CONFIG_DIR=tmp_path, DATABASE=tmp_path / "f.db", API_KEY="x", API_SECRET="x",
                          DISABLE_AUTO_SCRAPE=True))
    with app.app_context():
        t = app.jinja_env.from_string('<span data-value="{{ v }}">{{ v|usd(signo=True) }}</span>')
        assert t.render(v="-1234.5") == '<span data-value="-1234.5">' + M + "$1.234,5</span>"
        assert app.jinja_env.from_string("{{ 'October'|mes_es }}").render() == "Octubre"


JS = pathlib.Path(__file__).resolve().parents[1] / "src" / "futuresboard" / "static" / "js" / "formato.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_formato_js_coincide_con_python():
    # Valores que llegan a JS: números (Chart.js, JSON de la API) y textos limpios.
    casos = [("usd", "5416.80", {}), ("usd", "-0.69", {}), ("usd", 5.63, {"signo": True}), ("usd", "-0.00", {}),
             ("num", "1852.77", {}), ("num", "0.0022", {}), ("num", 131.5, {}), ("pct", "4.98", {}),
             ("pct", -0.04, {"signo": True}), ("usd", -27.43, {"dec": 2, "signo": True}), ("num", 0.0007, {}),
             ("usd", 57.37687, {"dec": 2}), ("num", "1234567.5", {}), ("usd", "-", {}), ("num", "n/a", {}),
             ("usd", 0, {"dec": 0}), ("usd", 5000, {"dec": 0}), ("usd", -100, {"dec": 0}),
             ("num", "+500.00", {"signo": True}), ("pct", "+0.00", {"signo": True})]
    script = ("global.window = {}; global.getComputedStyle = () => ({});"
              + JS.read_text(encoding="utf-8")
              + ";const c=" + json.dumps(casos) + ";"
              "console.log(JSON.stringify(c.map(([f,v,o])=>window.BB.fmt[f](v,o))));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    js = json.loads(out)
    py = [str(getattr(formato, f)(v, dec=o.get("dec"), signo=o.get("signo", False))) for f, v, o in casos]
    assert js == py


def _node(expr):
    script = ("global.window = {}; global.getComputedStyle = () => ({});" + JS.read_text(encoding="utf-8")
              + ";const f=window.BB.fmt;const eje=(vs)=>{const t=vs.map((v)=>({value:v}));"
              "return vs.map((v,i)=>f.usdEje(v,i,t));};console.log(JSON.stringify(" + expr + "));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
@pytest.mark.parametrize("ticks,esperado", [
    # Ganancias diarias entre 0,05 y 0,35: antes los 5 ticks decían "$0".
    ([0, 0.1, 0.2, 0.3, 0.4], ["$0,00", "$0,10", "$0,20", "$0,30", "$0,40"]),
    # Con −0,69 en los datos el eje ya no redondea a −$1 / $0 / $1.
    ([-1, -0.5, 0, 0.5, 1, 1.5], [M + "$1,00", M + "$0,50", "$0,00", "$0,50", "$1,00", "$1,50"]),
    # Balance que casi no se mueve: antes "$5.416, $5.416, $5.416, $5.417, $5.417".
    ([5416.2, 5416.4, 5416.6, 5416.8, 5417], ["$5.416,20", "$5.416,40", "$5.416,60", "$5.416,80", "$5.417,00"]),
    ([-0.4, -0.2, -2.7e-17, 0.2], [M + "$0,40", M + "$0,20", "$0,00", "$0,20"]),  # 0 con ruido: sin "−$0"
    ([0, 1000, 2000], ["$0", "$1.000", "$2.000"]),  # pasos enteros: sin decimales
    ([0, 0.025, 0.05], ["$0,000", "$0,025", "$0,050"]),
])
def test_eje_de_montos_con_los_decimales_que_distinguen_los_ticks(ticks, esperado):
    js = _node("eje(" + json.dumps(ticks) + ")")
    assert js == esperado
    assert len(set(js)) == len(js)


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_fechas_de_los_graficos_con_anio_si_el_rango_cruza_anios():
    assert _node('f.dias(["2026-10-22","2026-10-23"])') == ["22/10", "23/10"]
    assert _node('f.dias(["2025-10-23","2026-10-23"])') == ["23/10/25", "23/10/26"]
    assert _node('[f.fecha("2026-10-23"), f.fecha("x"), f.dias(["x"])]') == ["23/10/2026", "x", ["x"]]


def test_ningun_eje_de_grafico_redondea_a_cero_decimales():
    plantillas = pathlib.Path(__file__).resolve().parents[1] / "src" / "futuresboard" / "templates"
    for p in plantillas.glob("*.html"):
        s = p.read_text(encoding="utf-8")
        assert "dec: 0" not in s, p.name
        assert "etiquetaDia" not in s, p.name
    for nombre in ("home.html", "coin.html", "projection.html"):
        assert "callback: f.usdEje" in (plantillas / nombre).read_text(encoding="utf-8"), nombre
