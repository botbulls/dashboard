"""Chequeos de forager en cada ciclo sobre los pares aprobados (spec bot-estrategia § 2.3 y § 2.5).

Corren en un hilo del panel (no en un pedido web) cada ``update_interval_minutes``, guardan el último estado por par en
el directorio de datos y avisan por Telegram **solo cuando cambia** (ok → descarta y descarta → ok). Binance caído es
"sin datos": no cambia el estado conocido ni avisa.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from estrategia_fakes import DEMO
from estrategia_fakes import Red
from futuresboard import binance_publico
from futuresboard import chequeos_forager
from futuresboard import telegram_notify

CFG = {
    "user": "binance_01", "twe_long": 4, "n_longs": 4, "n_shorts": 0, "max_min_cost": 150,
    "market_age_threshold": "2023-01", "update_interval_minutes": 15,
    "symbols_to_ignore": ["INJUSDT"],
    "approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
    "approved_symbols_short": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
}


@pytest.fixture
def red(monkeypatch):
    r = Red()
    monkeypatch.setattr(binance_publico, "_get", r)
    return r


@pytest.fixture
def entorno(tmp_path):
    forager = tmp_path / "forager" / "new.json"
    forager.parent.mkdir()
    forager.write_text(json.dumps(CFG))
    data = tmp_path / "data"
    data.mkdir()
    return chequeos_forager.Entorno(forager_config=forager, data_dir=data, testnet=True, base_operativa=DEMO,
                                    hoy=lambda: dt.date(2026, 10, 25))


@pytest.fixture
def telegram(monkeypatch):
    enviados = []
    monkeypatch.setenv(telegram_notify.ENV_TOKEN, "t")
    monkeypatch.setenv(telegram_notify.ENV_CHAT_ID, "c")
    monkeypatch.setattr(chequeos_forager, "_enviar", lambda texto: enviados.append(texto) or True)
    return enviados


def test_primer_ciclo_todo_ok_no_avisa(entorno, red, telegram):
    estado = chequeos_forager.ciclo(entorno)
    assert set(estado["pares"]) == set(CFG["approved_symbols_long"])
    assert all(p["estado"] == "ok" for p in estado["pares"].values())
    assert telegram == []
    guardado = chequeos_forager.leer_estado(entorno.data_dir)
    assert guardado["pares"]["BTCUSDT"]["estado"] == "ok" and guardado["ts"]
    # El mínimo con su margen (BTC: 50 de 150 en Demo, 80,02 en real) va en el estado para Bot.
    assert any("Mínimo 50,00 de 150" in c["texto"] for c in guardado["pares"]["BTCUSDT"]["chequeos"])


def test_un_aviso_por_cambio_de_estado(entorno, red, telegram):
    chequeos_forager.ciclo(entorno)
    red.demo.precios["BTCUSDT"] = "2000000"  # mínimo 0,0001 × 2.000.000 = 200 > 150
    binance_publico.limpiar_cache()
    estado = chequeos_forager.ciclo(entorno)
    assert estado["pares"]["BTCUSDT"]["estado"] == "descarta"
    assert len(telegram) == 1 and "BTCUSDT" in telegram[0] and "200,00 de 150" in telegram[0]
    binance_publico.limpiar_cache()
    chequeos_forager.ciclo(entorno)
    assert len(telegram) == 1  # sigue descartado: no repite
    red.demo.precios["BTCUSDT"] = "80000"
    binance_publico.limpiar_cache()
    chequeos_forager.ciclo(entorno)
    assert len(telegram) == 2 and "vuelve a pasar" in telegram[1] and "BTCUSDT" in telegram[1]


def test_primer_ciclo_con_un_par_descartado_avisa(entorno, red, telegram):
    red.demo.set_status("XRPUSDT", "SETTLING")
    estado = chequeos_forager.ciclo(entorno)
    assert estado["pares"]["XRPUSDT"]["estado"] == "descarta"
    assert len(telegram) == 1 and "XRPUSDT" in telegram[0] and "SETTLING" in telegram[0]


@pytest.mark.parametrize("cambio,texto", [
    (lambda r: r.demo.n_velas.update(ETHUSDT=200), "200 velas"),
    (lambda r: r.demo.primera.update(ETHUSDT=1685577600000), "06/2023"),
])
def test_velas_y_edad(entorno, red, telegram, cambio, texto):
    cambio(red)
    estado = chequeos_forager.ciclo(entorno)
    assert estado["pares"]["ETHUSDT"]["estado"] == "descarta"
    assert any(texto in m for m in estado["pares"]["ETHUSDT"]["motivos"])


def test_par_aprobado_en_la_lista_negra(entorno, red, telegram):
    cfg = dict(CFG, approved_symbols_long=CFG["approved_symbols_long"] + ["INJUSDT"])
    entorno.forager_config.write_text(json.dumps(cfg))
    estado = chequeos_forager.ciclo(entorno)
    assert estado["pares"]["INJUSDT"]["estado"] == "descarta"
    assert any("lista negra" in m for m in estado["pares"]["INJUSDT"]["motivos"])


def test_binance_caido_no_cambia_el_estado_ni_avisa(entorno, red, telegram):
    red.demo.set_status("XRPUSDT", "SETTLING")
    chequeos_forager.ciclo(entorno)
    assert len(telegram) == 1
    binance_publico.limpiar_cache()
    red.demo.fallar.update({"/fapi/v1/exchangeInfo", "/fapi/v1/ticker/price", "/fapi/v1/klines"})
    estado = chequeos_forager.ciclo(entorno)
    assert estado["pares"]["XRPUSDT"]["estado"] == "sin_datos"
    assert estado["pares"]["XRPUSDT"]["conocido"] == "descarta"
    assert len(telegram) == 1
    # Vuelve Binance y XRP sigue descartado: no hay aviso nuevo (no cambió el estado conocido).
    binance_publico.limpiar_cache()
    red.demo.fallar.clear()
    chequeos_forager.ciclo(entorno)
    assert len(telegram) == 1


def test_sin_config_de_forager_no_rompe(entorno, red, telegram):
    entorno.forager_config.unlink()
    estado = chequeos_forager.ciclo(entorno)
    assert estado["error"] and estado["pares"] == {}
    assert telegram == []


def test_intervalo_del_ciclo_sale_de_forager():
    assert chequeos_forager.intervalo_s({"update_interval_minutes": 15}) == 900
    assert chequeos_forager.intervalo_s({"update_interval_minutes": 1}) == 300
    assert chequeos_forager.intervalo_s({}) == 900
    assert chequeos_forager.intervalo_s({"update_interval_minutes": "x"}) == 900


def test_texto_del_aviso_escapa_html(entorno, red, telegram):
    assert "<b>" in chequeos_forager.texto_aviso("BTCUSDT", "descarta", ["Mínimo 200 de 150 & <x>"])
    assert "&lt;x&gt;" in chequeos_forager.texto_aviso("BTCUSDT", "descarta", ["<x>"])


def test_no_arranca_en_los_tests_ni_con_el_flag_apagado(monkeypatch):
    monkeypatch.setenv(chequeos_forager.ENV_FLAG, "0")
    assert chequeos_forager.arrancar(object()) is None


def test_gunicorn_arranca_el_hilo_en_el_worker(monkeypatch):
    from futuresboard import gunicorn_conf

    llamados = []
    monkeypatch.setattr(chequeos_forager, "arrancar", lambda app: llamados.append(app))

    class Worker:
        wsgi = "la-app"

    gunicorn_conf.post_worker_init(Worker())
    assert llamados == ["la-app"]
