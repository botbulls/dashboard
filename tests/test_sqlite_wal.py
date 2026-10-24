"""SQLite: busy_timeout en todas las conexiones, WAL y lectura concurrente entre procesos."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest
from flask import Flask

import futuresboard
from futuresboard import db
from futuresboard import notifier
from futuresboard import scraper

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
PKG = SRC / "futuresboard"
BUSY_MS = int(db.BUSY_TIMEOUT_SECONDS * 1000)


def _busy_ms(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA busy_timeout").fetchone()[0])


def _journal_mode(path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def test_los_tests_importan_este_worktree():
    assert pathlib.Path(futuresboard.__file__).resolve().is_relative_to(SRC)


def test_no_hay_sqlite3_connect_fuera_de_db_connect():
    """Toda conexión pasa por db.connect (busy_timeout). Si alguien agrega un sqlite3.connect
    directo, este test lo marca."""
    culpables = [
        p.name
        for p in PKG.rglob("*.py")
        if p.name != "db.py" and "sqlite3.connect(" in p.read_text(encoding="utf-8")
    ]
    assert culpables == []


def test_connect_fija_busy_timeout(tmp_path):
    path = tmp_path / "futures.db"
    conn = db.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    assert _busy_ms(conn) == BUSY_MS
    conn.close()

    ro = db.connect(path, readonly=True)
    assert _busy_ms(ro) == BUSY_MS
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")
    ro.close()


def test_get_db_scraper_y_notifier_usan_busy_timeout(tmp_path):
    path = tmp_path / "futures.db"
    sqlite3.connect(path).close()

    app = Flask(__name__)
    app.config["DATABASE"] = path
    db.init_app(app)
    with app.app_context():
        assert _busy_ms(db.get_db()) == BUSY_MS

    conn = scraper.create_connection(path)
    assert _busy_ms(conn) == BUSY_MS
    conn.close()

    ro = notifier._connect_ro(path)
    assert _busy_ms(ro) == BUSY_MS
    ro.close()


def test_connect_readonly_con_ruta_con_espacios(tmp_path):
    carpeta = tmp_path / "con espacio #1"
    carpeta.mkdir()
    path = carpeta / "futures.db"
    sqlite3.connect(path).close()
    ro = db.connect(path, readonly=True)
    assert ro.execute("SELECT 1").fetchone()[0] == 1
    ro.close()


def test_ensure_wal_activa_y_persiste(tmp_path):
    path = tmp_path / "futures.db"
    sqlite3.connect(path).close()
    assert _journal_mode(path) == "delete"

    assert db.ensure_wal(path) == "wal"
    # Persistente: una conexión nueva (de cualquier proceso) ya lo ve en WAL.
    assert _journal_mode(path) == "wal"
    # Idempotente.
    assert db.ensure_wal(path) == "wal"


def test_ensure_wal_no_fuerza_con_transaccion_abierta(tmp_path, monkeypatch):
    path = tmp_path / "futures.db"
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE t (x)")
    setup.execute("INSERT INTO t VALUES (1)")
    setup.commit()
    setup.close()

    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.2)
    lector = sqlite3.connect(path, isolation_level=None)
    lector.execute("BEGIN")
    lector.execute("SELECT * FROM t").fetchall()  # lock SHARED retenido
    try:
        modo = db.ensure_wal(path)  # no lanza
        assert modo != "wal"
        assert _journal_mode(path) == "delete"
    finally:
        lector.execute("COMMIT")
        lector.close()

    assert db.ensure_wal(path) == "wal"


def test_check_scrape_del_notifier_mira_el_wal(tmp_path):
    path = tmp_path / "futures.db"
    sqlite3.connect(path).close()
    wal = tmp_path / "futures.db-wal"
    wal.write_bytes(b"")
    ahora = time.time()
    os.utime(path, (ahora - 3600, ahora - 3600))  # el archivo principal no cambia hasta el checkpoint
    os.utime(wal, (ahora - 30, ahora - 30))

    ok, detalle = notifier.check_scrape(path, 900, ahora)
    assert ok, detalle

    wal.unlink()
    ok, _ = notifier.check_scrape(path, 900, ahora)
    assert not ok


# --------------------------------------------------------------------------------------
# Dos procesos reales: escritor con el patrón del scraper y lector con el del web
# --------------------------------------------------------------------------------------

_PRELUDIO = """
import json, sqlite3, sys, time
sys.path.insert(0, __SRC__)
import futuresboard
assert futuresboard.__file__.startswith(__SRC__), futuresboard.__file__
from futuresboard import db, scraper
path, segundos = sys.argv[1], float(sys.argv[2])
fin = time.time() + segundos
"""

ESCRITOR = _PRELUDIO + """
vueltas = locked = 0
while time.time() < fin:
    try:
        with scraper.create_connection(path) as conn:
            scraper.delete_all_orders(conn)
            for i in range(50):
                scraper.create_orders(conn, (1.0, 100.0 + i, "BUY", "LONG", "NEW", "BTCUSDT", 1, "LIMIT"))
            conn.commit()
        with scraper.create_connection(path) as conn:
            scraper.delete_all_positions(conn)
            for i in range(300):
                scraper.create_position(conn, (0.1, 10, 1.0, 1.0 if i < 20 else 0.0, "S%dUSDT" % i, "LONG"))
            conn.commit()
        with scraper.create_connection(path) as conn:
            base = int(time.time() * 1e6)
            for j in range(200):
                scraper.create_income(conn, (base + j, "BTCUSDT", "REALIZED_PNL", 0.1, "USDT", "", base, j))
            conn.commit()
        vueltas += 1
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc):
            raise
        locked += 1
