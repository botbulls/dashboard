"""Regenera ``live_configs_manifest.json`` (fixture de los tests de Estrategia) con el código real de passivbot.

Uso (con el intérprete de passivbot, el que tiene ccxt 4.1.72 y numba; sin red):

    cd <checkout de botbulls/passivbot con forager_live_configs.py>
    <python de passivbot> -I <este archivo> <checkout de passivbot> <tests/fixtures/binance/exchangeInfo-demo.json> \
        <salida.json>

Arma el manifiesto con ``forager_live_configs.build_manifest`` (la misma función que usa forager al cargar los
markets) sobre las 202 configs clock del repo y los perpetuos USDT del ``exchangeInfo`` de Demo recortado. Después:

* recorta ``files`` y ``symbols`` a los pares que usan los tests (el manifiesto real tiene cientos);
* reemplaza ``cwd`` por ``/passivbot`` (el del contenedor) y fija ``generated_at`` y ``image_revision``;
* como config por defecto usa ``configs/live/neat_grid_mode.example.json`` (una grilla, como la
  ``cargaydescarga.json`` de dev, que no está en el repo) y la renombra a ``cargaydescarga.json`` con el
  ``config_name`` de dev (``neat_grid_XTZUSDT_876days``).
"""
import json
import os
import sys

PARES = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "SOLVUSDT", "SUIUSDT", "TUSDT", "SUSDT", "ANTUSDT",
         "AXSUSDT", "ATOMUSDT", "OMUSDT", "INJUSDT", "EOSUSDT", "LITUSDT"]
DEFAULT_REPO = "configs/live/neat_grid_mode.example.json"
DEFAULT_DEV = "configs/live/cargaydescarga.json"


def main(passivbot_dir, exchange_info_path, salida):
    os.chdir(passivbot_dir)
    sys.path.insert(0, passivbot_dir)
    from forager_live_configs import build_manifest

    info = json.load(open(exchange_info_path))
    simbolos = sorted(s["symbol"] for s in info["symbols"] if s["symbol"].endswith("USDT"))
    snapshot = {
        "live_configs_dir_long": "configs/live/single_symbol_optimizations/clock/",
        "live_configs_map": {},
        "live_configs_map_long": {},
        "default_config_path": DEFAULT_REPO,
    }
    m = build_manifest(snapshot, symbols=simbolos, exchange="binanceusdm", testnet=True,
                       forager_config_path="configs/forager/new.json", generated_at="2026-10-25T22:30:00Z")
    m["cwd"] = "/passivbot"
    m["image_revision"] = "6b2887ed0"
    usados = {s["file"] for k, s in m["symbols"].items() if k in PARES}
    m["files"] = {k: v for k, v in m["files"].items() if k in usados}
    m["symbols"] = {k: v for k, v in m["symbols"].items() if k in PARES}

    def renombrar(d):
        if d.get("path") == DEFAULT_REPO:
            d["path"] = DEFAULT_DEV
        if d.get("file") == os.path.basename(DEFAULT_REPO):
            d["file"] = os.path.basename(DEFAULT_DEV)
        if d.get("passivbot_mode") == "neat_grid":
            d["config_name"] = "neat_grid_XTZUSDT_876days"

    renombrar(m["default_config"])
    for s in m["symbols"].values():
        renombrar(s)
    with open(salida, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
