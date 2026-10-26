"""Historial de acciones del panel para Bot (diseno/bot-estrategia/spec.md § 2.3): ``bot_actions.log`` en palabras.

Cada renglón del log (``bot_control.Store.audit``) trae ``ts``, ``user``, ``action``, ``params``, ``result``,
``detail`` y ``remote_addr``. Acá se arma una vista **sin** ``remote_addr`` y sin ``params`` crudos (solo un título en
palabras): lo que sale de ``historial`` va al navegador. Un ``requiere_confirmacion`` cuenta como "plan mostrado" si
le sigue (en ``VENTANA_CONFIRMACION_S``) un ``ok`` de la misma acción con los mismos parámetros; si no, "sin
confirmar". Los renglones ilegibles se saltean.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

LIMITE_DEFECTO = 12
LIMITE_MAX = 100
FILTROS = ("todas", "aplicadas", "rechazadas")
VENTANA_CONFIRMACION_S = 15 * 60
_MAX_DETALLE = 200
# Claves que agrega la confirmación: no cambian qué acción es.
_CLAVES_CONFIRMACION = ("confirmar", "plan_hash", "confirmar_twe_alta", "confirmar_inyeccion", "confirmar_corte")

ESTADOS = {
    "aplicado": "Aplicado",
    "rechazado": "Rechazado",
    "error": "Falló",
    "plan_mostrado": "Plan mostrado",
    "sin_confirmar": "Sin confirmar",
    "interrumpido": "Interrumpido",
}
_POR_RESULTADO = {"ok": "aplicado", "rechazado": "rechazado", "error": "error", "interrumpido": "interrumpido"}
_RECHAZADAS = ("rechazado", "error", "sin_confirmar", "interrumpido")

_NOMBRES_PERFIL = {"conservador": "Conservador", "con_rescate": "Con rescate"}
_TITULOS_FIJOS = {
    "perfil": "Perfil",
    "rescate_config": "Rescate: configuración",
    "rescate_reconocer_tope": "Rescate: reconocer el tope",
    "c3_config": "C3: configuración",
}


def _num(value: Any) -> str:
    if isinstance(value, bool):
        return f"({str(value).lower()})"  # un twe_long rechazado por no ser número
    if not isinstance(value, (int, float)):
        return str(value)
    texto = f"{value:g}" if isinstance(value, float) else str(value)
    return texto.replace(".", ",")


def titulo(action: Any, params: Any) -> str:
    """La acción en palabras (sin parámetros crudos)."""
    p = params if isinstance(params, dict) else {}
    if action == "start":
        if p.get("origen") == "c3_reinicio":
            return "Reinicio automático de C3"
        if p.get("reanudar") is True:
            return {"reanudar": "Reanudar", "reiniciar": "Reiniciar"}.get(p.get("como"), "Iniciar")
        if "twe_long" in p:
            return f"TWE manual {_num(p.get('twe_long'))}"
        if p.get("riesgo"):
            return f"Iniciar con riesgo {p.get('riesgo')}"
        return "Iniciar"
    if action == "stop":
        if p.get("modo") == "apagar":
            return {"c3_corte": "Apagar por C3", "rescate_tope": "Apagar por el tope del rescate"}.get(
                p.get("origen"), "Apagar")
        if p.get("modo") == "graceful":
            return "Parada gradual"
        return "Detener"
    if action == "perfil":
        nombre = _NOMBRES_PERFIL.get(p.get("perfil"))
        return f"Perfil {nombre}" if nombre else "Perfil"
    return _TITULOS_FIJOS.get(action, str(action))


def _resumen_apagar(detalle: str) -> str:
    """"stop | resumen: {...}" -> "stop · 3 órdenes canceladas · 2 posiciones cerradas"."""
    cabeza, _, resto = detalle.partition(" | resumen: ")
    try:
        r = json.loads(resto)
    except ValueError:
        return cabeza
    if not isinstance(r, dict):
        return cabeza
    ordenes = sum(int(o.get("cantidad") or 0) for o in r.get("ordenes_canceladas") or [] if isinstance(o, dict))
    partes = [cabeza] if cabeza else []
    partes.append(f"{ordenes} {'orden cancelada' if ordenes == 1 else 'órdenes canceladas'}")
    cerradas = len(r.get("posiciones_cerradas") or [])
    partes.append(f"{cerradas} {'posición cerrada' if cerradas == 1 else 'posiciones cerradas'}")
    if r.get("errores"):
        partes.append(f"{len(r['errores'])} con error")
    if r.get("completo") is False:
        partes.append("cierre parcial")
    return " · ".join(partes)


def _detalle(entry: Dict[str, Any]) -> str:
    detalle = entry.get("detail")
    texto = detalle if isinstance(detalle, str) else ""
    if " | resumen: " in texto:
        texto = _resumen_apagar(texto)
    texto = " ".join(texto.split())
    return texto if len(texto) <= _MAX_DETALLE else texto[:_MAX_DETALLE - 1] + "…"


def _ts(value: Any) -> Optional[float]:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _params_base(params: Any) -> Dict[str, Any]:
    p = params if isinstance(params, dict) else {}
    return {k: v for k, v in p.items() if k not in _CLAVES_CONFIRMACION}


def leer(path: pathlib.Path) -> List[Dict[str, Any]]:
    """Renglones válidos del log, en orden (los ilegibles se saltean)."""
    try:
        lineas = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for linea in lineas:
        try:
            entry = json.loads(linea)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("ts"), str) and isinstance(entry.get("action"), str):
            out.append(entry)
    return out


def _vistas(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    vistas = []
    for i, e in enumerate(entries):
        resultado = str(e.get("result") or "")
        estado = _POR_RESULTADO.get(resultado, "rechazado")
        detalle = _detalle(e)
        if resultado == "requiere_confirmacion":
            estado, detalle = "sin_confirmar", "Pidió confirmación; no se confirmó."
            t0, base = _ts(e.get("ts")), _params_base(e.get("params"))
            for siguiente in entries[i + 1:]:
                if siguiente.get("action") != e.get("action") or _params_base(siguiente.get("params")) != base:
                    continue
                t1 = _ts(siguiente.get("ts"))
                if t0 is not None and t1 is not None and t1 - t0 > VENTANA_CONFIRMACION_S:
                    break
                if siguiente.get("result") == "ok":
                    estado = "plan_mostrado"
                    detalle = f"Pidió confirmación; confirmado a las {str(siguiente.get('ts'))[11:19]}."
                break
        vistas.append({
            "ts": str(e.get("ts")),
            "usuario": str(e.get("user") or "?"),
            "accion": str(e.get("action")),
            "titulo": titulo(e.get("action"), e.get("params")),
            "detalle": detalle,
            "resultado": resultado,
            "estado": estado,
            "estado_texto": ESTADOS[estado],
        })
    return vistas


def historial(path: pathlib.Path, desde: int = 0, limite: int = LIMITE_DEFECTO, filtro: str = "todas") -> Dict[str, Any]:
    """Página del historial, de la más nueva a la más vieja. ``desde``: cuántas saltear (las más nuevas primero)."""
    if filtro not in FILTROS:
        raise ValueError(f"filtro inválido: usar {', '.join(FILTROS)}.")
    if desde < 0:
        raise ValueError("desde tiene que ser 0 o más.")
    if not 1 <= limite <= LIMITE_MAX:
        raise ValueError(f"limite tiene que estar entre 1 y {LIMITE_MAX}.")
    vistas = list(reversed(_vistas(leer(path))))
    if filtro == "aplicadas":
        vistas = [v for v in vistas if v["estado"] == "aplicado"]
    elif filtro == "rechazadas":
        vistas = [v for v in vistas if v["estado"] in _RECHAZADAS]
    pagina = vistas[desde:desde + limite]
    siguiente = desde + limite if desde + limite < len(vistas) else None
    return {"acciones": pagina, "total": len(vistas), "desde": desde, "limite": limite, "siguiente": siguiente,
            "filtro": filtro}
