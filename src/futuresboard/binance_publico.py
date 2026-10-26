"""API pública de Binance USDⓈ-M (sin credenciales) para Estrategia y los chequeos de forager (spec bot-estrategia § 5.2).

Lo que necesita el panel para repetir los filtros de forager sobre un par: ``exchangeInfo`` (existe, tipo de contrato,
cotización, estado, ``minQty`` y ``minNotional``), el precio, la primera vela mensual (edad) y las 500 velas de 15 m
que pide forager. Todo con GET públicos, **sin key ni firma**, con el timeout del scraper
(``scraper.BINANCE_TIMEOUT``) y en caché por proceso:

* ``exchangeInfo``: 10 min (``EXCHANGE_INFO_TTL_S``);
* precios (``/fapi/v1/ticker/price`` de todos los pares, peso 2): 1 min;
* velas de 15 m: un ciclo de forager (15 min);
* primera vela mensual: 1 día (no cambia; un par sin velas todavía se vuelve a pedir en 1 h).

Cada clave de la caché se pide una sola vez aunque la pidan varios hilos a la vez (``_cacheado``). Los errores salen
como ``SinRespuesta`` con un texto sin URL ni parámetros. Los tests reemplazan ``_get`` (``tests/conftest.py`` lo
bloquea: ningún test sale a la red).
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

import requests

from futuresboard.config import BINANCE_FUTURES_MAINNET_URL
from futuresboard.scraper import BINANCE_TIMEOUT as TIMEOUT  # el mismo tope que el scraper: (conexión, lectura)

EXCHANGE_INFO_TTL_S = 600
PRECIOS_TTL_S = 60
VELAS_TTL_S = 15 * 60
PRIMERA_VELA_TTL_S = 24 * 3600
PRIMERA_VELA_VACIA_TTL_S = 3600
# forager pide las velas con el ``limit`` por defecto de Binance (500) y descarta los pares con menos (forager.py:518).
VELAS_FORAGER = 500

# Transporte: función tipo requests.get. Los tests la reemplazan.
_get: Callable[..., Any] = requests.get
_ahora: Callable[[], float] = time.monotonic

_cache: Dict[tuple, tuple] = {}
_cache_lock = threading.Lock()
_claves_lock: Dict[tuple, threading.Lock] = {}


class SinRespuesta(Exception):
    """Binance no respondió o respondió algo que no se puede usar. El texto no lleva URL."""


def limpiar_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _claves_lock.clear()


def _cacheado(clave: tuple, ttl: float, fn: Callable[[], Any], ttl_de: Optional[Callable[[Any], float]] = None) -> Any:
    with _cache_lock:
        entrada = _cache.get(clave)
        if entrada is not None and entrada[0] > _ahora():
            return entrada[1]
        lock = _claves_lock.setdefault(clave, threading.Lock())
    with lock:
        with _cache_lock:
            entrada = _cache.get(clave)
            if entrada is not None and entrada[0] > _ahora():
                return entrada[1]
        valor = fn()
        vence = _ahora() + (ttl_de(valor) if ttl_de else ttl)
        with _cache_lock:
            _cache[clave] = (vence, valor)
        return valor


def _dec(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)
    return d if d.is_finite() else Decimal(0)


def _filtros(sym: Dict[str, Any]) -> Dict[str, Any]:
    return {f.get("filterType"): f for f in sym.get("filters") or [] if isinstance(f, dict)}


class Mercado:
    """Un endpoint de Binance USDⓈ-M: el que opera (Demo con ``BINANCE_TESTNET``) o el real, para informar."""

    def __init__(self, base_url: str, nombre: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.nombre = nombre  # "Demo" o "real": va en los textos

    def _pedir(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            resp = _get(f"{self.base_url}{path}", params=params or None, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SinRespuesta(f"Binance ({self.nombre}) no respondió ({exc.__class__.__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code != 200:
            codigo = data.get("code") if isinstance(data, dict) else None
            raise SinRespuesta(f"Binance ({self.nombre}) respondió {resp.status_code}"
                               f"{f' (código {codigo})' if codigo is not None else ''}")
        if data is None:
            raise SinRespuesta(f"Binance ({self.nombre}) respondió sin JSON válido")
        return data

    def simbolos(self) -> Dict[str, Dict[str, Any]]:
        """``exchangeInfo`` resumido por símbolo: estado, tipo, cotización, ``minQty`` (LOT_SIZE) y ``minNotional``."""

        def pedir() -> Dict[str, Dict[str, Any]]:
            data = self._pedir("/fapi/v1/exchangeInfo")
            if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
                raise SinRespuesta(f"Binance ({self.nombre}) devolvió un exchangeInfo inesperado")
            out = {}
            for s in data["symbols"]:
                if not isinstance(s, dict) or not isinstance(s.get("symbol"), str):
                    continue
                f = _filtros(s)
                out[s["symbol"]] = {
                    "symbol": s["symbol"],
                    "status": s.get("status"),
                    "contractType": s.get("contractType"),
                    "quoteAsset": s.get("quoteAsset"),
                    # Como ccxt (limits.amount.min y limits.cost.min), que es lo que usa forager.
                    "min_qty": float(_dec((f.get("LOT_SIZE") or {}).get("minQty"))),
                    "min_notional": float(_dec((f.get("MIN_NOTIONAL") or {}).get("notional"))),
                }
            return out

        return _cacheado((self.base_url, "exchangeInfo"), EXCHANGE_INFO_TTL_S, pedir)

    def precios(self) -> Dict[str, float]:
        def pedir() -> Dict[str, float]:
            data = self._pedir("/fapi/v1/ticker/price")
            if not isinstance(data, list):
                raise SinRespuesta(f"Binance ({self.nombre}) devolvió precios inesperados")
            out = {}
            for p in data:
                if isinstance(p, dict) and isinstance(p.get("symbol"), str):
                    v = float(_dec(p.get("price")))
                    if v > 0:
                        out[p["symbol"]] = v
            return out

        return _cacheado((self.base_url, "precios"), PRECIOS_TTL_S, pedir)

    def primera_vela_1m(self, symbol: str) -> Optional[int]:
        """Apertura (ms) de la primera vela mensual, como ``get_first_ohlcv_timestamps`` de forager. None: sin velas."""

        def pedir() -> Optional[int]:
            data = self._pedir("/fapi/v1/klines", {"symbol": symbol, "interval": "1M", "startTime": 0, "limit": 1})
            if not isinstance(data, list):
                raise SinRespuesta(f"Binance ({self.nombre}) devolvió velas inesperadas")
            if not data:
                return None
            try:
                return int(data[0][0])
            except (TypeError, ValueError, IndexError):
                raise SinRespuesta(f"Binance ({self.nombre}) devolvió velas inesperadas") from None

        return _cacheado((self.base_url, "1M", symbol), PRIMERA_VELA_TTL_S, pedir,
                         ttl_de=lambda v: PRIMERA_VELA_TTL_S if v is not None else PRIMERA_VELA_VACIA_TTL_S)

    def velas_15m(self, symbol: str) -> List[List[Any]]:
        """Las velas de 15 m que pide forager (``limit`` 500), como las devuelve Binance."""

        def pedir() -> List[List[Any]]:
            data = self._pedir("/fapi/v1/klines", {"symbol": symbol, "interval": "15m", "limit": VELAS_FORAGER})
            if not isinstance(data, list) or not all(isinstance(r, list) and len(r) >= 6 for r in data):
                raise SinRespuesta(f"Binance ({self.nombre}) devolvió velas inesperadas")
            return data

        return _cacheado((self.base_url, "15m", symbol), VELAS_TTL_S, pedir)


def mercados(testnet: bool, base_operativa: Optional[str]) -> tuple:
    """(operativo, real): el endpoint que opera passivbot y el real (para mostrar el mínimo de Binance real). Fuera de
    Demo son el mismo. ``base_operativa``: ``API_BASE_URL`` de la config (con testnet apunta a demo-fapi)."""
    if testnet:
        return Mercado(base_operativa or "", "Demo"), Mercado(BINANCE_FUTURES_MAINNET_URL, "real")
    real = Mercado(BINANCE_FUTURES_MAINNET_URL, "real")
    return real, real
