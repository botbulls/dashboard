from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import pathlib
import re
import signal
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from sqlite3 import Error
from urllib.parse import urlencode

import requests  # type: ignore
from flask import current_app
from flask import Flask

from futuresboard import db


_SIGNATURE_RE = re.compile(r"(signature=)[^&\s'\"]+")

SCRAPE_STATE_FILE = "scrape_state.json"
# Latido del loop dedicado (python -m futuresboard.scraper): junto a la DB, en el directorio de datos.
LATIDO_FILE = "scraper_latido.json"

# Lo setea SIGTERM/SIGINT en el loop dedicado. En el hilo del web nunca se setea: ahí las esperas
# por peso de API son un sleep común.
_DETENER = threading.Event()


def redact_url(url) -> str:
    """Oculta la firma HMAC de la query string antes de loguear la URL."""
    return _SIGNATURE_RE.sub(r"\1<redacted>", str(url))


class HTTPRequestError(Exception):
    def __init__(self, url, code, msg=None):
        self.url = url
        self.code = code
        self.msg = msg

    def __str__(self) -> str:
        """
        Convert the exception into a printable string
        """
        return f"Request to {redact_url(self.url)!r} failed. Code: {self.code}; Message: {redact_url(self.msg)}"


# --------------------------------------------------------------------------------------
# Marca del ultimo scrape (para /api/bot/health y /metrics)
# --------------------------------------------------------------------------------------
# Se persiste en un archivo junto a la DB (no en memoria) porque el scrape puede correr en
# otro proceso (cron con `futuresboard --scrape-only`). No se toca el esquema de la DB.


def scrape_state_path(database) -> pathlib.Path:
    return pathlib.Path(database).parent / SCRAPE_STATE_FILE


