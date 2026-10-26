"""API key de Binance: un proveedor por proceso que relee ``/secretos/api-keys.json`` cuando cambia.

Spec bot-estrategia § 10.3. Panel, scraper y rescate firman con lo que devuelve :meth:`Proveedor.actuales`, que se
pide **en cada request** (nunca se guardan key y secret al crear un cliente). Así, después de un cambio de key, el
Apagar del tope, el de C3 y las velas usan la key nueva en su próximo pedido, igual que el latido.

* *Qué lee*: ``<dir>/api-keys.json`` (formato de passivbot), usuario ``user`` de ``new.json`` (``binance_01``). El
  directorio sale de ``FUTURESBOARD_SECRETOS_DIR`` (default ``/secretos``).
* *Cuándo relee*: antes de cada uso compara ``(st_ino, st_mtime_ns, st_size)`` del archivo; si cambió, lo relee.
* *Al releer*: valida el formato y registra key y secret en ``logs.register_secret`` **antes** de usarlos. Si el
  archivo es inválido (o falta), sigue con la última key válida y lo informa en ``estado()["error"]`` (va al latido).
* *Transición*: si el directorio no existe, usa ``API_KEY`` / ``API_SECRET`` de ``config.json`` (leídos en vivo de la
  config del proceso, que puede ser el ``app.config`` de Flask o el ``Config`` del rescate).
* ``key_id``: 12 hex del sha256 de la **API key** (nunca del secret). ``key_ok_at``: última firma OK con esa key.

Este módulo no conoce Flask ni a Binance: los clientes (``bot_control``, ``rescate``, ``scraper``) le piden la key
y le avisan cuando una firma salió bien (:meth:`Proveedor.firma_ok`).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import pathlib
import re
import tempfile
import threading
import time
from typing import Any
from typing import Callable
from typing import Dict
from typing import Optional
from typing import Tuple

from futuresboard import logs

ENV_DIR = "FUTURESBOARD_SECRETOS_DIR"
DIR_DEFECTO = "/secretos"
ENV_RESPALDO_DIR = "FUTURESBOARD_SECRETOS_RESPALDO_DIR"
RESPALDO_DIR_DEFECTO = "/secretos-respaldo"
ENV_USUARIO = "FUTURESBOARD_PASSIVBOT_USER"
ENV_FORAGER_CONFIG = "FUTURESBOARD_FORAGER_CONFIG"  # el mismo de bot_control (sin importarlo: bot_control importa este)
USUARIO_DEFECTO = "binance_01"
ARCHIVO = "api-keys.json"
EXTENSION = "futuresboard_credenciales"

ORIGEN_SECRETOS = "secretos"
ORIGEN_CONFIG = "config"
ORIGEN_FIJA = "fija"

# Lo que se acepta al LEER el archivo (lo que ya usa passivbot). La validación de una key nueva desde el panel es más
# estricta (64 caracteres, HMAC): ver futuresboard.cambio_key.
_FORMATO_ARCHIVO = re.compile(r"^[A-Za-z0-9]{16,128}$")

log = logging.getLogger(__name__)


class SinCredenciales(Exception):
    """No hay key ni secret para firmar (ni en el archivo ni en config.json). El mensaje nunca lleva valores."""


def key_id(api_key: str) -> str:
    """Huella corta de la API key para latidos y vistas: 12 hex del sha256. Nunca se calcula sobre el secret."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def enmascarar(api_key: Optional[str]) -> Optional[str]:
    """Primeros 4 + … + últimos 4. Con menos de 12 caracteres no se muestra nada de la key."""
    if not isinstance(api_key, str) or not api_key:
        return None
    if len(api_key) < 12:
        return "…"
    return f"{api_key[:4]}…{api_key[-4:]}"


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat(timespec="seconds")


def directorio() -> pathlib.Path:
    return pathlib.Path(os.environ.get(ENV_DIR, "").strip() or DIR_DEFECTO)


def directorio_respaldo() -> pathlib.Path:
    return pathlib.Path(os.environ.get(ENV_RESPALDO_DIR, "").strip() or RESPALDO_DIR_DEFECTO)


