"""Tarjetas del rescate en el dashboard principal (``/`` y ``/dashboard/<a>/<b>``).

La página solo lee lo que persiste el servicio rescate: nunca llama a Binance desde la request.
"""
from __future__ import annotations

import json
import pathlib
import re
import time
from decimal import Decimal
from unittest import mock

import pytest
import requests

from futuresboard import rescate
from futuresboard import resumen_rescate

from test_bot_control import app  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_rescate import FakeRescateBinance
from test_rescate import Harness


def _data_dir(app):  # noqa: F811
    return pathlib.Path(app.config["DATABASE"]).parent


@pytest.fixture
def hd(app, tmp_path):  # noqa: F811
    """Harness del servicio rescate sobre el directorio de datos del dashboard, con el reloj en ahora."""
    harness = Harness(_data_dir(app), tmp_path / "rescate" / "loan_state.json", FakeRescateBinance())
    harness.clock.t = time.time()
    harness.fake.now_ms = int(harness.clock.t * 1000)
    return harness


def _tarjeta(html, nombre):
    m = re.search(r'data-tarjeta="%s".*?</div>\s*<div class="card-footer' % nombre, html, re.S)
    assert m, f"falta la tarjeta {nombre}"
    return m.group(0)


def _home(client):  # noqa: F811
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_tarjetas_con_rescate_habilitado_en_real(client, hd):  # noqa: F811
    hd.configurar(tope=30, umbral=8)
    hd.fake.margin, hd.fake.maint, hd.fake.spot = Decimal("2000"), Decimal("200"), Decimal("20000")
    svc = hd.service("real")
    svc.tick()  # base 22000 (futuros + spot)
    hd.fake.margin = Decimal("1500")  # capital 21500: -500 vs base; ratio 7,5 < 8 => inyecta 1000
    hd.avanzar(15)
    svc.tick()
    hd.avanzar(15)
    svc.tick()  # lee después de la inyección: spot 19000, margen 2500 (ratio 12,5)
    html = _home(client)
    assert 'id="rescateTarjetas"' in html
    billetera = _tarjeta(html, "billetera")
    assert "$19000.00" in billetera and "USDT libre en spot" in billetera and "hace" in billetera
    assert "$1000.00" in _tarjeta(html, "prestamo")
    capital = _tarjeta(html, "capital")
    assert "$21500.00" in capital and "-500.00 (-2.27%) vs base $22000.00" in capital
    ratio = _tarjeta(html, "ratio")
    assert 'data-semaforo="verde"' in html and "12.50" in ratio and "rojo debajo de 8" in ratio
    estado = _tarjeta(html, "estado")
    assert "Real" in estado and "habilitado" in estado and "tope armado (piso $15400.00)" in estado
    assert "el servicio no late" not in html and "simulados" not in html


def test_tarjetas_con_rescate_deshabilitado_y_sin_servicio(client):  # noqa: F811
    html = _home(client)
    assert 'id="rescateTarjetas"' in html
    assert "sin datos" in _tarjeta(html, "billetera")
    assert "Sin datos del rescate" in html and "FUTURESBOARD_RESCATE_MODE=dry_run" in html
    assert "deshabilitado" in _tarjeta(html, "estado")


def test_tarjetas_en_dry_run_aclaran_que_el_prestamo_es_simulado(client, hd):  # noqa: F811
    hd.configurar(tope=30)
    hd.fake.margin, hd.fake.maint = Decimal("1500"), Decimal("200")  # ratio 7,5: inyecta (simulado)
    svc = hd.service("dry_run")
    svc.tick()
    hd.avanzar(15)
    svc.tick()
    html = _home(client)
    assert "simulado (dry_run)" in _tarjeta(html, "prestamo")
    assert "dry_run: los montos de préstamo son simulados" in html
    billetera = _tarjeta(html, "billetera")
    assert "$20000.00" in billetera and "simulado: $19000.00" in billetera  # saldo real + efectivo simulado
    assert "Simulación" in _tarjeta(html, "estado")


