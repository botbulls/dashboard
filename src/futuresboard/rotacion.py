"""Qué elegiría forager con más pares que lugares (spec bot-estrategia § 1.5, § 2.5 y supuesto S1).

Réplica en Python puro (el panel no tiene numpy) de lo que hace forager v6.1 en cada ciclo:

* ``ordenar``: ``forager.sort_symbols`` (``forager.py:80-104``). Ordena por volumen (de mayor a menor), unilateralidad
  (de menor a mayor) y ruido (de mayor a menor); en cada paso se queda con ``max(round(N × umbral), n_longs)``. El
  orden final es el del último filtro.
* ``elegir``: los lugares de ``forager.generate_yaml`` (``forager.py:130-148``). Las posiciones y las órdenes abiertas
  conservan su lugar; los libres se llenan con los primeros de la lista ideal; los que tienen posición u órdenes y no
  quedaron activos pasan a parada gradual.

``tests/fixtures/rotacion`` tiene el resultado del código real de forager con las mismas velas (y cómo regenerarlo): el
test compara las dos cosas. Es una **estimación con las velas de ahora**: cada 15 min forager vuelve a elegir.
"""
from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence

UMBRALES = (("volume", True), ("unilateralness", False), ("noisiness", True))


def _spans(n: int) -> List[int]:
    # np.linspace(0.1, 0.9, 4): start + i * step, con el último exacto.
    paso = (0.9 - 0.1) / 3
    fracciones = [i * paso + 0.1 for i in range(3)] + [0.9]
    return [int(round(n * f)) for f in fracciones]


def _emas(xs: Sequence[float], spans: Sequence[int]) -> List[List[float]]:
    alphas = [2 / (s + 1) for s in spans]
    alphas_ = [1 - a for a in alphas]
    emas = [[xs[0]] * len(spans)]
    for x in xs[1:]:
        prev = emas[-1]
        emas.append([prev[j] * alphas_[j] + x * alphas[j] for j in range(len(spans))])
    return emas


def _media(vals: Sequence[float]) -> float:
    return sum(vals) / len(vals)


def unilateralidad(velas: Sequence[Sequence[float]]) -> float:
    closes = [float(v[4]) for v in velas]
    emas = _emas(closes, _spans(len(closes)))
    total = 0.0
    for i in range(1, len(emas[0])):
        total += _media([1 - fila[i] / fila[i - 1] for fila in emas])
    return abs(total)


def ruido(velas: Sequence[Sequence[float]]) -> float:
    return _media([(float(v[2]) - float(v[3])) / float(v[4]) for v in velas])


def volumen(velas: Sequence[Sequence[float]]) -> float:
    return sum(float(v[5]) * float(v[4]) for v in velas)


_FUNCIONES = {"volume": volumen, "unilateralness": unilateralidad, "noisiness": ruido}


def ordenar(velas: Dict[str, Sequence[Sequence[float]]], cfg: Dict[str, Any]) -> List[tuple]:
    """``forager.sort_symbols``: lista de (valor del último filtro, par), del mejor al peor."""
    minimo = max(int(cfg.get("n_longs") or 0), int(cfg.get("n_shorts") or 0))
    filtrados = list(velas)
    por = [(0.0, s) for s in filtrados]
    for titulo, mayor_mejor in UMBRALES:
        umbral = float(cfg.get(f"{titulo}_clip_threshold", 0.5))
        if umbral == 0.0:
            continue
        por = sorted([(_FUNCIONES[titulo](velas[s]), s) for s in filtrados], reverse=mayor_mejor)
        por = por[: max(int(round(len(por) * umbral)), minimo)]
        filtrados = [e[1] for e in por]
    return por


def elegir(ordenados: List[tuple], aprobados: Sequence[str], n_longs: int, posiciones: Sequence[str],
           ordenes: Sequence[str], parada_gradual: bool = False) -> Dict[str, List[str]]:
    """Los lugares de ``forager.generate_yaml`` (lado long): ideales, activos y en parada gradual."""
    syms = [s for _, s in ordenados]
    actuales = sorted(set(posiciones) | set(ordenes))
    permitidos = set(aprobados) if aprobados else set(syms)
    ideales = [s for s in syms if s in permitidos][:n_longs]
    libres = max(0, n_longs - len(actuales))
    activos = [s for s in ideales if s in actuales]
    activos += [s for s in ideales if s not in activos][:libres]
    if parada_gradual:
        ideales, activos = [], []
    en_gs = [s for s in actuales if s not in activos]
    return {"ideales": ideales, "activos": activos, "en_parada_gradual": en_gs}


def a_floats(klines: Sequence[Sequence[Any]]) -> List[List[float]]:
    """Velas de ``/fapi/v1/klines`` (strings) como las devuelve ccxt: [ts, o, h, l, c, v] en float."""
    return [[float(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines]


def estimar(velas: Dict[str, Sequence[Sequence[Any]]], cfg: Dict[str, Any], aprobados: Sequence[str], n_longs: int,
            posiciones: Sequence[str], ordenes: Sequence[str], parada_gradual: bool = False) -> Dict[str, Any]:
    """Qué elegiría forager hoy. ``velas``: solo los pares que pasan sus filtros (mínimo, edad, lista negra). Igual que
    forager, se queda con los que tienen tantas velas como el que más (``forager.py:518``)."""
    floats = {s: a_floats(v) for s, v in velas.items() if v}
    if not floats:
        return {"ordenados": [], "ideales": [], "activos": [], "en_parada_gradual": sorted(set(posiciones) | set(ordenes)),
                "sin_velas": list(velas)}
    largo = max(len(v) for v in floats.values())
    completos = {s: v for s, v in floats.items() if len(v) == largo}
    ordenados = ordenar(completos, {**cfg, "n_longs": n_longs, "n_shorts": 0})
    out: Dict[str, Any] = {"ordenados": [s for _, s in ordenados],
                           "sin_velas": sorted(set(velas) - set(completos))}
    out.update(elegir(ordenados, aprobados, n_longs, posiciones, ordenes, parada_gradual))
    out["afuera"] = [s for s in aprobados if s not in out["ideales"]]
    return out


def texto(est: Dict[str, Any], nombre: str, corto: Optional[Any] = None) -> str:
    c = corto or (lambda s: s[:-4] if s.endswith("USDT") else s)
    if not est.get("ideales"):
        return f"En Binance {nombre} no se pudo estimar (sin velas)."
    afuera = est.get("afuera") or []
    gs = [s for s in est.get("en_parada_gradual") or [] if s in afuera]
    partes = [f"en Binance {nombre} elegiría {', '.join(c(s) for s in est['ideales'])}"]
    if afuera:
        partes.append(f"({', '.join(c(s) for s in afuera)} afuera{'; sigue en parada gradual hasta cerrar' if gs else ''})")
    return " ".join(partes)
