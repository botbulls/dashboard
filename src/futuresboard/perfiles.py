"""Perfiles de riesgo del panel: las dos opciones medidas en el backtest multianual 2021-2026.

Un perfil es, en UNA acción del panel (job con el mismo lock de acciones, ver ``futuresboard.jobs``):

* la TWE long de forager (``twe_long``; ``twe_short`` = ``bot_control.TWE_SHORT_SOLO_LONG`` con ``n_shorts`` 0),
  escrita con la rutina de START (backup, escritura atómica, start o restart y espera a forager);
* la config del rescate, el disparo por uPnL y el retiro periódico (``rescate_config.json``, ``rescate.save_config``).

Perfiles (ver ``docs/perfiles.md``):

* ``conservador``: TWE 3 (lw 0,75 con 4 pares), sin rescate (deshabilitado, disparo por uPnL off, retiro off).
* ``con_rescate``: TWE 4 (lw 1,0) + rescate colchón (reserva 10.000, paso 1.000, umbral 8, devolución 32, tope 60 %)
  + disparo por uPnL (X 70 %, paso 1.000) + retiro periódico de ganancias de futuros a spot (base 10.000).

Orden y vuelta atrás: se valida todo antes de escribir nada (contenedor, modos de forager, ``n_shorts``, tope sin
reconocer, cuerpo del rescate). Después se guarda el rescate (con copia del archivo) y se ejecuta el START con la TWE
del perfil. Si el START falla y la config de forager quedó como estaba (error de Docker con respuesta, contenedor
inexistente, error de escritura), se restaura también el rescate. Con un resultado incierto de Docker (sin respuesta)
quedan las dos escritas, igual que en ``bot_control._apply_and_restart``.

Los números son de backtest (OOS, ventanas de 12 meses desde 2023-11-10): no son una promesa. Herramienta, no asesor.
"""
from __future__ import annotations

import pathlib
from decimal import Decimal
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from futuresboard import bot_control
from futuresboard import c3
from futuresboard import rescate

CONSERVADOR = "conservador"
CON_RESCATE = "con_rescate"
PARES_MEDIDOS = 4  # BNB, BTC, ETH, XRP: lw = TWE / 4

PERFILES: Dict[str, Dict[str, Any]] = {
    CONSERVADOR: {
        "nombre": "Conservador",
        "twe_long": Decimal("3"),
        "rescate": {"habilitado": False, "upnl_habilitado": False, "retiro_frecuencia": rescate.FRECUENCIA_OFF},
        "resumen": "TWE 3 (lw 0,75 con 4 pares), sin rescate: rescate deshabilitado, disparo por uPnL y retiro off.",
        "riesgo": ("Arriesga los 10.000 de futuros y nada más: la reserva en spot no se presta. En las ventanas OOS la "
                   "cuenta de futuros llegó a bajar 71 % (3.369 de 10.000) sin liquidarse."),
        "numeros": {"mediana_pct": "59,5", "media_pct": "41,9", "peor_pct": "-9,4", "capital_en_riesgo": "10.000",
                    "peor_dd_pct": "45", "dd_sobre": "20.000 (10.000 de futuros + 10.000 quietos)"},
    },
    CON_RESCATE: {
        "nombre": "Con rescate",
        "twe_long": Decimal("4"),
        "rescate": {"habilitado": True, "reserva_max_usdt": 10000, "paso_usdt": 1000, "umbral_ratio": 8,
                    "umbral_devolucion_ratio": 32, "tope_perdida_pct": 60, "upnl_habilitado": True,
                    "upnl_x_pct": 70, "upnl_paso_usdt": 1000,
                    "retiro_frecuencia": rescate.FRECUENCIA_RECOMENDADA, "retiro_base_usdt": 10000},
        "resumen": ("TWE 4 (lw 1,0 con 4 pares) + rescate colchón (reserva 10.000, paso 1.000, inyecta con equity/MM < 8, "
                    "devuelve desde 32, tope de pérdida total 60 %) + disparo por uPnL (X 70 %, paso 1.000) + retiro "
                    "periódico de ganancias de futuros a spot (base 10.000)."),
        "riesgo": ("Arriesga 20.000: los 10.000 de futuros y la reserva de 10.000, que se presta cerca de la liquidación. "
                   "En may-21 la cuenta de futuros llegó a 401 USDT (-96 %) y la salvó la reserva. Los números suponen "
                   "RETIRAR la ganancia: sin retiro, con mechas 1,5 % más profundas, el camino continuo se liquida en "
                   "feb-26 y pierde también la reserva."),
        "numeros": {"mediana_pct": "86,3", "media_pct": "60", "peor_pct": "-12", "capital_en_riesgo": "20.000",
                    "peor_dd_pct": "63-65", "dd_sobre": "20.000 (10.000 de futuros + 10.000 de reserva)",
                    "planificar": "~60 %/año sobre 10.000 (~30 % sobre 20.000), como techo"},
    },
}
FUENTE = ("Backtest multianual 2021-2026 (botbulls-lab multianual; passivbot v6.1 clock, BNB/BTC/ETH/XRP, "
          "apalancamiento 10), OOS: ventanas de 12 meses desde 2023-11-10, sobre los 10.000 de futuros. Es un backtest, "
          "no una promesa: herramienta, no asesor.")

