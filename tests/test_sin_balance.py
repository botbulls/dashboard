"""Páginas con posiciones abiertas y sin balance para calcular la exposición (balance 0, NULL o sin fila de account).

get_coins() corre en casi todas las páginas (menú izquierdo): si tiraba excepción, todo el dashboard daba 500.
Con la exposición desconocida el menú muestra "—" (no "0 %", que mentiría) y la página responde 200.
"""
from __future__ import annotations

import re
import sqlite3
from unittest import mock

import pytest
import requests

from test_bot_control import app  # noqa: F401
from test_bot_control import client  # noqa: F401

RANGO = "2026-10-01/2026-10-23"
PAGINAS = ["/", f"/dashboard/{RANGO}", "/positions", "/coins/BTCUSDT", f"/coins/BTCUSDT/{RANGO}", "/history",
           "/projection", "/rescate", "/settings"]
SIN_FILA = "sin-fila"
GUION = "—"


def _preparar(app, saldo):  # noqa: F811
    """BTCUSDT long con una venta abierta; ETHUSDT short sin órdenes; ingresos en la última semana."""
    with sqlite3.connect(str(app.config["DATABASE"])) as con:
        if saldo == SIN_FILA:
            con.execute("DELETE FROM account")
        else:
            con.execute("UPDATE account SET totalWalletBalance = ? WHERE AID = 1", (saldo,))
        con.executemany(
            "INSERT INTO positions (symbol, unrealizedProfit, leverage, entryPrice, positionSide, positionAmt) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [("BTCUSDT", -2.25, 10, 82372.0, "LONG", 0.0022), ("ETHUSDT", 0.21, 10, 3905.4, "SHORT", -0.12)])
        con.execute(
            "INSERT INTO orders (origQty, price, side, positionSide, status, symbol, time, type) "
            "VALUES (0.0011, 82610.0, 'SELL', 'LONG', 'NEW', 'BTCUSDT', strftime('%s','now') * 1000, 'LIMIT')")
        con.execute(
            "INSERT INTO income (tranId, symbol, incomeType, income, asset, info, time, tradeId) "
            "VALUES ('1', 'BTCUSDT', 'REALIZED_PNL', 12.5, 'USDT', '', strftime('%s','now') * 1000 - 1000, 0)")


class _Resp:
    def __init__(self, data):
        self._data = data

    def __bool__(self):
        return True

    def json(self):
        return self._data


def _binance(url, *a, **kw):
    # Binance público con precios: lo único que falta en estos tests es el balance.
    if "premiumIndex?symbol=" in url:
        return _Resp({"symbol": "BTCUSDT", "markPrice": "81954.1"})
    if "premiumIndex" in url:
        return _Resp([{"symbol": "BTCUSDT", "markPrice": "81954.1"}, {"symbol": "ETHUSDT", "markPrice": "3907.15"}])
    if "klines" in url:
        return _Resp([])
    raise AssertionError(f"llamada inesperada: {url}")


def _get(client, app, url, saldo):  # noqa: F811
    with mock.patch.object(requests, "get", _binance):
        resp = client.get(url)
    if saldo == SIN_FILA:  # el caso sigue siendo "sin fila" después del request
        with sqlite3.connect(str(app.config["DATABASE"])) as con:
            assert con.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0
    return resp


def _menu(html):
    return html.split('id="activecoins-collapse"', 1)[1].split("</section>", 1)[0]


SIN_BALANCE = pytest.mark.parametrize("saldo", [0.0, None, SIN_FILA], ids=["balance-0", "balance-null", "sin-fila"])


@SIN_BALANCE
@pytest.mark.parametrize("url", PAGINAS)
def test_paginas_responden_200_sin_balance(client, app, saldo, url):  # noqa: F811
    _preparar(app, saldo)
    resp = _get(client, app, url, saldo)
    assert resp.status_code == 200, url
    menu = _menu(resp.get_data(as_text=True))
    # Long de BTC y total long: desconocidos.
    assert menu.count(f'exposición long </span>{GUION}</span>') == 2, menu
    # Short de ETH (sin órdenes) y total short: la línea aparece igual, con "—".
    assert menu.count(f"Short {GUION} · 0 compras · 0 ventas") == 2, menu
    assert 'title="Sin balance para calcular"' in menu
    # Ni un número inventado ni el None de Python en el crudo: el único % es el long de ETH, que no tiene long (0 de verdad).
    assert 'data-value="None"' not in menu
    assert re.findall(r"[0-9]+ %", menu) == ["0 %"], menu


@SIN_BALANCE
def test_moneda_que_no_existe_sigue_dando_404(client, app, saldo):  # noqa: F811
    _preparar(app, saldo)
    assert _get(client, app, "/coins/NOEXISTE", saldo).status_code == 404


def test_con_balance_el_menu_muestra_numeros(client, app):  # noqa: F811
    _preparar(app, 1000.0)
    resp = _get(client, app, "/", 1000.0)
    assert resp.status_code == 200
    menu = _menu(resp.get_data(as_text=True))
    assert GUION not in menu
    assert "Sin balance para calcular" not in menu
    assert re.search(r'class="bb-moneda__expo" data-value="0.18"><span class="bb-sr">exposición long </span>18 %', menu)
    assert "Short 47 % · 0 compras · 0 ventas" in menu
