from __future__ import annotations

import csv
import functools
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import time
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

import requests
from flask import Blueprint
from flask import redirect
from flask import render_template
from flask import request
from flask import Response
from flask import session
from flask.helpers import url_for
from flask import current_app
from typing_extensions import TypedDict

from futuresboard import acciones
from futuresboard import binance_publico
from futuresboard import bot_control
from futuresboard import cambio_key
from futuresboard import c3
from futuresboard import chequeos_forager
from futuresboard import conexion
from futuresboard import credenciales
from futuresboard import estrategia
from futuresboard import jobs
from futuresboard import manifiesto
from futuresboard import perfiles
from futuresboard import rescate
from futuresboard import resumen_rescate
from futuresboard import scraper
from futuresboard import telegram_notify
from futuresboard import db
from futuresboard import formato
from futuresboard import logs
from futuresboard.config import BINANCE_FUTURES_MAINNET_URL
from futuresboard.config import state_dir

app = Blueprint("main", __name__)


def _binance_public_base() -> str:
    """Base URL para datos publicos de Binance Futures (mark price, klines).

    Por defecto es el endpoint real; con BINANCE_TESTNET activo usa el mismo
    API_BASE_URL de prueba que el scraper.
    """
    if current_app.config.get("BINANCE_TESTNET"):
        return current_app.config["API_BASE_URL"]
    return BINANCE_FUTURES_MAINNET_URL


class CoinsTotals(TypedDict):
    active: int
    inactive: int
    buys_long: int
    sells_long: int
    pbr_long: int
    buys_short: int
    sells_short: int
    pbr_short: int


class Coins(TypedDict):
    active: dict[str, tuple[int, int, int]]
    inactive: list[str]
    totals: CoinsTotals
    warning: bool


class History(TypedDict):
    columns: list[dict[str, Any]]


class Projections(TypedDict):
    dates: list[str]
    proj: dict[float, list[float]]
    pcustom: list[float]
    pcustom_value: float


remove_incomeTypes = ["TRANSFER", "COIN_SWAP_DEPOSIT", "COIN_SWAP_WITHDRAW"]


def zero_value(x):
    if x is None:
        return 0
    else:
        return x


def format_dp(value, dp=2):
    return "{:.{}f}".format(value, dp)


def calc_pbr(volume, price, side, balance):
    if price > 0.0:
        if side == "SHORT":
            return abs(volume * price) / balance
        elif side == "LONG":
            return abs(volume * price) / balance
    return 0.0


def _sumar_pbr(acumulado, pbr):
    """Suma exposiciones; si una es desconocida (None: sin balance para calcular), el resultado también."""
    if acumulado is None or pbr is None:
        return None
    return acumulado + pbr


def average_down_target(posprice, posqty, currentprice, targetprice):
    return (posqty * (posprice - targetprice)) / (targetprice - currentprice)


def get_coins():
    coins: Coins = {
        "active": {},
        "inactive": [],
        "totals": {"active": 0, "inactive": 0, "buys_long": 0, "sells_long": 0, "pbr_long": 0, "buys_short": 0, "sells_short": 0, "pbr_short": 0},
        "warning": False,
    }

    all_active_positions = db.query(
        "SELECT symbol, entryPrice, positionSide, positionAmt FROM positions WHERE ABS(positionAmt) > 0 ORDER BY symbol ASC"
    )

    all_symbols_with_pnl = db.query(
        'SELECT DISTINCT(symbol) FROM income WHERE asset <> "BNB" AND symbol <> "" AND incomeType NOT IN'
        + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
        + " ORDER BY symbol ASC",
        remove_incomeTypes,
    )

    balance = db.query("SELECT totalWalletBalance FROM account WHERE AID = 1", one=True)
    # Con balance 0, NULL o sin fila de account la exposición no se puede calcular: queda None y el menú muestra "—".
    saldo = float(balance[0]) if balance is not None and balance[0] else None

    active_symbols = []

    for position in all_active_positions:
        if position[0] not in active_symbols:
            coins["totals"]["active"] += 1
        active_symbols.append(position[0])

        buy_long, sell_long, buy_short, sell_short,  = 0, 0, 0, 0
        pbr_long, pbr_short = 0.0, 0.0

        buyorders_long = db.query(
            'SELECT COUNT(OID) FROM orders WHERE symbol = ? AND side = "BUY" AND positionSide = "LONG"',
            [position[0]],
            one=True,
        )

        buyorders_short = db.query(
            'SELECT COUNT(OID) FROM orders WHERE symbol = ? AND side = "BUY" AND positionSide = "SHORT"',
            [position[0]],
            one=True,
        )

        sellorders_long = db.query(
            'SELECT COUNT(OID) FROM orders WHERE symbol = ? AND side = "SELL" AND positionSide = "LONG"',
            [position[0]],
            one=True,
        )

        sellorders_short = db.query(
            'SELECT COUNT(OID) FROM orders WHERE symbol = ? AND side = "SELL" AND positionSide = "SHORT"',
            [position[0]],
            one=True,
        )

        # En hedge mode el mismo símbolo llega en dos filas (LONG y SHORT): se inicializa una sola vez y se acumula.
        primera_fila = position[0] not in coins["active"]
        if primera_fila:
            coins["active"][position[0]] = [0, 0, 0.0, 0, 0, 0.0]
        if position[2] == 'LONG':
            pbr_long = round(calc_pbr(position[3], position[1], position[2], saldo), 2) if saldo else None
            pbr_short = 0.0
        if position[2] == 'SHORT':
            pbr_short = round(calc_pbr(position[3], position[1], position[2], saldo), 2) if saldo else None
            pbr_long = 0.0

        if buyorders_long is not None:
            buy_long = int(buyorders_long[0])
        if sellorders_long is not None:
            sell_long = int(sellorders_long[0])
        if buyorders_short is not None:
            buy_short = int(buyorders_short[0])
        if sellorders_short is not None:
            sell_short = int(sellorders_short[0])
        if buy_long == 0 and sell_long == 0 and buy_short == 0 and sell_short == 0:
            coins["warning"] = True

        coins["active"][position[0]][0] = buy_long
        coins["active"][position[0]][1] = sell_long
        coins["active"][position[0]][2] = _sumar_pbr(coins["active"][position[0]][2], pbr_long)
        coins["active"][position[0]][3] = buy_short
        coins["active"][position[0]][4] = sell_short
        coins["active"][position[0]][5] = _sumar_pbr(coins["active"][position[0]][5], pbr_short)
        # Las órdenes se cuentan por símbolo (no por lado): al total entran una vez, en la primera fila del símbolo.
        if primera_fila:
            coins["totals"]["buys_long"] += buy_long
            coins["totals"]["sells_long"] += sell_long
            coins["totals"]["buys_short"] += buy_short
            coins["totals"]["sells_short"] += sell_short
        coins["totals"]["pbr_long"] = _sumar_pbr(coins["totals"]["pbr_long"], pbr_long)
        coins["totals"]["pbr_short"] = _sumar_pbr(coins["totals"]["pbr_short"], pbr_short)
        if coins["active"][position[0]][2] is not None:
            coins["active"][position[0]][2] = round(coins["active"][position[0]][2],2)
        if coins["active"][position[0]][5] is not None:
            coins["active"][position[0]][5] = round(coins["active"][position[0]][5],2)

    for symbol in all_symbols_with_pnl:
        if symbol[0] not in active_symbols:
            coins["inactive"].append(symbol[0])
            coins["totals"]["inactive"] += 1
    
    if coins["totals"]["pbr_long"] is not None:
        coins["totals"]["pbr_long"] = format_dp(coins["totals"]["pbr_long"])
    if coins["totals"]["pbr_short"] is not None:
        coins["totals"]["pbr_short"] = format_dp(coins["totals"]["pbr_short"])
    return coins