print(json.dumps({"vueltas": vueltas, "locked": locked}))
"""

LECTOR = _PRELUDIO + """
lecturas = locked = sin_posiciones = 0
lat_max = 0.0
while time.time() < fin:
    t0 = time.time()
    try:
        conn = db.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        conn.execute("SELECT symbol, SUM(income) FROM income GROUP BY symbol").fetchall()
        conn.execute("SELECT SUM(income) FROM income WHERE incomeType != 'TRANSFER'").fetchone()
        conn.close()
        lecturas += 1
        sin_posiciones += n == 0
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc):
            raise
        locked += 1
    lat_max = max(lat_max, time.time() - t0)
print(json.dumps({"lecturas": lecturas, "locked": locked, "sin_posiciones": sin_posiciones,
                  "lat_max": lat_max}))
"""


def _db_poblada(path: pathlib.Path, filas: int = 20000) -> None:
    scraper.db_setup(str(path))
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO income(tranId, symbol, incomeType, income, asset, info, time, tradeId) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ((i, f"S{i % 40}USDT", "REALIZED_PNL", 0.01, "USDT", "", i, i) for i in range(filas)),
    )
    conn.executemany(
        "INSERT INTO positions(unrealizedProfit, leverage, entryPrice, positionAmt, symbol, positionSide) "
        "VALUES (?,?,?,?,?,?)",
        ((0.1, 10, 1.0, 1.0, f"S{i}USDT", "LONG") for i in range(300)),
    )
    conn.commit()
    conn.close()


def _correr_en_paralelo(path: pathlib.Path, segundos: float, tmp_path: pathlib.Path):
    escritor = tmp_path / "escritor.py"
    lector = tmp_path / "lector.py"
    escritor.write_text(ESCRITOR.replace("__SRC__", repr(str(SRC))))
    lector.write_text(LECTOR.replace("__SRC__", repr(str(SRC))))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    procs = [
        subprocess.Popen([sys.executable, "-I", str(script), str(path), str(segundos)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for script in (escritor, lector)
    ]
    salidas = []
    for proc in procs:
        out, err = proc.communicate(timeout=segundos + 60)
        assert proc.returncode == 0, err
        salidas.append(json.loads(out.strip().splitlines()[-1]))
    return salidas


def test_escritor_y_lector_concurrentes_sin_database_is_locked(tmp_path):
    path = tmp_path / "futures.db"
    _db_poblada(path)
    assert db.ensure_wal(path) == "wal"

    esc, lec = _correr_en_paralelo(path, 2.5, tmp_path)

    assert esc["vueltas"] > 0 and lec["lecturas"] > 0, (esc, lec)
    assert esc["locked"] == 0, esc
    assert lec["locked"] == 0, lec
    assert _journal_mode(path) == "wal"
