"""Regenera ``sort_symbols.json`` (fixture del test de la réplica de rotación) con el código real de forager.

Uso (con el intérprete de passivbot: ccxt 4.1.72, numpy; sin red):

    cd <checkout de botbulls/passivbot>
    <python de passivbot> -I <este archivo> <checkout de passivbot> <salida.json>

Arma velas de 15 m sintéticas y deterministas (un generador congruencial propio, sin numpy.random) para 6 pares con
volumen, ruido y tendencia distintos, y guarda lo que devuelven ``forager.sort_symbols`` y ``forager.generate_yaml``
(qué pares elige, cuáles quedan activos y cuáles en parada gradual, leído de lo que imprime) para varias configs y
posiciones. El test de ``futuresboard.rotacion`` compara su réplica en Python puro con este resultado.
"""
import ast
import contextlib
import io
import json
import os
import sys

PARES = {
    # par: (precio, volumen base, amplitud del ruido, tendencia por vela)
    "BNBUSDT": (740.0, 3.0e3, 0.004, 0.00002),
    "BTCUSDT": (80000.0, 9.0e1, 0.003, 0.00001),
    "ETHUSDT": (2300.0, 2.5e3, 0.005, -0.00003),
    "XRPUSDT": (1.3, 4.0e6, 0.006, 0.00004),
    "SOLUSDT": (100.0, 1.5e4, 0.007, 0.00008),
    "SUIUSDT": (0.9, 2.0e5, 0.009, -0.0001),
}
N = 500
CASOS = [
    {"n_longs": 4, "posiciones": ["BTCUSDT", "ETHUSDT", "XRPUSDT"], "ordenes": ["BNBUSDT"],
     "aprobados": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]},
    {"n_longs": 4, "posiciones": [], "ordenes": [], "aprobados": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]},
    {"n_longs": 3, "posiciones": ["SUIUSDT"], "ordenes": [],
     "aprobados": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "SUIUSDT"]},
    {"n_longs": 2, "posiciones": ["BNBUSDT", "SOLUSDT"], "ordenes": ["XRPUSDT"],
     "aprobados": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"]},
    {"n_longs": 6, "posiciones": [], "ordenes": [],
     "aprobados": ["BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "SUIUSDT"]},
]
UMBRALES = {"volume_clip_threshold": 0.51, "unilateralness_clip_threshold": 0.49, "noisiness_clip_threshold": 0.52}


def lcg(semilla):
    estado = semilla
    while True:
        estado = (1103515245 * estado + 12345) % (2 ** 31)
        yield estado / 2 ** 31


def velas(semilla, precio, volumen, ruido, tendencia):
    r = lcg(semilla)
    out, p = [], precio
    for i in range(N):
        a = next(r) - 0.5
        b, c, d = next(r), next(r), next(r)
        o = p
        p = max(p * (1 + tendencia + ruido * a), 1e-9)
        hi = max(o, p) * (1 + ruido * b * 0.5)
        lo = min(o, p) * (1 - ruido * c * 0.5)
        out.append([1792500000000 + i * 900000, round(o, 8), round(hi, 8), round(lo, 8), round(p, 8),
                    round(volumen * (0.5 + d), 4)])
    return out


def main(passivbot_dir, salida):
    os.chdir(passivbot_dir)
    sys.path.insert(0, passivbot_dir)
    import forager

    ohlcvs = {s: velas(i + 1, *v) for i, (s, v) in enumerate(PARES.items())}
    casos = []
    for caso in CASOS:
        cfg = {**UMBRALES, "n_longs": caso["n_longs"], "n_shorts": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            ordenados = forager.sort_symbols({s: ohlcvs[s] for s in caso["aprobados"]}, cfg)
        cfg_yaml = {
            **cfg, "user": "binance_01", "twe_long": 4, "twe_short": 1, "before_command": "", "sleep_interval": 5,
            "max_n_panes": 8, "passivbot_root_dir": "/passivbot", "leverage": 10, "price_distance_threshold": 0.07,
            "approved_symbols_long": caso["aprobados"], "approved_symbols_short": caso["aprobados"],
            "graceful_stop": False, "graceful_stop_long": False, "graceful_stop_short": False,
            "long_mode_code": None, "short_mode_code": None, "live_configs_map_long": {}, "live_configs_map_short": {},
            "live_configs_map": {}, "default_config_path": "configs/live/cargaydescarga.json",
        }
        salida_yaml = io.StringIO()
        with contextlib.redirect_stdout(salida_yaml):
            forager.generate_yaml(ordenados, cfg_yaml, list(caso["posiciones"]), [], list(caso["ordenes"]), [])
        impreso = {}
        for linea in salida_yaml.getvalue().splitlines():
            clave, _, resto = linea.partition(" ")
            if clave in ("ideal_longs", "active_longs", "longs_on_gs"):
                impreso[clave] = ast.literal_eval(resto)
        casos.append({**caso, "config": cfg, "ordenados": [[float(v), s] for v, s in ordenados], **impreso})
    with open(salida, "w", encoding="utf-8") as fh:
        json.dump({"velas": ohlcvs, "casos": casos}, fh, indent=1, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    main(*sys.argv[1:3])