def get_lastupdate():
    lastupdate = db.query("SELECT MAX(time) FROM orders", one=True)
    if lastupdate[0] is None:
        return "-"
    return datetime.fromtimestamp(lastupdate[0] / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


def get_last_order_details():
    """Get details of the most recent order."""
    order = db.query(
        "SELECT origQty, price, side, positionSide, status, symbol, time, type FROM orders ORDER BY time DESC LIMIT 1",
        one=True
    )
    if order is None:
        return None
    
    return {
        "origQty": float(order[0]) if order[0] is not None else 0,
        "price": float(order[1]) if order[1] is not None else 0,
        "side": order[2] or "",
        "positionSide": order[3] or "",
        "status": order[4] or "",
        "symbol": order[5] or "",
        "time": order[6],
        "type": order[7] or "",
        "formatted_time": datetime.fromtimestamp(order[6] / 1000.0).strftime("%Y-%m-%d %H:%M:%S") if order[6] else "-"
    }


def timeranges():
    today = date.today()
    yesterday_start = today - timedelta(days=1)

    this_week_start = today - timedelta(days=today.weekday())
    last_week_start = this_week_start - timedelta(days=7)
    last_week_end = this_week_start - timedelta(days=1)

    this_month_start = today.replace(day=1)
    last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)
    last_month_end = this_month_start - timedelta(days=1)

    this_year_start = today.replace(day=1).replace(month=1)
    last_year_start = (this_year_start - timedelta(days=1)).replace(day=1).replace(month=1)
    last_year_end = this_year_start - timedelta(days=1)

    return [
        [today.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")],
        [yesterday_start.strftime("%Y-%m-%d"), yesterday_start.strftime("%Y-%m-%d")],
        [this_week_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")],
        [last_week_start.strftime("%Y-%m-%d"), last_week_end.strftime("%Y-%m-%d")],
        [this_month_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")],
        [last_month_start.strftime("%Y-%m-%d"), last_month_end.strftime("%Y-%m-%d")],
        [this_year_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")],
        [last_year_start.strftime("%Y-%m-%d"), last_year_end.strftime("%Y-%m-%d")],
        [last_year_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")],
    ]


def _rescate_resumen():
    """Tarjetas del rescate para el dashboard principal: solo lee archivos (nunca Binance) y nunca lanza."""
    return resumen_rescate.tarjetas(state_dir(current_app.config))


def _hace(segundos) -> str:
    """'38 s', '12 min', '3 h', '2 d' (mismos cortes que resumen_rescate.antiguedad)."""
    s = max(0, int(segundos))
    if s < 90:
        return f"{s} s"
    if s < 90 * 60:
        return f"{s // 60} min"
    if s < 48 * 3600:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def _vivo_ctx(url: str) -> dict:
    """Lo que necesita el indicador "en vivo" de base.html: a dónde pide en_vivo.js y la edad de los datos al render."""
    f = scraper.frescura_web(current_app.config)
    edad = f["edad_segundos"]
    return {
        "url": url,
        "edad": edad,
        "intervalo": f["intervalo_segundos"],
        "texto": "Sin latido del scraper" if edad is None else f"Datos de hace {_hace(edad)}",
    }


def _cuenta(balance_db: float, upnl_db: float) -> dict:
    """Balance y PnL no realizado de Inicio con su fuente: la lectura del rescate si es fresca, si no la base."""
    frescura = scraper.frescura_web(current_app.config)
    base = {"origen": "base", "at": frescura["at"], "edad_segundos": frescura["edad_segundos"]}
    fuentes = {"balance": dict(base), "upnl": dict(base)}
    balance, upnl = balance_db, upnl_db
    lectura = resumen_rescate.lectura_cuenta(state_dir(current_app.config))
    if lectura:
        r = {"origen": "rescate", "at": lectura["at"], "edad_segundos": lectura["edad_segundos"]}
        if lectura["wallet"] is not None:
            balance = float(lectura["wallet"])
            fuentes["balance"] = {**r, "derivado": lectura["wallet_derivada"]}
        if lectura["upnl"] is not None:
            upnl = float(lectura["upnl"])
            fuentes["upnl"] = dict(r)
    return {"balance": balance, "upnl": upnl, "fuentes": fuentes}


def texto_fuente(fuentes: dict) -> str:
    nombre = {"base": "base del scraper", "rescate": "lectura del rescate"}
    b, u = fuentes["balance"]["origen"], fuentes["upnl"]["origen"]
    if b == u:
        return f"Balance y PnL: {nombre[b]}"
    return f"Balance: {nombre[b]} · PnL: {nombre[u]}"


def alto_grafico_monedas(n: int) -> int:
    """Alto en px del gráfico de ganancia por moneda (una barra de 34 px por moneda, mínimo 240)."""
    return max(240, 56 + 34 * n)


def _datos_inicio(startdate: str, enddate: str) -> dict:
    """Contexto de home.html para el período [startdate, enddate] (fechas ISO que ``fromisoformat`` acepta).

    Lo usan las dos rutas de Inicio (``/``, con la semana en curso, y ``/dashboard/<a>/<b>``) y ``/api/vivo/inicio``:
    lo que actualiza el JS sale de las mismas cuentas que la página. Balance y PnL no realizado salen de la lectura
    del rescate si es fresca (ver ``_cuenta``); los % del balance y la curva de balance usan ese mismo balance.
    """
    ranges = timeranges()
    start = (
        datetime.combine(datetime.fromisoformat(startdate), datetime.min.time()).timestamp() * 1000
    )
    end = datetime.combine(datetime.fromisoformat(enddate), datetime.max.time()).timestamp() * 1000

    todaystart = (
        datetime.combine(datetime.fromisoformat(ranges[0][0]), datetime.min.time()).timestamp()
        * 1000
    )
    todayend = (
        datetime.combine(datetime.fromisoformat(ranges[0][1]), datetime.max.time()).timestamp()
        * 1000
    )
    weekstart = (
        datetime.combine(datetime.fromisoformat(ranges[2][0]), datetime.min.time()).timestamp()
        * 1000
    )
    weekend = (
        datetime.combine(datetime.fromisoformat(ranges[2][1]), datetime.max.time()).timestamp()
        * 1000
    )
    monthstart = (
        datetime.combine(datetime.fromisoformat(ranges[4][0]), datetime.min.time()).timestamp()
        * 1000
    )
    monthend = (
        datetime.combine(datetime.fromisoformat(ranges[4][1]), datetime.max.time()).timestamp()
        * 1000
    )

    balance = db.query("SELECT totalWalletBalance FROM account WHERE AID = 1", one=True)
    total = db.query(
        'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
        + " ({0})".format(", ".join("?" for _ in remove_incomeTypes)),
        remove_incomeTypes,
        one=True,
    )

    today = db.query(
        'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
        + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
        + " AND time >= ? AND time <= ?",
        remove_incomeTypes + [todaystart, todayend],
        one=True,
    )
    week = db.query(
        'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
        + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
        + " AND time >= ? AND time <= ?",
        remove_incomeTypes + [weekstart, weekend],
        one=True,
    )
    month = db.query(
        'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
        + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
        + " AND time >= ? AND time <= ?",
        remove_incomeTypes + [monthstart, monthend],
        one=True,
    )

    unrealized = db.query("SELECT SUM(unrealizedProfit) FROM positions", one=True)

    all_fees = db.query(
        'SELECT SUM(income), asset FROM income WHERE incomeType ="COMMISSION" GROUP BY asset'
    )

    by_date = db.query(
        'SELECT DATE(time / 1000, "unixepoch") AS Date, SUM(income) AS inc FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
        + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
        + " AND time >= ?  AND time <= ? GROUP BY Date",
        remove_incomeTypes + [start, end],
    )

    by_symbol = db.query(
        'SELECT SUM(income) AS inc, symbol FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
        + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
        + " AND time >= ? AND time <= ? GROUP BY symbol ORDER BY inc DESC",
        remove_incomeTypes + [start, end],
    )

    fees = {"USDT": 0, "BNB": 0}

    # Sin fila de account o con balance NULL se muestra como balance 0 (los % quedan en "-").
    balance = float(balance[0]) if balance is not None and balance[0] is not None else 0.0
    cuenta = _cuenta(balance, zero_value(unrealized[0]))
    balance = cuenta["balance"]

    temptotal: tuple[list[float], list[float]] = ([], [])

    customframe = db.query(
        'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType <> "TRANSFER" AND incomeType NOT IN'
        + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
        + " AND time >= ? AND time <= ?",
        remove_incomeTypes + [start, end],
        one=True,
    )

    profit_period = balance - zero_value(customframe[0])

    temp: tuple[list[float], list[float]] = ([], [])
    for each in by_date:
        temp[0].append(round(float(each[1]), 2))
        temp[1].append(each[0])
        temptotal[1].append(each[0])
        temptotal[0].append(round(profit_period + float(each[1]), 2))
        profit_period += float(each[1])
    by_date = temp
    total_by_date = temptotal

    temp = ([], [])
    for each in by_symbol:
        temp[0].append(each[1])
        temp[1].append(round(float(each[0]), 2))
    by_symbol = temp

    if balance == 0.0:
        percentages = ["-", "-", "-", "-"]
    else:
        percentages = [
            format_dp(zero_value(today[0]) / balance * 100),
            format_dp(zero_value(week[0]) / balance * 100),
            format_dp(zero_value(month[0]) / balance * 100),
            format_dp(zero_value(total[0]) / balance * 100),
        ]

    for row in all_fees:
        fees[row[1]] = format_dp(abs(zero_value(row[0])), 4)
    pnl = [format_dp(cuenta["upnl"]), format_dp(balance)]
    totals = [
        format_dp(zero_value(total[0])),
        format_dp(zero_value(today[0])),
        format_dp(zero_value(week[0])),
        format_dp(zero_value(month[0])),
        ranges[3],
        fees,
        percentages,
        pnl,
        datetime.now().strftime("%B"),
        zero_value(customframe[0]),
        len(by_symbol[0]),
    ]
    return {
        "coin_list": get_coins(),
        "totals": totals,
        "data": [by_date, by_symbol, total_by_date],
        "lastupdate": get_lastupdate(),
        "startdate": startdate,
        "enddate": enddate,
        "timeranges": ranges,
        "custom": current_app.config["CUSTOM"],
        "rescate_resumen": _rescate_resumen(),
        "fuentes": cuenta["fuentes"],
        "fuente_texto": texto_fuente(cuenta["fuentes"]),
        "alto_por_moneda": alto_grafico_monedas(len(by_symbol[0])),
    }


@app.route("/", methods=["GET"])
def index_page():
    daterange = request.args.get("daterange")
    ranges = timeranges()

    if daterange is not None:
        daterange = daterange.split(" - ")
        if len(daterange) == 2:
            try:
                start = (
                    datetime.combine(
                        datetime.fromisoformat(daterange[0]), datetime.min.time()
                    ).timestamp()
                    * 1000
                )
                end = (
                    datetime.combine(
                        datetime.fromisoformat(daterange[1]), datetime.max.time()
                    ).timestamp()
                    * 1000
                )
                startdate, enddate = daterange[0], daterange[1]
                return redirect(url_for("main.dashboard_page", start=startdate, end=enddate))
            except Exception:
                pass

    # Semana en curso; /api/vivo/inicio sin fechas también usa la semana en curso (cambia el lunes sin recargar).
    startdate, enddate = ranges[2][0], ranges[2][1]
    return render_template(
        "home.html",
        timeframe="week",
        vivo=_vivo_ctx(url_for("vivo.inicio")),
        **_datos_inicio(startdate, enddate),
        **_bot_inicio(),
    )


def _bot_inicio() -> dict:
    """Estado del bot para la franja y el control rápido de Inicio (inspect con el tope corto, como en vivo)."""
    st, _http = bot_status_payload(docker_timeout=BOT_STATUS_RAPIDO_S)
    return {"bot_status": st, "control": contexto_control(st)}


@app.route("/dashboard/<start>/<end>", methods=["GET"])
def dashboard_page(start, end):
    ranges = timeranges()
    daterange = request.args.get("daterange")

    if daterange is not None:
        daterange = daterange.split(" - ")
        if len(daterange) == 2:
            try:
                start = (
                    datetime.combine(
                        datetime.fromisoformat(daterange[0]), datetime.min.time()
                    ).timestamp()
                    * 1000
                )
                end = (
                    datetime.combine(
                        datetime.fromisoformat(daterange[1]), datetime.max.time()
                    ).timestamp()
                    * 1000
                )
                startdate, enddate = daterange[0], daterange[1]
                return redirect(url_for("main.dashboard_page", start=startdate, end=enddate))
            except Exception:
                return redirect(url_for("main.dashboard_page", start=start, end=end))

    try:
        startdate, enddate = start, end
        datetime.combine(datetime.fromisoformat(start), datetime.min.time())
        datetime.combine(datetime.fromisoformat(end), datetime.max.time())
    except Exception:
        startdate, enddate = ranges[2][0], ranges[2][1]
        return redirect(url_for("main.dashboard_page", start=startdate, end=enddate))

    return render_template(
        "home.html",
        vivo=_vivo_ctx(url_for("vivo.inicio", start=startdate, end=enddate)),
        **_datos_inicio(startdate, enddate),
        **_bot_inicio(),
    )


def _datos_posiciones() -> dict:
    """Posiciones abiertas con sus órdenes y el mark price público: lo que pintan /positions y
    /api/vivo/posiciones (mismas cuentas en los dos)."""
    coins = get_coins()
    positions = {}

    try:
        response = requests.get(_binance_public_base() + "/fapi/v1/premiumIndex", timeout=2)
        markPrices: dict
        markPrices = {}
        if response:
            temp = response.json()
            for each in temp:
                markPrices[each["symbol"]] = float(each["markPrice"])
        else:
            markPrices = {}
    except Exception:
        markPrices = {}

    for coin in coins["active"]:

        allpositions = db.query(
            "SELECT * FROM positions WHERE symbol = ?",
            [coin],
        )
        allorders = db.query(
            "SELECT * FROM orders WHERE symbol = ? ORDER BY side, price, origQty",
            [coin],
        )

        temp = []
        for position in allpositions:
            position = list(position)
            position[4] = round(float(position[4]), 5)
            temp.append(position)
        allpositions = temp

        temp = []
        buys_long = []
        sells_long = []
        buys_short = []
        sells_short = []
        for order in allorders:
            order = list(order)
            order[7] = datetime.fromtimestamp(order[7] / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
            if order[3] == "BUY" and order[4] == "LONG":
                buys_long.append(order[2])
            elif order[3] == "SELL" and order[4] == "LONG":
                sells_long.append(order[2])
            elif order[3] == "BUY" and order[4] == "SHORT":
                buys_short.append(order[2])
            elif order[3] == "SELL" and order[4] == "SHORT":
                sells_short.append(order[2])
            temp.append(order)
        allorders = temp
        stats = [len(buys_long), len(sells_long), len(buys_short), len(sells_short)]
        if stats[0] == 0:
            stats.append("-")
            stats.append("-")
        else:
            stats.append(sorted(buys_long)[-1])
            if coin in markPrices:
                stats.append(round(float(markPrices[coin]) - sorted(buys_long)[-1], 5))
            else:
                stats.append("-")
        if stats[1] == 0:
            stats.append("-")
            stats.append("-")
        else:
            stats.append(sorted(sells_long)[0])
            if coin in markPrices:
                stats.append(round(float(markPrices[coin]) - sorted(sells_long)[0], 5))
            else:
                stats.append("-")
        if stats[2] == 0:
            stats.append("-")
            stats.append("-")
        else:
            stats.append(sorted(buys_short)[-1])
            if coin in markPrices:
                stats.append(round(float(markPrices[coin]) - sorted(buys_short)[-1], 5))
            else:
                stats.append("-")
        if stats[3] == 0:
            stats.append("-")
            stats.append("-")
        else:
            stats.append(sorted(sells_short)[0])
            if coin in markPrices:
                stats.append(round(float(markPrices[coin]) - sorted(sells_short)[0], 5))
            else:
                stats.append("-")
        positions[coin] = [allpositions, allorders, stats]

    return {"positions": positions, "markprices": markPrices}


@app.route("/positions")
def positions_page():
    return render_template(
        "positions.html",
        coin_list=get_coins(),
        custom=current_app.config["CUSTOM"],
        vivo=_vivo_ctx(url_for("vivo.posiciones")),
        **_datos_posiciones(),
    )


def _posicion_moneda(coin) -> dict:
    """Posición, órdenes y precio de una moneda: lo que pintan /coins/<coin> y /api/vivo/moneda/<coin>.

    Lee la DB y el mark price público de Binance (``premiumIndex?symbol=``, peso 1). Las velas no: son 4 pedidos
    de 1000 velas y quedan solo en la página. ``coin`` tiene que estar validada (activa o inactiva) antes.
    """
    unrealized = db.query(
        "SELECT SUM(unrealizedProfit) FROM positions WHERE symbol = ?",
        [coin],
        one=True,
    )
    allpositions = db.query(
        "SELECT * FROM positions WHERE symbol = ? AND entryPrice > 0",
        [coin],
    )
    allorders = db.query(
        "SELECT * FROM orders WHERE symbol = ? ORDER BY side, price, origQty",
        [coin],
    )

    temp = []
    for position in allpositions:
        position = list(position)
        position[4] = round(float(position[4]), 5)
        temp.append(position)
    allpositions = temp

    averagetargets = ["-", "-", "-", "-"]
    try:
        response = requests.get(
            _binance_public_base() + "/fapi/v1/premiumIndex?symbol=" + coin, timeout=2
        )
        markPrice: float | str
        if response:
            markPrice = float(response.json()["markPrice"])
            averagetargets = [
                round(
                    average_down_target(
                        allpositions[0][4], allpositions[0][6], markPrice, markPrice * 1.001
                    )
                ),
                round(
                    average_down_target(
                        allpositions[0][4], allpositions[0][6], markPrice, markPrice * 1.005
                    )
                ),
                round(
                    average_down_target(
                        allpositions[0][4], allpositions[0][6], markPrice, markPrice * 1.01
                    )
                ),
            ]
        else:
            markPrice = "-"
    except IndexError:
        pass  # sin posición (cerrada o moneda inactiva): el precio queda y los objetivos en "-"
    except Exception:
        markPrice = "-"

    temp = []
    for order in allorders:
        order = list(order)
        order[7] = datetime.fromtimestamp(order[7] / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
        temp.append(order)
    allorders = temp
    return {
        "unrealized": zero_value(unrealized[0]),
        "allpositions": allpositions,
        "allorders": allorders,
        "markprice": markPrice,
        "target": averagetargets,
    }


@app.route("/coins/<coin>", methods=["GET"])
def coin_page(coin):
    coins = get_coins()
    if coin not in coins["inactive"] and coin not in coins["active"]:
        return (
            render_template(
                "error.html",
                coin_list=get_coins(),
                custom=current_app.config["CUSTOM"],
            ),
            404,
        )

    daterange = request.args.get("daterange")
    ranges = timeranges()

    if daterange is not None:
        daterange = daterange.split(" - ")
        if len(daterange) == 2:
            try:
                (
                    datetime.combine(
                        datetime.fromisoformat(daterange[0]), datetime.min.time()
                    ).timestamp()
                    * 1000
                )
                (
                    datetime.combine(
                        datetime.fromisoformat(daterange[1]), datetime.max.time()
                    ).timestamp()
                    * 1000
                )
                startdate, enddate = daterange[0], daterange[1]
                return redirect(
                    url_for("main.coin_page_timeframe", coin=coin, start=startdate, end=enddate)
                )
            except Exception:
                pass

    balance = db.query("SELECT totalWalletBalance FROM account WHERE AID = 1", one=True)
    # Sin fila de account o con balance NULL la moneda se muestra como con balance 0 (los % quedan en "-").
    # Por eso la rama de None de abajo ya no se alcanza: no definía lo que usa el render y daba 500.
    if balance is None or balance[0] is None:
        balance = (0.0,)
    if balance[0] is None:
        totals = ["-", "-", "-", "-", "-", {"USDT": 0, "BNB": 0}, ["-", "-", "-", "-"]]
    else:

        todaystart = (
            datetime.combine(datetime.fromisoformat(ranges[0][0]), datetime.min.time()).timestamp()
            * 1000
        )
        todayend = (
            datetime.combine(datetime.fromisoformat(ranges[0][1]), datetime.max.time()).timestamp()
            * 1000
        )
        weekstart = (
            datetime.combine(datetime.fromisoformat(ranges[2][0]), datetime.min.time()).timestamp()
            * 1000
        )
        weekend = (
            datetime.combine(datetime.fromisoformat(ranges[2][1]), datetime.max.time()).timestamp()
            * 1000
        )
        monthstart = (
            datetime.combine(datetime.fromisoformat(ranges[4][0]), datetime.min.time()).timestamp()
            * 1000
        )
        monthend = (
            datetime.combine(datetime.fromisoformat(ranges[4][1]), datetime.max.time()).timestamp()
            * 1000
        )

        startdate, enddate = ranges[2][0], ranges[2][1]

        total = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType <> "TRANSFER" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND symbol = ?",
            remove_incomeTypes + [coin],
            one=True,
        )
        today = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType <> "TRANSFER" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ?",
            remove_incomeTypes + [todaystart, todayend, coin],
            one=True,
        )
        week = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType <> "TRANSFER" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ?",
            remove_incomeTypes + [weekstart, weekend, coin],
            one=True,
        )
        month = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType <> "TRANSFER" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ?",
            remove_incomeTypes + [monthstart, monthend, coin],
            one=True,
        )

        result = db.query(
            'SELECT SUM(income), asset FROM income WHERE incomeType ="COMMISSION" AND symbol = ? GROUP BY asset',
            [coin],
        )
        pm = _posicion_moneda(coin)

        sticks = {"15m": [], "1h": [], "4h": [], "1d": []}

        for timeframe in sticks:
            try:
                response = requests.get(
                    _binance_public_base() + "/fapi/v1/klines?symbol="
                    + coin
                    + "&interval="
                    + timeframe
                    + "&limit=1000",
                    timeout=2,
                )
                if response:
                    timestamps = []
                    candles = []
                    for candle in response.json():
                        dateconvert = candle[0]
                        timestamps.append(dateconvert)
                        candles.append(
                            [
                                dateconvert,
                                candle[1],
                                candle[2],
                                candle[3],
                                candle[4],
                                candle[5],
                            ]
                        )
                    sticks[timeframe] = [timestamps, candles]
            except:
                pass

        fees = {"USDT": 0, "BNB": 0}
        balance = float(balance[0])
        if balance == 0.0:
            percentages = ["-", "-", "-", "-"]
        else:
            percentages = [
                format_dp(zero_value(today[0]) / balance * 100),
                format_dp(zero_value(week[0]) / balance * 100),
                format_dp(zero_value(month[0]) / balance * 100),
                format_dp(zero_value(total[0]) / balance * 100),
            ]
        for row in result:
            fees[row[1]] = format_dp(abs(zero_value(row[0])), 4)
        pnl = [format_dp(pm["unrealized"]), format_dp(balance)]
        totals = [
            format_dp(zero_value(total[0])),
            format_dp(zero_value(today[0])),
            format_dp(zero_value(week[0])),
            format_dp(zero_value(month[0])),
            ranges[3],
            fees,
            percentages,
            pnl,
            datetime.now().strftime("%B"),
            zero_value(week[0]),
        ]
        by_date = db.query(
            'SELECT DATE(time / 1000, "unixepoch") AS Date, SUM(income) AS inc FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ? GROUP BY Date",
            remove_incomeTypes + [weekstart, weekend, coin],
        )
        temp = [[], []]
        for each in by_date:
            temp[0].append(round(float(each[1]), 2))
            temp[1].append(each[0])
        by_date = temp

    return render_template(
        "coin.html",
        coin_list=get_coins(),
        coin=coin,
        totals=totals,
        summary=[],
        data=[by_date],
        orders=[pm["allpositions"], pm["allorders"]],
        lastupdate=get_lastupdate(),
        markprice=pm["markprice"],
        startdate=startdate,
        enddate=enddate,
        timeranges=ranges,
        custom=current_app.config["CUSTOM"],
        target=pm["target"],
        vivo=_vivo_ctx(url_for("vivo.moneda", coin=coin)),
        candlesticks=sticks,
    )


@app.route("/coins/<coin>/<start>/<end>")
def coin_page_timeframe(coin, start, end):
    coins = get_coins()
    if coin not in coins["inactive"] and coin not in coins["active"]:
        return (
            render_template(
                "error.html",
                coin_list=get_coins(),
                custom=current_app.config["CUSTOM"],
            ),
            404,
        )

    ranges = timeranges()
    daterange = request.args.get("daterange")

    if daterange is not None:
        daterange = daterange.split(" - ")
        if len(daterange) == 2:
            try:
                start = (
                    datetime.combine(
                        datetime.fromisoformat(daterange[0]), datetime.min.time()
                    ).timestamp()
                    * 1000
                )
                end = (
                    datetime.combine(
                        datetime.fromisoformat(daterange[1]), datetime.max.time()
                    ).timestamp()
                    * 1000
                )
                startdate, enddate = daterange[0], daterange[1]
                return redirect(
                    url_for("main.coin_page_timeframe", coin=coin, start=startdate, end=enddate)
                )
            except Exception:
                return redirect(
                    url_for("main.coin_page_timeframe", coin=coin, start=start, end=end)
                )

    try:
        startdate, enddate = start, end
        start = (
            datetime.combine(datetime.fromisoformat(start), datetime.min.time()).timestamp() * 1000
        )
        end = datetime.combine(datetime.fromisoformat(end), datetime.max.time()).timestamp() * 1000
    except Exception:
        startdate, enddate = ranges[2][0], ranges[2][1]
        return redirect(
            url_for("main.coin_page_timeframe", coin=coin, start=startdate, end=enddate)
        )

    todaystart = (
        datetime.combine(datetime.fromisoformat(ranges[0][0]), datetime.min.time()).timestamp()
        * 1000
    )
    todayend = (
        datetime.combine(datetime.fromisoformat(ranges[0][1]), datetime.max.time()).timestamp()
        * 1000
    )
    weekstart = (
        datetime.combine(datetime.fromisoformat(ranges[2][0]), datetime.min.time()).timestamp()
        * 1000
    )
    weekend = (
        datetime.combine(datetime.fromisoformat(ranges[2][1]), datetime.max.time()).timestamp()
        * 1000
    )
    monthstart = (
        datetime.combine(datetime.fromisoformat(ranges[4][0]), datetime.min.time()).timestamp()
        * 1000
    )
    monthend = (
        datetime.combine(datetime.fromisoformat(ranges[4][1]), datetime.max.time()).timestamp()
        * 1000
    )

    balance = db.query("SELECT totalWalletBalance FROM account WHERE AID = 1", one=True)
    # Sin fila de account o con balance NULL la moneda se muestra como con balance 0 (los % quedan en "-").
    # Por eso la rama de None de abajo ya no se alcanza: no definía lo que usa el render y daba 500.
    if balance is None or balance[0] is None:
        balance = (0.0,)
    if balance[0] is None:
        totals = ["-", "-", "-", "-", "-", {"USDT": 0, "BNB": 0}, ["-", "-", "-", "-"]]
    else:
        total = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND symbol = ?",
            remove_incomeTypes + [coin],
            one=True,
        )
        today = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType <> "TRANSFER" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ?",
            remove_incomeTypes + [todaystart, todayend, coin],
            one=True,
        )
        week = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType <> "TRANSFER" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ?",
            remove_incomeTypes + [weekstart, weekend, coin],
            one=True,
        )
        month = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType <> "TRANSFER" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ?",
            remove_incomeTypes + [monthstart, monthend, coin],
            one=True,
        )
        result = db.query(
            'SELECT SUM(income), asset FROM income WHERE incomeType ="COMMISSION" AND symbol = ? GROUP BY asset',
            [coin],
        )
        pm = _posicion_moneda(coin)

        sticks = {"15m": [], "1h": [], "4h": [], "1d": []}

        for timeframe in sticks:
            try:
                response = requests.get(
                    _binance_public_base() + "/fapi/v1/klines?symbol="
                    + coin
                    + "&interval="
                    + timeframe
                    + "&limit=1000",
                    timeout=2,
                )
                if response:
                    timestamps = []
                    candles = []
                    for candle in response.json():
                        dateconvert = candle[0]
                        timestamps.append(dateconvert)
                        candles.append(
                            [
                                dateconvert,
                                candle[1],
                                candle[2],
                                candle[3],
                                candle[4],
                                candle[5],
                            ]
                        )
                    sticks[timeframe] = [timestamps, candles]
            except:
                pass

        fees = {"USDT": 0, "BNB": 0}
        balance = float(balance[0])
        if balance == 0.0:
            percentages = ["-", "-", "-", "-"]
        else:
            percentages = [
                format_dp(zero_value(today[0]) / balance * 100),
                format_dp(zero_value(week[0]) / balance * 100),
                format_dp(zero_value(month[0]) / balance * 100),
                format_dp(zero_value(total[0]) / balance * 100),
            ]
        for row in result:
            fees[row[1]] = format_dp(abs(zero_value(row[0])), 4)

        by_date = db.query(
            'SELECT DATE(time / 1000, "unixepoch") AS Date, SUM(income) AS inc FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ? GROUP BY Date",
            remove_incomeTypes + [start, end, coin],
        )
        temp = [[], []]
        for each in by_date:
            temp[0].append(round(float(each[1]), 2))
            temp[1].append(each[0])
        by_date = temp

        pnl = [format_dp(pm["unrealized"]), format_dp(balance)]

        customframe = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ? AND symbol = ?",
            remove_incomeTypes + [start, end, coin],
            one=True,
        )

        totals = [
            format_dp(zero_value(total[0])),
            format_dp(zero_value(today[0])),
            format_dp(zero_value(week[0])),
            format_dp(zero_value(month[0])),
            ranges[3],
            fees,
            percentages,
            pnl,
            datetime.now().strftime("%B"),
            zero_value(customframe[0]),
        ]

    return render_template(
        "coin.html",
        coin_list=get_coins(),
        coin=coin,
        totals=totals,
        summary=[],
        data=[by_date],
        orders=[pm["allpositions"], pm["allorders"]],
        lastupdate=get_lastupdate(),
        markprice=pm["markprice"],
        startdate=startdate,
        enddate=enddate,
        timeranges=ranges,
        custom=current_app.config["CUSTOM"],
        target=pm["target"],
        vivo=_vivo_ctx(url_for("vivo.moneda", coin=coin)),
        candlesticks=sticks,
    )


