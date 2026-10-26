"""Manifiesto de configs live de passivbot (spec bot-estrategia § 5.1; botbulls/passivbot#6).

forager escribe ``live_configs_manifest.json`` en el directorio de su config (``configs/forager/``, que el dashboard
monta como ``/forager``): qué archivo de config elegiría para cada símbolo con su propia regla (``map_long`` > ``map`` >
primer archivo, en orden alfabético, cuyo nombre contiene el símbolo > ``default_config_path``), con md5, modo y
``qty_pct_entry`` long. Es la única forma de que el panel vea las configs clock, que viven adentro de la imagen.

``leer`` nunca lanza: devuelve ``estado`` (``ok``, ``ausente``, ``invalido``, ``version``, ``sin_mercados``),
``usable`` (solo con ``ok``) y un ``motivo`` para mostrar. Sin un manifiesto usable, Estrategia **no deja agregar
pares** (bloquea con ese motivo) y no marca ningún perfil como medido; sí deja cambiar TWE, pares a la vez y quitar.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any
from typing import Dict
from typing import Optional

NOMBRE = "live_configs_manifest.json"
SCHEMA_VERSION = 1
# Más grande que esto no es un manifiesto (cientos de configs ocupan decenas de KB).
MAX_BYTES = 8 * 1024 * 1024

_NO_PUBLICO = ("passivbot todavía no publicó el manifiesto de configs (live_configs_manifest.json): hace falta la "
               "imagen de passivbot con el manifiesto. Sin él no se puede ver qué config usaría forager")


def ruta(forager_config: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(forager_config).parent / NOMBRE


def _resultado(estado: str, motivo: str, datos: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    datos = datos if isinstance(datos, dict) else None
    return {"estado": estado, "usable": estado == "ok", "motivo": motivo, "datos": datos,
            "generado": (datos or {}).get("generated_at"), "revision": (datos or {}).get("image_revision")}


def leer(forager_config: Optional[pathlib.Path]) -> Dict[str, Any]:
    if forager_config is None:
        return _resultado("ausente", _NO_PUBLICO + ".")
    path = ruta(forager_config)
    try:
        if path.stat().st_size > MAX_BYTES:
            return _resultado("invalido", f"El manifiesto de configs ({NOMBRE}) es demasiado grande: no se usa.")
        datos = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _resultado("ausente", _NO_PUBLICO + ".")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _resultado("invalido", f"El manifiesto de configs ({NOMBRE}) es inválido ({exc.__class__.__name__}): "
                                      "no se usa.")
    if not isinstance(datos, dict):
        return _resultado("invalido", f"El manifiesto de configs ({NOMBRE}) es inválido: no es un objeto.")
    version = datos.get("schema_version")
    if version != SCHEMA_VERSION:
        return _resultado("version", f"El manifiesto de configs tiene la versión {version!r} y el panel entiende la "
                                     f"{SCHEMA_VERSION}: no se usa.", datos)
    if not isinstance(datos.get("symbols"), dict) or not isinstance(datos.get("markets"), dict):
        return _resultado("invalido", f"El manifiesto de configs ({NOMBRE}) es inválido: faltan symbols o markets.",
                          datos)
    if datos["markets"].get("status") != "loaded":
        return _resultado("sin_mercados", "passivbot todavía no publicó los mercados en el manifiesto de configs "
                                          "(forager los carga en su primer ciclo, ~1 min después de arrancar).", datos)
    if datos.get("symbols_error"):
        return _resultado("invalido", "passivbot no pudo armar la lista de símbolos del manifiesto: no se usa.", datos)
    return _resultado("ok", "", datos)


def config_de(m: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    """La config que elegiría forager para ``symbol`` (None: no está en los mercados que cargó o sin manifiesto)."""
    if not m.get("usable"):
        return None
    info = (m.get("datos") or {}).get("symbols", {}).get(symbol)
    return info if isinstance(info, dict) else None


def config_propia_clock(info: Optional[Dict[str, Any]]) -> bool:
    """La regla del panel: forager elegiría ``<SYM>.json`` y es clock."""
    return bool(info) and info.get("own_file") is True and info.get("is_clock") is True
