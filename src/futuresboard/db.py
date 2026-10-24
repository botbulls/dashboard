from __future__ import annotations

import logging
import pathlib
import sqlite3

from flask import current_app
from flask import g

log = logging.getLogger(__name__)

# Espera máxima ante un lock de SQLite antes de fallar con "database is locked". Aplica a TODAS
# las conexiones (web, scraper, auth, notifier) vía connect(). En WAL los lectores no bloquean al
# escritor ni al revés; solo dos escritores compiten (scraper vs. login/panel del web), y las
# transacciones del scraper duran milisegundos.
BUSY_TIMEOUT_SECONDS = 15.0


def connect(path, *, readonly: bool = False, **kwargs) -> sqlite3.Connection:
    """Única forma de abrir la SQLite del dashboard: fija el busy_timeout.

    ``readonly=True`` abre con ``mode=ro`` (lo usa el notifier). En WAL un lector de solo lectura
    necesita ver ``-wal``/``-shm`` junto a la DB: hay que montar el directorio, no el archivo solo.
    """
    kwargs.setdefault("timeout", BUSY_TIMEOUT_SECONDS)
    if readonly:
        uri = pathlib.Path(path).absolute().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, **kwargs)
    return sqlite3.connect(path, **kwargs)


def ensure_wal(path) -> str:
    """Pasa la DB a ``journal_mode=WAL`` si no lo está y devuelve el modo resultante.

    El modo WAL queda grabado en el archivo (persistente): alcanza con hacerlo una vez y lo hace
    el escritor (el loop del scraper al arrancar). Si hay otra conexión con una transacción
    abierta, SQLite no puede cambiar el modo: no se fuerza nada, se loguea y el llamador
    reintenta en la vuelta siguiente. Nunca lanza.
    """
    try:
        conn = connect(path)
    except sqlite3.Error as exc:
        log.warning("No se pudo abrir la DB para activar WAL: %s", exc)
        return ""
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode != "wal":
            mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if mode != "wal":
            log.warning("SQLite sigue en journal_mode=%s (¿otra conexión con una transacción abierta?)", mode)
        return mode
    except sqlite3.Error as exc:
        log.warning("No se pudo activar WAL en la DB: %s", exc)
        return ""
    finally:
        conn.close()


def get_db():
    """Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"], detect_types=sqlite3.PARSE_DECLTYPES)
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


def init_app(app):
    """Register database functions with the Flask app. This is called by
    the application factory.
    """
    app.teardown_appcontext(close_db)