# Pasos del job: validar, rescate y después los del START (bot_control.START_STEPS sin validar).
PERFIL_STEPS: List[tuple] = ([("validar", "Validar perfil, forager y rescate"),
                              ("rescate", "Guardar rescate, disparo por uPnL y retiro")]
                             + [p for p in bot_control.START_STEPS if p[0] != "validar"])


class PerfilError(bot_control.BotControlError):
    status_code = 409


def validate_body(body: Any) -> Dict[str, Any]:
    """``{"perfil": "conservador" | "con_rescate", "confirmar": bool (opcional)}``. Estricto: ValueError si no."""
    if not isinstance(body, dict):
        raise ValueError("Body inválido: se esperaba un objeto JSON.")
    desconocidos = sorted(set(body) - {"perfil", "confirmar"})
    if desconocidos:
        raise ValueError(f"Campos desconocidos: {', '.join(desconocidos)}.")
    perfil = body.get("perfil")
    if not isinstance(perfil, str) or perfil not in PERFILES:
        raise ValueError(f"perfil inválido: usar {' o '.join(PERFILES)}.")
    confirmar = body.get("confirmar", False)
    if not isinstance(confirmar, bool):
        raise ValueError("confirmar tiene que ser true o false.")
    return {"perfil": perfil, "confirmar": confirmar}


def cuerpo_rescate(nombre: str, actual: Dict[str, Any]) -> Dict[str, Any]:
    """Body de ``rescate.validate_config_body`` para el perfil. Conservador deshabilita conservando los valores
    guardados (reserva, umbrales, X, base del retiro): volver a habilitar a mano no pierde la config."""
    perfil = PERFILES[nombre]["rescate"]
    if nombre == CON_RESCATE:
        return dict(perfil)
    body: Dict[str, Any] = {}
    for name in rescate.CAMPOS_NUMERICOS + rescate.CAMPOS_UPNL_NUMERICOS + ("retiro_base_usdt",):
        value = actual.get(name)
        body[name] = None if value is None else float(rescate.dec(value))
    body.update(perfil)
    return body


