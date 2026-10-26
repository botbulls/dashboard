"""Chequeos por par de Estrategia (spec bot-estrategia § 3.2) con el manifiesto de configs de passivbot (§ 5.1).

Binance falso con respuestas reales recortadas (``tests/fixtures/binance``) y el manifiesto generado con el código de
passivbot (``tests/fixtures/manifiesto``). Sin red.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from estrategia_fakes import DEMO
from estrategia_fakes import REAL
from estrategia_fakes import Red
from estrategia_fakes import manifiesto_fixture
from futuresboard import binance_publico
from futuresboard import estrategia
from futuresboard import manifiesto

HOY = dt.date(2026, 10, 25)

CFG = {
    "user": "binance_01",
    "twe_long": 4,
    "twe_short": 1,
    "n_longs": 4,
    "n_shorts": 0,
    "max_min_cost": 150,
    "market_age_threshold": "2023-01",
    "symbols_to_ignore": ["BAKEUSDT", "FTMUSDT", "LEVERUSDT", "LITUUSDT", "UNFIUSDT", "THETAUSDT", "ALGOUSDT", "OPUSDT",
                          "INJUSDT", "1INCHUSDT"],
    "approved_symbols_long": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
    "approved_symbols_short": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"],
}


@pytest.fixture
def red(monkeypatch):
    r = Red()
    monkeypatch.setattr(binance_publico, "_get", r)
    return r


@pytest.fixture
def manif(tmp_path):
    path = tmp_path / "forager" / "new.json"
    path.parent.mkdir()
    path.write_text(json.dumps(CFG))
    (path.parent / manifiesto.NOMBRE).write_text(json.dumps(manifiesto_fixture()))
    return path


def ctx(manif_path, testnet=True, cfg=None, balance=5417.55):
    return estrategia.Contexto.armar(cfg or CFG, manif_path, testnet=testnet, base_operativa=DEMO if testnet else REAL,
                                     balance=balance, hoy=HOY)


def chequeo(res, clave):
    return next(c for c in res["chequeos"] if c["clave"] == clave)


# ---------------------------------------------------------------- manifiesto


def test_manifiesto_ausente_bloquea_agregar_con_motivo(tmp_path, red):
    path = tmp_path / "new.json"
    path.write_text(json.dumps(CFG))
    m = manifiesto.leer(path)
    assert m["estado"] == "ausente" and not m["usable"]
    assert "passivbot todavía no publicó" in m["motivo"]
    res = estrategia.chequear_par("SOLUSDT", ctx(path), lw=0.8)
    c = chequeo(res, "config")
    assert c["estado"] == "bloquea" and "passivbot todavía no publicó" in c["texto"]
    assert res["bloquea"]


@pytest.mark.parametrize("cambio,motivo", [
    (lambda m: m.update(schema_version=2), "versión"),
    (lambda m: m["markets"].update(status="not_loaded", available=False), "todavía no publicó los mercados"),
    (lambda m: m.update(symbols="x"), "inválido"),
])
def test_manifiesto_no_usable(manif, red, cambio, motivo):
    m = manifiesto_fixture()
    cambio(m)
    (manif.parent / manifiesto.NOMBRE).write_text(json.dumps(m))
    leido = manifiesto.leer(manif)
    assert not leido["usable"] and motivo in leido["motivo"]
    assert estrategia.chequear_par("SOLUSDT", ctx(manif), lw=0.8)["bloquea"]


def test_manifiesto_json_roto(manif):
    (manif.parent / manifiesto.NOMBRE).write_text("{roto")
    leido = manifiesto.leer(manif)
    assert leido["estado"] == "invalido" and not leido["usable"]


def test_simbolo_que_forager_no_conoce(manif, red):
    m = manifiesto_fixture()
    del m["symbols"]["SOLUSDT"]
    (manif.parent / manifiesto.NOMBRE).write_text(json.dumps(m))
    c = chequeo(estrategia.chequear_par("SOLUSDT", ctx(manif), lw=0.8), "config")
    assert c["estado"] == "bloquea" and "forager no conoce SOLUSDT" in c["texto"]


# ---------------------------------------------------------------- config que elegiría forager (colisiones reales)


@pytest.mark.parametrize("sym,archivo", [("TUSDT", "ANTUSDT.json"), ("SUSDT", "AXSUSDT.json"),
                                         ("OMUSDT", "ATOMUSDT.json")])
def test_colision_por_nombre_bloquea(manif, red, sym, archivo):
    res = estrategia.chequear_par(sym, ctx(manif), lw=0.8)
    assert not any(c["clave"] == "formato" for c in res["chequeos"])  # base de 1 letra: TUSDT y SUSDT existen
    c = chequeo(res, "config")
    assert c["estado"] == "bloquea"
    assert archivo in c["texto"] and "no es la config de " + sym in c["texto"]
    assert res["bloquea"]


def test_sin_config_propia_correria_la_grilla_por_defecto(manif, red):
    c = chequeo(estrategia.chequear_par("SOLVUSDT", ctx(manif), lw=0.8), "config")
    assert c["estado"] == "bloquea"
    assert "cargaydescarga.json" in c["texto"] and "neat_grid_XTZUSDT_876days" in c["texto"]


def test_solusdt_se_puede_agregar_con_aviso_sin_backtest(manif, red):
    res = estrategia.chequear_par("SOLUSDT", ctx(manif), lw=0.8)
    assert not res["bloquea"], res
    assert chequeo(res, "config")["estado"] == "ok" and "SOLUSDT.json" in chequeo(res, "config")["texto"]
    assert chequeo(res, "medido")["estado"] == "aviso" and res["sin_backtest"]
    assert chequeo(res, "mercado")["estado"] == "ok" and "Demo" in chequeo(res, "mercado")["texto"]
    assert chequeo(res, "velas")["estado"] == "ok" and "500 velas" in chequeo(res, "velas")["texto"]
    assert "09/2020" in chequeo(res, "edad")["texto"]


def test_los_cuatro_medidos_por_md5(manif, red):
    for sym in estrategia.PARES_MEDIDOS:
        res = estrategia.chequear_par(sym, ctx(manif), lw=1.0)
        assert chequeo(res, "medido")["estado"] == "ok", sym
        assert chequeo(res, "config_medida")["estado"] == "ok", sym
        assert not res["sin_backtest"] and not res["bloquea"], sym


def test_md5_distinto_del_medido_es_sin_backtest(manif, red):
    m = manifiesto_fixture()
    m["symbols"]["BTCUSDT"]["md5"] = "0" * 32
    (manif.parent / manifiesto.NOMBRE).write_text(json.dumps(m))
    res = estrategia.chequear_par("BTCUSDT", ctx(manif), lw=1.0)
    assert chequeo(res, "config_medida")["estado"] == "aviso" and res["sin_backtest"]
    assert not res["bloquea"]


# ---------------------------------------------------------------- mercado (mismo endpoint que opera)


def test_mercado_tiene_que_existir_en_el_endpoint_que_opera(manif, red):
    # ANTUSDT existe en Demo (SETTLING) y no en real.
    c = chequeo(estrategia.chequear_par("ANTUSDT", ctx(manif), lw=1.0), "mercado")
    assert c["estado"] == "bloquea" and "SETTLING" in c["texto"]
    c = chequeo(estrategia.chequear_par("ANTUSDT", ctx(manif, testnet=False), lw=1.0), "mercado")
    assert c["estado"] == "bloquea" and "No existe en Binance (real)" in c["texto"]


@pytest.mark.parametrize("sym,texto", [("XAUUSDT", "TRADIFI_PERPETUAL"), ("BTCUSDC", "USDC")])
def test_mercado_perpetual_y_usdt(manif, red, sym, texto):
    res = estrategia.chequear_par(sym, ctx(manif), lw=1.0)
    # BTCUSDC ni siquiera pasa el formato.
    assert res["bloquea"]
    assert any(texto in c["texto"] or c["clave"] == "formato" for c in res["chequeos"] if c["estado"] == "bloquea")


@pytest.mark.parametrize("sym", ["btcusdt", "BTC", "BTC-USDT", "A" * 21 + "USDT", "BTCUSDT\n"])
def test_formato(manif, red, sym):
    res = estrategia.chequear_par(sym, ctx(manif), lw=1.0)
    assert res["bloquea"] and chequeo(res, "formato")["estado"] == "bloquea"


def test_binance_caido_es_sin_datos_y_bloquea_al_agregar(manif, red):
    red.demo.fallar.add("/fapi/v1/exchangeInfo")
    res = estrategia.chequear_par("SOLUSDT", ctx(manif), lw=1.0)
    c = chequeo(res, "mercado")
    assert c["estado"] == "sin_datos" and "no se pudo" in c["texto"].lower()
    assert res["bloquea"] and res["estado"] == "sin_datos"


# ---------------------------------------------------------------- edad con la primera vela 1M


def test_edad_sui_2023_06_queda_afuera(manif, red):
    res = estrategia.chequear_par("SUIUSDT", ctx(manif), lw=1.0)
    c = chequeo(res, "edad")
    assert c["estado"] == "bloquea" and "06/2023" in c["texto"] and "2023-01" in c["texto"]
    assert res["bloquea"]
    # Pide la primera vela mensual del mismo endpoint, desde el principio.
    assert ("/fapi/v1/klines", {"symbol": "SUIUSDT", "interval": "1M", "startTime": 0, "limit": 1}) in red.demo.llamadas


def test_edad_usa_el_endpoint_que_opera(manif, red):
    # TUSDT: 01/2023 en Demo (pasa con 2023-01, como dateutil completa el día con el de hoy) y 02/2023 en real.
    assert estrategia.edad_ok(red.demo.primera["TUSDT"], "2023-01", HOY)
    assert not estrategia.edad_ok(red.real.primera["TUSDT"], "2023-01", HOY)


def test_umbral_de_edad_como_dateutil():
    # forager usa dateutil.parser.parse: lo que falta lo completa con la fecha de hoy.
    assert estrategia.umbral_edad_ms("2023-01", HOY) == int(dt.datetime(2023, 1, 25, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert estrategia.umbral_edad_ms("2023", HOY) == int(dt.datetime(2023, 10, 25, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert estrategia.umbral_edad_ms("2023-02", dt.date(2026, 10, 31)) == int(
        dt.datetime(2023, 2, 28, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert estrategia.umbral_edad_ms("2023-01-01", HOY) == int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    for sin_filtro in (0, "0", 0.0, None, "basura"):
        assert estrategia.umbral_edad_ms(sin_filtro, HOY) is None
    # Primera vela 2023-01-01 contra el umbral 2023-01-01: forager compara con "<", queda afuera.
    ts = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert not estrategia.edad_ok(ts, "2023-01-01", HOY)


# ---------------------------------------------------------------- mínimo contra max_min_cost, con su margen


def test_minimo_btc_en_demo_y_en_real(manif, red):
    res = estrategia.chequear_par("BTCUSDT", ctx(manif), lw=1.0)
    c = chequeo(res, "minimo")
    # Demo: minNotional 50 > 0,0001 × precio. Real: 0,001 × 80.019,90 = 80,02.
    assert res["minimo"]["operativo"] == pytest.approx(50.0)
    assert res["minimo"]["real"] == pytest.approx(80.0199)
    assert c["estado"] == "info"
    assert "Mínimo 50,00 de 150" in c["texto"] and "real 80,02" in c["texto"] and "150.000" in c["texto"]
    assert not res["bloquea"]


def test_minimo_btc_operando_en_real_por_encima_de_150_bloquea(manif, red):
    red.real.precios["BTCUSDT"] = "160000"
    res = estrategia.chequear_par("BTCUSDT", ctx(manif, testnet=False), lw=1.0)
    c = chequeo(res, "minimo")
    assert c["estado"] == "bloquea" and "160,00 de 150" in c["texto"]
    assert res["bloquea"]


def test_minimo_real_por_encima_solo_informa_si_opera_en_demo(manif, red):
    red.real.precios["BTCUSDT"] = "160000"
    res = estrategia.chequear_par("BTCUSDT", ctx(manif), lw=1.0)
    assert chequeo(res, "minimo")["estado"] == "aviso" and not res["bloquea"]


def test_primera_compra_contra_el_minimo(manif, red):
    # BTC: 5.417,55 × 1,00 × 0,01015 = 54,99: en Demo pasa el mínimo de 50, en real compraría 80,02.
    c = chequeo(estrategia.chequear_par("BTCUSDT", ctx(manif), lw=1.0), "primera_compra")
    assert c["estado"] == "aviso" and "55,0 USDT" in c["texto"] and "en real compraría el mínimo, 80,02" in c["texto"]
    assert c["datos"]["qty_pct"] == pytest.approx(0.010150235355761174)
    # Con 5 a la vez (lw 0,80): 44,0, menor que el mínimo de Demo.
    c = chequeo(estrategia.chequear_par("BTCUSDT", ctx(manif), lw=0.8), "primera_compra")
    assert c["estado"] == "aviso" and "44,0 USDT" in c["texto"] and "compra el mínimo" in c["texto"]
    c = chequeo(estrategia.chequear_par("BNBUSDT", ctx(manif), lw=1.0), "primera_compra")
    assert c["estado"] == "ok" and "54,2 USDT" in c["texto"]


# ---------------------------------------------------------------- 500 velas y lista negra


def test_menos_de_500_velas_bloquea(manif, red):
    red.demo.n_velas["SOLUSDT"] = 312
    res = estrategia.chequear_par("SOLUSDT", ctx(manif), lw=1.0)
    c = chequeo(res, "velas")
    assert c["estado"] == "bloquea" and "312" in c["texto"] and "500" in c["texto"]
    assert res["bloquea"]
    assert ("/fapi/v1/klines", {"symbol": "SOLUSDT", "interval": "15m", "limit": 500}) in red.demo.llamadas


def test_lista_negra_bloquea(manif, red):
    res = estrategia.chequear_par("INJUSDT", ctx(manif), lw=1.0)
    c = chequeo(res, "lista_negra")
    assert c["estado"] == "bloquea" and "symbols_to_ignore" in c["texto"]
    assert res["bloquea"]


# ---------------------------------------------------------------- caché y sin credenciales


def test_exchange_info_cacheado_10_min_y_sin_credenciales(manif, red, monkeypatch):
    reloj = [1000.0]
    monkeypatch.setattr(binance_publico, "_ahora", lambda: reloj[0])
    m = binance_publico.Mercado(DEMO, "Demo")
    m.simbolos()
    m.simbolos()
    assert sum(1 for p, _ in red.demo.llamadas if p == "/fapi/v1/exchangeInfo") == 1
    reloj[0] += binance_publico.EXCHANGE_INFO_TTL_S + 1
    m.simbolos()
    assert sum(1 for p, _ in red.demo.llamadas if p == "/fapi/v1/exchangeInfo") == 2
    assert binance_publico.EXCHANGE_INFO_TTL_S == 600


def test_pedidos_con_el_timeout_del_scraper(monkeypatch):
    from futuresboard import scraper

    vistos = []

    def get(url, params=None, timeout=None, **kw):
        vistos.append((timeout, kw))
        return Red()(url, params=params, timeout=timeout)

    monkeypatch.setattr(binance_publico, "_get", get)
    binance_publico.Mercado(DEMO, "Demo").simbolos()
    assert vistos == [(scraper.BINANCE_TIMEOUT, {})]  # ni headers ni firma


def test_error_de_binance_sin_url(manif, red):
    red.demo.colgar.add("/fapi/v1/exchangeInfo")
    with pytest.raises(binance_publico.SinRespuesta) as exc:
        binance_publico.Mercado(DEMO, "Demo").simbolos()
    assert "http" not in str(exc.value) and "ReadTimeout" in str(exc.value)


def test_varios_con_presupuesto_devuelve_sin_datos_lo_que_no_llego(manif, red, monkeypatch):
    import threading

    traba = threading.Event()
    original = red.__call__

    def lento(url, params=None, timeout=None):
        if params and params.get("symbol") == "SOLUSDT" and params.get("interval") == "15m":
            traba.wait(5)
        return original(url, params=params, timeout=timeout)

    monkeypatch.setattr(binance_publico, "_get", lento)
    res = estrategia.chequear_varios(["BTCUSDT", "SOLUSDT"], ctx(manif), lw=1.0, presupuesto_s=0.5)
    traba.set()
    por = {r["symbol"]: r for r in res}
    assert list(por) == ["BTCUSDT", "SOLUSDT"]
    assert not por["BTCUSDT"]["bloquea"]
    assert por["SOLUSDT"]["estado"] == "sin_datos" and por["SOLUSDT"]["bloquea"]