def test_tarjetas_avisan_si_el_servicio_no_late(client, hd):  # noqa: F811
    hd.configurar()
    hd.clock.t = time.time() - 3600
    hd.service("real").tick()
    html = _home(client)
    assert "el servicio no late" in html
    assert "nadie está vigilando el tope" in html
    assert "hace 60 min" in _tarjeta(html, "billetera")


def test_tarjetas_con_spot_ilegible_usan_el_ultimo_saldo_leido(client, hd):  # noqa: F811
    hd.configurar()
    svc = hd.service("real")
    svc.tick()
    hd.fake.read_fail.add("/api/v3/account")
    hd.avanzar(15)
    svc.tick()
    html = _home(client)
    billetera = _tarjeta(html, "billetera")
    assert "$20000.00" in billetera and "último saldo leído" in billetera
    assert "sin lectura" in _tarjeta(html, "capital")
    assert "Última lectura con error" in html


def test_tarjetas_con_spot_ilegible_sin_saldo_previo(client, hd):  # noqa: F811
    hd.configurar()
    hd.fake.read_fail.add("/api/v3/account")
    hd.service("real").tick()
    html = _home(client)
    assert "sin datos" in _tarjeta(html, "billetera")
    assert 'data-semaforo="gris"' in html


def test_tarjetas_con_estado_ilegible_no_rompen_la_pagina(app, client):  # noqa: F811
    (_data_dir(app) / rescate.STATE_FILE_NAME).write_text("{no es json")
    html = _home(client)
    assert "Estado del rescate ilegible" in html


def test_tarjetas_ante_un_error_inesperado_muestran_sin_datos(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rescate, "panel_status", mock.Mock(side_effect=RuntimeError("boom")))
    html = _home(client)
    assert "No se pudo leer el estado del rescate." in html


@pytest.mark.parametrize("url", ["/", "/dashboard/2026-10-01/2026-10-22"])
def test_dashboard_no_llama_a_binance(client, hd, url):  # noqa: F811
    hd.configurar()
    hd.service("real").tick()
    boom = mock.Mock(side_effect=AssertionError("llamada HTTP saliente desde la página"))
    with mock.patch.object(requests, "get", boom), mock.patch.object(requests.Session, "request", boom), \
            mock.patch.object(requests, "request", boom):
        resp = client.get(url)
    assert resp.status_code == 200
    assert not boom.called
    assert 'id="rescateTarjetas"' in resp.get_data(as_text=True)


def test_dashboard_requiere_login(app):  # noqa: F811
    resp = app.test_client().get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


@pytest.mark.parametrize("ratio,posiciones,esperado", [
    ("7.99", 2, "rojo"), ("8", 2, "amarillo"), ("11.99", 2, "amarillo"), ("12", 2, "verde"),
    (None, 0, "verde"), (None, None, "gris"),
])
def test_semaforo(ratio, posiciones, esperado):
    valor = None if ratio is None else Decimal(ratio)
    assert resumen_rescate.semaforo(valor, Decimal("8"), posiciones) == esperado


def test_antiguedad():
    now = 1_700_000_000.0
    assert resumen_rescate.antiguedad(rescate.now_iso(now - 30), now) == "hace 30 s"
    assert resumen_rescate.antiguedad(rescate.now_iso(now - 600), now) == "hace 10 min"
    assert resumen_rescate.antiguedad(rescate.now_iso(now - 3 * 86400), now) == "hace 3 d"
    assert resumen_rescate.antiguedad("no es fecha", now) is None
    assert resumen_rescate.antiguedad(None, now) is None


def test_resumen_sin_variacion_si_el_tope_no_esta_armado(hd):
    hd.service("real").tick()  # rescate deshabilitado: lee, pero no fija base
    r = resumen_rescate.tarjetas(hd.data_dir, now=hd.clock.t)
    assert r["error"] is None and r["capital"]["usdt"] == "22000.00"
    assert r["capital"]["variacion_usdt"] is None and r["tope"]["estado"] == "sin_armar"
    assert json.dumps(r)  # serializable (sin Decimal)