def detectar(forager_cfg: Optional[Dict[str, Any]], rescate_cfg: Dict[str, Any]) -> Optional[str]:
    """Perfil vigente según los valores (no según el último aplicado): None si no coincide con ninguno.

    No mira ``twe_short`` (con ``n_shorts`` 0 forager no lo usa). "Con rescate" acepta cualquier frecuencia de retiro
    que no sea off (es configurable); el resto tiene que ser exactamente el del perfil."""
    if not isinstance(forager_cfg, dict):
        return None
    if bot_control.n_pares(forager_cfg)[1] != 0:
        return None
    twe = forager_cfg.get("twe_long")
    for nombre, perfil in PERFILES.items():
        if not bot_control._num_eq(twe, perfil["twe_long"]):
            continue
        esperado = perfil["rescate"]
        if nombre == CONSERVADOR:
            if (not rescate_cfg.get("habilitado") and not rescate_cfg.get("upnl_habilitado")
                    and rescate_cfg.get("retiro_frecuencia") == rescate.FRECUENCIA_OFF):
                return nombre
            continue
        ok = bool(rescate_cfg.get("habilitado")) and bool(rescate_cfg.get("upnl_habilitado"))
        ok = ok and rescate_cfg.get("retiro_frecuencia") not in (None, rescate.FRECUENCIA_OFF)
        for name in rescate.CAMPOS_NUMERICOS + rescate.CAMPOS_UPNL_NUMERICOS + ("retiro_base_usdt",):
            try:
                ok = ok and rescate_cfg.get(name) is not None and rescate.dec(rescate_cfg[name]) == Decimal(
                    str(esperado[name]))
            except ValueError:
                ok = False
        if ok:
            return nombre
    return None


def catalogo() -> Dict[str, Any]:
    """Definiciones para el panel (texto y números)."""
    out = {}
    for nombre, p in PERFILES.items():
        out[nombre] = {"nombre": p["nombre"], "twe_long": bot_control.twe_valor(p["twe_long"]), "resumen": p["resumen"],
                       "riesgo": p["riesgo"], "numeros": dict(p["numeros"]),
                       "rescate": {k: v for k, v in p["rescate"].items()}}
    return {"perfiles": out, "fuente": FUENTE}


# --------------------------------------------------------------------------------------
# Plan (texto de la confirmación)
# --------------------------------------------------------------------------------------


def _modos_servicio(data_dir: pathlib.Path) -> Dict[str, Any]:
    status = rescate.panel_status(data_dir)
    servicio = status["servicio"]
    return {"modo": servicio.get("modo"), "vivo": bool(servicio.get("vivo")),
            "upnl_modo": (status.get("upnl") or {}).get("modo"), "retiro_modo": (status.get("retiro") or {}).get("modo"),
            "prestamo": status.get("prestamo"), "prestamo_modo": status.get("prestamo_modo")}


def plan(settings: bot_control.Settings, data_dir: pathlib.Path, nombre: str,
         docker: Optional[bot_control.DockerClient] = None) -> Dict[str, Any]:
    """Qué va a pasar si se aplica el perfil (para pedir la confirmación). Lee forager, Docker y los archivos del
    rescate y de C3; no escribe nada. Errores que impiden aplicarlo: ``BotControlError`` (409/502/503) o ValueError."""
    perfil = PERFILES[nombre]
    settings.require_enabled()
    path = settings.require_config()
    docker = docker or bot_control.DockerClient(settings.docker_url)
    info = bot_control._inspect_existing(docker, settings.container)
    forager = bot_control.read_forager_config(path)
    _validar_forager(forager)
    bloqueo = rescate.start_bloqueado(data_dir)
    if bloqueo:
        raise PerfilError(bloqueo)
    actual, cfg_err = rescate.load_config(data_dir)
    parsed = rescate.validate_config_body(cuerpo_rescate(nombre, actual))
    lineas = _lineas_plan(nombre, perfil, forager, info, data_dir, parsed, cfg_err)
    return {"perfil": nombre, "nombre": perfil["nombre"], "texto": "\n".join(lineas), "lineas": lineas,
            "corriendo": bool(info.get("running"))}


