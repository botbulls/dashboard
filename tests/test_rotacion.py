"""Réplica de forager.sort_symbols y de los lugares de forager.generate_yaml (spec bot-estrategia S1).

``tests/fixtures/rotacion/sort_symbols.json`` sale del código real de forager con las mismas velas
(``tests/fixtures/rotacion/generar.py`` dice cómo regenerarlo).
"""
from __future__ import annotations

import json
import pathlib

import pytest

from futuresboard import rotacion

FIXTURE = json.loads((pathlib.Path(__file__).resolve().parent / "fixtures" / "rotacion" / "sort_symbols.json")
                     .read_text(encoding="utf-8"))


@pytest.mark.parametrize("caso", FIXTURE["casos"], ids=lambda c: f"n{c['n_longs']}-{len(c['aprobados'])}pares")
def test_igual_que_forager(caso):
    velas = {s: FIXTURE["velas"][s] for s in caso["aprobados"]}
    ordenados = rotacion.ordenar(velas, caso["config"])
    assert [s for _, s in ordenados] == [s for _, s in caso["ordenados"]]
    for (v, _s), (esperado, _e) in zip(ordenados, caso["ordenados"]):
        assert v == pytest.approx(esperado, rel=1e-9)
    lugares = rotacion.elegir(ordenados, caso["aprobados"], caso["n_longs"], caso["posiciones"], caso["ordenes"])
    assert sorted(lugares["ideales"]) == caso["ideal_longs"]
    assert sorted(lugares["activos"]) == caso["active_longs"]
    assert sorted(lugares["en_parada_gradual"]) == caso["longs_on_gs"]


def test_cinco_pares_cuatro_lugares_queda_afuera_el_de_menos_volumen():
    caso = FIXTURE["casos"][0]
    est = rotacion.estimar({s: FIXTURE["velas"][s] for s in caso["aprobados"]}, caso["config"], caso["aprobados"], 4,
                           caso["posiciones"], caso["ordenes"])
    assert est["afuera"] == ["SOLUSDT"] and "SOLUSDT" not in est["activos"]
    assert rotacion.texto(est, "Demo") == "en Binance Demo elegiría XRP, ETH, BNB, BTC (SOL afuera)"


def test_se_queda_con_los_que_tienen_todas_las_velas():
    velas = {s: FIXTURE["velas"][s] for s in ("BNBUSDT", "BTCUSDT", "ETHUSDT")}
    velas["ETHUSDT"] = velas["ETHUSDT"][:300]
    est = rotacion.estimar(velas, FIXTURE["casos"][0]["config"], list(velas), 2, [], [])
    assert est["sin_velas"] == ["ETHUSDT"] and "ETHUSDT" not in est["ordenados"]


def test_en_parada_gradual_no_elige_ninguno():
    caso = FIXTURE["casos"][0]
    est = rotacion.estimar({s: FIXTURE["velas"][s] for s in caso["aprobados"]}, caso["config"], caso["aprobados"], 4,
                           caso["posiciones"], caso["ordenes"], parada_gradual=True)
    assert est["ideales"] == [] and est["en_parada_gradual"] == sorted(caso["posiciones"] + caso["ordenes"])
