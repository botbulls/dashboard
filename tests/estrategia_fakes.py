"""Binance público falso para los tests de Estrategia (spec bot-estrategia § 3.2 y § 5.2).

Sirve ``exchangeInfo``, precios y la primera vela mensual desde ``tests/fixtures/binance`` (respuestas reales de
``demo-fapi`` y ``fapi`` del 25/10/2026, recortadas a los pares de los tests) y velas de 15 m sintéticas. Se instala
reemplazando ``binance_publico._get`` (lo que ``conftest.py`` bloquea en el resto de los tests).
"""
from __future__ import annotations

import json
import pathlib
from urllib.parse import urlparse

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
DEMO = "https://demo-fapi.binance.com"
REAL = "https://fapi.binance.com"
# 2026-10-25 22:00 UTC: la hora de las fixtures.
AHORA_MS = 1792965600000


def _cargar(nombre):
    return json.loads((FIXTURES / "binance" / nombre).read_text(encoding="utf-8"))


def manifiesto_fixture():
    """Manifiesto generado con el código real de passivbot (``fixtures/manifiesto/generar.py``)."""
    return json.loads((FIXTURES / "manifiesto" / "live_configs_manifest.json").read_text(encoding="utf-8"))


class Respuesta:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def velas(n, precio=100.0, volumen=1000.0, desde_ms=AHORA_MS - 500 * 900_000, paso=0.0):
    """``n`` velas de 15 m con la forma de ``/fapi/v1/klines`` (strings, como Binance)."""
    out = []
    for i in range(n):
        p = precio * (1 + paso * i)
        t = desde_ms + i * 900_000
        out.append([t, f"{p}", f"{p * 1.01}", f"{p * 0.99}", f"{p}", f"{volumen}", t + 899_999, "0", 1, "0", "0", "0"])
    return out


class BinancePublico:
    """Un endpoint (Demo o real). ``fallar``: paths que responden 500; ``colgar``: paths que tiran timeout."""

    def __init__(self, nombre):
        self.nombre = nombre
        sufijo = "demo" if nombre == "demo" else "real"
        self.info = _cargar(f"exchangeInfo-{sufijo}.json")
        self.precios = {p["symbol"]: p["price"] for p in _cargar(f"precios-{sufijo}.json")}
        self.primera = _cargar(f"primera-vela-1M-{sufijo}.json")
        self.n_velas = {}       # símbolo -> cantidad de velas de 15 m (default 500)
        self.velas_15m = {}     # símbolo -> velas propias (rotación)
        self.fallar = set()
        self.colgar = set()
        self.llamadas = []

    def set_status(self, symbol, status):
        for s in self.info["symbols"]:
            if s["symbol"] == symbol:
                s["status"] = status

    def responder(self, path, params):
        import requests

        self.llamadas.append((path, dict(params or {})))
        if path in self.colgar:
            raise requests.ReadTimeout("timeout")
        if path in self.fallar:
            return Respuesta(500, {"code": -1001, "msg": "Internal error"})
        if path == "/fapi/v1/exchangeInfo":
            return Respuesta(200, json.loads(json.dumps(self.info)))
        if path == "/fapi/v1/ticker/price":
            return Respuesta(200, [{"symbol": s, "price": p, "time": AHORA_MS} for s, p in self.precios.items()])
        if path == "/fapi/v1/klines":
            sym, intervalo = params["symbol"], params["interval"]
            if intervalo == "1M":
                ts = self.primera.get(sym)
                if ts is None:
                    return Respuesta(400, {"code": -1121, "msg": "Invalid symbol."})
                return Respuesta(200, [[ts, "1", "1", "1", "1", "1", ts + 1, "1", 1, "1", "1", "0"]])
            if sym in self.velas_15m:
                return Respuesta(200, self.velas_15m[sym][-int(params.get("limit", 500)):])
            return Respuesta(200, velas(self.n_velas.get(sym, 500)))
        return Respuesta(404, {"code": -5000, "msg": "Path not found"})


class Red:
    """``binance_publico._get`` falso: enruta por la base de la URL a Demo o real."""

    def __init__(self):
        self.demo = BinancePublico("demo")
        self.real = BinancePublico("real")

    def __call__(self, url, params=None, timeout=None):
        u = urlparse(url)
        base = f"{u.scheme}://{u.netloc}"
        destino = {DEMO: self.demo, REAL: self.real}.get(base)
        if destino is None:
            raise AssertionError(f"URL inesperada en los tests: {base}")
        assert timeout is not None, "los pedidos a Binance llevan timeout"
        return destino.responder(u.path, params)
