"""Notificaciones a Telegram (Bot API, sendMessage con parse_mode HTML).

Config por env:
- TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: si falta alguno, todo queda deshabilitado (no se envía nada).
- TELEGRAM_PREFIX: texto opcional al inicio de cada mensaje (ej. "[DEV client17]").

Reglas:
- Enviar nunca lanza: un fallo de Telegram se loguea y se devuelve False.
- El token nunca se loguea: los errores de red se reportan solo con el tipo de excepción (el mensaje
  de requests incluye la URL, que contiene el token) y la respuesta de la API solo con status/description.
- Usa ``logging`` y no ``current_app``: ``send_async`` corre en otro hilo, fuera del contexto de Flask.
"""
from __future__ import annotations

import html
import logging
import os
import threading
from typing import Any
from typing import Callable
from typing import Dict
from typing import Optional

import requests

log = logging.getLogger(__name__)

ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_PREFIX = "TELEGRAM_PREFIX"

API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 10
MAX_MESSAGE_LEN = 4096
_MAX_ITEMS = 10

# urllib3 a nivel DEBUG loguea el path del request, que en la Bot API incluye el token.
logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)

# Transporte HTTP: función tipo requests.post. Los tests la reemplazan. No se usa requests.Session
# para no depender de parches globales de Session.
_post: Callable[..., Any] = requests.post


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


