"""Cambiar la API key de Binance desde el panel (spec bot-estrategia § 10.2 y § 10.4 a § 10.7).

El secret **no sale nunca del servidor**: ninguna respuesta, log, historial, job ni Telegram lleva la key entera, el
secret ni el uid. La key viaja enmascarada (4 + … + 4) y la cuenta se compara por una **huella** del uid (HMAC con una
clave que vive en ``/secretos``, así ``client17-api`` no puede sacar el uid por fuerza bruta).

Flujo (todo detrás de ``FUTURESBOARD_CREDENCIALES_WEB``, apagado por defecto):

1. *Validar* (:func:`validar`): formato HMAC de 64; distinta de la actual; firma en futuros; spot; **misma cuenta**
   (uid contra la huella guardada; sin huella, la línea de base con la key actual; con la actual revocada, ``tranId`` de
   ``/fapi/v1/income`` contra la tabla ``income``); hedge mode; la sonda del permiso de operar (``POST
   positionSide/dual`` **solo** después de ver ``dualSidePosition: true``: ``-4059`` = puede operar, ``-2015`` = sin
   permiso); posiciones (informa, no bloquea); permisos (``apiRestrictions``, solo en real). Si pasa, queda un
   :class:`Pendiente` en la memoria del proceso: 5 min, un solo uso.
2. *Plan* (:func:`armar_plan`) y confirmación con la contraseña del panel (en el blueprint).
3. *Job* (:func:`aplicar`, con el lock de acciones): revalidar, respaldo 600 en ``/secretos-respaldo``, temporal dentro
   de ``/secretos`` + fsync + ``os.replace`` (solo cambian ``key`` y ``secret`` del usuario), restart de passivbot por el
   proxy; si Docker lo rechaza (4xx/5xx), se restaura **copiando** el respaldo a un temporal dentro de ``/secretos``
   (``rename`` entre montajes da ``EXDEV``).
4. *Después* (:func:`esperar_consumidores`, sin el lock y cancelable): panel, scraper y rescate con el ``key_id`` nuevo
   y passivbot con una orden nueva. Un Apagar la corta.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from urllib.parse import urlencode

import requests

from futuresboard import bot_control
from futuresboard import credenciales

log = logging.getLogger(__name__)

ENV_WEB = "FUTURESBOARD_CREDENCIALES_WEB"
ENV_ESPERA = "FUTURESBOARD_CREDENCIALES_ESPERA_SECONDS"
FORMATO = re.compile(r"^[A-Za-z0-9]{64}$")
TIMEOUT_S = 10
RECV_WINDOW = 5000
PENDIENTE_TTL_S = 5 * 60
VALIDACIONES_MAX = 5
VALIDACIONES_VENTANA_S = 15 * 60
RESPALDO_PREFIJO = "api-keys.json.bak-"
RESPALDOS_MAX = 2
RESPALDO_MAX_DIAS = 7
HUELLA_ARCHIVO = "huella.key"
# Rescate ≤ 30 s y scraper ≤ 120 s (spec § 10.4, paso 5), más la orden nueva de passivbot.
ESPERA_DEFECTO_S = 150
ESPERA_MAX_S = 900
ESPERA_TICK_S = 2.0
SKEW_S = 2.0

CREDENCIALES_STEPS: List[tuple] = [
    ("revalidar", "Validar de nuevo con Binance"),
    ("respaldo", "Respaldo del archivo actual"),
    ("escribir", "Escribir el archivo nuevo"),
    ("docker", "Reiniciar passivbot"),
    ("verificar", "Verificar passivbot en ejecución"),
    ("consumidores", "Esperar a los 4 con la key nueva"),
]

OK, BLOQUEA, INFO = "ok", "bloquea", "info"
_ORDEN = ("formato", "distinta", "firma", "cuenta", "contexto", "historial", "hedge", "operar", "spot", "posiciones",
          "permisos")


def web_habilitada() -> bool:
    return os.environ.get(ENV_WEB, "").strip().lower() in ("1", "true", "yes", "on")


def espera_segundos() -> int:
    raw = os.environ.get(ENV_ESPERA, "").strip()
    try:
        value = int(raw) if raw else ESPERA_DEFECTO_S
    except ValueError:
        value = ESPERA_DEFECTO_S
    return max(0, min(ESPERA_MAX_S, value))


def ahora_iso(ts: Optional[float] = None) -> str:
    return credenciales.iso(time.time() if ts is None else ts) or ""


class CambioError(bot_control.BotControlError):
    """Error del cambio de key con mensaje apto para el usuario (sin key, secret ni uid)."""

    def __init__(self, message: str, status_code: int = 409, payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, payload)
        self.status_code = status_code


def validar_formato(api_key: Any, api_secret: Any) -> None:
    """``ValueError`` con el motivo (sin los valores). El panel firma con HMAC: RSA o Ed25519 no sirven."""
    for nombre, valor in (("La API key", api_key), ("El secret", api_secret)):
        if not isinstance(valor, str) or not valor:
            raise ValueError(f"{nombre} está vacía o no es texto." if nombre == "La API key" else
                             f"{nombre} está vacío o no es texto.")
        if not FORMATO.match(valor):
            raise ValueError(f"{nombre} tiene que tener 64 letras y números (clave HMAC de Binance). Las claves RSA "
                             "o Ed25519 no sirven: el panel firma con HMAC.")


# --------------------------------------------------------------------------------------
# Cliente firmado con la key a validar (nunca la del proveedor)
# --------------------------------------------------------------------------------------


class ErrorBinance(Exception):
    """Respuesta de Binance que no sirve. El mensaje no lleva URL, firma, key ni secret."""

    def __init__(self, mensaje: str, code: Any = None, http_status: Optional[int] = None, sin_respuesta: bool = False):
        super().__init__(mensaje)
        self.code = code
        self.http_status = http_status
        self.sin_respuesta = sin_respuesta


class Cliente:
    """Pedidos firmados con una key y un secret fijos (los que se están validando). Timeout 10 s, peso bajo."""

    def __init__(self, futures_url: str, spot_url: str, api_key: str, api_secret: str,
                 session: Optional[Any] = None, timeout: float = TIMEOUT_S) -> None:
        self.futures_url = futures_url.rstrip("/")
        self.spot_url = spot_url.rstrip("/")
        self._cred = credenciales.Fijas(api_key, api_secret)
        self.http = session or requests.Session()
        self.timeout = timeout

    def _pedir(self, method: str, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        cred = self._cred.actuales()
        query_params = dict(params or {})
        query_params["recvWindow"] = RECV_WINDOW
        query_params["timestamp"] = int(time.time() * 1000)
        query = urlencode(query_params)
        firma = hmac.new(cred.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        try:
            resp = self.http.request(method, f"{base}{path}?{query}&signature={firma}",
                                     headers={"X-MBX-APIKEY": cred.api_key}, timeout=self.timeout)
        except requests.RequestException as exc:
            # "from None": el error de requests trae la URL (con la firma); no se encadena.
            raise ErrorBinance(f"Sin respuesta de Binance en {method} {path} ({exc.__class__.__name__}).",
                               sin_respuesta=True) from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        code = data.get("code") if isinstance(data, dict) else None
        if resp.status_code != 200 or (code is not None and code not in (0, 200)):
            msg = str(data.get("msg", ""))[:120] if isinstance(data, dict) else ""
            raise ErrorBinance(f"Binance respondió {resp.status_code} en {method} {path}: {code} {msg}".rstrip(),
                               code=code, http_status=resp.status_code)
        if data is None:
            raise ErrorBinance(f"Binance respondió {resp.status_code} sin JSON válido en {method} {path}.",
                               http_status=resp.status_code)
        return data

    def futuros(self) -> Dict[str, Any]:
        data = self._pedir("GET", self.futures_url, "/fapi/v2/account")
        if not isinstance(data, dict):
            raise ErrorBinance("Respuesta inesperada de Binance en GET /fapi/v2/account.")
        return data

    def spot(self) -> Dict[str, Any]:
        data = self._pedir("GET", self.spot_url, "/api/v3/account", {"omitZeroBalances": "true"})
        if not isinstance(data, dict):
            raise ErrorBinance("Respuesta inesperada de Binance en GET /api/v3/account.")
        return data

    def dual(self) -> bool:
        data = self._pedir("GET", self.futures_url, "/fapi/v1/positionSide/dual")
        if not isinstance(data, dict) or not isinstance(data.get("dualSidePosition"), bool):
            raise ErrorBinance("Respuesta inesperada de Binance en GET /fapi/v1/positionSide/dual.")
        return data["dualSidePosition"]

    def sonda_operar(self) -> Any:
        """``POST positionSide/dual dualSidePosition=true`` con la cuenta YA en hedge mode: no cambia nada. Devuelve
        el código de Binance (``-4059`` = "no hace falta cambiar", el que acepta passivbot) o 200 si respondió OK."""
        try:
            self._pedir("POST", self.futures_url, "/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})
        except ErrorBinance as exc:
            if exc.sin_respuesta:
                raise
            return exc.code
        return 200

    def restricciones(self) -> Optional[Dict[str, Any]]:
        """``apiRestrictions`` de la key. None si el endpoint no existe (Binance Demo no tiene ``/sapi``: 404)."""
        try:
            data = self._pedir("GET", self.spot_url, "/sapi/v1/account/apiRestrictions")
        except ErrorBinance as exc:
            if exc.http_status == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    def income(self) -> List[str]:
        data = self._pedir("GET", self.futures_url, "/fapi/v1/income", {"limit": 1000})
        if not isinstance(data, list):
            raise ErrorBinance("Respuesta inesperada de Binance en GET /fapi/v1/income.")
        return [str(r.get("tranId")) for r in data if isinstance(r, dict) and r.get("tranId") is not None]


def motivo(exc: ErrorBinance, testnet: bool) -> str:
    """El error de Binance en palabras (spec § 10.2)."""
    if exc.sin_respuesta:
        return "Binance no respondió; no se guardó nada."
    if exc.code in (-2015, -2014):
        if testnet:
            return "Binance Demo no reconoce esta key: ¿es de Binance real? Este bot opera en Demo."
        return "Binance real no reconoce esta key: ¿es de Binance Demo? Este bot opera en real."
    if exc.code == -1022:
        return "El secret no corresponde a la key."
    return f"{exc} No se puede usar."


# --------------------------------------------------------------------------------------
# Archivos: huella, respaldo, escritura atómica y vuelta atrás
# --------------------------------------------------------------------------------------


def _fsync_dir(path: pathlib.Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def leer_archivo(dir_secretos: pathlib.Path) -> bytes:
    return (pathlib.Path(dir_secretos) / credenciales.ARCHIVO).read_bytes()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def huella_clave(dir_secretos: pathlib.Path) -> bytes:
    """Clave del HMAC de la huella del uid, en ``/secretos`` (600). La crea el panel la primera vez."""
    path = pathlib.Path(dir_secretos) / HUELLA_ARCHIVO
    try:
        valor = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        valor = ""
    if len(valor) >= 64:
        return valor.encode("ascii")
    nuevo = secrets.token_hex(32)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="ascii").strip().encode("ascii")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(nuevo)
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_dir(path.parent)
    return nuevo.encode("ascii")


def huella(uid: Any, clave: bytes) -> str:
    return hmac.new(clave, str(uid).encode("utf-8"), hashlib.sha256).hexdigest()


def _escribir_en(dir_destino: pathlib.Path, nombre: str, data: bytes) -> None:
    """Temporal 600 DENTRO del directorio de destino, fsync, ``os.replace`` y fsync del directorio."""
    fd, tmp = tempfile.mkstemp(prefix=f".{nombre}.", suffix=".tmp", dir=str(dir_destino))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(str(dir_destino), nombre))
        tmp = ""
        _fsync_dir(pathlib.Path(dir_destino))
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _respaldos(dir_respaldo: pathlib.Path) -> List[pathlib.Path]:
    try:
        return sorted((p for p in pathlib.Path(dir_respaldo).iterdir()
                       if p.is_file() and p.name.startswith(RESPALDO_PREFIJO)), key=lambda p: p.name)
    except OSError:
        return []


def podar_respaldos(dir_respaldo: pathlib.Path, ahora: Optional[float] = None) -> None:
    """Quedan los 2 últimos y se borran a los 7 días: una key vieja ya revocada no sirve para nada."""
    ahora = time.time() if ahora is None else ahora
    lista = _respaldos(dir_respaldo)
    for i, p in enumerate(lista):
        try:
            viejo = ahora - p.stat().st_mtime > RESPALDO_MAX_DIAS * 86400
            if i < len(lista) - RESPALDOS_MAX or viejo:
                p.unlink()
        except OSError:
            log.warning("No se pudo podar el respaldo %s", p.name)


def respaldar(dir_secretos: pathlib.Path, dir_respaldo: pathlib.Path, data: bytes,
              ahora: Optional[float] = None) -> pathlib.Path:
    """Copia 600 del archivo actual en ``/secretos-respaldo`` (fsync). Nunca al lado ni en el repo."""
    ahora = time.time() if ahora is None else ahora
    dir_respaldo = pathlib.Path(dir_respaldo)
    if not dir_respaldo.is_dir():
        raise CambioError(f"Falta el directorio de respaldos ({dir_respaldo}): no se cambió nada.", 503)
    nombre = f"{RESPALDO_PREFIJO}{dt.datetime.fromtimestamp(ahora, tz=dt.timezone.utc):%Y%m%d-%H%M%S}"
    path = dir_respaldo / nombre
    n = 1
    while path.exists():
        path = dir_respaldo / f"{nombre}-{n}"
        n += 1
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_dir(dir_respaldo)
    podar_respaldos(dir_respaldo, ahora)
    return path


def contenido_nuevo(original: bytes, usuario: str, api_key: str, api_secret: str) -> bytes:
    """El mismo JSON con ``key`` y ``secret`` del usuario reemplazados: el resto de las entradas y ``testnet`` quedan
    iguales (se verifica al releer)."""
    data = json.loads(original.decode("utf-8"))
    entrada = data.get(usuario) if isinstance(data, dict) else None
    if not isinstance(entrada, dict):
        raise CambioError(f"El archivo de credenciales no tiene el usuario {usuario}: no se cambió nada.", 409)
    entrada["key"] = api_key
    entrada["secret"] = api_secret
    return (json.dumps(data, indent=4, ensure_ascii=False) + "\n").encode("utf-8")


def verificar_contenido(original: bytes, escrito: bytes, usuario: str, api_key: str, api_secret: str) -> None:
    """Solo cambiaron ``key`` y ``secret`` del usuario."""
    antes, despues = json.loads(original.decode("utf-8")), json.loads(escrito.decode("utf-8"))
    entrada = despues.get(usuario) if isinstance(despues, dict) else None
    if not isinstance(entrada, dict) or entrada.get("key") != api_key or entrada.get("secret") != api_secret:
        raise CambioError("La relectura del archivo nuevo no tiene la key nueva: no se cambió nada.", 500)
    for d in (antes, despues):
        d[usuario] = {k: v for k, v in d[usuario].items() if k not in ("key", "secret")}
    if antes != despues:
        raise CambioError("La relectura del archivo nuevo cambió algo más que key y secret: no se cambió nada.", 500)


def escribir(dir_secretos: pathlib.Path, original: bytes, usuario: str, api_key: str, api_secret: str) -> None:
    """Temporal 600 en ``/secretos``, fsync, relectura (JSON válido, solo cambiaron key y secret), ``os.replace`` y
    fsync del directorio."""
    dir_secretos = pathlib.Path(dir_secretos)
    nuevo = contenido_nuevo(original, usuario, api_key, api_secret)
    fd, tmp = tempfile.mkstemp(prefix=f".{credenciales.ARCHIVO}.", suffix=".tmp", dir=str(dir_secretos))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(nuevo)
            fh.flush()
            os.fsync(fh.fileno())
        with open(tmp, "rb") as fh:
            verificar_contenido(original, fh.read(), usuario, api_key, api_secret)
        os.replace(tmp, str(dir_secretos / credenciales.ARCHIVO))
        tmp = ""
        _fsync_dir(dir_secretos)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def restaurar(dir_secretos: pathlib.Path, respaldo: pathlib.Path) -> None:
    """Vuelta atrás: COPIA del respaldo a un temporal dentro de ``/secretos`` y ``os.replace`` ahí. Un ``rename`` desde
    ``/secretos-respaldo`` daría ``EXDEV``: son dos montajes distintos aunque sea el mismo disco (revisión M1)."""
    _escribir_en(pathlib.Path(dir_secretos), credenciales.ARCHIVO, pathlib.Path(respaldo).read_bytes())


def respaldo_anterior(dir_respaldo: pathlib.Path, usuario: str, key_id_actual: Optional[str],
                      ahora: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """El respaldo más nuevo (≤ 7 días) con una key distinta de la actual, para "Volver a la key anterior"."""
    ahora = time.time() if ahora is None else ahora
    for p in reversed(_respaldos(dir_respaldo)):
        try:
            if ahora - p.stat().st_mtime > RESPALDO_MAX_DIAS * 86400:
                continue
            key, secret, _testnet = credenciales.leer_entrada(p.read_bytes(), usuario)
        except (OSError, ValueError):
            continue
        if credenciales.key_id(key) != key_id_actual:
            return {"path": p, "api_key": key, "api_secret": secret, "key": credenciales.enmascarar(key),
                    "key_id": credenciales.key_id(key), "fecha": p.stat().st_mtime}
    return None


# --------------------------------------------------------------------------------------
# Validación
# --------------------------------------------------------------------------------------


@dataclasses.dataclass
class Entorno:
    """Lo que la validación y el job necesitan del panel (capturado en la request: el job corre sin Flask)."""

    testnet: bool
    futures_url: str
    spot_url: str
    proveedor: Any
    data_dir: pathlib.Path
    db_path: str
    dir_secretos: pathlib.Path
    dir_respaldo: pathlib.Path
    rescate_real: bool = False
    cliente: Optional[Callable[[str, str], Cliente]] = None

    def nuevo_cliente(self, api_key: str, api_secret: str) -> Cliente:
        if self.cliente is not None:
            return self.cliente(api_key, api_secret)
        return Cliente(self.futures_url, self.spot_url, api_key, api_secret)

    @property
    def nombre(self) -> str:
        return "Binance Demo" if self.testnet else "Binance real"


@dataclasses.dataclass
class Resultado:
    ok: bool
    chequeos: List[Dict[str, str]]
    motivo: Optional[str] = None
    huella: Optional[str] = None
    hedge: Optional[bool] = None
    permisos: Optional[Dict[str, Any]] = None
    otra_cuenta: bool = False


def _chequeo(clave: str, estado: str, texto: str) -> Dict[str, str]:
    return {"clave": clave, "estado": estado, "texto": texto}


def _db_filas(db_path: str, sql: str) -> List[tuple]:
    from futuresboard import db

    try:
        conn = db.connect(db_path, readonly=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        return list(conn.execute(sql).fetchall())
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _posiciones_db(db_path: str) -> List[Tuple[str, str]]:
    filas = _db_filas(db_path, "SELECT symbol, positionSide, positionAmt FROM positions")
    return sorted((str(s), str(ps or "BOTH")) for s, ps, a in filas if a not in (None, 0, 0.0))


def _ordenes_db(db_path: str) -> int:
    filas = _db_filas(db_path, "SELECT COUNT(*) FROM orders")
    return int(filas[0][0]) if filas else 0


def _posiciones_binance(fut: Dict[str, Any]) -> List[Tuple[str, str]]:
    out = []
    for r in fut.get("positions") or []:
        try:
            if isinstance(r, dict) and float(r.get("positionAmt") or 0) != 0:
                out.append((str(r.get("symbol")), str(r.get("positionSide") or "BOTH")))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _pares(pos: List[Tuple[str, str]]) -> str:
    nombres = [f"{s[:-4] if s.endswith('USDT') else s} {'long' if ps == 'LONG' else ('short' if ps == 'SHORT' else '')}"
               .strip() for s, ps in pos]
    if len(nombres) > 1:
        return ", ".join(nombres[:-1]) + " y " + nombres[-1]
    return nombres[0] if nombres else ""


def _balance(fut: Dict[str, Any]) -> str:
    try:
        valor = float(fut.get("totalWalletBalance"))
    except (TypeError, ValueError):
        return ""
    entero, _, dec = f"{valor:,.2f}".partition(".")
    return f" · balance {entero.replace(',', '.')},{dec} USDT"


def asegurar_linea_base(env: Entorno) -> Tuple[Dict[str, Any], bool]:
    """La primera vez (o si la key vigente cambió a mano) guarda la línea de base: huella del uid y ``key_id`` de la
    key actual, validada en ese momento. Devuelve la meta y si la key actual respondió."""
    meta = credenciales.leer_meta(env.data_dir)
    try:
        actual = env.proveedor.actuales()
    except credenciales.SinCredenciales:
        return meta, False
    if meta.get("huella_uid") and meta.get("key_id") == actual.key_id:
        return meta, True
    cli = env.nuevo_cliente(actual.api_key, actual.api_secret)
    try:
        uid = cli.spot().get("uid")
        hedge = cli.dual()
    except ErrorBinance as exc:
        log.warning("Línea de base de la API key: la key actual no respondió (%s).", exc)
        return meta, False
    if uid is None:
        return meta, True
    ahora = ahora_iso()
    meta = {**meta, "key": credenciales.enmascarar(actual.api_key), "key_id": actual.key_id,
            "huella_uid": huella(uid, huella_clave(env.dir_secretos)), "linea_base_en": ahora, "validada_en": ahora,
            "hedge": hedge, "entorno": "demo" if env.testnet else "real"}
    try:
        credenciales.guardar_meta(env.data_dir, meta)
    except OSError:
        log.exception("No se pudo guardar la línea de base de la API key")
    return meta, True


def _requeridos(rescate_real: bool) -> Dict[str, bool]:
    req = {"enableReading": True, "enableFutures": True, "enableWithdrawals": False, "enableInternalTransfer": False,
           "ipRestrict": True}
    if rescate_real:
        req["permitsUniversalTransfer"] = True
    return req


def validar(env: Entorno, api_key: str, api_secret: str) -> Resultado:
    """Los chequeos de § 10.2 contra el endpoint que opera. No escribe nada salvo la línea de base de la key actual."""
    ch: Dict[str, Dict[str, str]] = {}
    res = Resultado(ok=False, chequeos=[])

    def fin(motivo_txt: Optional[str] = None) -> Resultado:
        res.chequeos = [ch[k] for k in _ORDEN if k in ch]
        res.ok = motivo_txt is None and not any(c["estado"] == BLOQUEA for c in res.chequeos)
        res.motivo = motivo_txt or next((c["texto"] for c in res.chequeos if c["estado"] == BLOQUEA), None)
        return res

    try:
        validar_formato(api_key, api_secret)
    except ValueError as exc:
        ch["formato"] = _chequeo("formato", BLOQUEA, str(exc))
        return fin()
    ch["formato"] = _chequeo("formato", OK, "Formato: API key y secret de 64 caracteres (clave HMAC, la que sabe "
                                            "firmar el panel)")
    try:
        actual = env.proveedor.actuales()
    except credenciales.SinCredenciales:
        actual = None
    if actual is not None and actual.api_key == api_key:
        ch["distinta"] = _chequeo("distinta", BLOQUEA, "Es la misma API key que ya está configurada.")
        return fin()
    ch["distinta"] = _chequeo("distinta", OK, f"Distinta de la actual ({credenciales.enmascarar(actual.api_key)})"
                              if actual else "No hay una key vigente para comparar")

    cli = env.nuevo_cliente(api_key, api_secret)
    try:
        fut = cli.futuros()
    except ErrorBinance as exc:
        ch["firma"] = _chequeo("firma", BLOQUEA, motivo(exc, env.testnet))
        return fin()
    ch["firma"] = _chequeo("firma", OK, f"Firma en {env.nombre}: la cuenta de futuros responde{_balance(fut)}")

    uid = None
    try:
        uid = cli.spot().get("uid")
        ch["spot"] = _chequeo("spot", OK, f"Spot de {env.nombre.replace('Binance ', '')} responde: el rescate lee "
                                          "ahí el USDT libre")
    except ErrorBinance as exc:
        ch["spot"] = _chequeo("spot", BLOQUEA, f"El spot no responde con esta key (el rescate lee ahí el USDT libre): "
                                               f"{motivo(exc, env.testnet)}")

    # Misma cuenta (decisión 3: otra cuenta bloquea).
    meta, actual_ok = asegurar_linea_base(env)
    base = meta.get("huella_uid")
    clave = huella_clave(env.dir_secretos)
    if uid is not None:
        res.huella = huella(uid, clave)
    if base and res.huella:
        if hmac.compare_digest(base, res.huella):
            ch["cuenta"] = _chequeo("cuenta", OK, "Misma cuenta: el UID coincide con el guardado")
        else:
            res.otra_cuenta = True
            ch["cuenta"] = _chequeo("cuenta", BLOQUEA, "Es otra cuenta: el UID no coincide con el guardado")
            pos, ords = _posiciones_db(env.db_path), _ordenes_db(env.db_path)
            ch["contexto"] = _chequeo("contexto", BLOQUEA, f"La cuenta actual quedaría sin bot: {len(pos)} "
                                      f"{'posición' if len(pos) == 1 else 'posiciones'} y {ords} "
                                      f"{'orden abierta' if ords == 1 else 'órdenes abiertas'}, sin cierres ni promedios")
            ch["historial"] = _chequeo("historial", INFO, "El historial, los KPIs y la base del rescate son de la "
                                                          "cuenta actual")
            return fin()
    else:
        # Sin huella guardada o sin uid: tranId de /fapi/v1/income contra la tabla income (la key actual no responde).
        ids = {str(r[0]) for r in _db_filas(env.db_path, "SELECT tranId FROM income") if r[0] is not None}
        try:
            comunes = ids & set(cli.income()) if ids else set()
        except ErrorBinance:
            comunes = set()
        if comunes:
            ch["cuenta"] = _chequeo("cuenta", OK, f"Misma cuenta: {len(comunes)} movimientos de Binance coinciden con "
                                                  "el historial del panel (no hay huella del UID para comparar)")
        else:
            ch["cuenta"] = _chequeo("cuenta", BLOQUEA, "No se puede saber si es la misma cuenta: no hay huella del UID "
                                                       f"guardada{'' if actual_ok else ', la key actual no responde'} y "
                                                       "no hay movimientos en común con el historial del panel")
            return fin()

    try:
        res.hedge = cli.dual()
    except ErrorBinance as exc:
        ch["hedge"] = _chequeo("hedge", BLOQUEA, f"No se pudo leer el hedge mode: {motivo(exc, env.testnet)}")
        return fin()
    if not res.hedge:
        ch["hedge"] = _chequeo("hedge", BLOQUEA, "La cuenta está en one-way: passivbot necesita hedge mode y Binance no "
                                                 "deja cambiarlo con posiciones abiertas")
        return fin()
    ch["hedge"] = _chequeo("hedge", OK, "Hedge mode activo: passivbot lo necesita para arrancar")

    # La sonda es un POST: solo corre después de ver dualSidePosition true, así que no cambia nada (-4059).
    try:
        code = cli.sonda_operar()
    except ErrorBinance as exc:
        ch["operar"] = _chequeo("operar", BLOQUEA, motivo(exc, env.testnet))
        return fin()
    if code == -4059:
        ch["operar"] = _chequeo("operar", OK, "Permiso de operar futuros: la prueba de hedge mode respondió «ya está "
                                              "en hedge» (−4059)")
    elif code in (-2015, -2014):
        ch["operar"] = _chequeo("operar", BLOQUEA, "La key no tiene permiso de operar futuros (−2015): passivbot "
                                                   "seguiría corriendo sin operar nada")
    else:
        ch["operar"] = _chequeo("operar", BLOQUEA, f"La prueba del permiso de operar respondió {code}: no se puede "
                                                   "usar")

    en_binance, en_panel = _posiciones_binance(fut), _posiciones_db(env.db_path)
    if en_binance == en_panel:
        texto = (f"Posiciones: las mismas {len(en_binance)} que ve el panel ({_pares(en_binance)})" if en_binance
                 else "Posiciones: ninguna abierta, igual que en el panel")
    else:
        texto = (f"Posiciones: Binance muestra {len(en_binance)} y el panel {len(en_panel)} (el scraper tiene hasta "
                 "60 s de atraso)")
    ch["posiciones"] = _chequeo("posiciones", INFO, texto + ". Las órdenes no se comparan: passivbot las rehace en "
                                                            "cada ciclo.")

    if env.testnet:
        try:
            cli.restricciones()
        except ErrorBinance:
            pass
        ch["permisos"] = _chequeo("permisos", INFO, "Retiros y restricción de IP: Binance Demo no los informa (no "
                                                    "tiene /sapi). En real se exigen antes de guardar.")
    else:
        try:
            r = cli.restricciones()
        except ErrorBinance as exc:
            r, err = None, motivo(exc, env.testnet)
        else:
            err = None
        if r is None:
            ch["permisos"] = _chequeo("permisos", BLOQUEA, f"No se pudieron leer los permisos de la key"
                                                           f"{': ' + err if err else ''}")
        else:
            problemas = [f"{k} debería ser {str(v).lower()}" for k, v in _requeridos(env.rescate_real).items()
                         if r.get(k) is not v]
            res.permisos = {"problemas": problemas}
            ch["permisos"] = (_chequeo("permisos", BLOQUEA, "Permisos de la key: " + "; ".join(problemas)) if problemas
                              else _chequeo("permisos", OK, "Permisos: lectura y futuros, sin retiros ni transferencia "
                                                            "interna, con restricción de IP"))
    return fin()


def revalidar(env: Entorno, api_key: str, api_secret: str, huella_esperada: Optional[str]) -> None:
    """En el job, con el lock: misma cuenta y hedge mode (sin la sonda, para no alargar el lock)."""
    cli = env.nuevo_cliente(api_key, api_secret)
    try:
        if huella_esperada:
            uid = cli.spot().get("uid")
            if uid is None or not hmac.compare_digest(huella(uid, huella_clave(env.dir_secretos)), huella_esperada):
                raise CambioError("La key ya no responde con la misma cuenta: no se cambió nada.", 409)
        if not cli.dual():
            raise CambioError("La cuenta dejó de estar en hedge mode: no se cambió nada.", 409)
    except ErrorBinance as exc:
        raise CambioError(f"{motivo(exc, env.testnet)} No se cambió nada.", 502) from None


# --------------------------------------------------------------------------------------
# Pendientes (memoria del proceso: 5 min, un solo uso) y límite de validaciones
# --------------------------------------------------------------------------------------


class PendienteError(Exception):
    """Vencido, usado o de otro usuario: 409."""


@dataclasses.dataclass
class Pendiente:
    id: str
    usuario: str
    api_key: str = dataclasses.field(repr=False)
    api_secret: str = dataclasses.field(repr=False)
    key_id: str
    enmascarada: str
    huella: Optional[str]
    archivo_sha: str
    creado: float
    origen: str = "nueva"
    hedge: Optional[bool] = None
    permisos: Optional[Dict[str, Any]] = None
    chequeos: List[Dict[str, str]] = dataclasses.field(default_factory=list)


class Pendientes:
    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = PENDIENTE_TTL_S) -> None:
        self._items: Dict[str, Pendiente] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._ttl = ttl

    def _purgar(self) -> None:
        ahora = self._clock()
        for pid, p in list(self._items.items()):
            if ahora - p.creado > self._ttl:
                del self._items[pid]

    def crear(self, **kw: Any) -> Pendiente:
        with self._lock:
            self._purgar()
            # Uno por usuario: una validación nueva reemplaza a la anterior.
            for pid, p in list(self._items.items()):
                if p.usuario == kw["usuario"]:
                    del self._items[pid]
            p = Pendiente(id=secrets.token_hex(16), creado=self._clock(), **kw)
            self._items[p.id] = p
            return p

    def _buscar(self, pid: Any, usuario: str) -> Pendiente:
        if not isinstance(pid, str):
            raise PendienteError("Falta la validación.")
        p = self._items.get(pid)
        if p is None:
            raise PendienteError("La validación venció o ya se usó: validá la key de nuevo.")
        if self._clock() - p.creado > self._ttl:
            del self._items[pid]
            raise PendienteError("La validación venció (5 minutos): validá la key de nuevo.")
        if p.usuario != usuario:
            raise PendienteError("La validación es de otro usuario: validá la key de nuevo.")
        return p

    def ver(self, pid: Any, usuario: str) -> Pendiente:
        with self._lock:
            return self._buscar(pid, usuario)

    def tomar(self, pid: Any, usuario: str) -> Pendiente:
        """Un solo uso: lo saca del registro bajo el lock."""
        with self._lock:
            p = self._buscar(pid, usuario)
            del self._items[pid]
            return p

    def devolver(self, p: Pendiente) -> None:
        """Si el job no arrancó (otra acción en curso), vuelve a estar disponible hasta que venza."""
        with self._lock:
            self._items[p.id] = p


class Limitador:
    """5 validaciones cada 15 min por usuario. En memoria, como el del login: un reinicio lo pone en cero."""

    def __init__(self, maximo: int = VALIDACIONES_MAX, ventana: float = VALIDACIONES_VENTANA_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.maximo, self.ventana, self._clock = maximo, ventana, clock
        self._intentos: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def intentar(self, usuario: str) -> Optional[int]:
        """None si se puede; si no, los segundos que faltan."""
        ahora = self._clock()
        with self._lock:
            recientes = [t for t in self._intentos.get(usuario, []) if ahora - t < self.ventana]
            if len(recientes) >= self.maximo:
                self._intentos[usuario] = recientes
                return max(1, int(self.ventana - (ahora - recientes[0])))
            recientes.append(ahora)
            self._intentos[usuario] = recientes
            return None


# --------------------------------------------------------------------------------------
# Plan, job y espera
# --------------------------------------------------------------------------------------


def armar_plan(env: Entorno, p: Pendiente, corriendo: Optional[bool]) -> Tuple[Dict[str, Any], str]:
    """El diff y los avisos (mockup ``bot-key-confirmar``). Sin key, secret ni uid."""
    try:
        actual = env.proveedor.actuales()
        antes = credenciales.enmascarar(actual.api_key)
        kid_actual = actual.key_id
    except credenciales.SinCredenciales:
        antes, kid_actual = "—", None
    usuario = env.proveedor.estado().get("usuario") or credenciales.USUARIO_DEFECTO
    entorno = "Binance Demo (testnet)" if env.testnet else "Binance real"
    cambios = [
        {"que": "API key", "nota": "primeros y últimos 4", "antes": antes, "despues": p.enmascarada, "clase": "key"},
        {"que": "Secret", "antes": "guardado", "despues": "cambia · no se muestra ninguno de los dos", "clase": "cambia"},
        {"que": "Cuenta de Binance", "antes": "UID verificado" if p.huella else "sin huella del UID",
         "despues": "la misma" + (" (UID igual)" if p.huella else " (movimientos en común)"), "clase": "igual"},
        {"que": "Entorno", "antes": entorno, "despues": "igual", "clase": "igual"},
        {"que": "Dónde se escribe", "nota": f"secretos/api-keys.json, usuario {usuario}", "antes": "key y secret",
         "despues": "key y secret nuevos · testnet y las otras entradas, iguales", "clase": "cambia"},
    ]
    avisos = []
    if corriendo:
        avisos.append({"tipo": "info", "titulo": "passivbot está corriendo: se reinicia.",
                       "texto": "docker restart; forager tarda ~90 s en volver a operar. Las posiciones y las órdenes "
                                "siguen en Binance y passivbot las retoma con la key nueva."})
    elif corriendo is False:
        avisos.append({"tipo": "info", "titulo": "passivbot está detenido: no se reinicia.",
                       "texto": "Usa la key nueva cuando lo inicies."})
    else:
        avisos.append({"tipo": "aviso", "titulo": "No se pudo ver el estado de passivbot.",
                       "texto": "El job lo vuelve a consultar antes de escribir."})
    avisos.append({"tipo": "info", "titulo": "Panel, scraper y rescate cambian solos.",
                   "texto": "Toman la key nueva en su próxima vuelta: el rescate en ~15 s, el scraper en ~60 s. El tope "
                            "de pérdida no se queda sin key: las dos son de la misma cuenta."})
    avisos.append({"tipo": "aviso", "titulo": "No borres la key vieja en Binance todavía.",
                   "texto": "Borrala cuando Bot diga \"4 de 4 con esta key\": panel, scraper y rescate firmando con "
                            "ella y passivbot creando órdenes. Hasta entonces, alguno puede seguir usándola."})
    pasos = [
        "Validar de nuevo con Binance (misma cuenta, hedge mode) y que el archivo no haya cambiado desde que abriste esto.",
        "Respaldo del archivo actual: modo 600, en un directorio que solo monta el panel.",
        f"Escribir el archivo nuevo: temporal, relectura y reemplazo atómico. Solo cambian key y secret de {usuario}.",
        "Reiniciar passivbot y verificar que quedó corriendo. Si Docker lo rechaza, se restaura el respaldo.",
        "Soltar el lock de acciones y recién ahí esperar a los 4 (huella en los latidos y órdenes nuevas de passivbot). "
        "Esa espera se puede cortar: un Apagar no queda esperando.",
        "Queda en el historial de acciones y avisa por Telegram, con la key enmascarada.",
    ]
    titulo = "Volver a la API key anterior" if p.origen == "volver" else "Cambiar la API key"
    plan = {"titulo": titulo, "origen": p.origen, "cambios": cambios, "avisos": avisos, "pasos": pasos,
            "casilla": ("Entiendo que passivbot se reinicia y que la key vieja sigue activa en Binance hasta que yo la "
                        "borre." if corriendo is not False else
                        "Entiendo que la key nueva se usa cuando inicie passivbot y que la vieja sigue activa en "
                        "Binance hasta que yo la borre.")}
    clave = json.dumps({"pendiente": p.id, "actual": kid_actual, "nuevo": p.key_id, "archivo": p.archivo_sha,
                        "corriendo": corriendo}, sort_keys=True)
    plan_hash = hashlib.sha256(clave.encode("utf-8")).hexdigest()[:32]
    texto = f"{titulo}: {antes} → {p.enmascarada}. " + " ".join(a["titulo"] for a in avisos)
    return {**plan, "texto": texto}, plan_hash


def params_audit(p: Pendiente, testnet: bool) -> Dict[str, Any]:
    """Lo único que va a ``bot_actions.log``, ``bot_job_last.json``, el job y Telegram (spec § 10.4, paso 6)."""
    out = {"key": p.enmascarada, "cuenta": "misma", "entorno": "demo" if testnet else "real"}
    if p.origen == "volver":
        out["volver"] = True
    return out


def aplicar(env: Entorno, settings: bot_control.Settings, p: Pendiente, usuario_panel: str,
            rep: bot_control.Reporter, docker: Optional[bot_control.DockerClient] = None) -> Dict[str, Any]:
    """El job, con el lock de acciones (spec § 10.4, paso 5)."""
    usuario = env.proveedor.estado().get("usuario") or credenciales.usuario_passivbot()
    rep.step("revalidar", bot_control.PASO_EN_CURSO, "misma cuenta, hedge mode y el archivo")
    try:
        original = leer_archivo(env.dir_secretos)
    except OSError as exc:
        raise CambioError(f"No se pudo leer el archivo de credenciales ({exc.__class__.__name__}): no se cambió nada.",
                          500) from None
    if not hmac.compare_digest(sha(original), p.archivo_sha):
        raise CambioError("El archivo de credenciales cambió desde la validación: validá la key de nuevo.", 409)
    revalidar(env, p.api_key, p.api_secret, p.huella)
    rep.step("revalidar", bot_control.PASO_OK, "misma cuenta y hedge mode; el archivo no cambió")
    docker = docker or bot_control.DockerClient(settings.docker_url)
    info = bot_control._inspect_existing(docker, settings.container)  # antes de escribir: sin contenedor, no se toca

    rep.step("respaldo", bot_control.PASO_EN_CURSO)
    respaldo = respaldar(env.dir_secretos, env.dir_respaldo, original)
    rep.step("respaldo", bot_control.PASO_OK, f"{respaldo.name} (600, en el directorio de respaldos)")

    anterior = None
    try:
        anterior = credenciales.leer_entrada(original, usuario)[0]
    except ValueError:
        pass
    rep.step("escribir", bot_control.PASO_EN_CURSO)
    escribir(env.dir_secretos, original, usuario, p.api_key, p.api_secret)
    escrito_en = time.time()
    rep.step("escribir", bot_control.PASO_OK, f"key {p.enmascarada} y secret nuevos de {usuario}; lo demás igual")

    accion, desde = None, None
    if info.get("running"):
        rep.step("docker", bot_control.PASO_EN_CURSO, f"Reiniciando {settings.container}")
        try:
            docker.restart(settings.container)
        except bot_control.DockerError as exc:
            if exc.uncertain:
                err = bot_control.DockerError(f"{exc} Resultado incierto: la key nueva quedó escrita (respaldo "
                                              f"{respaldo.name}). Revisá Bot → Conexión con Binance antes de reintentar.")
                err.uncertain = True
                raise err from None
            try:
                restaurar(env.dir_secretos, respaldo)
            except OSError as restore_exc:
                raise bot_control.DockerError(f"{exc} ATENCIÓN: no se pudo restaurar la key anterior "
                                              f"({restore_exc.__class__.__name__}); respaldo {respaldo.name}.") from None
            raise bot_control.DockerError(f"{exc} Se restauró la key anterior desde el respaldo: no cambió nada.") \
                from None
        accion = "restart"
        rep.step("docker", bot_control.PASO_OK, "docker restart")
        rep.step("verificar", bot_control.PASO_EN_CURSO)
        info2 = docker.inspect(settings.container)
        if not info2.get("running"):
            raise bot_control.DockerError(f"passivbot no quedó en ejecución tras el reinicio (estado "
                                          f"{info2.get('status')}). La key nueva ya quedó escrita: revisá sus logs.")
        desde = _ts_docker(info2.get("started_at"))
        if desde is not None and desde + SKEW_S < escrito_en:
            raise bot_control.DockerError("passivbot no se reinició después de escribir la key (StartedAt anterior). "
                                          "La key nueva ya quedó escrita: revisá Bot → Conexión con Binance.")
        rep.step("verificar", bot_control.PASO_OK, f"{settings.container}: running"
                 + (f" desde {dt.datetime.fromtimestamp(desde, tz=dt.timezone.utc):%H:%M:%S} UTC" if desde else ""))
    else:
        rep.step("docker", bot_control.PASO_OMITIDO, "passivbot está detenido: usa la key nueva al iniciar")
        rep.step("verificar", bot_control.PASO_OMITIDO, "passivbot está detenido")

    meta = credenciales.leer_meta(env.data_dir)
    ahora = ahora_iso()
    nueva_meta = {"key": p.enmascarada, "key_id": p.key_id, "huella_uid": p.huella or meta.get("huella_uid"),
                  "linea_base_en": meta.get("linea_base_en"), "configurada_en": ahora,
                  "configurada_por": usuario_panel, "validada_en": ahora, "hedge": p.hedge,
                  "permisos": p.permisos, "entorno": "demo" if env.testnet else "real",
                  "anterior": {"key": credenciales.enmascarar(anterior), "key_id": credenciales.key_id(anterior)}
                  if anterior else None}
    try:
        credenciales.guardar_meta(env.data_dir, nueva_meta)
    except OSError:
        log.exception("No se pudo guardar credenciales_meta.json")
    return {"ok": True, "key": p.enmascarada, "cuenta": "misma", "entorno": "demo" if env.testnet else "real",
            "docker_action": accion, "respaldo": respaldo.name,
            "cambios_texto": f"API key {credenciales.enmascarar(anterior) or '—'} → {p.enmascarada}",
            "passivbot_desde": desde, "key_id": p.key_id}


def _ts_docker(valor: Any) -> Optional[float]:
    from futuresboard import conexion

    return conexion._ts(valor)


def _ultima_orden_ms(db_path: str) -> Optional[int]:
    filas = _db_filas(db_path, "SELECT MAX(time) FROM orders")
    return int(filas[0][0]) if filas and filas[0][0] is not None else None


def consumidores_con(env: Entorno, kid: str, passivbot_desde: Optional[float]) -> Dict[str, bool]:
    """Quién ya usa la key ``kid``: panel (este proceso), scraper y rescate (``key_id`` del latido) y passivbot (una
    orden creada después de su arranque)."""
    from futuresboard import rescate
    from futuresboard import scraper

    out = {"panel": env.proveedor.estado().get("key_id") == kid}
    out["scraper"] = scraper.read_latido(env.db_path).get("key_id") == kid
    try:
        servicio = (rescate.load_state(env.data_dir)[0] or {}).get("servicio") or {}
    except Exception:  # noqa: BLE001
        servicio = {}
    out["rescate"] = isinstance(servicio, dict) and servicio.get("key_id") == kid
    if passivbot_desde is not None:
        orden = _ultima_orden_ms(env.db_path)
        out["passivbot"] = orden is not None and orden / 1000.0 > passivbot_desde
    return out


def esperar_consumidores(env: Entorno, job: Any, payload: Dict[str, Any], segundos: Optional[int] = None,
                         tick: float = ESPERA_TICK_S) -> Dict[str, Any]:
    """Sin el lock y cancelable (``job.cancel``): espera a que los 4 tomen la key nueva. No revierte nada: si alguno no
    llega a tiempo, termina con avisos (Marcos pudo haber borrado ya la vieja)."""
    kid = payload.get("key_id")
    total = espera_segundos() if segundos is None else max(0, int(segundos))
    desde = payload.get("passivbot_desde")
    nombres = {"panel": "panel", "scraper": "scraper", "rescate": "rescate", "passivbot": "passivbot"}
    inicio = time.monotonic()
    job.step("consumidores", bot_control.PASO_EN_CURSO, "Esperando a que tomen la key nueva.", 0, max(total, 1))
    while True:
        estado = consumidores_con(env, kid, desde)
        faltan = [nombres[k] for k, v in estado.items() if not v]
        n, t = len(estado) - len(faltan), len(estado)
        if not faltan:
            extra = "" if desde is not None else " (passivbot detenido: la usa al iniciar)"
            job.step("consumidores", bot_control.PASO_OK, f"{n} de {t} con la key nueva{extra}: ya podés borrar la "
                                                          "vieja en Binance.", max(total, 1), max(total, 1))
            return payload
        transcurrido = time.monotonic() - inicio
        if transcurrido >= total:
            aviso = (f"Sin confirmar a tiempo: {', '.join(faltan)}. No borres la key vieja en Binance hasta ver "
                     "\"4 de 4\" en Bot → Conexión con Binance.")
            job.step("consumidores", bot_control.PASO_ERROR, f"{n} de {t} con la key nueva. {aviso}")
            return {**payload, "warning": aviso}
        job.step("consumidores", bot_control.PASO_EN_CURSO, f"{n} de {t} con la key nueva · faltan: {', '.join(faltan)}",
                 int(transcurrido), max(total, 1))
        cancel = getattr(job, "cancel", None)
        pausa = min(tick, max(0.0, total - transcurrido))
        if cancel is not None and cancel.wait(pausa):
            job.step("consumidores", bot_control.PASO_OMITIDO, "Espera interrumpida por otra acción del panel.")
            return {**payload, "warning": "La espera a los consumidores se cortó: revisá Bot → Conexión con Binance "
                                          "antes de borrar la key vieja."}
        if cancel is None:
            time.sleep(pausa)