def _validar_forager(forager: Dict[str, Any]) -> None:
    bot_control._mode(forager, "short")  # InvalidModeError (409): con ese short_mode forager no arranca
    n_longs, n_shorts = bot_control.n_pares(forager)
    if n_shorts != 0:
        raise PerfilError(f"Los perfiles se midieron solo con longs (n_shorts 0) y la config de forager tiene "
                          f"n_shorts={forager.get('n_shorts')!r}: no se aplica (usá los presets o la TWE manual).")
    if not n_longs:
        raise PerfilError(f"La config de forager tiene n_longs={forager.get('n_longs')!r}: no se puede calcular la lw.")


def _fmt(value: Any) -> str:
    texto = format(Decimal(str(value)).normalize(), "f") if value is not None else "-"
    return texto.replace(".", ",")


def _lineas_plan(nombre: str, perfil: Dict[str, Any], forager: Dict[str, Any], info: Dict[str, Any],
                 data_dir: pathlib.Path, parsed: Dict[str, Any], cfg_err: Optional[str]) -> List[str]:
    twe = perfil["twe_long"]
    n_longs, _ = bot_control.n_pares(forager)
    lw = (twe / n_longs).quantize(Decimal("0.0001"))
    num = perfil["numeros"]
    lineas = [f"Perfil {perfil['nombre']}: {perfil['resumen']}"]
    lineas.append(f"Forager: twe_long {forager.get('twe_long')} -> {bot_control.twe_valor(twe)} (lw {_fmt(lw)} con "
                  f"{n_longs} pares), twe_short {bot_control.TWE_SHORT_SOLO_LONG} (sin efecto con n_shorts 0).")
    if n_longs != PARES_MEDIDOS:
        lineas.append(f"ATENCIÓN: los números se midieron con {PARES_MEDIDOS} pares (lw {_fmt(twe / PARES_MEDIDOS)}); con "
                      f"{n_longs} pares la exposición total es la misma pero por par cambia: no aplican tal cual.")
    warmup = bot_control.forager_warmup_seconds()
    if info.get("running"):
        lineas.append(f"passivbot está OPERANDO: se reinicia (docker restart) y forager tarda ~{warmup} s en volver a "
                      "abrir los pares. Las posiciones y órdenes abiertas siguen en Binance.")
    else:
        lineas.append(f"passivbot está DETENIDO ({info.get('status')}): se inicia (docker start) y forager tarda "
                      f"~{warmup} s en abrir los pares.")
    svc = _modos_servicio(data_dir)
    if cfg_err:
        lineas.append(f"La config actual del rescate es inválida ({cfg_err}): el perfil la reemplaza.")
    if nombre == CONSERVADOR:
        lineas.append("Rescate: se DESHABILITA (se conservan reserva, umbrales y tope guardados). Disparo por uPnL: off. "
                      "Retiro periódico: off.")
        try:
            prestamo = rescate.dec(svc.get("prestamo") or "0")
        except ValueError:
            prestamo = Decimal(0)
        if prestamo > 0:
            lineas.append(f"Hay un préstamo pendiente de {rescate.money(prestamo)} USDT ({svc.get('prestamo_modo')}): con el "
                          "rescate deshabilitado se sigue devolviendo, y si es real el tope de pérdida sigue armado hasta "
                          "devolverlo.")
    else:
        retiro = parsed["retiro"]
        lineas.append("Rescate: habilitado, reserva 10.000, paso 1.000, inyecta con equity/MM < 8, devuelve desde 32, "
                      "tope de pérdida total 60 %. Si estaba deshabilitado, habilitarlo fija una base nueva con el "
                      "capital total (futuros + spot) del próximo ciclo del servicio.")
        lineas.append("Disparo por uPnL: X 70 %, paso 1.000.")
        lineas.append(f"Retiro periódico: {retiro['retiro_frecuencia']}, de lo que exceda "
                      f"{rescate.money(retiro['retiro_base_usdt'])} USDT en futuros (wallet realizada), a spot. El primer "
                      "retiro es al comienzo del próximo período.")
        lineas.extend(_avisos_servicio(svc))
        aviso_u = rescate.inyeccion_al_guardar(data_dir, parsed)
        if aviso_u:
            lineas.append("ATENCIÓN: " + aviso_u.replace(" ¿Guardar igual?", ""))
    c3_status = c3.panel_status(data_dir)
    if c3_status["config"].get("habilitado") or c3_status["c3_parado"]:
        lineas.append(f"C3 (stop con reinicio) está {'PARADO por un corte' if c3_status['c3_parado'] else 'habilitado'}: "
                      "el perfil no lo cambia. El START del perfil es un START a mano para C3.")
    lineas.append(f"Backtest 2021-2026 (no es una promesa), OOS en ventanas de 12 meses desde 2023-11-10, sobre los "
                  f"10.000 de futuros: mediana {num['mediana_pct']} %, media {num['media_pct']} %, peor "
                  f"{num['peor_pct']} %; peor drawdown {num['peor_dd_pct']} % sobre {num['dd_sobre']}; capital en "
                  f"riesgo {num['capital_en_riesgo']}.")
    lineas.append(perfil["riesgo"])
    lineas.append("Herramienta, no asesor: configurar el perfil es una decisión de quien opera la cuenta.")
    return lineas