def usuario_passivbot(forager_config: Optional[pathlib.Path] = None) -> str:
    """Usuario de passivbot: ``user`` de ``new.json`` si se puede leer; si no, ``FUTURESBOARD_PASSIVBOT_USER``; si
    no, ``binance_01``. El scraper no monta ``/forager``: usa el env (o el default)."""
    if forager_config is None:
        raw = os.environ.get(ENV_FORAGER_CONFIG, "").strip()
        forager_config = pathlib.Path(raw) if raw else None
    if forager_config is not None:
        try:
            import hjson

            cfg = hjson.loads(forager_config.read_text(encoding="utf-8"))
            user = cfg.get("user") if isinstance(cfg, dict) else None
            if isinstance(user, str) and user.strip():
                return user.strip()
        except Exception:  # noqa: BLE001 - un new.json ilegible no deja al proceso sin key
            pass
    return os.environ.get(ENV_USUARIO, "").strip() or USUARIO_DEFECTO


class Credencial:
    """Key y secret con los que se firma un pedido. ``repr`` y ``str`` nunca muestran los valores."""

    __slots__ = ("api_key", "api_secret", "key_id", "origen")

    def __init__(self, api_key: str, api_secret: str, origen: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.key_id = key_id(api_key)
        self.origen = origen

    def __repr__(self) -> str:
        return f"Credencial(key_id={self.key_id}, origen={self.origen})"

    __str__ = __repr__

    def igual(self, otra: Optional["Credencial"]) -> bool:
        return otra is not None and otra.api_key == self.api_key and otra.api_secret == self.api_secret


def leer_entrada(data: bytes, usuario: str) -> Tuple[str, str, Optional[bool]]:
    """Key, secret y ``testnet`` del usuario en un ``api-keys.json``. ``ValueError`` (sin valores) si no sirve."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("no es JSON válido") from exc
    if not isinstance(obj, dict):
        raise ValueError("no es un objeto JSON")
    entrada = obj.get(usuario)
    if not isinstance(entrada, dict):
        raise ValueError(f"no tiene el usuario {usuario}")
    exchange = entrada.get("exchange")
    if exchange is not None and exchange != "binance":
        raise ValueError(f"el usuario {usuario} no es de Binance")
    key, secret = entrada.get("key"), entrada.get("secret")
    for nombre, valor in (("key", key), ("secret", secret)):
        if not isinstance(valor, str) or not _FORMATO_ARCHIVO.match(valor):
            raise ValueError(f"{nombre} de {usuario} con formato inválido")
    testnet = entrada.get("testnet")
    return key, secret, testnet if isinstance(testnet, bool) else None


class Fijas:
    """Key y secret fijos, con la misma interfaz que :class:`Proveedor` (tests, validación de una key nueva)."""

    def __init__(self, api_key: Any, api_secret: Any) -> None:
        ok = isinstance(api_key, str) and isinstance(api_secret, str) and api_key and api_secret
        self._cred = Credencial(api_key, api_secret, ORIGEN_FIJA) if ok else None

    def actuales(self) -> Credencial:
        if self._cred is None:
            raise SinCredenciales("Faltan API_KEY y API_SECRET.")
        return self._cred

    def disponible(self) -> bool:
        return self._cred is not None

    def firma_ok(self, cred: Credencial) -> None:
        return None


class Proveedor:
    """La key del proceso. Thread-safe: el panel firma desde los hilos de los jobs y desde las requests."""

    def __init__(self, dir_secretos: Optional[pathlib.Path], *,
                 respaldo: Optional[Callable[[], Tuple[Any, Any]]] = None,
                 usuario: Optional[Callable[[], str]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.dir = pathlib.Path(dir_secretos) if dir_secretos is not None else None
        self._respaldo = respaldo
        self._usuario_fn = usuario or usuario_passivbot
        self._clock = clock
        self._lock = threading.RLock()
        self._actual: Optional[Credencial] = None
        self._firma: Optional[Tuple[int, int, int]] = None
        self._error: Optional[str] = None
        self._ok_at: Dict[str, float] = {}
        self._archivo_mtime: Optional[float] = None
        self._testnet: Optional[bool] = None
        self._usuario: Optional[str] = None
        self._cambios = 0

    # -- lectura ----------------------------------------------------------------------
    @property
    def archivo(self) -> Optional[pathlib.Path]:
        return self.dir / ARCHIVO if self.dir is not None else None

    def modo_secretos(self) -> bool:
        """True si existe el directorio de credenciales (si no, la key sale de config.json)."""
        return self.dir is not None and self.dir.is_dir()

    def actuales(self) -> Credencial:
        """Key y secret para UN pedido (leer una vez por request y usar ese objeto para firmar y para el header)."""
        with self._lock:
            self._revisar()
            if self._actual is None:
                raise SinCredenciales(self._error or "Faltan API_KEY y API_SECRET (ni en el archivo de credenciales "
                                                     "ni en config.json).")
            return self._actual

    def disponible(self) -> bool:
        try:
            self.actuales()
        except SinCredenciales:
            return False
        return True

    def firma_ok(self, cred: Credencial) -> None:
        """Un pedido firmado con ``cred`` respondió bien: es su ``key_ok_at``."""
        with self._lock:
            self._ok_at[cred.key_id] = self._clock()

    def estado(self) -> Dict[str, Any]:
        """Para latidos y vistas: nunca lleva key ni secret."""
        with self._lock:
            self._revisar()
            actual = self._actual
            kid = actual.key_id if actual else None
            return {
                "key_id": kid,
                "key_ok_at": iso(self._ok_at.get(kid)) if kid else None,
                "key_error": self._error,
                "key_origen": actual.origen if actual else None,
                "usuario": self._usuario,
                "testnet": self._testnet,
                "archivo_mtime": self._archivo_mtime,
            }

    def enmascarada(self) -> Optional[str]:
        """La key vigente enmascarada (4 + … + 4), para vistas. Nunca el secret."""
        with self._lock:
            self._revisar()
            return enmascarar(self._actual.api_key) if self._actual else None

    def latido(self) -> Dict[str, Any]:
        e = self.estado()
        return {"key_id": e["key_id"], "key_ok_at": e["key_ok_at"], "key_error": e["key_error"]}

    # -- interno ----------------------------------------------------------------------
    def _revisar(self) -> None:
        if not self.modo_secretos():
            self._error = None
            self._firma = None
            self._archivo_mtime = None
            self._testnet = None
            self._desde_config()
            return
        path = self.archivo
        assert path is not None
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError as exc:
            falta = isinstance(exc, FileNotFoundError)
            self._firma = None
            self._fallo(f"falta {ARCHIVO} en {self.dir}" if falta else
                        f"no se pudo leer {ARCHIVO} ({exc.__class__.__name__})")
            return
        try:
            st = os.fstat(fd)
            firma = (st.st_ino, st.st_mtime_ns, st.st_size)
            if firma == self._firma:
                return
            partes = []
            while True:
                bloque = os.read(fd, 65536)
                if not bloque:
                    break
                partes.append(bloque)
        except OSError as exc:
            self._fallo(f"no se pudo leer {ARCHIVO} ({exc.__class__.__name__})")
            return
        finally:
            os.close(fd)
        self._firma = firma
        usuario = self._usuario_fn()
        try:
            key, secret, testnet = leer_entrada(b"".join(partes), usuario)
        except ValueError as exc:
            self._fallo(f"{ARCHIVO} inválido: {exc}")
            return
        self._usuario = usuario
        self._archivo_mtime = st.st_mtime
        self._testnet = testnet
        self._error = None
        self._cambiar(Credencial(key, secret, ORIGEN_SECRETOS))

    def _fallo(self, motivo: str) -> None:
        """Archivo inválido o ausente: se sigue con la última key válida; sin ninguna, con config.json."""
        if self._actual is not None and self._actual.origen == ORIGEN_SECRETOS:
            texto = f"{motivo}; se sigue con la última key válida ({self._actual.key_id})."
        else:
            texto = f"{motivo}; se usa la key de config.json si la hay."
            self._desde_config()
        if texto != self._error:
            log.error("Credenciales: %s", texto)
        self._error = texto

    def _desde_config(self) -> None:
        key, secret = self._respaldo() if self._respaldo is not None else ("", "")
        if isinstance(key, str) and isinstance(secret, str) and key.strip() and secret.strip():
            self._cambiar(Credencial(key.strip(), secret.strip(), ORIGEN_CONFIG))
        elif self._actual is not None and self._actual.origen == ORIGEN_CONFIG:
            self._actual = None  # sacaron la key de la config (o nunca hubo): no se firma con una vieja

    def _cambiar(self, nueva: Credencial) -> None:
        if nueva.igual(self._actual):
            if self._actual is not None and self._actual.origen != nueva.origen:
                self._actual = nueva
            return
        # Antes del primer uso: desde acá cualquier log que la nombre sale tapado.
        logs.register_secret(nueva.api_key)
        logs.register_secret(nueva.api_secret)
        anterior = self._actual
        self._actual = nueva
        self._cambios += 1
        if anterior is not None and anterior.key_id != nueva.key_id:
            log.warning("Credenciales: la API key cambió (%s -> %s, desde %s).", anterior.key_id, nueva.key_id,
                        nueva.origen)
        elif anterior is None:
            log.info("Credenciales: API key %s (desde %s).", nueva.key_id, nueva.origen)


def configurar_app(app: Any) -> Proveedor:
    """El proveedor del proceso para una app de Flask (panel o loop del scraper). El respaldo lee ``app.config`` en
    vivo: el hilo de un job no tiene contexto de Flask y no puede usar ``current_app``."""
    config = app.config

    def respaldo() -> Tuple[Any, Any]:
        return config.get("API_KEY"), config.get("API_SECRET")

    prov = Proveedor(directorio(), respaldo=respaldo)
    app.extensions[EXTENSION] = prov
    return prov


def de_app(app: Any) -> Proveedor:
    """El proveedor de la app (lo crea si la app se armó sin él, como algunas apps de prueba)."""
    prov = app.extensions.get(EXTENSION)
    if prov is None:
        prov = configurar_app(app)
    return prov


def para_config(config: Any) -> Proveedor:
    """El proveedor de un proceso sin Flask (el rescate), con ``config.json`` de respaldo."""

    def respaldo() -> Tuple[Any, Any]:
        return getattr(config, "API_KEY", None), getattr(config, "API_SECRET", None)

    return Proveedor(directorio(), respaldo=respaldo)


# --------------------------------------------------------------------------------------
# credenciales_meta.json (directorio de estado): lo que el panel sabe de la key vigente, sin secretos
# --------------------------------------------------------------------------------------

META_FILE = "credenciales_meta.json"
# Campos que puede tener (nunca key, secret ni uid): key enmascarada, key_id, huella del uid (HMAC con una clave que vive
# en /secretos) y el id de esa clave, cuándo y quién la configuró, última validación, hedge mode, permisos (solo en
# real) y entorno.
META_CAMPOS = ("key", "key_id", "huella_uid", "huella_clave_id", "linea_base_en", "configurada_en", "configurada_por",
               "validada_en", "hedge", "permisos", "entorno", "anterior")
# Leer-modificar-guardar de la meta (la línea de base desde un GET y el cierre del job corren en hilos distintos).
META_LOCK = threading.RLock()


def leer_meta(data_dir: pathlib.Path) -> Dict[str, Any]:
    try:
        data = json.loads((pathlib.Path(data_dir) / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: data.get(k) for k in META_CAMPOS if k in data} if isinstance(data, dict) else {}


def guardar_meta(data_dir: pathlib.Path, meta: Dict[str, Any]) -> None:
    """Escritura atómica, 600. Solo los campos conocidos: nada que llegue de un request pasa sin filtrar.

    Un temporal propio por llamada (``mkstemp``): gunicorn corre varios hilos en un proceso y el GET de la línea de base
    y el job pueden guardar a la vez. Con un temporal fijo por pid los dos lo truncaban y el archivo quedaba mezclado.
    """
    path = pathlib.Path(data_dir) / META_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{META_FILE}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({k: meta.get(k) for k in META_CAMPOS if k in meta}, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
