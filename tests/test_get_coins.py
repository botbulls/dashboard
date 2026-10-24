"""Exposición por moneda del menú izquierdo (get_coins).

La exposición de cada moneda es tamaño de la posición sobre el balance, redondeada
a 2 decimales por fila; el total es la suma de esas filas. Cada moneda tiene que
mostrar SU exposición, sin arrastrar la de la moneda anterior de la lista.
"""
from __future__ import annotations

import itertools
import sqlite3

import pytest

from futuresboard import auth
from futuresboard.app import init_app
from futuresboard.blueprint import get_coins
from futuresboard.config import Config

BALANCE = 1000.0


@pytest.fixture
def armar(tmp_path):
    """Arma una base nueva con posiciones (símbolo, lado, nocional) y órdenes
    (símbolo, side, positionSide, cantidad) y devuelve get_coins() sobre ella."""
    contador = itertools.count()

    def _armar(posiciones, ordenes=()):
        carpeta = tmp_path / f"base{next(contador)}"
        carpeta.mkdir()
        db_path = carpeta / "futures.db"
        app = init_app(
            Config(
                CONFIG_DIR=carpeta,
                DATABASE=db_path,
                API_KEY="x",
                API_SECRET="x",
                DISABLE_AUTO_SCRAPE=True,
            )
        )
        auth._ensure_database_schema(str(db_path))
        conn = sqlite3.connect(db_path)
        try:
            with conn:
                conn.execute("UPDATE account SET totalWalletBalance = ? WHERE AID = 1", (BALANCE,))
                for symbol, lado, nocional in posiciones:
                    cantidad = 1.0 if lado == "LONG" else -1.0
                    conn.execute(
                        "INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, positionAmt)"
                        " VALUES (?, 0, 10, ?, ?, ?)",
                        (symbol, float(nocional), lado, cantidad),
                    )
                for symbol, side, lado, n in ordenes:
                    for _ in range(n):
                        conn.execute(
                            "INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type)"
                            " VALUES (1, 1, ?, ?, 'NEW', ?, 0, 'LIMIT')",
                            (side, lado, symbol),
                        )
        finally:
            conn.close()
        with app.app_context():
            return get_coins()

    return _armar


@pytest.mark.parametrize("lado, col, total", [("LONG", 2, "pbr_long"), ("SHORT", 5, "pbr_short")])
def test_cada_moneda_tiene_su_exposicion_y_el_total_es_la_suma(armar, lado, col, total):
    # Caso real de dev: filas 0,01 / 0,04 / 0,07 / 0,07 cuando debían ser 0,01 / 0,03 / 0,04 / 0,03.
    coins = armar(
        [("BNBUSDT", lado, 10), ("BTCUSDT", lado, 30), ("ETHUSDT", lado, 40), ("XRPUSDT", lado, 30)]
    )
    expo = {symbol: fila[col] for symbol, fila in coins["active"].items()}
    assert expo == pytest.approx({"BNBUSDT": 0.01, "BTCUSDT": 0.03, "ETHUSDT": 0.04, "XRPUSDT": 0.03})
    assert coins["totals"][total] == "0.11"
    otra = 5 if col == 2 else 2
    assert all(fila[otra] == 0.0 for fila in coins["active"].values())


@pytest.mark.parametrize("hedge", [("LONG", "SHORT"), ("SHORT", "LONG")], ids=["long-primero", "short-primero"])
def test_hedge_mismo_simbolo_long_y_short(armar, hedge):
    nocional = {"LONG": 20, "SHORT": 50}
    posiciones = (
        [("AAAUSDT", "LONG", 10)]
        + [("BTCUSDT", lado, nocional[lado]) for lado in hedge]
        + [("ZZZUSDT", "LONG", 30)]
    )
    ordenes = [
        ("AAAUSDT", "BUY", "LONG", 1),
        ("AAAUSDT", "SELL", "LONG", 1),
        ("BTCUSDT", "BUY", "LONG", 2),
        ("BTCUSDT", "SELL", "LONG", 1),
        ("BTCUSDT", "BUY", "SHORT", 1),
        ("BTCUSDT", "SELL", "SHORT", 3),
        ("ZZZUSDT", "BUY", "LONG", 1),
        ("ZZZUSDT", "SELL", "LONG", 1),
    ]
    coins = armar(posiciones, ordenes)

    # [compras long, ventas long, pbr long, compras short, ventas short, pbr short]
    assert coins["active"] == {
        "AAAUSDT": [1, 1, pytest.approx(0.01), 0, 0, 0.0],
        "BTCUSDT": [2, 1, pytest.approx(0.02), 1, 3, pytest.approx(0.05)],
        "ZZZUSDT": [1, 1, pytest.approx(0.03), 0, 0, 0.0],
    }
    assert coins["totals"]["active"] == 3
    assert coins["totals"]["pbr_long"] == "0.06"
    assert coins["totals"]["pbr_short"] == "0.05"
    # La fila "Total" cuenta las órdenes de BTC una sola vez aunque BTC venga en dos filas (LONG y SHORT).
    assert coins["totals"]["buys_long"] == 1 + 2 + 1
    assert coins["totals"]["sells_long"] == 1 + 1 + 1
    assert coins["totals"]["buys_short"] == 1
    assert coins["totals"]["sells_short"] == 3


@pytest.mark.parametrize("nocionales", list(itertools.permutations([10, 30, 40])))
def test_el_orden_de_las_monedas_no_cambia_los_valores(armar, nocionales):
    simbolos = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    coins = armar([(s, "LONG", n) for s, n in zip(simbolos, nocionales)])
    expo = {symbol: fila[2] for symbol, fila in coins["active"].items()}
    assert expo == pytest.approx({s: n / BALANCE for s, n in zip(simbolos, nocionales)})
    assert coins["totals"]["pbr_long"] == "0.08"


def test_una_moneda_vale_lo_mismo_sola_que_acompanada(armar):
    sola = armar([("BTCUSDT", "LONG", 30)])
    acompanada = armar([("AAAUSDT", "LONG", 10), ("AAAUSDT", "SHORT", 40), ("BTCUSDT", "LONG", 30)])
    assert acompanada["active"]["BTCUSDT"] == sola["active"]["BTCUSDT"]


def test_fila_one_way_both_no_hereda_la_exposicion_de_la_anterior(armar):
    # positionSide BOTH (modo one-way) no entra en ninguna rama: no puede quedarse con el pbr de la moneda anterior.
    coins = armar([("AAAUSDT", "LONG", 10), ("BBBUSDT", "BOTH", 30)])
    assert coins["active"]["BBBUSDT"][2] == 0.0
    assert coins["active"]["BBBUSDT"][5] == 0.0
    assert coins["totals"]["pbr_long"] == "0.01"