def _avisos_servicio(svc: Dict[str, Any]) -> List[str]:
    """Con el servicio rescate (o sus modos) apagado, "Con rescate" no protege: se dice en la confirmación."""
    avisos = []
    modo = svc.get("modo")
    if modo not in (rescate.MODE_DRY_RUN, rescate.MODE_REAL) or not svc.get("vivo"):
        avisos.append(f"ATENCIÓN: el servicio rescate {'no late' if modo else 'no tiene latido'}"
                      f"{' (modo ' + modo + ')' if modo else ''}: con el servicio en off o caído el perfil NO PROTEGE "
                      "NADA (ni rescate, ni tope, ni retiro) hasta levantarlo con FUTURESBOARD_RESCATE_MODE.")
        if modo == rescate.MODE_OFF and svc.get("vivo"):
            avisos[-1] = ("ATENCIÓN: el servicio rescate está en off (FUTURESBOARD_RESCATE_MODE=off): el perfil NO "
                          "PROTEGE NADA (ni rescate, ni tope, ni retiro) hasta pasarlo a dry_run o real.")
        return avisos
    if modo == rescate.MODE_DRY_RUN:
        avisos.append("El servicio rescate está en dry_run: el rescate, el disparo por uPnL y el retiro son SIMULADOS "
                      "(no se transfiere nada).")
    upnl = rescate.upnl_estado(modo, svc.get("upnl_modo"))
    if upnl == rescate.UPNL_OFF:
        avisos.append("El disparo por uPnL no se evalúa: FUTURESBOARD_RESCATE_UPNL_MODE=off en el servicio.")
    elif upnl == rescate.UPNL_REGISTRA:
        avisos.append("El disparo por uPnL solo registra (FUTURESBOARD_RESCATE_UPNL_MODE=dry_run): no presta.")
    retiro = rescate.retiro_estado(modo, svc.get("retiro_modo"))
    if retiro == rescate.RETIRO_OFF:
        avisos.append("El retiro periódico no se ejecuta: FUTURESBOARD_RESCATE_RETIRO_MODE=off en el servicio. Los "
                      "números de TWE 4 suponen retirar la ganancia.")
    elif retiro == rescate.RETIRO_SIMULADO and modo == rescate.MODE_REAL:
        avisos.append("El retiro periódico es SIMULADO (FUTURESBOARD_RESCATE_RETIRO_MODE=dry_run): no transfiere.")
    return avisos


# --------------------------------------------------------------------------------------
# Aplicar (corre en el job, con el lock de acciones del panel)
# --------------------------------------------------------------------------------------


class _SinValidar(bot_control.Reporter):
    """El START reporta su propio paso "validar": el del perfil ya terminó, no se vuelve a abrir."""

    def __init__(self, rep: bot_control.Reporter) -> None:
        self.rep = rep

    def step(self, clave: str, estado: str, detalle: Optional[str] = None,
             actual: Optional[int] = None, total: Optional[int] = None) -> None:
        if clave != "validar":
            self.rep.step(clave, estado, detalle, actual, total)