@app.route("/history")
def history_page():
    ranges = timeranges()
    history: History = {"columns": []}

    for timeframe in ranges:
        start = (
            datetime.combine(datetime.fromisoformat(timeframe[0]), datetime.min.time()).timestamp()
            * 1000
        )
        end = (
            datetime.combine(datetime.fromisoformat(timeframe[1]), datetime.max.time()).timestamp()
            * 1000
        )
        incomesummary = db.query(
            "SELECT incomeType, COUNT(IID) FROM income WHERE time >= ? AND time <= ? GROUP BY incomeType",
            [start, end],
        )
        temp = timeframe[0] + "/" + timeframe[1]
        if temp not in history:
            history[temp] = {}  # type: ignore[misc]
            history[temp]["total"] = 0  # type: ignore[misc]
            history[temp]["start"] = timeframe[0]
            history[temp]["end"] = timeframe[1]

        for totals in incomesummary:
            history[temp][totals[0]] = int(totals[1])  # type: ignore[misc]
            history[temp]["total"] += int(totals[1])  # type: ignore[misc]
            if totals[0] not in history["columns"]:
                history["columns"].append(totals[0])
    for timeframe in ranges:
        temp = timeframe[0] + "/" + timeframe[1]
        for column in history["columns"]:
            if column not in history[temp]:  # type: ignore[misc]
                history[temp][column] = 0  # type: ignore[misc]

    history["columns"].sort()
    previous_files = []
    for file in os.listdir(os.path.join(app.root_path, "static", "csv")):
        if file.endswith(".csv"):
            previous_files.append("csv/" + file)

    return render_template(
        "history.html",
        coin_list=get_coins(),
        history=history,
        filename="-",
        files=previous_files,
        custom=current_app.config["CUSTOM"],
    )


