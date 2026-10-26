"""Bot → "Conexión con Binance" (spec bot-estrategia § 10.4, paso 1): la key en uso y quién la usa.

Solo lee: el proveedor de credenciales del panel (un ``stat`` del archivo), ``credenciales_meta.json``, el latido del
scraper, el estado del rescate, el inspect de passivbot que ya hizo Bot y la última orden del scraper. **Nunca llama a
Binance** (Bot no consulta Binance al armarse) y nunca devuelve key, secret ni uid: la key va enmascarada (4 + … + 4).

"Quién la usa":

* **Panel, scraper y rescate**: el ``key_id`` de su latido contra el de la key vigente (la del archivo).
* **passivbot**: lee la key al arrancar. Cuenta como "con esta key" solo si arrancó **después** del último cambio del
  archivo y ya creó una orden **después** de arrancar (sin permiso de operar el contenedor sigue ``running`` y no
  opera: revisión A5). Sin orden nueva queda "sin confirmar" (ámbar), nunca en rojo.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

ESTADO_OK = "ok"
ESTADO_ATRAS = "atras"
ESTADO_SIN_CONFIRMAR = "sin_confirmar"
ESTADO_DETENIDO = "detenido"
ESTADO_SIN_DATOS = "sin_datos"


def _hace(segundos: float) -> str:
    s = max(0, int(segundos))
    if s < 90:
        return f"{s} s"
    if s < 90 * 60:
        return f"{s // 60} min"
    if s < 48 * 3600:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def _ts(valor: Any) -> Optional[float]:
    """Epoch de un ISO 8601 (Docker trae nanosegundos y ``Z``) o de un número."""
    if isinstance(valor, bool) or valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not texto or texto.startswith("0001-"):
        return None
    texto = texto.replace("Z", "+00:00")
    if "." in texto:  # 2026-10-23T15:47:48.123456789+00:00 → microsegundos
        cabeza, _, resto = texto.partition(".")
        frac = "".join(c for c in resto if c.isdigit())
        zona = resto[len(frac):]
        texto = f"{cabeza}.{frac[:6]}{zona}"
    try:
        momento = dt.datetime.fromisoformat(texto)
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=dt.timezone.utc)
    return momento.timestamp()


def fecha(ts: Optional[float], con_segundos: bool = False) -> str:
    if ts is None:
        return ""
    m = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    return f"{m:%d/%m %H:%M:%S}" if con_segundos else f"{m:%d/%m %H:%M}"


def _hora(ts: float) -> str:
    return f"{dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc):%H:%M:%S}"


def _consumidor(clave: str, nombre: str, detalle: str, estado: str, texto: str) -> Dict[str, Any]:
    return {"clave": clave, "nombre": nombre, "detalle": detalle, "estado": estado, "texto": texto}


def _firma(ok_at: Any, ahora: float) -> str:
    t = _ts(ok_at)
    return f"firma OK hace {_hace(ahora - t)}" if t is not None else "con esta key, sin firma OK todavía"


def _proceso(clave: str, nombre: str, detalle: str, kid: Optional[str], ref: Optional[str], ok_at: Any,
             error: Optional[str], vivo: bool, ahora: float) -> Dict[str, Any]:
    if not vivo:
        return _consumidor(clave, nombre, f"{detalle} · sin latido reciente", ESTADO_SIN_DATOS, "sin latido")
    if not kid:
        return _consumidor(clave, nombre, f"{detalle} · su latido no informa la key (versión anterior)",
                           ESTADO_SIN_DATOS, "sin datos")
    if ref and kid != ref:
        return _consumidor(clave, nombre, f"{detalle} · firma con otra key; la toma en su próxima vuelta",
                           ESTADO_ATRAS, "con otra key")
    if error:
        detalle = f"{detalle} · {error}"
    return _consumidor(clave, nombre, detalle, ESTADO_OK, _firma(ok_at, ahora))


def _passivbot(container: Optional[Dict[str, Any]], archivo_mtime: Optional[float], modo_secretos: bool,
               ultima_orden_ms: Optional[int]) -> Dict[str, Any]:
    nombre = "passivbot"
    base = "forager y un bot por par · la lee al arrancar"
    if not isinstance(container, dict) or not container.get("status") or container.get("status") == "not_found":
        return _consumidor("passivbot", nombre, base, ESTADO_SIN_DATOS, "sin datos de Docker")
    if not container.get("running"):
        return _consumidor("passivbot", nombre, f"{base} · detenido: la usa al iniciar", ESTADO_DETENIDO, "detenido")
    inicio = _ts(container.get("started_at"))
    detalle = f"{base} ({fecha(inicio)})" if inicio else base
    if not modo_secretos:
        return _consumidor("passivbot", nombre, f"{detalle} · lee su propio archivo hasta la migración a /secretos",
                           ESTADO_SIN_CONFIRMAR, "sin verificar")
    if inicio is None:
        return _consumidor("passivbot", nombre, detalle, ESTADO_SIN_CONFIRMAR, "sin confirmar")
    if archivo_mtime is not None and inicio < archivo_mtime:
        return _consumidor("passivbot", nombre, f"{detalle} · arrancó antes del último cambio del archivo: sigue con "
                           "la key anterior hasta reiniciarse", ESTADO_ATRAS, "con la key anterior")
    orden = ultima_orden_ms / 1000.0 if isinstance(ultima_orden_ms, (int, float)) else None
    if orden is not None and orden > inicio:
        return _consumidor("passivbot", nombre, detalle, ESTADO_OK, f"orden nueva {_hora(orden)}")
    return _consumidor("passivbot", nombre, f"{detalle} · todavía no creó órdenes desde que arrancó",
                       ESTADO_SIN_CONFIRMAR, "sin confirmar")


def vista(estado: Dict[str, Any], *, modo_secretos: bool, testnet: bool, meta: Dict[str, Any],
          scraper_latido: Dict[str, Any], scraper_vivo: bool, scraper_intervalo: Optional[int],
          rescate_servicio: Dict[str, Any], rescate_vivo: bool,
          container: Optional[Dict[str, Any]], ultima_orden_ms: Optional[int], ahora: float,
          key_enmascarada: Optional[str], web: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Lo que pinta Bot y devuelve ``GET /api/bot/credenciales``. Sin key, secret ni uid."""
    ref = estado.get("key_id")
    meta_vigente = bool(meta) and bool(ref) and meta.get("key_id") == ref
    archivo_mtime = estado.get("archivo_mtime")

    if meta_vigente and meta.get("huella_uid"):
        cuenta = {"verificada": True, "texto": "UID verificado"}
    else:
        cuenta = {"verificada": False, "texto": "UID sin verificar"}
    hedge = meta.get("hedge") if meta_vigente else None

    if meta_vigente and meta.get("configurada_en"):
        configurada = f"{fecha(_ts(meta['configurada_en']))} · {meta.get('configurada_por') or 'sin registro de quién'}"
    elif modo_secretos and archivo_mtime:
        configurada = f"{fecha(archivo_mtime)} · sin registro de quién"
    elif not modo_secretos:
        configurada = "config.json · sin registro de quién"
    else:
        configurada = "sin datos"
    validada = fecha(_ts(meta.get("validada_en"))) if meta_vigente and meta.get("validada_en") else ""

    if testnet:
        permisos = {"estado": "demo", "texto": "Demo no los informa"}
    elif meta_vigente and isinstance(meta.get("permisos"), dict):
        problemas = meta["permisos"].get("problemas") or []
        permisos = {"estado": "problemas" if problemas else "verificados",
                    "texto": "; ".join(problemas) if problemas else "lectura y futuros, sin retiros ni transferencia "
                                                                    "interna, con restricción de IP"}
    else:
        permisos = {"estado": "sin_verificar", "texto": "sin verificar"}

    consumidores: List[Dict[str, Any]] = [
        _passivbot(container, archivo_mtime, modo_secretos, ultima_orden_ms),
        _consumidor("panel", "Panel", "Apagar y cierre a mercado · la relee si cambia",
                    ESTADO_OK if ref else ESTADO_SIN_DATOS,
                    _firma(estado.get("key_ok_at"), ahora) if ref else "sin key"),
        _proceso("scraper", "Scraper",
                 f"posiciones, órdenes e ingresos cada {scraper_intervalo or 60} s",
                 scraper_latido.get("key_id"), ref, scraper_latido.get("key_ok_at"), scraper_latido.get("key_error"),
                 scraper_vivo, ahora),
        _proceso("rescate", "Rescate", "lectura de la cuenta, tope de pérdida y C3",
                 rescate_servicio.get("key_id"), ref, rescate_servicio.get("key_ok_at"),
                 rescate_servicio.get("key_error"), rescate_vivo, ahora),
    ]
    con_esta = sum(1 for c in consumidores if c["estado"] == ESTADO_OK)
    avisos = []
    if estado.get("key_error"):
        avisos.append(f"Archivo de credenciales: {estado['key_error']}")
    if estado.get("testnet") is not None and bool(estado.get("testnet")) != bool(testnet):
        avisos.append(f"El archivo dice testnet {str(bool(estado['testnet'])).lower()} y el panel opera en "
                      f"{'Binance Demo' if testnet else 'Binance real'}: revisarlo a mano.")
    return {
        "hay_key": bool(ref),
        "entorno": "demo" if testnet else "real",
        "entorno_texto": "Binance Demo" if testnet else "Binance real",
        "key": key_enmascarada,
        "key_id": ref,
        "secret": "guardado · no se muestra" if ref else "—",
        "origen": "secretos" if modo_secretos else "config",
        "usuario": estado.get("usuario") or ("—" if not modo_secretos else ""),
        "cuenta": cuenta,
        "hedge": hedge,
        "configurada": configurada,
        "validada": validada,
        "permisos": permisos,
        "consumidores": consumidores,
        "con_esta_key": con_esta,
        "total": len(consumidores),
        "avisos": avisos,
        "web": web or {"habilitada": False, "motivo": None},
    }
