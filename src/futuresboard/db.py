from __future__ import annotations

import sqlite3

from flask import current_app
from flask import g


def get_db():
    """Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row

    return g.db


def close_db(e=None):
    """If this request connected to the database, close the
    connection.
    """
    db = g.pop("db", None)

    if db is not None:
        db.close()


def query(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


def _ensure_indices():
    """Create performance indices if they do not yet exist (idempotent)."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_orders_symbol_side_pos ON orders(symbol, side, positionSide);
        CREATE INDEX IF NOT EXISTS idx_orders_symbol               ON orders(symbol);
        CREATE INDEX IF NOT EXISTS idx_income_time                ON income(time);
        CREATE INDEX IF NOT EXISTS idx_income_type_time           ON income(incomeType, time);
        """
    )
    conn.commit()
    cursor.close()


def init_app(app):
    """Register database functions with the Flask app. This is called by
    the application factory.
    """
    app.teardown_appcontext(close_db)

    # Create indices once after first request to avoid migration scripts
    @app.before_first_request
    def _create_db_indices():  # pylint: disable=unused-variable
        try:
            _ensure_indices()
        except Exception as exc:  # pragma: no cover
            app.logger.warning("Could not ensure DB indices: %s", exc)