def read_scrape_state(database) -> dict:
    try:
        data = json.loads(scrape_state_path(database).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_scrape(**fields) -> None:
    database = current_app.config["DATABASE"]
    path = scrape_state_path(database)
    tmp_name = None
    try:
        state = read_scrape_state(database)
        state.update(fields)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        current_app.logger.warning("No se pudo registrar el estado del scrape: %s", exc)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def latido_path(database) -> pathlib.Path:
    return pathlib.Path(database).parent / LATIDO_FILE


def read_latido(database) -> dict:
    """Última vuelta OK del loop dedicado (``{}`` si no hay latido o está corrupto)."""
    try:
        data = json.loads(latido_path(database).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("at"), (int, float)) else {}


def _write_latido(database, data: dict) -> None:
    path = latido_path(database)
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        current_app.logger.warning("No se pudo escribir el latido del scraper: %s", exc)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def frescura(database, now: float | None = None, intervalo_defecto: int | None = None) -> dict:
    """Edad de los datos para el web ("datos de hace X s").

    Toma lo más nuevo entre el latido del loop dedicado y el último scrape OK de
    ``scrape_state.json`` (este último cubre el scraper como hilo del web o por cron). ``fuente`` e
    ``intervalo_segundos`` son los de esa marca más nueva: el intervalo sale del latido; si gana
    ``scrape_state.json`` (o no hay latido) es ``intervalo_defecto`` (ver ``frescura_web``).
    """
    now = time.time() if now is None else now
    latido = read_latido(database)
    ok = read_scrape_state(database).get("last_success_at")
    marcas = [(float(latido["at"]), "latido")] if latido else []
    if isinstance(ok, (int, float)):
        marcas.append((float(ok), "scrape_state"))
    if not marcas:
        return {"fuente": None, "at": None, "edad_segundos": None, "intervalo_segundos": None}
    at, fuente = max(marcas, key=lambda m: m[0])  # empate: el latido (va primero)
    intervalo = latido.get("intervalo") if fuente == "latido" else None
    if not isinstance(intervalo, int):
        intervalo = intervalo_defecto if isinstance(intervalo_defecto, int) else None
    return {
        "fuente": fuente,
        "at": at,
        "edad_segundos": max(0, int(now - at)),
        "intervalo_segundos": intervalo,
    }


def frescura_web(config, now: float | None = None) -> dict:
    """``frescura`` para el web (``app.config``): sin un latido más nuevo y con el hilo del web prendido
    (compose sin servicio scraper), el intervalo es el ``AUTO_SCRAPE_INTERVAL`` del web. Sin eso el indicador
    "en vivo" usaría el umbral de 180 s y se pondría amarillo entre dos vueltas de 300 s del hilo."""
    defecto = None if config.get("DISABLE_AUTO_SCRAPE") else config.get("AUTO_SCRAPE_INTERVAL")
    return frescura(config["DATABASE"], now=now, intervalo_defecto=defecto)


def auto_scrape(app):
    thread = threading.Thread(target=_auto_scrape, args=(app,))
    thread.daemon = True
    thread.start()


def _auto_scrape(app):
    with app.app_context():
        interval = app.config["AUTO_SCRAPE_INTERVAL"]
        while True:
            app.logger.info("Auto scrape routines starting")
            scrape(app=app)
            app.logger.info(
                f"Auto scrape routines terminated. Sleeping {interval} seconds...",
            )
            time.sleep(interval)


def hashing(query_string, exchange="binance", timestamp=None):
    if exchange == "bybit":
        query_string = f"{timestamp}{current_app.config['API_KEY']}5000" + query_string
        return hmac.new(
            bytes(current_app.config["API_SECRET"].encode("utf-8")),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    return hmac.new(
        bytes(current_app.config["API_SECRET"].encode("utf-8")),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def get_timestamp():
    return int(time.time() * 1000)


def dispatch_request(http_method, signature=None, timestamp=None):
    session = requests.Session()
    session.headers.update(
        {
            "Content-Type": "application/json;charset=utf-8",
            "X-MBX-APIKEY": current_app.config["API_KEY"],
            "X-BAPI-API-KEY": current_app.config["API_KEY"],
            "X-BAPI-SIGN": f"{signature}",
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": f"{timestamp}",
            "X-BAPI-RECV-WINDOW": "5000",
        }
    )

    return {
        "GET": session.get,
        "DELETE": session.delete,
        "PUT": session.put,
        "POST": session.post,
    }.get(http_method, "GET")


# used for sending request requires the signature
def send_signed_request(http_method, url_path, payload={}, exchange="binance"):
    if exchange == "binance":
        payload["timestamp"] = get_timestamp()
    query_string = urlencode(OrderedDict(sorted(payload.items())))
    query_string = query_string.replace(
        "%27", "%22"
    )  # replace single quote to double quote

    url = f"{current_app.config['API_BASE_URL']}{url_path}?{query_string}"
    if exchange == "binance":
        url += f"&signature={hashing(query_string, exchange)}"

    # print("{} {}".format(http_method, url))
    params = {"url": url, "params": {}}
    try:
        timestamp = get_timestamp()
        response = dispatch_request(
            http_method,
            hashing(query_string=query_string, exchange=exchange, timestamp=timestamp),
            timestamp=timestamp,
        )(**params)
        headers = response.headers
        try:
            json_response = response.json()
        except requests.exceptions.JSONDecodeError as e:
            raise HTTPRequestError(url=url, code=-3, msg=f"{e}")
        if "code" in json_response:
            raise HTTPRequestError(
                url=url, code=json_response["code"], msg=json_response["msg"]
            )
        if "retCode" in json_response:
            if json_response["retCode"] != 0:
                raise HTTPRequestError(
                    url=url, code=json_response["retCode"], msg=json_response["retMsg"]
                )
        return headers, json_response
    except requests.exceptions.ConnectionError as e:
        raise HTTPRequestError(url=url, code=-1, msg=f"{e}")


# used for sending public data request
def send_public_request(url_path, payload={}):
    query_string = urlencode(payload, True)
    url = current_app.config["API_BASE_URL"] + url_path
    if query_string:
        url = url + "?" + query_string
    # print("{}".format(url))
    try:
        response = dispatch_request("GET")(url=url)
        headers = response.headers
        try:
            json_response = response.json()
        except requests.exceptions.JSONDecodeError as e:
            raise HTTPRequestError(url=url, code=-3, msg=f"{e}")
        if "code" in json_response:
            raise HTTPRequestError(
                url=url, code=json_response["code"], msg=json_response["msg"]
            )
        return headers, json_response
    except requests.exceptions.ConnectionError as e:
        raise HTTPRequestError(url=url, code=-2, msg=f"{e}")


def create_connection(db_file):
    conn = None
    try:
        conn = db.connect(db_file)
    except Error as e:
        print(e)

    return conn


def create_table(conn, create_table_sql):
    try:
        c = conn.cursor()
        c.execute(create_table_sql)
    except Error as e:
        print(e)


def db_setup(database):
    sql_create_income_table = """ CREATE TABLE IF NOT EXISTS income (
                                        IID integer PRIMARY KEY AUTOINCREMENT,
                                        tranId text,
                                        symbol text,
                                        incomeType text,
                                        income real,
                                        asset text,
                                        info text,
                                        time integer,
                                        tradeId integer,
                                        UNIQUE(tranId, incomeType) ON CONFLICT REPLACE
                                    ); """

    sql_create_position_table = """ CREATE TABLE IF NOT EXISTS positions (
                                        PID integer PRIMARY KEY AUTOINCREMENT,
                                        symbol text,
                                        unrealizedProfit real,
                                        leverage integer,
                                        entryPrice real,
                                        positionSide text,
                                        positionAmt real
                                    ); """

    sql_create_account_table = """ CREATE TABLE IF NOT EXISTS account (
                                        AID integer PRIMARY KEY,
                                        totalWalletBalance real,
                                        totalUnrealizedProfit real,
                                        totalMarginBalance real,
                                        availableBalance real,
                                        maxWithdrawAmount real
                                    ); """

    sql_create_orders_table = """ CREATE TABLE IF NOT EXISTS orders (
                                        OID integer PRIMARY KEY AUTOINCREMENT,
                                        origQty real,
                                        price real,
                                        side text,
                                        positionSide text,
                                        status text,
                                        symbol text,
                                        time integer,
                                        type text
                                    ); """
    # create a database connection
    conn = create_connection(database)

    # create tables
    if conn is not None:
        create_table(conn, sql_create_income_table)
        create_table(conn, sql_create_position_table)
        create_table(conn, sql_create_account_table)
        create_table(conn, sql_create_orders_table)
    else:
        print("Error! cannot create the database connection.")


# income interactions
def create_income(conn, income):
    sql = """ INSERT INTO income(tranId, symbol, incomeType, income, asset, info, time, tradeId)
              VALUES(?,?,?,?,?,?,?,?) """
    cur = conn.cursor()
    cur.execute(sql, income)


def select_latest_income(conn):
    cur = conn.cursor()
    cur.execute("SELECT time FROM income ORDER BY time DESC LIMIT 0, 1")
    return cur.fetchone()


def select_latest_income_symbol(conn, symbol):
    cur = conn.cursor()
    cur.execute(
        "SELECT time FROM income WHERE symbol = ? ORDER BY time DESC LIMIT 0, 1",
        (symbol,),
    )
    return cur.fetchone()


# position interactions
def create_position(conn, position):
    sql = """ INSERT INTO positions(unrealizedProfit, leverage, entryPrice, positionAmt, symbol, positionSide) VALUES(?,?,?,?,?,?) """
    cur = conn.cursor()
    cur.execute(sql, position)


def update_position(conn, position):
    sql = """ UPDATE positions SET unrealizedProfit = ?, leverage = ?, entryPrice = ?, positionAmt = ? WHERE symbol = ? AND positionSide = ? """
    cur = conn.cursor()
    cur.execute(sql, position)


def delete_all_positions(conn):
    # Sin commit: el borrado y los inserts nuevos van en la misma transacción (commitea el
    # llamador), así un lector concurrente nunca ve la tabla vacía a mitad del reemplazo.
    sql = """ DELETE FROM positions """
    cur = conn.cursor()
    cur.execute(sql)


def select_position(conn, symbol):
    cur = conn.cursor()
    cur.execute(
        "SELECT unrealizedProfit FROM positions WHERE symbol = ? AND positionSide = ? LIMIT 0, 1",
        (
            symbol[0],
            symbol[1],
        ),
    )
    return cur.fetchone()


# account interactions
def create_account(conn, account):
    sql = """ INSERT INTO account(totalWalletBalance, totalUnrealizedProfit, totalMarginBalance, availableBalance, maxWithdrawAmount, AID) VALUES(?,?,?,?,?,?) """
    cur = conn.cursor()
    cur.execute(sql, account)


def update_account(conn, account):
    sql = """ UPDATE account SET totalWalletBalance = ?, totalUnrealizedProfit = ?, totalMarginBalance = ?, availableBalance = ?, maxWithdrawAmount = ? WHERE AID = ?"""
    cur = conn.cursor()
    cur.execute(sql, account)


def select_account(conn):
    cur = conn.cursor()
    cur.execute("SELECT totalWalletBalance FROM account LIMIT 0, 1")
    return cur.fetchone()


# orders interactions
def delete_all_orders(conn):
    # Sin commit, por lo mismo que delete_all_positions.
    sql = """ DELETE FROM orders """
    cur = conn.cursor()
    cur.execute(sql)


def create_orders(conn, orders):
    sql = """ INSERT INTO orders(origQty, price, side, positionSide, status, symbol, time, type) VALUES(?,?,?,?,?,?,?,?) """
    cur = conn.cursor()
    cur.execute(sql, orders)


def scrape(app=None) -> bool:
    """Una pasada. Devuelve True si terminó bien (False ante un error HTTP del exchange, o si la cuenta y las
    posiciones no se pudieron actualizar: esa pasada no cuenta como scrape OK ni deja latido)."""
    _record_scrape(last_started_at=time.time())
    try:
        completo = _scrape(app=app)
    except HTTPRequestError as exc:
        _record_scrape(last_error_at=time.time(), last_error=f"HTTP error code {exc.code}")
        if app is None:
            print(exc)
        else:
            app.logger.error(f"{exc}")
        return False
    except Exception as exc:
        # Se registra y se re-lanza (mismo comportamiento que antes).
        _record_scrape(last_error_at=time.time(), last_error=exc.__class__.__name__)
        raise
    else:
        if completo is False:
            _record_scrape(last_error_at=time.time(), last_error="cuenta sin positions: cuenta y posiciones sin actualizar")
            current_app.logger.warning("Pasada incompleta: la respuesta de la cuenta no trae positions; "
                                       "cuenta y posiciones quedaron como estaban")
            return False
        _record_scrape(last_success_at=time.time())
        return True


# flake8: noqa: C901
def _scrape(app=None):
    start = time.time()
    db_setup(current_app.config["DATABASE"])

    up_to_date = False
    weightused = 0
    processed, updated_positions, new_positions, updated_orders, sleeps = 0, 0, 0, 0, 0
    overweight = False

    if current_app.config["EXCHANGE"].lower() == "binance":
        if weightused < 800:
            responseHeader, responseJSON = send_signed_request(
                "GET", "/fapi/v1/openOrders"
            )
            weightused = int(responseHeader["X-MBX-USED-WEIGHT-1M"])

            with create_connection(current_app.config["DATABASE"]) as conn:
                delete_all_orders(conn)
                for order in responseJSON:
                    updated_orders += 1
                    row = (
                        float(order["origQty"]),
                        float(order["price"]),
                        order["side"],
                        order["positionSide"],
                        order["status"],
                        order["symbol"],
                        int(order["time"]),
                        order["type"],
                    )
                    create_orders(conn, row)
                conn.commit()

        responseHeader, responseJSON = send_signed_request("GET", "/fapi/v2/account")
        weightused = int(responseHeader["X-MBX-USED-WEIGHT-1M"])

        overweight = False
        try:
            positions = responseJSON["positions"]
        except Exception:
            overweight = True

        if not overweight:
            with create_connection(current_app.config["DATABASE"]) as conn:
                totals_row = (
                    float(responseJSON["totalWalletBalance"]),
                    float(responseJSON["totalUnrealizedProfit"]),
                    float(responseJSON["totalMarginBalance"]),
                    float(responseJSON["availableBalance"]),
                    float(responseJSON["maxWithdrawAmount"]),
                    1,
                )
                accountCheck = select_account(conn)
                if accountCheck is None:
                    create_account(conn, totals_row)
                elif float(accountCheck[0]) != float(
                    responseJSON["totalWalletBalance"]
                ):
                    update_account(conn, totals_row)

                delete_all_positions(conn)

                for position in positions:
                    position_row = (
                        float(position["unrealizedProfit"]),
                        int(position["leverage"]),
                        float(position["entryPrice"]),
                        float(position["positionAmt"]),
                        position["symbol"],
                        position["positionSide"],
                    )

                    create_position(conn, position_row)
                    updated_positions += 1

                conn.commit()

        while not up_to_date:
            if weightused > 800:
                current_app.logger.info(
                    f"Weight used: {weightused}/800; Processed: {processed}; Sleep: 1 minute"
                )
                sleeps += 1
                _DETENER.wait(60)  # = time.sleep(60), salvo que el loop dedicado reciba SIGTERM

            if _DETENER.is_set():
                # Apagado pedido: se corta entre páginas (nunca a mitad de una transacción) y la
                # próxima pasada sigue desde el último income guardado.
                current_app.logger.info(f"Apagado pedido: income cortado tras {processed} filas")
                break

            with create_connection(current_app.config["DATABASE"]) as conn:
                startTime = select_latest_income(conn)
                if startTime is None:
                    startTime = int(
                        datetime.fromisoformat("2020-01-01 00:00:00+00:00").timestamp()
                        * 1000
                    )
                else:
                    startTime = startTime[0]

                params = {"startTime": startTime + 1, "limit": 1000}

                responseHeader, responseJSON = send_signed_request(
                    http_method="GET", url_path="/fapi/v1/income", payload=params
                )
                weightused = int(responseHeader["X-MBX-USED-WEIGHT-1M"])

                if len(responseJSON) == 0:
                    up_to_date = True
                else:
                    for income in responseJSON:
                        if len(income["tradeId"]) == 0:
                            income["tradeId"] = 0
                        income_row = (
                            int(income["tranId"]),
                            income["symbol"],
                            income["incomeType"],
                            income["income"],
                            income["asset"],
                            income["info"],
                            int(income["time"]),
                            int(income["tradeId"]),
                        )
                        create_income(conn, income_row)
                        processed += 1

                    conn.commit()
    elif current_app.config["EXCHANGE"].lower() == "bybit":
        all_symbols = []
        exec_type = {
            "Trade": "REALIZED_PNL",
            "Funding": "FUNDING_FEE",
            "AdlTrade": "ADLTRADE",
            "BustTrade": "BUSTTRADE",
        }

        params = {"category": "linear", "limit": 200, "settleCoin": "USDT"}
        responseHeader, responseJSON = send_signed_request(
            http_method="GET",
            url_path="/v5/position/list",
            payload=params,
            exchange="bybit",
        )
        if "rate_limit_status" in responseJSON:
            weightused = int(responseJSON["rate_limit_status"])

        with create_connection(current_app.config["DATABASE"]) as conn:
            app.logger.info("Deleting orders and positions from db")
            delete_all_orders(conn)
            delete_all_positions(conn)
            app.logger.info("Loading orders and positions from exchange")
            if "result" in responseJSON:
                if "list" in responseJSON["result"]:
                    for position in responseJSON["result"]["list"]:
                        if weightused > 50:
                            current_app.logger.info(
                                f"Weight used: {weightused}/{120-weightused}; Processed: {updated_positions + new_positions + updated_orders}; Sleep: 1 minute"
                            )
                            sleeps += 1
                            time.sleep(60)
                            weightused = 0

                        if position["symbol"] not in all_symbols:
                            all_symbols.append(position["symbol"])

                        if float(position["size"]) > 0:
                            position_sides = {"buy": "LONG", "sell": "SHORT"}
                            positionside = position_sides[position["side"].lower()]

                            position_row = (
                                float(position["unrealisedPnl"]),
                                int(position["leverage"]),
                                float(position["avgPrice"]),
                                float(position["size"]),
                                position["symbol"],
                                positionside,
                            )

                            create_position(conn, position_row)
                            updated_positions += 1

                            params = {
                                "symbol": position["symbol"],
                                "category": "linear",
                            }
                            responseHeader, responseJSON = send_signed_request(
                                http_method="GET",
                                url_path="/v5/order/realtime",
                                payload=params,
                                exchange="bybit",
                            )
                            if "rate_limit_status" in responseJSON:
                                weightused = int(responseJSON["rate_limit_status"])
                            else:
                                weightused += 1

                            if "result" in responseJSON:
                                if "list" in responseJSON["result"]:
                                    for order in responseJSON["result"]["list"]:
                                        updated_orders += 1

                                        orderside = order["side"].upper()

                                        row = (
                                            float(order["qty"]),
                                            float(order["price"]),
                                            orderside,
                                            positionside,
                                            order["orderStatus"],
                                            order["symbol"],
                                            int(order["createdTime"]),
                                            order["orderType"],
                                        )
                                        create_orders(conn, row)
                                else:
                                    app.logger.warning(
                                        "Orders: 'list' not in responseJSON['result']"
                                    )
                            else:
                                app.logger.warning(
                                    "Orders: 'result' not in responseJSON"
                                )
                else:
                    app.logger.warning(
                        "Positions: 'list' not in responseJSON['result']"
                    )
            else:
                app.logger.warning("Positions: 'result' not in responseJSON")

            params = {"coin": "USDT", "accountType": "CONTRACT"}
            responseHeader, responseJSON = send_signed_request(
                http_method="GET",
                url_path="/v5/account/wallet-balance",
                payload=params,
                exchange="bybit",
            )
            app.logger.info("Updating wallet balance from exchange")
            if "result" in responseJSON:
                if "list" in responseJSON["result"]:
                    if (
                        responseJSON["result"]["list"][0]["totalMaintenanceMargin"]
                        == ""
                    ):
                        maintenance_margin = 0.0
                    else:
                        maintenance_margin = float(
                            responseJSON["result"]["list"][0]["totalMaintenanceMargin"]
                        )

                    totals_row = (
                        float(
                            responseJSON["result"]["list"][0]["coin"][0][
                                "walletBalance"
                            ]
                        ),
                        float(
                            responseJSON["result"]["list"][0]["coin"][0][
                                "unrealisedPnl"
                            ]
                        ),
                        maintenance_margin,
                        float(
                            responseJSON["result"]["list"][0]["coin"][0][
                                "availableToWithdraw"
                            ]
                        ),
                        float(0),
                        1,
                    )

                    accountCheck = select_account(conn)
                    if accountCheck is None:
                        create_account(conn, totals_row)
                    elif float(accountCheck[0]) != float(
                        responseJSON["result"]["list"][0]["coin"][0]["walletBalance"]
                    ):
                        update_account(conn, totals_row)

                    conn.commit()
                else:
                    app.logger.warning("Wallet: 'list' not in responseJSON['result']")
            else:
                app.logger.warning("Wallet: 'result' not in responseJSON")

        all_symbols = sorted(all_symbols)
        app.logger.info("Updating closed PnL from exchange")
        for symbol in all_symbols:
            trades = {}
            params = {
                "symbol": symbol,
                "category": "linear",
                "limit": 100,
            }
            with create_connection(current_app.config["DATABASE"]) as conn:
                startTime = select_latest_income_symbol(conn, symbol)
                two_years_ago = datetime.now() - timedelta(days=729)
                two_years_ago_timestamp = int(two_years_ago.timestamp() * 1000)
                if startTime is None:
                    startTime = int(
                        datetime.fromisoformat("2020-01-01 00:00:00+00:00").timestamp()
                        * 1000
                    )
                else:
                    startTime = int(startTime[0]) + 1

                if startTime < two_years_ago_timestamp:
                    startTime = two_years_ago_timestamp
                params["startTime"] = startTime

            if weightused > 50:
                current_app.logger.info(
                    f"Weight used: {weightused}/100; Processed: {processed}; Sleep: 1 minute"
                )
                sleeps += 1
                time.sleep(60)
                weightused = 0

            # params["cursor"] = f"{page}"
            responseHeader, responseJSON = send_signed_request(
                http_method="GET",
                url_path="/v5/position/closed-pnl",
                payload=params,
                exchange="bybit",
            )

            if "rate_limit_status" in responseJSON:
                weightused = int(responseJSON["rate_limit_status"])
            else:
                weightused += 1

            if "result" in responseJSON:
                if responseJSON["result"] is not None:
                    if "list" in responseJSON["result"]:
                        if responseJSON["result"]["list"] is not None:
                            for trade in responseJSON["result"]["list"]:
                                trades[trade["createdTime"]] = [
                                    trade["orderId"],
                                    trade["execType"],
                                    trade["closedPnl"],
                                    trade["orderId"],
                                ]

                        else:
                            app.logger.warning(
                                "Closed PNL: responseJSON['result']['list'] is None"
                            )
                            break
                    else:
                        app.logger.warning(
                            "Closed PNL: 'data' not found in responseJSON['result']['list']"
                        )
                        break
                else:
                    app.logger.warning("Closed PNL: 'result' is None")
                    break
            else:
                app.logger.warning("Closed PNL: 'result' not found in responseJSON")
                break

            if len(trades) > 0:
                trades = OrderedDict(sorted(trades.items()))
                with create_connection(current_app.config["DATABASE"]) as conn:
                    for trade in trades:
                        income_row = (
                            trades[trade][0],
                            symbol,
                            exec_type[trades[trade][1]],
                            trades[trade][2],
                            "USDT",
                            exec_type[trades[trade][1]],
                            int(trade),
                            trades[trade][0],
                        )

                        create_income(conn, income_row)
                        processed += 1
                    conn.commit()
    else:
        current_app.logger.info(
            f"Exchange: {current_app.config['EXCHANGE']} is not currently supported"
        )

    elapsed = time.time() - start
    if app is not None:
        current_app.logger.info(
            f"Orders updated: {updated_orders}; Positions updated: {updated_positions} (new: {new_positions}); Trades processed: {processed}; Time elapsed: {timedelta(seconds=elapsed)}; Sleeps: {sleeps}",
        )
    else:
        print(
            f"Orders updated: {updated_orders}\nPositions updated: {updated_positions} (new: {new_positions})\nTrades processed: {processed}\nTime elapsed: {timedelta(seconds=elapsed)}\nSleeps: {sleeps}"
        )
    # False: Binance respondió la cuenta sin "positions" (cuenta y posiciones no se actualizaron).
    return not overweight


# --------------------------------------------------------------------------------------
# Loop dedicado: python -m futuresboard.scraper (su propio contenedor, misma imagen)
# --------------------------------------------------------------------------------------
# Reemplaza al hilo del web (que queda apagado con FUTURESBOARD_DISABLE_AUTO_SCRAPE=1). Misma pasada
# scrape() cada AUTO_SCRAPE_INTERVAL s, con el mismo control de peso y scrape_state.json. Además deja
# un latido (scraper_latido.json, junto a la DB) por cada vuelta OK y es el único que pasa la DB a WAL.


def build_app(config) -> Flask:
    """App mínima para el loop: config + logger. Sin login, blueprints ni secret key."""
    from futuresboard import logs

    logs.configure_logging()
    app = Flask(__name__)
    app.config.from_mapping(**json.loads(config.json()))
    for value in (config.API_KEY, config.API_SECRET):
        logs.register_secret(value)
    return app


def run_loop(app, *, interval=None, detener=None, max_vueltas=None) -> int:
    """Corre scrape() hasta que ``detener`` se setee (SIGTERM). Nunca se cae por una vuelta con error.

    El apagado nunca corta una escritura: la señal solo setea el evento, la vuelta en curso termina
    (o se corta entre páginas de income) y recién ahí sale.
    """
    detener = _DETENER if detener is None else detener
    interval = int(app.config["AUTO_SCRAPE_INTERVAL"] if interval is None else interval)
    database = app.config["DATABASE"]
    log = app.logger
    with app.app_context():
        modo = db.ensure_wal(database)
        log.info("Scraper en loop: cada %s s, DB %s (journal_mode=%s)", interval, database, modo or "?")
        vuelta = 0
        while not detener.is_set():
            vuelta += 1
            inicio = time.time()
            try:
                ok = scrape(app=app)
            except Exception as exc:  # un "database is locked" o un JSON raro no matan el loop
                ok = False
                log.error("Vuelta %d con error (%s): %s", vuelta, exc.__class__.__name__, redact_url(exc))
            duracion = time.time() - inicio
            if ok:
                _write_latido(database, {"at": time.time(), "vuelta": vuelta, "intervalo": interval,
                                         "duracion_segundos": round(duracion, 3), "pid": os.getpid()})
                log.info("Vuelta %d OK en %.1f s; próxima en %s s", vuelta, duracion, interval)
            else:
                log.warning("Vuelta %d sin éxito en %.1f s (sin latido); reintento en %s s",
                            vuelta, duracion, interval)
            if modo != "wal":
                modo = db.ensure_wal(database)  # no se pudo al arrancar (otra conexión con transacción)
            if max_vueltas is not None and vuelta >= max_vueltas:
                break
            detener.wait(interval)
        log.info("Scraper detenido tras %d vueltas", vuelta)
    return 0


def _pedir_apagado(signum, frame) -> None:
    # Solo setea el evento (nada de logging ni I/O dentro del handler).
    _DETENER.set()


def main(argv=None) -> int:
    from futuresboard.app import default_config_dir
    from futuresboard.config import Config

    parser = argparse.ArgumentParser(
        prog="futuresboard.scraper",
        description="Scraper en loop: scrape() cada AUTO_SCRAPE_INTERVAL s hasta SIGTERM.",
    )
    parser.add_argument("-c", "--config-dir", type=pathlib.Path, default=None,
                        help="Directorio de config (default: $FUTURESBOARD_CONFIG_DIR o ./config).")
    args = parser.parse_args(argv)
    config_dir = args.config_dir.resolve() if args.config_dir else default_config_dir()
    app = build_app(Config.from_config_dir(config_dir))
    signal.signal(signal.SIGTERM, _pedir_apagado)
    signal.signal(signal.SIGINT, _pedir_apagado)
    return run_loop(app)


if __name__ == "__main__":  # pragma: no cover
    # Con -m este archivo corre como __main__: se delega al módulo importado para que el evento de
    # apagado sea el mismo que usa _scrape().
    from futuresboard import scraper as _scraper

    raise SystemExit(_scraper.main())
