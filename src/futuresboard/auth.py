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

from futuresboard.blueprint import get_coins


auth = Blueprint("auth", __name__)


def _ensure_users_columns(conn: sqlite3.Connection) -> None:
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "theme_default" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN theme_default text NOT NULL DEFAULT 'auto'")


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
                    password_hash text NOT NULL,
                    theme_default text NOT NULL DEFAULT 'auto'
                ); """
        )
        _ensure_users_columns(conn)

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
                "SELECT username, password_hash, theme_default FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if row and check_password_hash(row["password_hash"], password):
            session["username"] = row["username"]
            session["theme_default"] = row["theme_default"] or "auto"
            return redirect(next_url)

        flash("Invalid username or password", "error")

    return render_template("login.html", next=next_url, custom=current_app.config["CUSTOM"])


@auth.route("/logout", methods=["GET"])
def logout_page():
    session.clear()
    return redirect(url_for("auth.login_page"))


@auth.route("/settings", methods=["GET", "POST"])
def settings_page():
    if "username" not in session:
        return redirect(url_for("auth.login_page", next=url_for("auth.settings_page")))

    current_username = session["username"]

    with sqlite3.connect(current_app.config["DATABASE"]) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT username, password_hash, theme_default FROM users WHERE username = ?",
            (current_username,),
        ).fetchone()

        if row is None:
            session.clear()
            return redirect(url_for("auth.login_page"))

        if request.method == "POST":
            new_username = (request.form.get("username") or "").strip()
            theme_default = (request.form.get("theme_default") or "").strip().lower()
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""

            if theme_default not in {"light", "dark"}:
                theme_default = row["theme_default"] or "auto"

            # Update username (if changed)
            if new_username and new_username != row["username"]:
                try:
                    conn.execute(
                        "UPDATE users SET username = ? WHERE username = ?",
                        (new_username, row["username"]),
                    )
                    session["username"] = new_username
                    current_username = new_username
                    row = dict(row)
                    row["username"] = new_username
                except sqlite3.IntegrityError:
                    flash("Username already exists", "error")

            # Update password (if provided)
            if new_password:
                if check_password_hash(row["password_hash"], current_password):
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE username = ?",
                        (generate_password_hash(new_password), current_username),
                    )
                else:
                    flash("Current password is incorrect", "error")

            # Update default theme
            conn.execute(
                "UPDATE users SET theme_default = ? WHERE username = ?",
                (theme_default, current_username),
            )
            session["theme_default"] = theme_default

            conn.commit()
            flash("Settings saved", "success")

    current_theme_default = session.get("theme_default") or (row["theme_default"] or "auto")
    if current_theme_default not in {"light", "dark"}:
        current_theme_default = "dark"

    return render_template(
        "settings.html",
        coin_list=get_coins(),
        custom=current_app.config["CUSTOM"],
        username=session.get("username") or "",
        theme_default=current_theme_default,
    )


def _require_login():
    # Skip auth for static and auth endpoints.
    if request.endpoint is None:
        return None
    if request.endpoint.startswith("static"):
        return None
    if request.endpoint == "auth.login_page":
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



