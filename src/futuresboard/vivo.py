"""Actualización en vivo de Inicio, Posiciones y Moneda: el JSON que pide ``static/js/en_vivo.js`` cada 15 s.

* ``GET /api/vivo/inicio[?start=AAAA-MM-DD&end=AAAA-MM-DD]`` (sin fechas: la semana en curso, como ``/``)
* ``GET /api/vivo/posiciones``
* ``GET /api/vivo/moneda/<coin>``

Requieren login como el resto de ``/api`` (``auth._require_login``: sin sesión, redirect al login, que el JS detecta
y avisa). Son GET y no cambian nada: no llevan CSRF. ``Cache-Control: no-store``.

Las cuentas son las de la página (``blueprint._datos_inicio``, ``_datos_posiciones``, ``_posicion_moneda``) y lo
que cambia de forma (listas, tarjetas, filas) sale de los **mismos macros de Jinja** que usa la página
(``_sidebar.html``, ``_inicio.html``, ``_posiciones.html``, ``_moneda.html``, ``_rescate_tarjetas.html``): el JS no
formatea ni arma HTML. La forma de la respuesta:

* ``valores[k] = {"crudo", "texto", "tono"}``: crudo = lo que la página pone en ``data-value``; texto = el filtro de
  ``formato.py`` con los mismos argumentos que el template; tono = ``gana``/``pierde``/``cero``/``""``. Van a los
  elementos ``[data-vivo="k"]``.
* ``bloques[k]``: HTML del elemento ``[data-vivo-bloque="k"]`` entero.
* ``tablas[id]``: las ``<tr>`` de la DataTable ``#id`` (con ``data-fila``).
* ``graficos`` (Inicio): series crudas de los tres gráficos del período.
* ``bot`` (Inicio): lo mismo que ``/api/bot/status`` y su HTTP status.
* ``datos``: frescura de la base (latido del scraper, ``scraper.frescura``); ``fuentes`` (Inicio): de dónde salen
  el balance y el PnL no realizado (lectura del rescate si es fresca, si no la base).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import time

from flask import Blueprint
from flask import current_app
from flask import get_template_attribute
from flask import render_template
from flask import request
from flask import Response

from futuresboard import blueprint as bp
from futuresboard import formato
from futuresboard import scraper

vivo = Blueprint("vivo", __name__)
log = logging.getLogger(__name__)


def _iso(ts):
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat(timespec="seconds")


def _respuesta(payload, status=200):
    return Response(json.dumps(payload), status=status, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


def _error(status, mensaje):
    return _respuesta({"ok": False, "error": mensaje}, status)


def valor(crudo, texto, tono=""):
    """Un número para ``[data-vivo]``: el crudo como lo imprime Jinja (``str``) y el texto ya formateado."""
    return {"crudo": str(crudo), "texto": str(texto), "tono": tono}


def _o_raya(crudo):
    """Precio u objetivo de la calculadora de Moneda; sin precio de Binance (``"-"``), crudo vacío y "—" (como
    ``celda_precio`` de ``_moneda.html``)."""
    return valor("", "—") if crudo == "-" else valor(crudo, formato.num(crudo))


def _macro(plantilla, nombre, *args):
    return str(get_template_attribute(plantilla, nombre)(*args))


def _comun(pagina, coin_list, moneda_actual=None):
    """Lo de todas las páginas: menú izquierdo (monedas activas e inactivas, cuentas) y frescura de la base."""
    n = coin_list["totals"]["active"]
    f = scraper.frescura_web(current_app.config)
    return {
        "ok": True,
        "pagina": pagina,
        "generado_en": _iso(time.time()),
        "datos": {"fuente": f["fuente"], "at": _iso(f["at"]), "edad_segundos": f["edad_segundos"],
                  "intervalo_segundos": f["intervalo_segundos"]},
        "valores": {
            "n_activas": valor(n, n),
            "monedas_titulo": valor(n, f"Monedas activas ({n})"),
        },
        "bloques": {
            "monedas_activas": _macro("_sidebar.html", "activas", coin_list, moneda_actual),
            "monedas_inactivas": _macro("_sidebar.html", "inactivas", coin_list, moneda_actual),
            "nav_posiciones": _macro("_sidebar.html", "badge_posiciones", n),
        },
    }


# Tope del inspect a Docker dentro de /api/vivo/inicio: muy por debajo de los 10 s con los que en_vivo.js corta el
# pedido. Con el docker-proxy colgado, los números se actualizan igual y el estado del bot dice "Sin datos".
BOT_DOCKER_TIMEOUT_S = 3


def _bot():
    """Estado del bot como ``/api/bot/status``; un error de Docker no corta la actualización de los números."""
    try:
        datos, http = bp.bot_status_payload(docker_timeout=BOT_DOCKER_TIMEOUT_S)
    except Exception:  # defensivo: el resto del JSON sale igual
        log.exception("No se pudo leer el estado del bot para /api/vivo/inicio")
        return {"ok": False, "http": 500, "datos": {"error": "No se pudo leer el estado del bot."}}
    return {"ok": http == 200, "http": http, "datos": datos}


@vivo.route("/api/vivo/inicio", methods=["GET"])
def inicio():
    start, end = request.args.get("start"), request.args.get("end")
    if start is None and end is None:
        rangos = bp.timeranges()
        start, end = rangos[2][0], rangos[2][1]
    elif start is None or end is None:
        return _error(400, "Mandar start y end (AAAA-MM-DD), o ninguno para la semana en curso.")
    try:
        dt.datetime.fromisoformat(start)
        dt.datetime.fromisoformat(end)
    except ValueError:
        return _error(400, "start y end tienen que ser fechas AAAA-MM-DD.")
    try:
        ctx = bp._datos_inicio(start, end)
    except sqlite3.Error:
        log.exception("No se pudo leer la base para /api/vivo/inicio")
        return _error(503, "No se pudo leer la base de datos.")

    t, data, r = ctx["totals"], ctx["data"], ctx["rescate_resumen"]
    by_date, by_symbol, total_by_date = data
    out = _comun("inicio", ctx["coin_list"])
    balance, upnl = t[7][1], t[7][0]
    v = out["valores"]
    v["balance"] = valor(balance, formato.usd(balance))
    v["upnl"] = valor(upnl, formato.usd(upnl, signo=True))
    v["fuente"] = valor(ctx["fuente_texto"], ctx["fuente_texto"])
    # En / sin fechas el período es la semana en curso: el lunes cambia y el subtítulo tiene que acompañar.
    v["periodo"] = valor(f"{ctx['startdate']}/{ctx['enddate']}", f"del {ctx['startdate']} al {ctx['enddate']}")
    v["mes_nombre"] = valor(t[8], formato.mes_es(t[8]))
    v["monedas_movimiento"] = valor(t[10], f"{t[10]} {'moneda' if t[10] == 1 else 'monedas'} con movimiento")
    b = out["bloques"]
    for clave, i, j in (("hoy", 1, 0), ("semana", 2, 1), ("mes", 3, 2), ("total", 0, 3)):
        v[clave] = valor(t[i], formato.usd(t[i], signo=True))
        # El % del balance: el color va por el signo del monto (como en el template), no del %.
        v[clave + "_pct"] = valor(t[6][j], formato.pct(t[6][j], signo=True), formato.tendencia(t[i]))
        b[clave + "_pct"] = _macro("_inicio.html", "kpi_var", clave + "_pct", t[i], t[6][j])
    b["upnl_pct"] = _macro("_inicio.html", "upnl_var", t[7])
    b["ultimo_pedido"] = _macro("_inicio.html", "ultimo_pedido", ctx["lastupdate"])
    b["franja_rescate"] = _macro("_inicio.html", "franja_rescate", r, current_app.config.get("BINANCE_TESTNET"))
    b["pie_periodo"] = _macro("_inicio.html", "pie_periodo", t)
    b["pie_balance"] = _macro("_inicio.html", "pie_balance", data)
    b["tabla_diario"] = _macro("_inicio.html", "tabla_diario", data)
    b["tabla_monedas"] = _macro("_inicio.html", "tabla_monedas", data)
    b["nav_rescate"] = _macro("_sidebar.html", "punto_rescate", r)
    if r:
        b["rescate"] = render_template("_rescate_tarjetas.html", rescate_resumen=r)
    out["graficos"] = {
        "diario": {"dias": by_date[1], "valores": by_date[0]},
        "balance": {"dias": total_by_date[1], "valores": total_by_date[0]},
        "por_moneda": {"monedas": by_symbol[0], "valores": by_symbol[1], "alto": ctx["alto_por_moneda"]},
    }
    out["periodo"] = {"start": ctx["startdate"], "end": ctx["enddate"]}
    out["fuentes"] = {k: {**f, "at": _iso(f["at"])} for k, f in ctx["fuentes"].items()}
    out["bot"] = _bot()
    # Franja y control rápido del bot (spec bot-estrategia § 2.2): el mismo macro que la página.
    datos_bot = out["bot"]["datos"]
    b["franja_bot"] = _macro("_bot.html", "franja_estado", datos_bot)
    b["control_bot"] = _macro("_bot.html", "control", bp.contexto_control(datos_bot))
    return _respuesta(out)


@vivo.route("/api/vivo/posiciones", methods=["GET"])
def posiciones():
    try:
        datos = bp._datos_posiciones()
        coins = bp.get_coins()
    except sqlite3.Error:
        log.exception("No se pudo leer la base para /api/vivo/posiciones")
        return _error(503, "No se pudo leer la base de datos.")
    out = _comun("posiciones", coins)
    out["tablas"] = {"orders": _macro("_posiciones.html", "filas", datos["positions"], datos["markprices"])}
    out["bloques"]["aviso_precio"] = _macro("_posiciones.html", "aviso_precio", datos["positions"], datos["markprices"])
    return _respuesta(out)


@vivo.route("/api/vivo/moneda/<coin>", methods=["GET"])
def moneda(coin):
    try:
        coins = bp.get_coins()
        # Antes de cualquier pedido a Binance: la moneda va pegada en la URL de premiumIndex.
        if coin not in coins["inactive"] and coin not in coins["active"]:
            return _error(404, "Moneda inexistente.")
        pm = bp._posicion_moneda(coin)
        lastupdate = bp.get_lastupdate()
    except sqlite3.Error:
        log.exception("No se pudo leer la base para /api/vivo/moneda")
        return _error(503, "No se pudo leer la base de datos.")
    out = _comun("moneda", coins, coin)
    v = out["valores"]
    upnl = bp.format_dp(pm["unrealized"])
    v["upnl_moneda"] = valor(upnl, formato.usd(upnl, signo=True), formato.tendencia(upnl))
    precio = pm["markprice"]
    v["precio"] = _o_raya(precio)
    for k in range(3):
        v[f"objetivo_{k}"] = _o_raya(pm["target"][k])
    # Las filas "Posición" de la calculadora (una, o dos en hedge). Si la posición se cerró, la primera va en 0 (es la
    # que usa la cuenta del promedio): sin esto los campos se quedaban con la posición vieja.
    posiciones = pm["allpositions"]
    for k in range(max(len(posiciones), 1)):
        cantidad, entrada = (posiciones[k][6], posiciones[k][4]) if k < len(posiciones) else (0, 0)
        v[f"pos_cantidad_{k}"] = valor(cantidad, cantidad)
        v[f"pos_precio_{k}"] = valor(entrada, entrada)
    out["bloques"]["bajada_moneda"] = _macro("_moneda.html", "bajada", lastupdate, precio)
    out["bloques"]["precio_tag"] = _macro("_moneda.html", "precio_tag", precio)
    out["tablas"] = {"orders": _macro("_moneda.html", "filas_ordenes", pm["allpositions"], pm["allorders"])}
    return _respuesta(out)
