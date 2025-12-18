from __future__ import annotations

import sqlite3

from flask import Blueprint
from flask import current_app
from flask import flash
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask.helpers import url_for
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash


auth = Blueprint("auth", __name__)


def _ensure_database_schema(database_path: str) -> None:
    """
    Ensure the minimal database schema exists for both the dashboard and login.
    This keeps first-run behavior working with an empty DB.
    """
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS income (
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
        )
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS positions (
                    PID integer PRIMARY KEY AUTOINCREMENT,
                    symbol text,
                    unrealizedProfit real,
                    leverage integer,
                    entryPrice real,
                    positionSide text,
                    positionAmt real
                ); """
        )
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS account (
                    AID integer PRIMARY KEY,
                    totalWalletBalance real,
                    totalUnrealizedProfit real,
                    totalMarginBalance real,
                    availableBalance real,
                    maxWithdrawAmount real
                ); """
        )
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS orders (
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
        )
        conn.execute(
            """ CREATE TABLE IF NOT EXISTS users (
                    UID integer PRIMARY KEY AUTOINCREMENT,
                    username text UNIQUE NOT NULL,
                    password_hash text NOT NULL
                ); """
        )

        # Seed required baseline rows for first-run.
        conn.execute(
            """
            INSERT OR IGNORE INTO account (
                AID,
                totalWalletBalance,
                totalUnrealizedProfit,
                totalMarginBalance,
                availableBalance,
                maxWithdrawAmount
            ) VALUES (1, 0.0, 0.0, 0.0, 0.0, 0.0)
            """
        )

        # Seed a test user. Credentials: cliente17 / 123456
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, ?)",
            ("cliente17", generate_password_hash("123456")),
        )
        conn.commit()


@auth.route("/login", methods=["GET", "POST"])
def login_page():
    next_url = request.args.get("next") or url_for("main.index_page")

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        with sqlite3.connect(current_app.config["DATABASE"]) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT username, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if row and check_password_hash(row["password_hash"], password):
            session["username"] = row["username"]
            return redirect(next_url)

        flash("Invalid username or password", "error")

    return render_template("login.html", next=next_url, custom=current_app.config["CUSTOM"])


@auth.route("/logout", methods=["GET"])
def logout_page():
    session.clear()
    return redirect(url_for("auth.login_page"))


def _require_login():
    # Skip auth for static and auth endpoints.
    if request.endpoint is None:
        return None
    if request.endpoint.startswith("static"):
        return None
    if request.endpoint.startswith("auth."):
        return None

    if "username" not in session:
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.login_page", next=next_url))
    return None


def init_app(app) -> None:
    app.register_blueprint(auth)
    with app.app_context():
        _ensure_database_schema(str(current_app.config["DATABASE"]))
    app.before_request(_require_login)