@app.route("/history/<start>/<end>")
def history_page_timeframe(start, end):
    try:
        startdate, enddate = start, end
        start = (
            datetime.combine(datetime.fromisoformat(start), datetime.min.time()).timestamp() * 1000
        )
        end = datetime.combine(datetime.fromisoformat(end), datetime.max.time()).timestamp() * 1000
    except Exception:
        return redirect(url_for("main.history_page"))

    ranges = timeranges()

    history = db.query(
        "SELECT * FROM income WHERE time >= ? AND time <= ? ORDER BY time desc",
        [start, end],
    )

    history_temp = []
    for inc in history:
        inc = list(inc)
        inc[7] = datetime.fromtimestamp(inc[7] / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
        history_temp.append(inc)
    history = history_temp

    filename = (
        datetime.now().strftime("%Y-%m-%dT%H%M%S") + "_income_" + startdate + "_" + enddate + ".csv"
    )

    with open(os.path.join(app.root_path, "static", "csv", filename), "w", newline="") as csvfile:
        spamwriter = csv.writer(csvfile, delimiter=",")
        spamwriter.writerow(
            [
                "sqliteID",
                "TransactionId",
                "Symbol",
                "IncomeType",
                "Income",
                "Asset",
                "Info",
                "Time",
                "TradeId",
            ]
        )
        spamwriter.writerows(history)

    history = {"columns": []}

    temp: tuple[str, str]
    for timeframe in ranges:
        start = (
            datetime.combine(datetime.fromisoformat(timeframe[0]), datetime.min.time()).timestamp()
            * 1000
        )
        end = (
            datetime.combine(datetime.fromisoformat(timeframe[1]), datetime.max.time()).timestamp()
            * 1000
        )
        incomesummary = db.query(
            "SELECT incomeType, COUNT(IID) FROM income WHERE time >= ? AND time <= ? GROUP BY incomeType",
            [start, end],
        )
        temp = timeframe[0] + "/" + timeframe[1]
        if temp not in history:
            history[temp] = {}
            history[temp]["total"] = 0
            history[temp]["start"] = timeframe[0]
            history[temp]["end"] = timeframe[1]

        for totals in incomesummary:
            history[temp][totals[0]] = int(totals[1])
            history[temp]["total"] += int(totals[1])
            if totals[0] not in history["columns"]:
                history["columns"].append(totals[0])
    for timeframe in ranges:
        temp = timeframe[0] + "/" + timeframe[1]
        for column in history["columns"]:
            if column not in history[temp]:
                history[temp][column] = 0

    history["columns"].sort()

    filename = "csv/" + filename

    previous_files = []
    for file in os.listdir(os.path.join(app.root_path, "static", "csv")):
        if file.endswith(".csv"):
            previous_files.append("csv/" + file)

    return render_template(
        "history.html",
        coin_list=get_coins(),
        history=history,
        fname=filename,
        files=previous_files,
        custom=current_app.config["CUSTOM"],
    )


@app.route("/projection")
def projection_page():
    balance = db.query("SELECT totalWalletBalance FROM account WHERE AID = 1", one=True)
    projections: Projections = {
        "dates": [],
        "proj": {},
        "pcustom": [],
        "pcustom_value": 0.0,
    }
    # Sin balance (0, NULL o sin fila de account) no hay nada que proyectar; con 0 dividía por cero.
    if balance is not None and balance[0]:

        ranges = timeranges()

        todayend = (
            datetime.combine(datetime.fromisoformat(ranges[0][1]), datetime.max.time()).timestamp()
            * 1000
        )
        minus_7_start = (
            datetime.combine(
                datetime.fromisoformat((date.today() - timedelta(days=7)).strftime("%Y-%m-%d")),
                datetime.max.time(),
            ).timestamp()
            * 1000
        )

        week = db.query(
            'SELECT SUM(income) FROM income WHERE asset <> "BNB" AND incomeType NOT IN'
            + " ({0})".format(", ".join("?" for _ in remove_incomeTypes))
            + " AND time >= ? AND time <= ?",
            remove_incomeTypes + [minus_7_start, todayend],
            one=True,
        )
        custom = round(week[0] / balance[0] * 100 / 7, 2)
        projections["pcustom_value"] = custom
        today = date.today()
        x = 1
        config_projections = current_app.config["CUSTOM"]["PROJECTIONS"]
        while x < 365:
            nextday = today + timedelta(days=x)
            projections["dates"].append(nextday.strftime("%Y-%m-%d"))

            for each_projection in config_projections:
                if each_projection not in projections["proj"]:
                    projections["proj"][each_projection] = []

                if len(projections["proj"][each_projection]) < 1:
                    newbalance = balance[0]
                    projections["proj"][each_projection].append(newbalance)
                else:
                    newbalance = projections["proj"][each_projection][-1]

                projections["proj"][each_projection].append(newbalance * each_projection)

            if len(projections["pcustom"]) < 1:
                newbalance = balance[0]
            else:
                newbalance = projections["pcustom"][-1]

            projections["pcustom"].append(newbalance * (1 + (week[0] / balance[0]) / 7))

            x += 1

    return render_template(
        "projection.html",
        coin_list=get_coins(),
        data=projections,
        custom=current_app.config["CUSTOM"],
    )


# --------------------------------------------------------------------------------------
# Panel passivbot (reemplaza guardian / servicio SCC)
# --------------------------------------------------------------------------------------


def _json_response(payload, status=200):
    return Response(json.dumps(payload), status=status, mimetype="application/json")


def get_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.app_context_processor
def inject_csrf_token():
    return {"csrf_token": get_csrf_token}


def csrf_protect(view):
    """Exige header X-CSRF-Token igual al token de la sesion y body JSON."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        expected = session.get("csrf_token") or ""
        sent = request.headers.get("X-CSRF-Token") or ""
        if not expected or not hmac.compare_digest(expected, sent):
            return _json_response({"ok": False, "error": "CSRF token invalido o ausente."}, 403)
        if not request.is_json:
            return _json_response({"ok": False, "error": "Se requiere Content-Type application/json."}, 415)
        return view(*args, **kwargs)

    return wrapped


def _bot_store() -> bot_control.Store:
    return bot_control.Store(state_dir(current_app.config))


JOBS_EXTENSION = "futuresboard_bot_jobs"


@app.record_once
def _init_bot_jobs(state) -> None:
    """Registro de jobs del panel (uno por app/proceso). Recupera un job cortado por un reinicio."""
    store = bot_control.Store(state_dir(state.app.config))
    registry = jobs.JobRegistry(store)
    state.app.extensions[JOBS_EXTENSION] = registry
    jobs.recover_interrupted(registry, state.app.logger)


def _bot_jobs() -> jobs.JobRegistry:
    return current_app.extensions[JOBS_EXTENSION]


def _reject(action: str, params: dict, status: int, payload: dict, outcome: str):
    """Rechazo síncrono (antes de crear el job): se audita y, si corresponde, se notifica."""
    store = _bot_store()
    user = session.get("username") or "?"
    try:
        store.audit(user, action, params, outcome, jobs.audit_detail(payload), request.remote_addr)
    except OSError:
        current_app.logger.exception("No se pudo escribir el log de auditoria")
    try:
        telegram_notify.notify_bot_action(action, params, outcome, payload, user)
    except Exception:  # pragma: no cover - notify_bot_action ya no lanza
        current_app.logger.warning("No se pudo notificar la accion %s a Telegram", action)
    return _json_response(payload, status)


def _run_bot_action(action: str, params: dict, validate, work, despues=None, si_no_arranca=None):
    """Valida de forma síncrona y lanza la acción como job (202 + job_id).

    ``validate()`` no hace I/O contra Docker ni Binance: enum, panel habilitado, modos y
    credenciales. Sus errores responden 400/403/409/503 en el momento, sin crear job.
    ``work(store, settings, extra, reporter)`` corre en el hilo del job.
    ``despues(job, payload)``: paso sin el lock y cancelable (``jobs.launch``). ``si_no_arranca()``: se llama si el job
    no se creó (validación o una acción en curso), por ejemplo para devolver un pendiente de un solo uso.
    """
    try:
        settings, extra = validate()
    except ValueError as exc:
        if si_no_arranca is not None:
            si_no_arranca()
        return _reject(action, params, 400, {"ok": False, "error": str(exc)}, "rechazado")
    except bot_control.BotControlError as exc:
        if si_no_arranca is not None:
            si_no_arranca()
        return _reject(action, params, exc.status_code, {**exc.payload, "ok": False, "error": str(exc)}, "error")

    store = _bot_store()
    ctx = jobs.JobContext(store, settings, session.get("username") or "?", request.remote_addr,
                          current_app.logger)
    try:
        job = jobs.launch(_bot_jobs(), action, params, ctx, lambda rep: work(store, settings, extra, rep),
                          despues=despues)
    except jobs.ActiveJobError as exc:
        if si_no_arranca is not None:
            si_no_arranca()
        return _json_response(
            {"ok": False, "error": "Ya hay una acción del panel en curso: esperá a que termine.",
             "job_id": exc.job_id},
            409,
        )
    return _json_response({"ok": True, "job_id": job.id, "job": job.to_dict()}, 202)


# Tope del inspect a Docker cuando la página o el JS solo necesitan el estado para pintar (píldora, Inicio, Bot):
# con el docker-proxy colgado la página sale igual y el estado dice "sin datos" (como /api/vivo/inicio).
BOT_STATUS_RAPIDO_S = 3


def exposicion_scraper() -> dict:
    """Posiciones y órdenes abiertas según la base del scraper (no Binance): el cuarto estado del bot (detenido con
    exposición) y el control rápido de Inicio. Nunca lanza: sin base, ``error`` y ceros."""
    try:
        posiciones = db.query("SELECT symbol, positionSide, positionAmt, entryPrice, unrealizedProfit FROM positions "
                              "WHERE ABS(positionAmt) > 0 ORDER BY symbol ASC")
        ordenes = db.query("SELECT symbol, side, positionSide, origQty, price FROM orders ORDER BY symbol ASC")
    except Exception:  # noqa: BLE001 - defensivo: el estado del bot no se cae por la base
        current_app.logger.exception("No se pudo leer posiciones y órdenes para el estado del bot")
        return {"posiciones": 0, "ordenes": 0, "pares": [], "upnl": 0.0, "nocional": 0.0, "detalle": [],
                "error": "No se pudo leer la base del scraper."}
    detalle = [{"symbol": r[0], "lado": r[1], "cantidad": float(r[2]), "precio_entrada": float(r[3] or 0),
                "upnl": float(r[4] or 0)} for r in posiciones]
    pares = sorted({r[0] for r in posiciones} | {r[0] for r in ordenes})
    return {
        "posiciones": len(posiciones),
        "ordenes": len(ordenes),
        "pares": pares,
        "upnl": round(sum(p["upnl"] for p in detalle), 2),
        "nocional": round(sum(abs(p["cantidad"] * p["precio_entrada"]) for p in detalle), 2),
        "detalle": detalle,
    }


def pares_ahora(estrategia: dict | None, exposicion: dict) -> dict:
    """Lugares ocupados y, por par, posición (% del balance) y órdenes abiertas (base del scraper).

    Para forager una orden abierta ocupa un lugar igual que una posición (``forager.py:130``): ocupados = pares con
    posición ∪ pares con órdenes. ``fuera_de_lista``: con posición u órdenes y fuera de ``approved_symbols_long`` (sigue
    en parada gradual hasta cerrar, spec § 1.2). Los chequeos de forager por ciclo (mínimo, edad, velas, TRADING)
    necesitan exchangeInfo y velas cacheados (spec § 5.2): no van acá todavía."""
    est = estrategia or {}
    aprobados = list(est.get("pares_long") or [])
    try:
        fila = db.query("SELECT totalWalletBalance FROM account WHERE AID = 1", one=True)
        balance = float(fila[0]) if fila is not None and fila[0] else None
        ordenes = db.query("SELECT symbol, side FROM orders")
    except Exception:  # noqa: BLE001 - defensivo: el estado no se cae por la base
        balance, ordenes = None, []
    por: dict = {}

    def par(sym: str) -> dict:
        return por.setdefault(sym, {"symbol": sym, "aprobado": sym in aprobados, "posicion": 0.0, "nocional": 0.0,
                                    "pct_balance": None, "compras": 0, "ventas": 0})

    for sym in aprobados:
        par(sym)
    for p in exposicion.get("detalle") or []:
        if str(p.get("lado")).upper() == "SHORT":
            continue
        d = par(p["symbol"])
        d["posicion"] += abs(p["cantidad"])
        d["nocional"] += abs(p["cantidad"] * p["precio_entrada"])
    for sym, side in ordenes:
        d = par(sym)
        d["compras" if side == "BUY" else "ventas"] += 1
    ocupados = 0
    for d in por.values():
        d["nocional"] = round(d["nocional"], 2)
        if balance:
            d["pct_balance"] = round(d["nocional"] / balance * 100, 2)
        con_algo = d["posicion"] > 0 or d["compras"] or d["ventas"]
        d["estado"] = "posicion" if d["posicion"] > 0 else ("orden" if con_algo else "libre")
        d["fuera_de_lista"] = bool(con_algo) and not d["aprobado"]
        ocupados += 1 if con_algo else 0
    orden = {s: i for i, s in enumerate(aprobados)}
    pares = sorted(por.values(), key=lambda d: (not d["aprobado"], orden.get(d["symbol"], 0), d["symbol"]))
    return {"lugares_total": est.get("n_longs"), "lugares_ocupados": ocupados, "pares": pares, "balance": balance}


def bot_status_payload(docker_timeout: float | None = None) -> tuple[dict, int]:
    """Estado del bot y HTTP status: lo devuelven /api/bot/status y, dentro de su JSON, /api/vivo/inicio.

    ``docker_timeout``: tope del inspect al docker-proxy (default ``bot_control.HTTP_TIMEOUT_SECONDS``, 10 s).
    ``estado_bot`` (operando / gradual / detenido / expuesto / sin_datos) y ``exposicion`` (base del scraper) van
    también en el error: la píldora y el punto del menú los pintan igual.
    """
    settings = bot_control.Settings()
    docker = None
    if docker_timeout is not None and settings.enabled:
        docker = bot_control.DockerClient(settings.docker_url, timeout=docker_timeout)
    exposicion = exposicion_scraper()
    try:
        out = bot_control.get_status(settings, docker=docker)
    except bot_control.BotControlError as exc:
        return {"enabled": settings.enabled, "error": str(exc), "estado_bot": bot_control.ESTADO_SIN_DATOS,
                "exposicion": exposicion}, exc.status_code
    out["exposicion"] = exposicion
    out["estado_bot"] = bot_control.estado_del_bot(out, exposicion)
    out["pares_ahora"] = pares_ahora(out.get("estrategia"), exposicion)
    data_dir = _bot_store().data_dir
    out["rescate_start_bloqueado"] = rescate.start_bloqueado(data_dir)
    # Perfiles de riesgo y TWE manual (docs/perfiles.md): definiciones y el perfil vigente según los valores.
    out.update(perfiles.catalogo())
    rescate_cfg, _err = rescate.load_config(data_dir)
    # Un perfil es TWE + rescate + los 4 pares medidos, 4 a la vez y sus configs (manifiesto de passivbot).
    detalle = {"perfil": None, "motivo": None}
    if out.get("config_available"):
        est = out.get("estrategia") or {}
        detalle = perfiles.detectar_detalle(
            {"twe_long": out.get("twe_long"), "n_longs": out.get("n_longs"), "n_shorts": out.get("n_shorts"),
             "approved_symbols_long": est.get("pares_long")}, rescate_cfg, manifiesto.leer(settings.forager_config))
    out["perfil_vigente"] = detalle["perfil"]
    out["perfil_motivo"] = detalle["motivo"]
    out["perfil_ultimo"] = rescate_cfg.get("perfil")
    out["twe_manual"] = {"min": str(bot_control.TWE_MANUAL_MIN), "max": str(bot_control.TWE_MANUAL_MAX),
                         "paso": str(bot_control.TWE_MANUAL_PASO), "advertencia": str(bot_control.TWE_ADVERTENCIA)}
    return out, 200


@app.route("/api/bot/status", methods=["GET"])
def bot_status():
    """``?rapido=1``: inspect con tope corto (la píldora de cada página, ver static/js/bot_acciones.js)."""
    rapido = request.args.get("rapido") == "1"
    out, status = bot_status_payload(docker_timeout=BOT_STATUS_RAPIDO_S if rapido else None)
    return _json_response(out, status)


# --------------------------------------------------------------------------------------
# Páginas Bot y Estrategia (diseno/bot-estrategia/spec.md § 2.3 y § 2.4); reemplazan a la rueda #menu_admin
# --------------------------------------------------------------------------------------


def _estrategia_vigente() -> tuple[dict | None, str | None]:
    """Claves de estrategia de la config de forager (solo lectura) o el motivo por el que no se pudo leer."""
    try:
        cfg = bot_control.read_forager_config(bot_control.Settings().require_config())
    except bot_control.BotControlError as exc:
        return None, str(exc)
    vigente = bot_control.resumen_estrategia(cfg)
    rescate_cfg, _err = rescate.load_config(_bot_store().data_dir)
    detalle = perfiles.detectar_detalle(cfg, rescate_cfg, manifiesto.leer(bot_control.Settings().forager_config))
    vigente["perfil"], vigente["perfil_motivo"] = detalle["perfil"], detalle["motivo"]
    vigente["long_mode"], vigente["short_mode"] = cfg.get("long_mode"), cfg.get("short_mode")
    return vigente, None


def _pares_cortos(pares) -> str:
    """["BNBUSDT", "BTCUSDT", "ETHUSDT"] -> "BNB, BTC y ETH"."""
    cortos = [p[:-4] if isinstance(p, str) and p.endswith("USDT") else str(p) for p in pares or []]
    if len(cortos) <= 1:
        return "".join(cortos)
    return ", ".join(cortos[:-1]) + " y " + cortos[-1]


def _fecha_utc(momento: datetime | None) -> str:
    return f"{momento:%d/%m %H:%M} UTC" if momento else ""


_TITULOS_ACCION = {"start": "inicio", "graceful": "parada gradual", "apagar": "apagar", "perfil": "perfil",
                   "estrategia": "estrategia", "credenciales": "cambio de API key"}
_ESTADOS_JOB = {"ok": "ok", "error": "con error", "interrumpido": "interrumpida", "en_curso": "en curso"}


def ultima_accion() -> dict | None:
    """La última acción del panel (``bot_job_last.json``), con un título en palabras. Sin IP (el job no la guarda)."""
    data = _bot_jobs().load_persisted()
    if not data:
        return None
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    accion = data.get("accion")
    titulo = _TITULOS_ACCION.get(accion, str(accion))
    if accion == "start" and params.get("reanudar"):
        titulo = {"reanudar": "reanudar", "reiniciar": "reiniciar"}.get(params.get("como"), "iniciar")
    elif accion == "start" and params.get("twe_long") is not None:
        titulo = f"TWE manual {formato.num(params.get('twe_long'))}"
    elif accion == "start" and params.get("riesgo"):
        titulo = f"inicio con riesgo {params.get('riesgo')}"
    elif accion == "perfil":
        nombre = (perfiles.PERFILES.get(params.get("perfil")) or {}).get("nombre") or params.get("perfil")
        titulo = f"perfil {nombre}"
    elif accion == "estrategia":
        titulo = "volver a una versión" if params.get("volver") else "estrategia"
    elif accion == "credenciales":
        titulo = ("volver a la API key anterior" if params.get("volver") else "cambio de API key") + \
            (f" ({params.get('key')})" if params.get("key") else "")
    return {**{k: v for k, v in data.items() if k != "pid"}, "titulo": titulo,
            "estado_texto": _ESTADOS_JOB.get(data.get("estado"), str(data.get("estado")))}


def contexto_control(st: dict) -> dict:
    """Lo que necesita el control rápido de Inicio (``_bot.html`` → ``control``): la página y /api/vivo/inicio."""
    est = st.get("estrategia") or {}
    c = st.get("container") or {}
    ahora = datetime.now().astimezone()
    desde = bot_control.parse_docker_time(c.get("started_at")) if c.get("running") else None
    parado = bot_control.parse_docker_time(c.get("finished_at")) if not c.get("running") else None
    perfil = (st.get("perfiles") or {}).get(st.get("perfil_vigente") or "") or {}
    ultima = ultima_accion()
    return {
        "enabled": bool(st.get("enabled")),
        "estado": st.get("estado_bot") or bot_control.ESTADO_SIN_DATOS,
        "error": st.get("error") or st.get("message") or "",
        "perfil": perfil.get("nombre") or "Personalizado",
        "twe": est.get("twe_long", st.get("twe_long")),
        "n_longs": est.get("n_longs", st.get("n_longs")),
        "pares": _pares_cortos(est.get("pares_long")),
        "desde": _fecha_utc(desde),
        "hace": _hace((ahora - desde).total_seconds()) if desde else "",
        "detenido_desde": _fecha_utc(parado),
        "ultima": f"{ultima['titulo']}, {ultima['estado_texto']}" if ultima else "",
        "expo": st.get("exposicion") or {},
        "bloqueo": st.get("rescate_start_bloqueado"),
    }


def latidos() -> dict:
    """Latidos para Bot: scraper, servicio rescate, C3 y acciones del panel. Solo lee archivos y la memoria del
    proceso: el de acciones mira el registro de jobs y el último persistido, nunca el lock de archivo (tomarlo, aunque
    sea un instante, podría hacer que un Apagar responda 409)."""
    data_dir = _bot_store().data_dir
    f = scraper.frescura_web(current_app.config)
    edad, intervalo = f["edad_segundos"], f["intervalo_segundos"] or 60
    scr = {"estado": "sin_latido" if edad is None else ("late" if edad <= 3 * intervalo else "viejo"),
           "edad": _hace(edad) if edad is not None else None, "intervalo": intervalo}
    try:
        r = rescate.panel_status(data_dir)
        svc = r.get("servicio") or {}
        hb = svc.get("heartbeat")
        edad_r = (datetime.now().astimezone() - datetime.fromisoformat(hb)).total_seconds() if hb else None
        res = {"estado": "late" if svc.get("vivo") else ("sin_latido" if not hb else "no_late"), "modo": svc.get("modo"),
               "edad": _hace(edad_r) if edad_r is not None else None}
    except Exception:  # noqa: BLE001 - un archivo roto no tira la página
        res = {"estado": "sin_latido", "modo": None, "edad": None}
    try:
        c = c3.panel_status(data_dir)
        c3_ = {"estado": c.get("estado"), "servicio_vivo": c.get("servicio_vivo"), "modo": c.get("modo"),
               "parado": c.get("c3_parado"), "corte": (c.get("corte") or {}).get("en")}
    except Exception:  # noqa: BLE001
        c3_ = {"estado": None, "servicio_vivo": False, "modo": None, "parado": False, "corte": None}
    activo = _bot_jobs().active()
    persistido = _bot_jobs().load_persisted() or {}
    if activo is not None:
        acc = {"estado": "en_curso", "accion": activo.accion, "desde": activo.inicio}
    elif persistido.get("estado") == jobs.ESTADO_EN_CURSO:
        acc = {"estado": "en_curso", "accion": persistido.get("accion"), "desde": persistido.get("inicio")}
    else:
        acc = {"estado": "libre", "accion": None, "desde": None}
    return {"scraper": scr, "rescate": res, "c3": c3_, "acciones": acc}


def _fecha_iso_utc(valor) -> str:
    try:
        return f"{datetime.fromisoformat(str(valor)).astimezone(timezone.utc):%d/%m %H:%M:%S}"
    except ValueError:
        return str(valor or "")


def _duracion(inicio, fin) -> str:
    try:
        s = (datetime.fromisoformat(str(fin)) - datetime.fromisoformat(str(inicio))).total_seconds()
    except ValueError:
        return ""
    s = max(0, int(s))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def _chequeos_ciclo(aprobados) -> dict | None:
    """Último estado de los chequeos de forager por ciclo (``chequeos_forager``), solo de los aprobados de ahora. Lee
    el archivo: Bot nunca llama a Binance al armarse."""
    estado = chequeos_forager.leer_estado(_bot_store().data_dir)
    if not estado:
        return None
    pares = estado.get("pares") or {}
    aprobados = list(aprobados or [])
    filas = [{"symbol": s, **pares[s]} for s in aprobados if isinstance(pares.get(s), dict)]
    try:
        edad = (datetime.now(timezone.utc) - datetime.fromisoformat(str(estado.get("ts")))).total_seconds()
    except ValueError:
        edad = None
    return {"filas": filas, "hace": _hace(edad) if edad is not None else "", "error": estado.get("error"),
            "intervalo_min": int((estado.get("intervalo_s") or 900) // 60), "endpoint": estado.get("endpoint"),
            "sin_chequear": [s for s in aprobados if s not in pares]}


def _ultima_orden_ms() -> int | None:
    """Creación de la orden abierta más nueva según el scraper (para "passivbot creó órdenes después de arrancar")."""
    try:
        with db.connect(current_app.config["DATABASE"]) as conn:
            row = conn.execute("SELECT MAX(time) FROM orders").fetchone()
    except Exception:  # noqa: BLE001 - sin DB, sin dato
        return None
    return int(row[0]) if row and row[0] is not None else None


def conexion_binance(st: dict) -> dict:
    """Bot → "Conexión con Binance" (spec § 10.4, paso 1). Solo lee archivos y la memoria del proceso: nunca llama a
    Binance. Sin key, secret ni uid (``futuresboard.conexion``)."""
    prov = credenciales.de_app(current_app._get_current_object())
    data_dir = _bot_store().data_dir
    ahora = time.time()
    latido_scr = scraper.read_latido(current_app.config["DATABASE"])
    intervalo = latido_scr.get("intervalo") if isinstance(latido_scr.get("intervalo"), int) else None
    scr_vivo = bool(latido_scr) and ahora - float(latido_scr["at"]) <= 3 * (intervalo or 60)
    try:
        estado_r, _err = rescate.load_state(data_dir)
        servicio = estado_r.get("servicio") if isinstance(estado_r.get("servicio"), dict) else {}
    except Exception:  # noqa: BLE001
        estado_r, servicio = {}, {}
    hb = servicio.get("heartbeat")
    try:
        edad_r = ahora - datetime.fromisoformat(hb).timestamp() if hb else None
        res_vivo = edad_r is not None and edad_r <= 3 * int(servicio.get("intervalo") or rescate.DEFAULT_INTERVAL) + 30
    except (TypeError, ValueError):
        res_vivo = False
    return conexion.vista(
        prov.estado(), modo_secretos=prov.modo_secretos(), testnet=bool(current_app.config.get("BINANCE_TESTNET")),
        meta=credenciales.leer_meta(data_dir), scraper_latido=latido_scr, scraper_vivo=scr_vivo,
        scraper_intervalo=intervalo, rescate_servicio=servicio, rescate_vivo=res_vivo,
        container=st.get("container"), ultima_orden_ms=_ultima_orden_ms(), ahora=ahora,
        key_enmascarada=prov.enmascarada(), web=_cambio_web(prov),
        rescate_no_firma=conexion.rescate_no_firma(estado_r))


# --------------------------------------------------------------------------------------
# Cambiar la API key desde el panel (spec bot-estrategia § 10.4 a § 10.7; futuresboard.cambio_key)
# --------------------------------------------------------------------------------------

# La página de la key no usa base.html ni scripts de terceros: todo local y esta CSP (§ 10.4, paso 0).
CSP_CREDENCIALES = ("default-src 'self'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; "
                    "base-uri 'none'")
CAMBIO_EXTENSION = "futuresboard_cambio_key"


def _cambio_estado() -> dict:
    """Pendientes y límite de validaciones del proceso (gunicorn corre 1 worker). ``cliente``: fábrica de clientes de
    Binance para validar (los tests ponen uno falso)."""
    app_ = current_app._get_current_object()
    est = app_.extensions.get(CAMBIO_EXTENSION)
    if est is None:
        est = app_.extensions.setdefault(CAMBIO_EXTENSION, {"pendientes": cambio_key.Pendientes(),
                                                            "limite": cambio_key.Limitador(), "cliente": None})
    return est


def _cambio_motivo(prov=None) -> str | None:
    """Por qué no se puede cambiar la key desde la web (None: se puede)."""
    if not cambio_key.web_habilitada():
        return (f"Apagado: se prende con {cambio_key.ENV_WEB}=1 en el entorno del panel (en dev para probar; en prod, "
                "solo con DALE).")
    prov = prov or credenciales.de_app(current_app._get_current_object())
    if not prov.modo_secretos():
        return ("Falta el directorio de credenciales (/secretos): la key todavía sale de config.json y el panel no "
                "tiene dónde escribirla.")
    if str(current_app.config.get("EXCHANGE") or "").lower() != "binance":
        return "Solo para Binance Futures."
    return None


def _cambio_web(prov) -> dict:
    motivo = _cambio_motivo(prov)
    web = {"habilitada": motivo is None, "motivo": motivo, "url": None, "volver": None}
    if motivo is None:
        web["url"] = url_for("main.credenciales_page")
        try:
            r = cambio_key.respaldo_anterior(credenciales.directorio_respaldo(),
                                             prov.estado().get("usuario") or credenciales.USUARIO_DEFECTO,
                                             prov.estado().get("key_id"))
        except Exception:  # noqa: BLE001
            r = None
        if r:
            web["volver"] = {"key": r["key"], "fecha": conexion.fecha(r["fecha"]), "url": web["url"] + "?volver=1"}
    return web


def _cambio_entorno() -> cambio_key.Entorno:
    cfg = current_app.config
    testnet = bool(cfg.get("BINANCE_TESTNET"))
    prov = credenciales.de_app(current_app._get_current_object())
    data_dir = _bot_store().data_dir
    try:
        servicio = (rescate.load_state(data_dir)[0] or {}).get("servicio") or {}
    except Exception:  # noqa: BLE001
        servicio = {}
    return cambio_key.Entorno(
        testnet=testnet, futures_url=cfg.get("API_BASE_URL") or "",
        spot_url=rescate.spot_base_url(testnet, os.environ.get(rescate.ENV_SPOT_URL, "").strip().rstrip("/")),
        proveedor=prov, data_dir=data_dir, db_path=str(cfg["DATABASE"]), dir_secretos=prov.dir,
        dir_respaldo=credenciales.directorio_respaldo(),
        rescate_real=isinstance(servicio, dict) and servicio.get("modo") == "real",
        cliente=_cambio_estado().get("cliente"))


def _cambio_404():
    """Con FUTURESBOARD_CREDENCIALES_WEB apagado, la página y los POST no existen."""
    if not cambio_key.web_habilitada():
        return _json_response({"ok": False, "error": "No existe."}, 404)
    return None


def _audit_cambio(action: str, params: dict, outcome: str, detail: str) -> None:
    try:
        _bot_store().audit(session.get("username") or "?", action, params, outcome, detail, request.remote_addr)
    except OSError:
        current_app.logger.exception("No se pudo escribir el log de auditoria")


def _reautenticar(password) -> tuple[int, str] | None:
    """La contraseña del panel, contra ``users`` (como /settings), con el MISMO limitador del login por usuario e IP
    (``auth._reserve_attempt``: 5 fallos en 15 min). Cada fallo avisa por Telegram. None = ok."""
    from werkzeug.security import check_password_hash

    from futuresboard import auth

    usuario = session.get("username") or ""
    ip = auth._client_ip()
    clave = (usuario.lower(), ip)
    if auth._reserve_attempt(clave) is None:
        current_app.logger.warning("Re-autenticación bloqueada por rate limit: usuario=%r ip=%s", usuario, ip)
        return 429, "Demasiados intentos fallidos con la contraseña: probá de nuevo en unos minutos."
    with db.connect(current_app.config["DATABASE"]) as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (usuario,)).fetchone()
    valida = (bool(row) and isinstance(password, str) and bool(password)
              and check_password_hash(row[0], password)
              and not check_password_hash(row[0], auth.BLOCKED_DEFAULT_PASSWORD))
    if valida:
        auth._clear_failures(clave)
        return None
    current_app.logger.warning("Re-autenticación fallida en el cambio de API key: usuario=%r ip=%s", usuario, ip)
    try:
        telegram_notify.notify_bot_action("credenciales", {}, "error",
                                          {"error": "Contraseña del panel incorrecta al confirmar el cambio de API "
                                                    "key."}, usuario or "?")
    except Exception:  # pragma: no cover - notify_bot_action no lanza
        pass
    return 403, "Contraseña incorrecta."


def _container_rapido() -> dict | None:
    settings = bot_control.Settings()
    if not settings.enabled:
        return None
    try:
        return bot_control.DockerClient(settings.docker_url, timeout=BOT_STATUS_RAPIDO_S).inspect(settings.container)
    except bot_control.BotControlError:
        return None


@app.route("/bot/credenciales", methods=["GET"])
def credenciales_page():
    """Página propia del cambio de key: template mínimo sin base.html, solo JS y CSS locales, campos fuera de un
    ``<form>`` y la CSP de § 10.4. Con el flag apagado, 404."""
    if not cambio_key.web_habilitada():
        return not_found(None)
    prov = credenciales.de_app(current_app._get_current_object())
    motivo = _cambio_motivo(prov)
    if motivo is None:
        # La primera vez, la línea de base de la key actual (huella del uid): Bot pasa a decir "UID verificado".
        try:
            cambio_key.asegurar_linea_base(_cambio_entorno())
        except Exception:  # noqa: BLE001 - abrir la página nunca falla por Binance
            current_app.logger.exception("No se pudo guardar la línea de base de la API key")
    volver = None
    if motivo is None and request.args.get("volver") == "1":
        r = cambio_key.respaldo_anterior(credenciales.directorio_respaldo(),
                                         prov.estado().get("usuario") or credenciales.USUARIO_DEFECTO,
                                         prov.estado().get("key_id"))
        volver = {"key": r["key"], "fecha": conexion.fecha(r["fecha"])} if r else {"key": None, "fecha": None}
    testnet = bool(current_app.config.get("BINANCE_TESTNET"))
    html = render_template("credenciales.html", motivo=motivo, volver=volver, testnet=testnet,
                           key_actual=prov.enmascarada(), usuario=prov.estado().get("usuario") or "—",
                           max_validaciones=cambio_key.VALIDACIONES_MAX)
    resp = Response(html, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = CSP_CREDENCIALES
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


@app.route("/api/bot/credenciales", methods=["GET"])
def credenciales_ver():
    """Bot → "Conexión con Binance" en JSON. Nunca devuelve key, secret ni uid. Con el cambio desde la web prendido, la
    primera vez guarda la línea de base (huella del uid de la key actual, validada en ese momento)."""
    prov = credenciales.de_app(current_app._get_current_object())
    if _cambio_motivo(prov) is None:
        try:
            cambio_key.asegurar_linea_base(_cambio_entorno())
        except Exception:  # noqa: BLE001 - ver la conexión nunca falla por Binance
            current_app.logger.exception("No se pudo guardar la línea de base de la API key")
    data = conexion_binance({"container": _container_rapido()})
    return _json_response({"ok": True, **data})


@app.route("/api/bot/credenciales/validar", methods=["POST"])
@csrf_protect
def credenciales_validar():
    """``{api_key, api_secret}`` → chequeos de § 10.2 y, si pasa, ``pendiente_id`` (5 min, un uso). Nunca repite lo que
    llegó: la key vuelve enmascarada y el secret no vuelve."""
    no = _cambio_404()
    if no is not None:
        return no
    motivo = _cambio_motivo()
    if motivo:
        return _json_response({"ok": False, "error": motivo}, 409)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json_response({"ok": False, "error": "Se espera un objeto JSON con api_key y api_secret."}, 400)
    api_key, api_secret = body.get("api_key"), body.get("api_secret")
    del body
    usuario = session.get("username") or "?"
    try:
        cambio_key.validar_formato(api_key, api_secret)
    except ValueError as exc:
        _audit_cambio("credenciales_validar", {"key": None}, "rechazado", str(exc))
        # Telegram en cada intento (§ 10.6), también los que no llegan a Binance. Sin lo que se pegó.
        telegram_notify.notify_bot_action("credenciales_validar", {}, "error", {"error": str(exc)}, usuario)
        return _json_response({"ok": False, "error": str(exc),
                               "chequeos": [{"clave": "formato", "estado": cambio_key.BLOQUEA, "texto": str(exc)}]}, 400)
    # Apenas llegan y con formato válido: desde acá cualquier log que los nombre sale tapado.
    logs.register_secret(api_key)
    logs.register_secret(api_secret)
    enmascarada = credenciales.enmascarar(api_key)
    est = _cambio_estado()
    espera = est["limite"].intentar(usuario)
    if espera is not None:
        texto = (f"Hasta {cambio_key.VALIDACIONES_MAX} validaciones cada 15 minutos: probá de nuevo en "
                 f"{max(1, espera // 60)} min.")
        _audit_cambio("credenciales_validar", {"key": enmascarada}, "rechazado", texto)
        telegram_notify.notify_bot_action("credenciales_validar", {"key": enmascarada}, "error", {"error": texto},
                                          usuario)
        return _json_response({"ok": False, "error": texto}, 429)
    env = _cambio_entorno()
    try:
        original = cambio_key.leer_archivo(env.dir_secretos)
    except OSError as exc:
        return _json_response({"ok": False, "error": f"No se pudo leer el archivo de credenciales "
                                                     f"({exc.__class__.__name__})."}, 409)
    res = cambio_key.validar(env, api_key, api_secret)
    params = {"key": enmascarada, "cuenta": "otra" if res.otra_cuenta else ("misma" if res.ok else "sin_verificar"),
              "entorno": "demo" if env.testnet else "real"}
    if not res.ok:
        _audit_cambio("credenciales_validar", params, "rechazado", res.motivo or "")
        telegram_notify.notify_bot_action("credenciales_validar", params, "error", {"error": res.motivo}, usuario)
        return _json_response({"ok": False, "key": enmascarada, "chequeos": res.chequeos, "error": res.motivo,
                               "otra_cuenta": res.otra_cuenta}, 422)
    p = est["pendientes"].crear(usuario=usuario, api_key=api_key, api_secret=api_secret,
                                key_id=credenciales.key_id(api_key), enmascarada=enmascarada, huella=res.huella,
                                archivo_sha=cambio_key.sha(original), hedge=res.hedge, permisos=res.permisos,
                                chequeos=res.chequeos)
    _audit_cambio("credenciales_validar", params, "ok", "validada: misma cuenta")
    telegram_notify.notify_bot_action("credenciales_validar", params, "ok", {}, usuario)
    return _json_response({"ok": True, "pendiente_id": p.id, "key": enmascarada, "chequeos": res.chequeos,
                           "vence_en_s": cambio_key.PENDIENTE_TTL_S})


def _cambio_confirmar(body, origen: str):
    """Plan (409) o, con ``confirmar``, el mismo ``plan_hash`` y la contraseña del panel, el job (202) con el mismo
    lock que START, perfil, stop y estrategia."""
    usuario = session.get("username") or "?"
    est = _cambio_estado()
    try:
        p = est["pendientes"].ver(body.get("pendiente_id"), usuario)
    except cambio_key.PendienteError as exc:
        return _json_response({"ok": False, "error": str(exc), "vencido": True}, 409)
    if p.origen != origen:
        return _json_response({"ok": False, "error": "La validación no corresponde a esta acción: validá de nuevo.",
                               "vencido": True}, 409)
    env = _cambio_entorno()
    info = _container_rapido()
    corriendo = bool(info.get("running")) if info and info.get("status") != "not_found" else None
    plan, plan_hash = cambio_key.armar_plan(env, p, corriendo)
    params = cambio_key.params_audit(p, env.testnet)
    if body.get("confirmar") is not True or body.get("plan_hash") != plan_hash:
        if body.get("confirmar"):
            texto = ("El plan ya no corresponde: cambió la key vigente, el archivo o el estado de passivbot desde que se "
                     "armó. Revisalo de nuevo.")
            _audit_cambio("credenciales", params, "rechazado", "plan_hash viejo o ausente")
        else:
            texto = plan["texto"]
            _audit_cambio("credenciales", params, "requiere_confirmacion", plan["titulo"])
        return _json_response({"ok": False, "requiere_confirmacion": True, "error": texto, "plan": plan,
                               "plan_hash": plan_hash, "pendiente_id": p.id}, 409)
    fallo = _reautenticar(body.get("password"))
    if fallo is not None:
        status, texto = fallo
        _audit_cambio("credenciales", params, "rechazado", texto)
        return _json_response({"ok": False, "error": texto, "reauth": True}, status)
    try:
        p = est["pendientes"].tomar(p.id, usuario)  # un solo uso
    except cambio_key.PendienteError as exc:
        return _json_response({"ok": False, "error": str(exc), "vencido": True}, 409)

    def validate():
        settings = bot_control.Settings()
        settings.require_enabled()
        return settings, None

    def work(_store, settings, _extra, rep):
        return cambio_key.aplicar(env, settings, p, usuario, rep)

    def despues(job, payload):
        return cambio_key.esperar_consumidores(env, job, payload)

    return _run_bot_action("credenciales", params, validate, work, despues=despues,
                           si_no_arranca=lambda: est["pendientes"].devolver(p))


@app.route("/api/bot/credenciales", methods=["POST"])
@csrf_protect
def credenciales_aplicar():
    """``{pendiente_id}`` → 409 con el plan; ``{pendiente_id, plan_hash, password, confirmar: true}`` → job (202)."""
    no = _cambio_404()
    if no is not None:
        return no
    motivo = _cambio_motivo()
    if motivo:
        return _json_response({"ok": False, "error": motivo}, 409)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json_response({"ok": False, "error": "Se espera un objeto JSON."}, 400)
    return _cambio_confirmar(body, "nueva")


@app.route("/api/bot/credenciales/volver", methods=["POST"])
@csrf_protect
def credenciales_volver():
    """Volver a la key del último respaldo (≤ 7 días, distinta de la actual): ``{}`` la valida con Binance y responde el
    plan (409, con ``pendiente_id``); después, el mismo flujo de confirmación con contraseña."""
    no = _cambio_404()
    if no is not None:
        return no
    motivo = _cambio_motivo()
    if motivo:
        return _json_response({"ok": False, "error": motivo}, 409)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json_response({"ok": False, "error": "Se espera un objeto JSON."}, 400)
    if body.get("pendiente_id"):
        return _cambio_confirmar(body, "volver")
    usuario = session.get("username") or "?"
    env = _cambio_entorno()
    estado = env.proveedor.estado()
    r = cambio_key.respaldo_anterior(env.dir_respaldo, estado.get("usuario") or credenciales.USUARIO_DEFECTO,
                                     estado.get("key_id"))
    if not r:
        return _json_response({"ok": False, "error": "No hay un respaldo de los últimos 7 días con otra key."}, 409)
    est = _cambio_estado()
    espera = est["limite"].intentar(usuario)
    if espera is not None:
        texto = (f"Hasta {cambio_key.VALIDACIONES_MAX} validaciones cada 15 minutos: probá de nuevo en "
                 f"{max(1, espera // 60)} min.")
        return _json_response({"ok": False, "error": texto}, 429)
    try:
        original = cambio_key.leer_archivo(env.dir_secretos)
    except OSError as exc:
        return _json_response({"ok": False, "error": f"No se pudo leer el archivo de credenciales "
                                                     f"({exc.__class__.__name__})."}, 409)
    res = cambio_key.validar(env, r["api_key"], r["api_secret"])
    params = {"key": r["key"], "cuenta": "misma" if res.ok else "sin_verificar",
              "entorno": "demo" if env.testnet else "real", "volver": True}
    if not res.ok:
        _audit_cambio("credenciales_validar", params, "rechazado", res.motivo or "")
        telegram_notify.notify_bot_action("credenciales_validar", params, "error", {"error": res.motivo}, usuario)
        return _json_response({"ok": False, "key": r["key"], "chequeos": res.chequeos, "error": res.motivo}, 422)
    p = est["pendientes"].crear(usuario=usuario, api_key=r["api_key"], api_secret=r["api_secret"],
                                key_id=r["key_id"], enmascarada=r["key"], huella=res.huella,
                                archivo_sha=cambio_key.sha(original), origen="volver", hedge=res.hedge,
                                permisos=res.permisos, chequeos=res.chequeos)
    _audit_cambio("credenciales_validar", params, "ok", "respaldo validado: misma cuenta")
    out = _cambio_confirmar({"pendiente_id": p.id}, "volver")
    data = json.loads(out.get_data(as_text=True))
    data["chequeos"] = res.chequeos
    data["key"] = r["key"]
    return _json_response(data, out.status_code)


@app.route("/bot", methods=["GET"])
def bot_page():
    st, _http = bot_status_payload(docker_timeout=BOT_STATUS_RAPIDO_S)
    c = st.get("container") or {}
    desde = bot_control.parse_docker_time(st.get("started_at")) if c.get("running") else None
    ultima = ultima_accion()
    if ultima:
        ultima["rango"] = f"{_fecha_iso_utc(ultima.get('inicio'))} → {_fecha_iso_utc(ultima.get('fin'))[6:]}" \
            if ultima.get("fin") else _fecha_iso_utc(ultima.get("inicio"))
        ultima["duracion"] = _duracion(ultima.get("inicio"), ultima.get("fin")) if ultima.get("fin") else ""
    historial = acciones.historial(_bot_store().audit_path)
    try:
        conexion_ctx = conexion_binance(st)
    except Exception:  # noqa: BLE001 - un archivo roto no tira la página
        current_app.logger.exception("No se pudo armar la conexión con Binance")
        conexion_ctx = None
    return render_template("bot.html", coin_list=get_coins(), custom=current_app.config["CUSTOM"], bot_status=st,
                           conexion=conexion_ctx,
                           desde=_fecha_utc(desde),
                           hace=_hace((datetime.now().astimezone() - desde).total_seconds()) if desde else "",
                           latidos=latidos(), ultima=ultima, historial=historial,
                           chequeos_ciclo=_chequeos_ciclo((st.get("estrategia") or {}).get("pares_long")))


@app.route("/api/bot/acciones", methods=["GET"])
def bot_acciones():
    """Historial de acciones del panel, paginado y en palabras (``?desde=0&limite=12&filtro=todas``). Nunca devuelve
    ``remote_addr`` ni los parámetros crudos (``futuresboard.acciones``)."""
    try:
        desde = int(request.args.get("desde", "0"))
        limite = int(request.args.get("limite", str(acciones.LIMITE_DEFECTO)))
        data = acciones.historial(_bot_store().audit_path, desde=desde, limite=limite,
                                  filtro=request.args.get("filtro", "todas"))
    except ValueError as exc:
        return _json_response({"ok": False, "error": str(exc)}, 400)
    return _json_response(data)


# --------------------------------------------------------------------------------------
# Estrategia: vista con chequeos por par (spec bot-estrategia § 3.2 y § 4)
# --------------------------------------------------------------------------------------

_Q_CANDIDATOS = re.compile(r"^[A-Z0-9]{1,20}$")
MAX_CANDIDATOS = 10


def _balance_estrategia() -> dict:
    """Balance para dimensionar (``estrategia.balance_dimensionar``, que también usa el job sin contexto de Flask)."""
    return estrategia.balance_dimensionar(current_app.config["DATABASE"], _bot_store().data_dir)


def _contexto_estrategia(cfg: dict, path, balance: float | None, manif: dict | None = None) -> estrategia.Contexto:
    return estrategia.Contexto.armar(cfg, path, testnet=bool(current_app.config.get("BINANCE_TESTNET")),
                                     base_operativa=current_app.config.get("API_BASE_URL"), balance=balance,
                                     manif=manif)


def _leer_forager() -> tuple:
    settings = bot_control.Settings()
    path = settings.require_config()
    return settings, path, bot_control.read_forager_config(path)


def _ahora_por_par(vigente: dict) -> dict:
    """Posición (unidades y % del balance) y órdenes abiertas por par, de la base del scraper (``pares_ahora``)."""
    pa = pares_ahora(vigente, exposicion_scraper())
    return {p["symbol"]: {k: p[k] for k in ("posicion", "nocional", "pct_balance", "compras", "ventas", "estado")}
            for p in pa["pares"]}


@app.route("/api/bot/estrategia", methods=["GET"])
def bot_estrategia():
    """Estrategia vigente con derivados, pares con sus chequeos, lugares, avanzado y ``base_hash`` (spec § 4).

    Consulta la API pública de Binance (caché de ``binance_publico``, con tope de tiempo): por eso no la llama la página
    al armarse, sino el JS de Estrategia."""
    try:
        _settings, path, cfg = _leer_forager()
    except bot_control.BotControlError as exc:
        return _json_response({"ok": False, "error": str(exc)}, exc.status_code)
    balance = _balance_estrategia()
    manif = manifiesto.leer(path)
    ctx = _contexto_estrategia(cfg, path, balance["para_dimensionar"], manif)
    vigente = bot_control.resumen_estrategia(cfg)
    rescate_cfg, _err = rescate.load_config(_bot_store().data_dir)
    detalle = perfiles.detectar_detalle(cfg, rescate_cfg, manif)
    vigente["perfil"], vigente["perfil_motivo"] = detalle["perfil"], detalle["motivo"]
    vigente["long_mode"], vigente["short_mode"] = cfg.get("long_mode"), cfg.get("short_mode")
    expo = estrategia.leer_exposicion(current_app.config["DATABASE"])
    ahora = _ahora_por_par(vigente)
    pares = estrategia.chequear_varios(vigente["pares_long"], ctx, lw=vigente["lw"])
    for p in pares:
        p["ahora"] = ahora.get(p["symbol"]) or {"posicion": 0.0, "nocional": 0.0, "pct_balance": None, "compras": 0,
                                                "ventas": 0, "estado": "libre"}
    fuera = [{"symbol": s, **a} for s, a in ahora.items() if s not in vigente["pares_long"] and a["estado"] != "libre"]
    nocional = sum(a["nocional"] for a in ahora.values())
    lug = estrategia.lugares(vigente["pares_long"], expo, cfg.get("symbols_to_ignore"))
    return _json_response({
        "ok": True,
        "vigente": vigente,
        "base_hash": bot_control.estrategia_hash(cfg),
        "endpoint": ctx.operativo.nombre,
        "manifiesto": {k: manif.get(k) for k in ("estado", "usable", "motivo", "generado", "revision")},
        "balance": balance,
        "leverage": cfg.get("leverage"),
        "max_min_cost": cfg.get("max_min_cost"),
        "derivados": estrategia.derivados(cfg.get("twe_long"), vigente["n_longs"], balance["para_dimensionar"],
                                          cfg.get("leverage"), nocional),
        "lugares": {**lug, "error": expo.get("error")},
        # Para recalcular los lugares del borrador en el navegador (el servidor los vuelve a validar al aplicar).
        "exposicion": {"con_posicion": sorted(expo.get("posiciones") or {}),
                       "con_ordenes": sorted(expo.get("ordenes") or {}),
                       "ignorar": [s for s in cfg.get("symbols_to_ignore") or [] if isinstance(s, str)]},
        "ciclo_min": cfg.get("update_interval_minutes") or 15,
        "pares": pares,
        "fuera_de_lista": fuera,
        "avanzado": estrategia.avanzado(cfg, manif),
        "max_pares": estrategia.MAX_PARES,
        "pares_medidos": list(estrategia.PARES_MEDIDOS),
        "twe": {"min": str(bot_control.TWE_MANUAL_MIN), "max": str(bot_control.TWE_MANUAL_MAX),
                "paso": str(bot_control.TWE_MANUAL_PASO), "advertencia": str(bot_control.TWE_ADVERTENCIA)},
    })


@app.route("/api/bot/estrategia/candidatos", methods=["GET"])
def bot_estrategia_candidatos():
    """Hasta 10 pares del endpoint que opera (y del manifiesto) que contienen ``q``, con sus chequeos (spec § 4).

    ``lw`` (opcional): la del borrador, para la 1.ª compra. Si Binance no responde: 200 con ``error`` y sin resultados."""
    q = (request.args.get("q") or "").strip().upper()
    if not _Q_CANDIDATOS.match(q):
        return _json_response({"ok": False, "error": "Buscá con letras y números (ej.: SOL)."}, 400)
    lw = None
    if request.args.get("lw"):
        try:
            lw = float(request.args["lw"])
            if not 0 < lw <= 16:
                raise ValueError
        except ValueError:
            return _json_response({"ok": False, "error": "lw inválido."}, 400)
    try:
        _settings, path, cfg = _leer_forager()
    except bot_control.BotControlError as exc:
        return _json_response({"ok": False, "error": str(exc)}, exc.status_code)
    manif = manifiesto.leer(path)
    ctx = _contexto_estrategia(cfg, path, _balance_estrategia()["para_dimensionar"], manif)
    try:
        universo = {s for s in ctx.operativo.simbolos() if s.endswith("USDT")}
    except binance_publico.SinRespuesta as exc:
        return _json_response({"ok": True, "q": q, "resultados": [],
                               "error": f"No se pudo consultar Binance: {exc}. Reintentá en un rato."})
    universo |= set(((manif.get("datos") or {}).get("symbols") or {}) if manif.get("usable") else ())

    def orden(s):
        return (0 if s in (q, f"{q}USDT") else (1 if s.startswith(q) else 2), s)

    candidatos = sorted((s for s in universo if q in s), key=orden)[:MAX_CANDIDATOS]
    if lw is None:
        lw = bot_control.resumen_estrategia(cfg)["lw"]
    aprobados = bot_control.resumen_estrategia(cfg)["pares_long"]
    resultados = estrategia.chequear_varios(candidatos, ctx, lw=lw)
    for r in resultados:
        r["ya_esta"] = r["symbol"] in aprobados
    return _json_response({"ok": True, "q": q, "lw": lw, "endpoint": ctx.operativo.nombre, "resultados": resultados,
                           "manifiesto": {k: manif.get(k) for k in ("estado", "usable", "motivo")}, "error": None})


def _armador_estrategia(objetivo: dict, origen: str = "estrategia", titulo: str = "Revisar y aplicar la estrategia",
                        estricto: bool = False):
    """Función que arma el plan (``estrategia.armar_plan``) con lo que hay en ese momento. Captura acá lo que sale de la
    app (rutas, Demo o real) porque el job la vuelve a llamar desde su hilo, con el lock y sin contexto de Flask."""
    data_dir = _bot_store().data_dir
    db_path = current_app.config["DATABASE"]
    testnet = bool(current_app.config.get("BINANCE_TESTNET"))
    base = current_app.config.get("API_BASE_URL")
    nombres = {k: v["nombre"] for k, v in perfiles.PERFILES.items()}

    def armar(en_job: bool = False) -> dict:
        settings = bot_control.Settings()
        settings.require_enabled()
        path = settings.require_config()
        docker = bot_control.DockerClient(settings.docker_url)
        info = bot_control._inspect_existing(docker, settings.container)
        try:
            crudo = path.read_bytes()
        except OSError as exc:
            raise bot_control.ConfigError(f"No se pudo leer la config de forager: {exc}") from exc
        cfg = bot_control.read_forager_config(path)
        manif = manifiesto.leer(path)
        balance = estrategia.balance_dimensionar(db_path, data_dir)
        ctx = estrategia.Contexto.armar(cfg, path, testnet, base, balance["para_dimensionar"], manif=manif)
        rescate_cfg, _err = rescate.load_config(data_dir)
        entrada = estrategia.Entrada(
            cfg=cfg, archivo_sha=hashlib.sha256(crudo).hexdigest(), objetivo=objetivo, ctx=ctx,
            expo=estrategia.leer_exposicion(db_path), rescate_cfg=rescate_cfg, running=bool(info.get("running")),
            container=settings.container, detectar=lambda c: perfiles.detectar(c, rescate_cfg, manif),
            nombres_perfil=nombres,
            warmup_s=bot_control.forager_warmup_seconds(), bloqueo_rescate=rescate.start_bloqueado(data_dir),
            origen=origen, titulo=titulo, estricto=estricto, estimar_rotacion=not en_job)
        return estrategia.armar_plan(entrada)

    return armar


def _estrategia_plan_o_job(objetivo: dict, params: dict, armar, origen: str = "estrategia",
                           extra_version: dict | None = None):
    """Plan (409 con ``requiere_confirmacion``, el diff, los avisos y ``plan_hash``) o, con ``confirmar`` y el mismo
    ``plan_hash``, el job (202) con el mismo lock que START, perfil y stop (spec § 3.3)."""
    try:
        plan = armar()
    except estrategia.EstrategiaError as exc:
        _rescate_audit("estrategia", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": "No se puede aplicar: " + " ".join(exc.bloqueos),
                               "bloqueos": exc.bloqueos, "plan": exc.plan}, exc.status_code)
    except bot_control.BotControlError as exc:
        _rescate_audit("estrategia", params, "rechazado", str(exc))
        return _json_response({**exc.payload, "ok": False, "error": str(exc)}, exc.status_code)
    if not objetivo.get("confirmar") or objetivo.get("plan_hash") != plan["plan_hash"]:
        if objetivo.get("confirmar"):
            texto = ("El plan ya no corresponde: cambió la estrategia, las posiciones o el estado del bot desde que se "
                     "armó. Revisalo de nuevo.\n\n" + plan["texto"])
            _rescate_audit("estrategia", params, "rechazado", "plan_hash viejo o ausente")
        else:
            texto = plan["texto"]
            _rescate_audit("estrategia", params, "requiere_confirmacion", plan["resumen"])
        return _json_response({"ok": False, "requiere_confirmacion": True, "error": texto, "plan": plan,
                               "plan_hash": plan["plan_hash"]}, 409)
    esperado = plan["plan_hash"]
    usuario = session.get("username") or "?"

    def validate():
        settings = bot_control.Settings()
        settings.require_enabled()
        return settings, None

    def work(store, settings, _extra, rep):
        return estrategia.aplicar(settings, store, armar, esperado, usuario, origen=origen, reporter=rep,
                                  extra_version=extra_version)

    return _run_bot_action("estrategia", params, validate, work)


@app.route("/api/bot/estrategia", methods=["POST"])
@csrf_protect
def bot_estrategia_aplicar():
    """Cambiar TWE, pares a la vez y pares (spec § 3.3): ``{twe_long, n_longs, pares, base_hash}`` responde el plan
    (409); ``{..., confirmar: true, plan_hash}`` lanza el job. Escribe también ``approved_symbols_short`` (= long) y
    ``twe_short`` 1; no toca los modos."""
    body = request.get_json(silent=True)
    params = {k: body.get(k) for k in ("twe_long", "n_longs", "pares")} if isinstance(body, dict) else {}
    try:
        objetivo = estrategia.validar_objetivo(body)
    except ValueError as exc:
        _rescate_audit("estrategia", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 400)
    params = estrategia.params_audit(objetivo)
    return _estrategia_plan_o_job(objetivo, params, _armador_estrategia(objetivo))


@app.route("/api/bot/estrategia/rotacion", methods=["GET"])
def bot_estrategia_rotacion():
    """Qué elegiría forager hoy con ``pares`` (separados por coma) y ``n`` lugares (S1): réplica de sort_symbols con
    las velas de ahora, en el endpoint que opera y, en Demo, también en Binance real. Estimación."""
    pares = [p for p in (request.args.get("pares") or "").split(",") if p]
    try:
        n = int(request.args.get("n", ""))
        if not 1 <= n <= estrategia.MAX_PARES or not 1 <= len(pares) <= estrategia.MAX_PARES:
            raise ValueError
        if len(set(pares)) != len(pares) or not all(estrategia.FORMATO.match(p) for p in pares):
            raise ValueError
    except ValueError:
        return _json_response({"ok": False, "error": "pares o n inválidos."}, 400)
    try:
        _settings, path, cfg = _leer_forager()
    except bot_control.BotControlError as exc:
        return _json_response({"ok": False, "error": str(exc)}, exc.status_code)
    ctx = _contexto_estrategia(cfg, path, None, {"usable": False})
    expo = estrategia.leer_exposicion(current_app.config["DATABASE"])
    if expo.get("error"):
        return _json_response({"ok": False, "error": expo["error"]}, 503)
    return _json_response({"ok": True, **estrategia.estimar_rotacion(ctx, pares, n, expo)})


@app.route("/api/bot/estrategia/versiones", methods=["GET"])
def bot_estrategia_versiones():
    """Versiones de la estrategia con cuándo, quién, qué cambió y la diferencia con la vigente (spec § 2.4 paso 6)."""
    try:
        _settings, path, _cfg = _leer_forager()
    except bot_control.BotControlError as exc:
        return _json_response({"ok": False, "error": str(exc)}, exc.status_code)
    nombres = {k: v["nombre"] for k, v in perfiles.PERFILES.items()}
    data = estrategia.listar_versiones(path, _bot_store().data_dir, _bot_store().audit_path, nombres)
    return _json_response({"ok": True, **data})


@app.route("/api/bot/estrategia/volver", methods=["POST"])
@csrf_protect
def bot_estrategia_volver():
    """Volver a una versión: ``{backup, base_hash}`` arma el mismo plan que un cambio de estrategia con los valores de
    estrategia de ese backup (TWE, pares a la vez y pares; nada más) y repite confirmación y job. Bloquea si un par de
    esa versión ya no pasa los chequeos o si viola la regla de exposición con las posiciones de ahora (spec § 3.3)."""
    body = request.get_json(silent=True)
    params: dict = {"volver": body.get("backup") if isinstance(body, dict) else None}
    try:
        if not isinstance(body, dict):
            raise ValueError("Body inválido: se esperaba un objeto JSON.")
        desconocidos = sorted(set(body) - {"backup", "base_hash", "confirmar", "plan_hash"})
        if desconocidos:
            raise ValueError(f"Campos desconocidos: {', '.join(desconocidos)}.")
        _settings, path, _cfg = _leer_forager()
        if not estrategia.backup_valido(path, body.get("backup")):
            raise ValueError("backup inválido: tiene que ser un backup con fecha de new.json.")
    except ValueError as exc:
        _rescate_audit("estrategia", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 400)
    except bot_control.BotControlError as exc:
        return _json_response({"ok": False, "error": str(exc)}, exc.status_code)
    nombre = body["backup"]
    try:
        version = estrategia.leer_backup(path, nombre)
        n_longs, _n_shorts = bot_control.n_pares(version)
        objetivo = estrategia.validar_objetivo({
            "twe_long": version.get("twe_long"), "n_longs": n_longs,
            "pares": version.get("approved_symbols_long") if isinstance(version.get("approved_symbols_long"), list)
            else None, "base_hash": body.get("base_hash"), "confirmar": body.get("confirmar", False),
            "plan_hash": body.get("plan_hash")})
    except estrategia.EstrategiaError as exc:
        _rescate_audit("estrategia", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": "No se puede volver: " + " ".join(exc.bloqueos),
                               "bloqueos": exc.bloqueos}, 409)
    except ValueError as exc:
        texto = f"Esa versión no se puede restaurar con Estrategia: {exc}"
        _rescate_audit("estrategia", params, "rechazado", texto)
        return _json_response({"ok": False, "error": texto, "bloqueos": [texto]}, 409)
    params = {"volver": nombre, **estrategia.params_audit(objetivo)}
    sello = estrategia._sello(nombre)
    titulo = f"Volver a la versión del {sello:%d/%m %H:%M} UTC" if sello else "Volver a una versión"
    armar = _armador_estrategia(objetivo, origen="volver", titulo=titulo, estricto=True)
    return _estrategia_plan_o_job(objetivo, params, armar, origen="volver",
                                  extra_version={"accion": "volver", "volver_a": nombre})


@app.route("/estrategia", methods=["GET"])
def estrategia_page():
    """Estrategia: lo vigente en solo lectura, más Perfil y TWE manual (los mismos endpoints que tenía la rueda).

    La edición de pares y pares a la vez (borrador, chequeos, versiones) entra en ``data-estrategia-editor``: la
    agrega el flujo de ``POST /api/bot/estrategia`` (spec § 3.3), que todavía no existe."""
    st, _http = bot_status_payload(docker_timeout=BOT_STATUS_RAPIDO_S)
    vigente, error = _estrategia_vigente()
    return render_template("estrategia.html", coin_list=get_coins(), custom=current_app.config["CUSTOM"],
                           bot_status=st, vigente=vigente, error_vigente=error, perfiles=perfiles.catalogo()["perfiles"],
                           twe_manual={"min": str(bot_control.TWE_MANUAL_MIN), "max": str(bot_control.TWE_MANUAL_MAX),
                                       "paso": str(bot_control.TWE_MANUAL_PASO)})


def _check_rescate_start(data_dir: pathlib.Path) -> None:
    """START bloqueado mientras haya un tope de pérdida del rescate sin reconocer (409)."""
    bloqueo = rescate.start_bloqueado(data_dir)
    if bloqueo:
        err = bot_control.BotControlError(bloqueo)
        err.status_code = 409
        raise err


@app.route("/api/bot/start", methods=["POST"])
@csrf_protect
def bot_start():
    """START con un preset (``{"riesgo": ...}``) o con la TWE manual (``{"twe_long": n}``, ver docs/perfiles.md).

    TWE manual: número entre 0,5 y 8 en pasos de 0,25; por encima de 4 hay que mandar ``"confirmar_twe_alta": true``
    (si no, 409 con ``requiere_confirmacion``)."""
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    if "reanudar" in body:
        return _bot_reanudar(body)
    manual = "twe_long" in body
    riesgo = body.get("riesgo")
    params = {"twe_long": body.get("twe_long")} if manual else {"riesgo": riesgo}
    twe = None
    if manual:
        try:
            if "riesgo" in body:
                raise ValueError("Mandar riesgo (preset) o twe_long (TWE manual), no los dos.")
            confirmar = body.get("confirmar_twe_alta", False)
            if not isinstance(confirmar, bool):
                raise ValueError("confirmar_twe_alta tiene que ser true o false.")
            twe = bot_control.validate_twe_manual(body["twe_long"])
        except ValueError as exc:
            _rescate_audit("start", params, "rechazado", str(exc))
            return _json_response({"ok": False, "error": str(exc)}, 400)
        if twe > bot_control.TWE_ADVERTENCIA and not confirmar:
            aviso = (f"TWE {bot_control.twe_valor(twe)} está por encima de {bot_control.twe_valor(bot_control.TWE_ADVERTENCIA)}: "
                     "en el backtest multianual 2021-2026 aparecen liquidaciones desde TWE 6, y los perfiles medidos son "
                     "TWE 3 y TWE 4. Herramienta, no asesor. ¿Aplicar igual?")
            _rescate_audit("start", params, "requiere_confirmacion", aviso)
            return _json_response({"ok": False, "requiere_confirmacion": True, "error": aviso}, 409)

    def validate():
        if not manual:
            bot_control.validate_riesgo(riesgo)
        _check_rescate_start(_bot_store().data_dir)
        settings = bot_control.Settings()
        settings.require_enabled()
        return settings, None

    usuario = session.get("username") or "?"

    def work(store, settings, _extra, rep):
        # Se vuelve a chequear dentro del job, ya con el lock de acciones tomado.
        _check_rescate_start(store.data_dir)
        return bot_control.start_bot(settings, store, riesgo, reporter=rep, twe_long=twe, usuario=usuario)

    return _run_bot_action("start", params, validate, work)


def _bot_reanudar(body: dict):
    """Iniciar / Reanudar / Reiniciar con la estrategia guardada: ``{"reanudar": true}`` (camino ``mantener_twe``:
    modos a normal, sin tocar TWE ni pares). Spec bot-estrategia § 2.2 y § 4.

    Detenido (o con la config pendiente de reinicio): primero ``409 {requiere_confirmacion, plan, plan_hash}`` con lo
    que va a operar y qué cambió desde el último arranque; arranca con ``{"reanudar": true, "confirmar": true,
    "plan_hash": ...}``. Corriendo con la config en efecto (Reanudar desde parada gradual, Reiniciar): de un toque.
    Dentro del job, con el lock, se vuelve a armar el plan: si cambió algo, 409 y no arranca."""
    params: dict = {"reanudar": True}
    try:
        if body.get("reanudar") is not True:
            raise ValueError("reanudar tiene que ser true.")
        if "riesgo" in body or "twe_long" in body:
            raise ValueError("reanudar arranca con la estrategia guardada: no se manda riesgo ni twe_long.")
        confirmar = body.get("confirmar", False)
        if not isinstance(confirmar, bool):
            raise ValueError("confirmar tiene que ser true o false.")
        enviado = body.get("plan_hash")
        if enviado is not None and not isinstance(enviado, str):
            raise ValueError("plan_hash tiene que ser texto.")
    except ValueError as exc:
        _rescate_audit("start", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 400)
    settings = bot_control.Settings()
    try:
        _check_rescate_start(_bot_store().data_dir)
        settings.require_enabled()
        plan = bot_control.plan_reanudar(settings, _bot_store())
    except bot_control.BotControlError as exc:
        _rescate_audit("start", params, "rechazado", str(exc))
        return _json_response({**exc.payload, "ok": False, "error": str(exc)}, exc.status_code)
    params["como"] = plan["como"]
    if plan["requiere_plan"] and (not confirmar or enviado != plan["plan_hash"]):
        if confirmar:
            texto = ("El resumen ya no corresponde: cambió la estrategia o el estado del bot desde que se armó. "
                     "Revisalo de nuevo.\n\n" + plan["texto"])
            _rescate_audit("start", params, "rechazado", "plan_hash viejo o ausente")
        else:
            texto = plan["texto"]
            _rescate_audit("start", params, "requiere_confirmacion", plan["texto"])
        return _json_response({"ok": False, "requiere_confirmacion": True, "error": texto, "plan": plan,
                               "plan_hash": plan["plan_hash"]}, 409)
    esperado = plan["plan_hash"]
    usuario = session.get("username") or "?"

    def work(store, settings, _extra, rep):
        _check_rescate_start(store.data_dir)
        actual = bot_control.plan_reanudar(settings, store)
        if actual["plan_hash"] != esperado:
            err = bot_control.BotControlError("La estrategia o el estado del bot cambió desde el resumen: no se arrancó. "
                                              "Pedí Iniciar de nuevo para ver lo que va a operar.")
            err.status_code = 409
            raise err
        return bot_control.start_bot(settings, store, None, reporter=rep, mantener_twe=True, usuario=usuario,
                                     origen=actual["como"])

    return _run_bot_action("start", params, lambda: (settings, None), work)


@app.route("/api/bot/perfil", methods=["POST"])
@csrf_protect
def bot_perfil():
    """Aplica un perfil de riesgo (TWE de forager + rescate + disparo por uPnL + retiro) en una sola acción.

    ``{"perfil": "conservador" | "con_rescate"}`` responde 409 con ``requiere_confirmacion`` y el plan (qué cambia, si
    passivbot se reinicia, los modos del servicio y los números del backtest). Con ``"confirmar": true`` lanza el job
    (202), con el mismo lock que START. Ver docs/perfiles.md."""
    body = request.get_json(silent=True)
    params = {"perfil": body.get("perfil") if isinstance(body, dict) else None}
    try:
        parsed = perfiles.validate_body(body)
    except ValueError as exc:
        _rescate_audit("perfil", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 400)
    nombre = parsed["perfil"]
    data_dir = _bot_store().data_dir
    db_path = current_app.config["DATABASE"]
    if not parsed["confirmar"]:
        try:
            plan = perfiles.plan(bot_control.Settings(), data_dir, nombre, db_path=db_path)
        except ValueError as exc:
            _rescate_audit("perfil", params, "rechazado", str(exc))
            return _json_response({"ok": False, "error": str(exc)}, 400)
        except bot_control.BotControlError as exc:
            _rescate_audit("perfil", params, "rechazado", str(exc))
            return _json_response({**exc.payload, "ok": False, "error": str(exc)}, exc.status_code)
        _rescate_audit("perfil", params, "requiere_confirmacion", plan["texto"])
        return _json_response({"ok": False, "requiere_confirmacion": True, "error": plan["texto"], "plan": plan}, 409)
    usuario = session.get("username") or "?"

    def validate():
        settings = bot_control.Settings()
        settings.require_enabled()
        _check_rescate_start(data_dir)
        return settings, usuario

    def work(store, settings, user, rep):
        return perfiles.aplicar(settings, store, nombre, user, reporter=rep, db_path=db_path)

    return _run_bot_action("perfil", params, validate, work)


@app.route("/api/bot/stop", methods=["POST"])
@csrf_protect
def bot_stop():
    body = request.get_json(silent=True) or {}
    modo = body.get("modo") if isinstance(body, dict) else None

    def validate():
        settings = bot_control.Settings()
        bot_control.validate_stop(settings, modo)
        # El cliente de Binance se arma acá: lee la config de la app (no disponible en el hilo).
        return settings, (_binance_futures_client() if modo == "apagar" else None)

    return _run_bot_action(
        "stop", {"modo": modo}, validate,
        lambda store, settings, binance, rep: bot_control.stop_bot(
            settings, store, modo, binance=binance, reporter=rep
        ),
    )


@app.route("/api/bot/jobs/activo", methods=["GET"])
def bot_job_active():
    """Job en curso (para reanudar el modal de progreso al recargar la página) o null."""
    job = _bot_jobs().active()
    return _json_response({"job": job.to_dict() if job is not None else None})


@app.route("/api/bot/jobs/<job_id>", methods=["GET"])
def bot_job(job_id):
    data = _bot_jobs().get(job_id)
    if data is None:
        return _json_response({"ok": False, "error": "Acción inexistente o vencida."}, 404)
    return _json_response(data)


def _binance_futures_client() -> bot_control.BinanceFuturesClient:
    """Cliente de Binance USDⓈ-M con las mismas credenciales/base URL que el scraper.

    Con BINANCE_TESTNET activo, API_BASE_URL ya apunta a demo-fapi (ver config.py).
    """
    cfg = current_app.config
    if str(cfg.get("EXCHANGE") or "").lower() != "binance":
        err = bot_control.BotControlError("Apagar con cierre de posiciones solo está soportado para Binance Futures.")
        err.status_code = 409
        raise err
    # La key sale del proveedor del proceso (credenciales.py): se relee antes de cada pedido si cambió el archivo.
    return bot_control.BinanceFuturesClient(cfg.get("API_BASE_URL") or "",
                                            credenciales=credenciales.de_app(current_app._get_current_object()),
                                            algo_optional=bool(cfg.get("BINANCE_TESTNET")))


# --------------------------------------------------------------------------------------
# Rescate (colchón): config del panel y estado del servicio (ver docs/rescate.md)
# --------------------------------------------------------------------------------------


def _rescate_audit(action: str, params: dict, outcome: str, detail: str) -> None:
    try:
        _bot_store().audit(session.get("username") or "?", action, params, outcome, detail, request.remote_addr)
    except OSError:
        current_app.logger.exception("No se pudo escribir el log de auditoria")


def _rescate_status() -> dict:
    """Estado del rescate y de C3 para el panel (solo lee archivos; no consulta Binance)."""
    data_dir = _bot_store().data_dir
    out = rescate.panel_status(data_dir)
    out["c3"] = c3.panel_status(data_dir)
    return out


@app.route("/rescate", methods=["GET"])
def rescate_page():
    return render_template(
        "rescate.html",
        coin_list=get_coins(),
        custom=current_app.config["CUSTOM"],
        limites={k: [str(lo), str(hi)] for k, (lo, hi) in rescate.LIMITES.items()},
        aviso=rescate.AVISO,
        retiro_recomendada=rescate.FRECUENCIA_RECOMENDADA,
        c3_limites={**{k: [str(lo), str(hi)] for k, (lo, hi) in c3.LIMITES.items()},
                    **{k: [str(lo), str(hi)] for k, (lo, hi) in c3.LIMITES_ENTEROS.items()}},
        c3_aviso=c3.AVISO,
    )


@app.route("/api/rescate/status", methods=["GET"])
def rescate_status():
    return _json_response(_rescate_status())


@app.route("/api/rescate/config", methods=["POST"])
@csrf_protect
def rescate_config():
    body = request.get_json(silent=True)
    params = body if isinstance(body, dict) else {}
    confirmar = False
    if isinstance(body, dict) and "confirmar_inyeccion" in body:
        body = dict(body)
        confirmar = body.pop("confirmar_inyeccion")
    try:
        if not isinstance(confirmar, bool):
            raise ValueError("confirmar_inyeccion tiene que ser true o false.")
        parsed = rescate.validate_config_body(body)
        aviso = None if confirmar else rescate.inyeccion_al_guardar(_bot_store().data_dir, parsed)
        if aviso:
            # Bajar X o prender el disparo U en plena pérdida inyecta ya (uno o varios pasos): el panel pide confirmarlo.
            _rescate_audit("rescate_config", params, "requiere_confirmacion", aviso)
            return _json_response({"ok": False, "requiere_confirmacion": True, "error": aviso}, 409)
        cfg = rescate.save_config(_bot_store().data_dir, parsed, session.get("username") or "?")
    except ValueError as exc:
        _rescate_audit("rescate_config", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 400)
    except rescate.ConflictError as exc:
        _rescate_audit("rescate_config", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 409)
    _rescate_audit("rescate_config", params, "ok", f"generacion {cfg.get('generacion')}")
    return _json_response({"ok": True, "status": _rescate_status()})


@app.route("/api/rescate/reconocer-tope", methods=["POST"])
@csrf_protect
def rescate_reconocer_tope():
    body = request.get_json(silent=True)
    tope_id = body.get("id") if isinstance(body, dict) else None
    params = {"id": tope_id if isinstance(tope_id, str) else None}
    try:
        rescate.reconocer_tope(_bot_store().data_dir, tope_id, session.get("username") or "?")
    except ValueError as exc:
        _rescate_audit("rescate_reconocer_tope", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 400)
    except rescate.ConflictError as exc:
        _rescate_audit("rescate_reconocer_tope", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 409)
    _rescate_audit("rescate_reconocer_tope", params, "ok", "START desbloqueado; rescate deshabilitado")
    return _json_response({"ok": True, "status": _rescate_status()})


@app.route("/api/c3/config", methods=["POST"])
@csrf_protect
def c3_config():
    """Config de C3 (stop con reinicio). Validación estricta; habilitar abre una generación nueva (base nueva)."""
    body = request.get_json(silent=True)
    params = body if isinstance(body, dict) else {}
    confirmar = False
    if isinstance(body, dict) and "confirmar_corte" in body:
        body = dict(body)
        confirmar = body.pop("confirmar_corte")
    try:
        if not isinstance(confirmar, bool):
            raise ValueError("confirmar_corte tiene que ser true o false.")
        parsed = c3.validate_config_body(body)
        aviso = None if confirmar else c3.corte_al_guardar(_bot_store().data_dir, parsed)
        if aviso:
            # Bajar el tope con la cuenta en pérdida corta a mercado en ~1 min: el panel pide confirmarlo.
            _rescate_audit("c3_config", params, "requiere_confirmacion", aviso)
            return _json_response({"ok": False, "requiere_confirmacion": True, "error": aviso}, 409)
        cfg = c3.save_config(_bot_store().data_dir, parsed, session.get("username") or "?")
    except ValueError as exc:
        _rescate_audit("c3_config", params, "rechazado", str(exc))
        return _json_response({"ok": False, "error": str(exc)}, 400)
    _rescate_audit("c3_config", params, "ok", f"generacion {cfg.get('generacion')}")
    return _json_response({"ok": True, "status": _rescate_status()})


@app.route("/api/last-order", methods=["GET"])
def get_last_order():
    """API endpoint to get details of the last order."""
    try:
        order = get_last_order_details()
        if order is None:
            return Response(
                json.dumps({"error": "No orders found"}),
                status=404,
                mimetype='application/json'
            )
        return Response(
            json.dumps(order),
            status=200,
            mimetype='application/json'
        )
    except Exception as e:
        current_app.logger.error(f"Error getting last order: {e}")
        return Response(
            json.dumps({"error": str(e)}),
            status=500,
            mimetype='application/json'
        )


@app.errorhandler(404)
def not_found(error):
    return (
        render_template(
            "error.html",
            coin_list=get_coins(),
            custom=current_app.config["CUSTOM"],
        ),
        404,
    )