class TelegramConfig:
    def __init__(self, token: str = "", chat_id: str = "", prefix: str = "") -> None:
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        return cls(
            os.environ.get(ENV_TOKEN, "").strip(),
            os.environ.get(ENV_CHAT_ID, "").strip(),
            os.environ.get(ENV_PREFIX, "").strip(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def __repr__(self) -> str:  # nunca exponer el token
        return f"TelegramConfig(enabled={self.enabled}, chat_id={'***' if self.chat_id else ''})"


def log_startup_state(logger: Optional[logging.Logger] = None, cfg: Optional[TelegramConfig] = None) -> bool:
    """Un log al arrancar con el estado (habilitado/deshabilitado). Devuelve si está habilitado."""
    cfg = cfg or TelegramConfig.from_env()
    logger = logger or log
    if cfg.enabled:
        logger.info("Notificaciones Telegram habilitadas.")
    else:
        logger.info("Notificaciones Telegram deshabilitadas: faltan %s y/o %s.", ENV_TOKEN, ENV_CHAT_ID)
    return cfg.enabled


def _with_prefix(cfg: TelegramConfig, text: str) -> str:
    if cfg.prefix:
        text = f"<b>{esc(cfg.prefix)}</b> {text}"
    if len(text) > MAX_MESSAGE_LEN:
        text = text[: MAX_MESSAGE_LEN - 20].rsplit("\n", 1)[0] + "\n… (recortado)"
    return text


def send(text: str, cfg: Optional[TelegramConfig] = None) -> bool:
    """Envía ``text`` (HTML ya escapado por el que lo arma). Nunca lanza."""
    try:
        cfg = cfg or TelegramConfig.from_env()
        if not cfg.enabled:
            return False
        url = f"{API_BASE}/bot{cfg.token}/sendMessage"
        body = {
            "chat_id": cfg.chat_id,
            "text": _with_prefix(cfg, text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = _post(url, json=body, timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # el texto de la excepción incluye la URL con el token
            log.warning("Telegram: no se pudo enviar (%s).", exc.__class__.__name__)
            return False
        if getattr(resp, "status_code", None) != 200:
            desc = ""
            try:
                desc = str((resp.json() or {}).get("description", ""))[:200]
            except Exception:
                pass
            log.warning("Telegram: la API respondió %s %s", getattr(resp, "status_code", "?"),
                        desc.replace(cfg.token, "***") if cfg.token else desc)
            return False
        return True
    except Exception as exc:  # defensivo: notificar nunca rompe al que llama
        log.warning("Telegram: error inesperado al enviar (%s).", exc.__class__.__name__)
        return False


def _start_thread(fn: Callable[[], Any]) -> Optional[threading.Thread]:
    t = threading.Thread(target=fn, name="telegram-notify", daemon=True)
    t.start()
    return t


def send_async(text: str, cfg: Optional[TelegramConfig] = None) -> Optional[threading.Thread]:
    """Envía en un hilo aparte (no bloquea la request). Nunca lanza."""
    try:
        cfg = cfg or TelegramConfig.from_env()
        if not cfg.enabled:
            return None
        return _start_thread(lambda: send(text, cfg))
    except Exception as exc:
        log.warning("Telegram: no se pudo programar el envío (%s).", exc.__class__.__name__)
        return None


# --------------------------------------------------------------------------------------
# Mensajes de acciones del panel
# --------------------------------------------------------------------------------------


def _items(rows: Any, fmt: Callable[[Dict[str, Any]], str]) -> str:
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    lines = [f"  • {fmt(r)}" for r in rows[:_MAX_ITEMS]]
    if len(rows) > _MAX_ITEMS:
        lines.append(f"  • … y {len(rows) - _MAX_ITEMS} más")
    return "\n".join(lines)


def _cantidad(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("cantidad") or 0)
    except (TypeError, ValueError):
        return 0


def format_resumen(resumen: Dict[str, Any]) -> str:
    cerradas = resumen.get("posiciones_cerradas") or []
    canceladas = resumen.get("ordenes_canceladas") or []
    errores = resumen.get("errores") or []
    restante = resumen.get("restante") or {}
    out = [f"Posiciones cerradas: {len(cerradas)}"]
    if cerradas:
        out.append(_items(cerradas, lambda r: f"{esc(r.get('symbol'))} {esc(r.get('lado'))} "
                                              f"{esc(r.get('cantidad'))}"))
    total_ord = sum(_cantidad(r) for r in canceladas if isinstance(r, dict))
    out.append(f"Órdenes canceladas: {total_ord}")
    if canceladas:
        out.append(_items(canceladas, lambda r: f"{esc(r.get('symbol'))} ({esc(r.get('tipo'))}): "
                                                f"{esc(r.get('cantidad'))}"))
    out.append(f"Errores: {len(errores)}")
    if errores:
        out.append(_items([{"e": e} for e in errores], lambda r: esc(r["e"])))
    # restante.ordenes / ordenes_algo traen una fila por símbolo ({symbol, cantidad}): se suman las
    # cantidades, no las filas. restante.posiciones trae una fila por posición.
    pos_rest = [r for r in (restante.get("posiciones") or []) if isinstance(r, dict)]
    ord_rest = [r for r in (restante.get("ordenes") or []) if isinstance(r, dict)]
    algo_rest = [r for r in (restante.get("ordenes_algo") or []) if isinstance(r, dict)]
    n_ord = sum(_cantidad(r) for r in ord_rest)
    n_algo = sum(_cantidad(r) for r in algo_rest)
    if pos_rest or n_ord or n_algo:
        out.append(f"Quedó abierto: {len(pos_rest)} posiciones, {n_ord} órdenes, {n_algo} condicionales")
        if pos_rest:
            out.append(_items(pos_rest, lambda r: f"posición {esc(r.get('symbol'))} {esc(r.get('lado'))} "
                                                  f"{esc(r.get('cantidad'))}"))
        if ord_rest:
            out.append(_items(ord_rest, lambda r: f"órdenes {esc(r.get('symbol'))}: {_cantidad(r)}"))
        if algo_rest:
            out.append(_items(algo_rest, lambda r: f"condicionales {esc(r.get('symbol'))}: {_cantidad(r)}"))
    if not restante.get("verificado", False):
        out.append("⚠️ Estado final sin verificar: revisar en Binance.")
    if resumen.get("algo_verificado") is False:
        out.append("⚠️ Órdenes condicionales sin verificar (demo/testnet).")
    return "\n".join(out)


def format_bot_action(action: str, params: Dict[str, Any], outcome: str, payload: Dict[str, Any],
                      user: str) -> Optional[str]:
    """Texto HTML para una acción del panel. None si no corresponde notificar (ej. validación 400)."""
    if outcome not in ("ok", "error"):
        return None
    ok = outcome == "ok"
    if action == "start":
        titulo = f"START riesgo {esc(params.get('riesgo'))}"
    elif action == "stop" and params.get("modo") == "apagar":
        titulo = "APAGAR (stop + cierre de posiciones)"
    elif action == "stop":
        titulo = "Graceful stop"
    else:
        titulo = esc(action)
    icon = "✅" if ok else "❌"
    estado = "OK" if ok else "FALLÓ"
    lines = [f"{icon} <b>{titulo}</b>: {estado}", f"Usuario: {esc(user)}"]
    if payload.get("docker_action"):
        lines.append(f"Docker: {esc(payload.get('docker_action'))}")
    if not ok and payload.get("error"):
        lines.append(f"Error: {esc(payload.get('error'))}")
    if payload.get("warning"):
        lines.append(f"⚠️ {esc(payload.get('warning'))}")
    if isinstance(payload.get("resumen"), dict):
        lines.append(format_resumen(payload["resumen"]))
    return "\n".join(lines)


def notify_bot_action(action: str, params: Dict[str, Any], outcome: str, payload: Dict[str, Any],
                      user: str) -> Optional[threading.Thread]:
    """Notifica una acción del panel de forma asíncrona. Nunca lanza."""
    try:
        cfg = TelegramConfig.from_env()
        if not cfg.enabled:
            return None
        text = format_bot_action(action, params, outcome, payload, user)
        if not text:
            return None
        return send_async(text, cfg)
    except Exception as exc:
        log.warning("Telegram: no se pudo armar la notificación (%s).", exc.__class__.__name__)
        return None