def aplicar(settings: bot_control.Settings, store: bot_control.Store, nombre: str, usuario: str,
            docker: Optional[bot_control.DockerClient] = None,
            reporter: Optional[bot_control.Reporter] = None) -> Dict[str, Any]:
    """Aplica el perfil (ver docstring del módulo). Devuelve el resultado del START más ``perfil`` y ``rescate``."""
    if nombre not in PERFILES:
        raise ValueError(f"perfil inválido: usar {' o '.join(PERFILES)}.")
    perfil = PERFILES[nombre]
    rep = reporter or bot_control.NULL_REPORTER
    data_dir = store.data_dir
    rep.step("validar", bot_control.PASO_EN_CURSO)
    settings.require_enabled()
    path = settings.require_config()
    docker = docker or bot_control.DockerClient(settings.docker_url)
    info = bot_control._inspect_existing(docker, settings.container)
    forager = bot_control.read_forager_config(path)
    _validar_forager(forager)
    bloqueo = rescate.start_bloqueado(data_dir)
    if bloqueo:
        raise PerfilError(bloqueo)
    actual, _cfg_err = rescate.load_config(data_dir)
    parsed = rescate.validate_config_body(cuerpo_rescate(nombre, actual))
    rep.step("validar", bot_control.PASO_OK, f"{perfil['nombre']}: TWE {bot_control.twe_valor(perfil['twe_long'])}; "
                                            f"contenedor {settings.container}: {info.get('status')}")

    rep.step("rescate", bot_control.PASO_EN_CURSO)
    cruda = rescate.leer_config_cruda(data_dir)
    marca = {"nombre": nombre, "aplicado_en": rescate.now_iso(), "aplicado_por": usuario}
    try:
        cfg = rescate.save_config(data_dir, parsed, usuario, perfil=marca)
    except rescate.ConflictError as exc:
        raise PerfilError(str(exc)) from exc
    resumen_rescate = {"habilitado": cfg.get("habilitado"), "generacion": cfg.get("generacion"),
                       "upnl_habilitado": cfg.get("upnl_habilitado"), "retiro_frecuencia": cfg.get("retiro_frecuencia"),
                       "retiro_base_usdt": cfg.get("retiro_base_usdt")}
    rep.step("rescate", bot_control.PASO_OK,
             (f"rescate {'habilitado' if cfg.get('habilitado') else 'deshabilitado'}, disparo por uPnL "
              f"{'on' if cfg.get('upnl_habilitado') else 'off'}, retiro {cfg.get('retiro_frecuencia')}"))
    try:
        out = bot_control.start_bot(settings, store, None, docker=docker, reporter=_SinValidar(rep),
                                    twe_long=perfil["twe_long"])
    except bot_control.BotControlError as exc:
        if isinstance(exc, bot_control.DockerError) and exc.uncertain:
            nota = " La config del rescate del perfil quedó guardada, igual que la de forager."
        else:
            try:
                rescate.restaurar_config(data_dir, cruda)
                nota = " Se restauró también la config del rescate: no se aplicó el perfil."
            except (OSError, ValueError) as restore_exc:
                nota = (f" ATENCIÓN: no se pudo restaurar la config del rescate ({restore_exc.__class__.__name__}): "
                        "revisala en la sección Rescate.")
        if isinstance(exc, bot_control.DockerError):
            err: bot_control.BotControlError = bot_control.DockerError(f"{exc}{nota}", exc.payload)
            err.uncertain = exc.uncertain
        else:
            err = bot_control.BotControlError(f"{exc}{nota}", exc.payload)
        err.status_code = exc.status_code
        raise err from exc
    out["perfil"] = nombre
    out["rescate"] = resumen_rescate
    return out
