"""Proxy minimo de la Docker Engine API para el panel de passivbot.

Escucha HTTP (por defecto :2375) y reenvia al socket unix de Docker SOLO estas operaciones, y
solo para el contenedor configurado en ``PROXY_CONTAINER`` (nombre exacto):

* ``GET  [/v1.NN]/containers/{name}/json``                (respuesta filtrada a Id/Name/State)
* ``POST [/v1.NN]/containers/{name}/start``               (sin query)
* ``POST [/v1.NN]/containers/{name}/stop[?t=N]``          (0 <= N <= PROXY_MAX_STOP_T)
* ``POST [/v1.NN]/containers/{name}/restart[?t=N]``       (0 <= N <= PROXY_MAX_STOP_T)

Todo lo demas responde 403 sin tocar Docker. Sin dependencias fuera de la stdlib.

Variables de entorno:

* ``PROXY_CONTAINER`` (obligatoria): nombre del contenedor, ej. ``client17-passivbot``.
* ``PROXY_SOCKET`` (``/var/run/docker.sock``): socket unix de Docker.
* ``PROXY_LISTEN_HOST`` (``0.0.0.0``) / ``PROXY_LISTEN_PORT`` (``2375``).
* ``PROXY_MAX_STOP_T`` (``120``): maximo permitido para ``t`` en stop/restart.
* ``PROXY_UPSTREAM_TIMEOUT`` (``10``): timeout en segundos hacia Docker (stop/restart suman ``t``).
* ``PROXY_MAX_CONCURRENCY`` (``8``): requests simultaneos; el excedente recibe 503.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Optional
from typing import Tuple

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# Charset permitido en el request-target completo: sin '%', '&', '#', espacios ni no-ASCII.
TARGET_CHARS_RE = re.compile(r"^[A-Za-z0-9/._?=-]+$")
PATH_RE = re.compile(
    r"^(?P<version>/v1\.[0-9]{1,3})?/containers/(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"/(?P<action>json|start|stop|restart)$"
)
T_QUERY_RE = re.compile(r"^t=(?P<t>[0-9]{1,4})$")

METHOD_FOR_ACTION = {"json": "GET", "start": "POST", "stop": "POST", "restart": "POST"}
INSPECT_KEYS = ("Id", "Name", "State")

MAX_TARGET_LENGTH = 256
MAX_RESPONSE_BYTES = 1024 * 1024
CLIENT_READ_TIMEOUT = 10


@dataclass(frozen=True)
class Config:
    container: str
    socket_path: str = "/var/run/docker.sock"
    max_stop_t: int = 120
    upstream_timeout: float = 10.0
    max_concurrency: int = 8

    def __post_init__(self) -> None:
        if not NAME_RE.match(self.container or ""):
            raise ValueError("PROXY_CONTAINER vacio o invalido")
        if self.max_stop_t < 0 or self.upstream_timeout <= 0 or self.max_concurrency < 1:
            raise ValueError("limites invalidos")


class Denied(Exception):
    def __init__(self, reason: str, status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Allowed:
    method: str
    action: str
    upstream_target: str
    t: Optional[int]


def check_request(method: str, target: str, headers, cfg: Config) -> Allowed:
    """Valida un request entrante. Devuelve el request a reenviar o levanta ``Denied``."""
    if len(target) > MAX_TARGET_LENGTH:
        raise Denied("target demasiado largo")
    if not TARGET_CHARS_RE.match(target):
        raise Denied("caracteres no permitidos en el path")
    if target.count("?") > 1:
        raise Denied("query invalida")
    path, sep, query = target.partition("?")

    m = PATH_RE.match(path)
    if not m:
        raise Denied("ruta no permitida")
    if m.group("name") != cfg.container:
        raise Denied("contenedor no permitido")
    action = m.group("action")
    if method != METHOD_FOR_ACTION[action]:
        raise Denied("metodo no permitido para esta ruta")

    t: Optional[int] = None
    if sep:
        if action not in ("stop", "restart"):
            raise Denied("query no permitida")
        qm = T_QUERY_RE.match(query)
        if not qm:
            raise Denied("query no permitida")
        t = int(qm.group("t"))
        if t > cfg.max_stop_t:
            raise Denied("t fuera de rango")

    # Sin body: ni chunked ni Content-Length distinto de 0 (requests manda "0" en POST vacio).
    if headers.get("Transfer-Encoding") is not None:
        raise Denied("body no permitido")
    if headers.get("Expect") is not None:
        raise Denied("Expect no permitido")
    content_lengths = headers.get_all("Content-Length") or []
    if any(v.strip() != "0" for v in content_lengths):
        raise Denied("body no permitido")

    upstream = f"{m.group('version') or ''}/containers/{cfg.container}/{action}"
    if t is not None:
        upstream += f"?t={t}"
    return Allowed(method=method, action=action, upstream_target=upstream, t=t)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def filter_inspect(body: bytes) -> bytes:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("inspect no es un objeto JSON")
    return json.dumps({k: data[k] for k in INSPECT_KEYS if k in data}).encode()


def forward(allowed: Allowed, cfg: Config) -> Tuple[int, Optional[str], bytes]:
    timeout = cfg.upstream_timeout + (allowed.t or 0)
    conn = UnixHTTPConnection(cfg.socket_path, timeout=timeout)
    try:
        conn.request(
            allowed.method,
            allowed.upstream_target,
            body=b"" if allowed.method == "POST" else None,
            headers={"Host": "docker", "User-Agent": "docker-proxy-min"},
        )
        resp = conn.getresponse()
        body = resp.read(MAX_RESPONSE_BYTES + 1)
        status = resp.status
        content_type = resp.getheader("Content-Type")
    finally:
        conn.close()
    if len(body) > MAX_RESPONSE_BYTES:
        raise Denied("respuesta de Docker demasiado grande", status=502)
    if allowed.action == "json" and status == 200:
        try:
            body = filter_inspect(body)
        except ValueError as exc:
            raise Denied("respuesta de Docker invalida", status=502) from exc
        content_type = "application/json"
    return status, content_type, body


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "docker-proxy-min"
    sys_version = ""
    timeout = CLIENT_READ_TIMEOUT
    server: "ProxyServer"

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if self.command not in ("GET", "POST"):
            self._reply_error(403, "metodo no permitido")
            return False
        return True

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        if not self.server.slots.acquire(blocking=False):
            self._reply_error(503, "demasiados requests simultaneos")
            return
        try:
            try:
                allowed = check_request(
                    self.command, self._raw_target(), self.headers, self.server.cfg
                )
                status, content_type, body = forward(allowed, self.server.cfg)
            except Denied as exc:
                self._reply_error(exc.status, exc.reason)
                return
            except socket.timeout:
                self._reply_error(504, "timeout hablando con Docker")
                return
            except (OSError, http.client.HTTPException):
                self._reply_error(502, "no se pudo hablar con Docker")
                return
            self._reply(status, content_type, body)
        finally:
            self.server.slots.release()

    def _raw_target(self) -> str:
        # http.server normaliza self.path (ej. colapsa '//' inicial); se valida el target tal
        # cual vino en la request-line.
        words = self.requestline.split()
        return words[1] if len(words) == 3 else ""

    def _reply_error(self, status: int, reason: str) -> None:
        self.log_message(
            "deny %s %r -> %d (%s)", self.command, self._raw_target()[:200], status, reason
        )
        body = json.dumps({"message": reason}).encode()
        self._reply(status, "application/json", body)

    def _reply(self, status: int, content_type: Optional[str], body: bytes) -> None:
        self.close_connection = True
        self.send_response(status)
        if status in (204, 304) or 100 <= status < 200:
            body = b""
        else:
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("%s %s\n" % (self.address_string(), format % args))


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, cfg: Config) -> None:
        self.cfg = cfg
        self.slots = threading.BoundedSemaphore(cfg.max_concurrency)
        super().__init__(address, ProxyHandler)


def config_from_env(env=os.environ) -> Config:
    return Config(
        container=env.get("PROXY_CONTAINER", "").strip(),
        socket_path=env.get("PROXY_SOCKET", "/var/run/docker.sock"),
        max_stop_t=int(env.get("PROXY_MAX_STOP_T", "120")),
        upstream_timeout=float(env.get("PROXY_UPSTREAM_TIMEOUT", "10")),
        max_concurrency=int(env.get("PROXY_MAX_CONCURRENCY", "8")),
    )


def main() -> int:
    try:
        cfg = config_from_env()
    except ValueError as exc:
        sys.stderr.write(f"config invalida: {exc}\n")
        return 2
    host = os.environ.get("PROXY_LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("PROXY_LISTEN_PORT", "2375"))
    server = ProxyServer((host, port), cfg)
    sys.stderr.write(f"docker-proxy-min escuchando en {host}:{port} para {cfg.container}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
