"""Tests del proxy minimo de Docker (docker-proxy/docker_proxy.py).

Levanta un Docker falso en un socket unix temporal y el proxy en 127.0.0.1:<puerto libre>.
"""

import http.client
import json
import os
import pathlib
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "docker-proxy"))

import docker_proxy  # noqa: E402

CONTAINER = "client17-passivbot"

INSPECT = {
    "Id": "abc123",
    "Name": "/client17-passivbot",
    "State": {"Status": "running", "Running": True, "StartedAt": "2026-10-01T10:00:00.123456789Z"},
    "Config": {"Env": ["BINANCE_API_KEY=secreto"]},
    "Mounts": [{"Source": "/root/botbulls"}],
}


class FakeDocker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.requests = []
        self.responses = {}  # (method, path_sin_query) -> (status, body_bytes)
        self.delay = None
        super().__init__(path, FakeDockerHandler)

    def handle_error(self, request, client_address):
        # el proxy corta la conexion en los tests de timeout / respuesta gigante
        pass


class FakeDockerHandler(BaseHTTPRequestHandler):
    def address_string(self):
        return "unix"

    def log_message(self, *args):
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
        )
        if self.server.delay is not None:
            self.server.delay.wait(5)
        key = (self.command, self.path.split("?")[0])
        status, payload = self.server.responses.get(key, (204, b""))
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve
    do_PUT = _serve
    do_DELETE = _serve


@pytest.fixture
def env():
    # Path corto: AF_UNIX tiene limite de ~104 bytes en macOS.
    tmp = tempfile.mkdtemp(prefix="dp")
    sock_path = os.path.join(tmp, "d.sock")
    fake = FakeDocker(sock_path)
    threading.Thread(target=fake.serve_forever, args=(0.05,), daemon=True).start()

    cfg = docker_proxy.Config(
        container=CONTAINER, socket_path=sock_path, upstream_timeout=2.0, max_stop_t=60
    )
    proxy = docker_proxy.ProxyServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=proxy.serve_forever, args=(0.05,), daemon=True).start()

    yield fake, proxy

    proxy.shutdown()
    proxy.server_close()
    fake.shutdown()
    fake.server_close()
    shutil.rmtree(tmp, ignore_errors=True)


def call(proxy, method, target, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
    conn.request(method, target, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def raw_call(proxy, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=5) as s:
        s.sendall(raw)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


# --- permitidos --------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["", "/v1.43", "/v1.56"])
def test_inspect_permitido_y_filtrado(env, prefix):
    fake, proxy = env
    fake.responses[("GET", f"{prefix}/containers/{CONTAINER}/json")] = (
        200,
        json.dumps(INSPECT).encode(),
    )
    status, data = call(proxy, "GET", f"{prefix}/containers/{CONTAINER}/json")
    assert status == 200
    out = json.loads(data)
    assert out == {"Id": "abc123", "Name": "/client17-passivbot", "State": INSPECT["State"]}
    assert "Config" not in out and "Mounts" not in out
    assert fake.requests[-1]["path"] == f"{prefix}/containers/{CONTAINER}/json"
    assert fake.requests[-1]["method"] == "GET"


@pytest.mark.parametrize("prefix", ["", "/v1.43"])
@pytest.mark.parametrize(
    "target,expected",
    [
        ("start", "start"),
        ("stop", "stop"),
        ("stop?t=20", "stop?t=20"),
        ("restart", "restart"),
        ("restart?t=0", "restart?t=0"),
        ("restart?t=60", "restart?t=60"),
    ],
)
def test_acciones_permitidas(env, prefix, target, expected):
    fake, proxy = env
    status, data = call(
        proxy, "POST", f"{prefix}/containers/{CONTAINER}/{target}", headers={"Content-Length": "0"}
    )
    assert status == 204
    assert data == b""
    req = fake.requests[-1]
    assert req["method"] == "POST"
    assert req["path"] == f"{prefix}/containers/{CONTAINER}/{expected}"
    assert req["body"] == b""


def test_post_sin_content_length_permitido(env):
    fake, proxy = env
    resp = raw_call(
        proxy, f"POST /containers/{CONTAINER}/start HTTP/1.1\r\nHost: x\r\n\r\n".encode()
    )
    assert resp.startswith(b"HTTP/1.0 204")
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "status,payload",
    [
        (304, b""),
        (404, b'{"message":"No such container: client17-passivbot"}'),
        (500, b'{"message":"boom"}'),
    ],
)
def test_status_de_docker_pasa_tal_cual(env, status, payload):
    fake, proxy = env
    fake.responses[("POST", f"/containers/{CONTAINER}/start")] = (status, payload)
    got, data = call(proxy, "POST", f"/containers/{CONTAINER}/start")
    assert got == status
    assert data == payload


def test_inspect_404_pasa_tal_cual(env):
    fake, proxy = env
    payload = b'{"message":"No such container: client17-passivbot"}'
    fake.responses[("GET", f"/containers/{CONTAINER}/json")] = (404, payload)
    assert call(proxy, "GET", f"/containers/{CONTAINER}/json") == (404, payload)


# --- denegados (nunca llegan a Docker) ---------------------------------------------------


DENIED_TARGETS = [
    ("GET", "/_ping"),
    ("GET", "/version"),
    ("GET", "/containers/json"),
    ("GET", "/containers/json?all=1"),
    ("POST", "/containers/create"),
    ("POST", "/containers/create?name=x"),
    # otro contenedor / nombre parecido
    ("GET", "/containers/otro/json"),
    ("GET", "/containers/client17-passivbot2/json"),
    ("GET", "/containers/client17-passivbo/json"),
    ("GET", "/containers/xclient17-passivbot/json"),
    ("POST", "/containers/client17-passivbot.old/start"),
    ("GET", "/containers/CLIENT17-PASSIVBOT/json"),
    # otras operaciones sobre el contenedor permitido
    ("GET", f"/containers/{CONTAINER}/archive?path=/"),
    ("GET", f"/containers/{CONTAINER}/logs"),
    ("GET", f"/containers/{CONTAINER}/top"),
    ("POST", f"/containers/{CONTAINER}/exec"),
    ("POST", f"/containers/{CONTAINER}/kill"),
    ("POST", f"/containers/{CONTAINER}/update"),
    ("POST", f"/containers/{CONTAINER}/rename?name=x"),
    # metodo cruzado
    ("POST", f"/containers/{CONTAINER}/json"),
    ("GET", f"/containers/{CONTAINER}/start"),
    ("GET", f"/containers/{CONTAINER}/stop"),
    # path traversal / normalizacion
    ("GET", f"/containers/{CONTAINER}/../otro/json"),
    ("GET", f"/containers/../containers/{CONTAINER}/json"),
    ("GET", f"/containers/otro/../{CONTAINER}/json"),
    ("GET", f"/containers/{CONTAINER}/json/"),
    ("GET", f"/containers/{CONTAINER}//json"),
    ("GET", f"//containers/{CONTAINER}/json"),
    ("GET", f"/containers/{CONTAINER}%2Fjson"),
    ("GET", "/containers/client17%2Dpassivbot/json"),
    ("GET", f"/containers/{CONTAINER}/json%3F"),
    ("GET", f"/Containers/{CONTAINER}/json"),
    ("GET", f"/containers/{CONTAINER}/JSON"),
    ("GET", f"/./containers/{CONTAINER}/json"),
    ("GET", f"http://docker/containers/{CONTAINER}/json"),
    # version
    ("GET", f"/v2.0/containers/{CONTAINER}/json"),
    ("GET", f"/v1/containers/{CONTAINER}/json"),
    ("GET", f"/v1.43.1/containers/{CONTAINER}/json"),
    ("GET", f"/v1.4321/containers/{CONTAINER}/json"),
    ("GET", f"/vX/containers/{CONTAINER}/json"),
    ("GET", f"/v1.43/v1.43/containers/{CONTAINER}/json"),
    # query
    ("GET", f"/containers/{CONTAINER}/json?size=1"),
    ("GET", f"/containers/{CONTAINER}/json?"),
    ("POST", f"/containers/{CONTAINER}/start?detachKeys=ctrl-p"),
    ("POST", f"/containers/{CONTAINER}/start?t=1"),
    ("POST", f"/containers/{CONTAINER}/stop?t=abc"),
    ("POST", f"/containers/{CONTAINER}/stop?t=-1"),
    ("POST", f"/containers/{CONTAINER}/stop?t=61"),
    ("POST", f"/containers/{CONTAINER}/stop?t=9999"),
    ("POST", f"/containers/{CONTAINER}/stop?t=99999"),
    ("POST", f"/containers/{CONTAINER}/stop?t="),
    ("POST", f"/containers/{CONTAINER}/stop?t=1.5"),
    ("POST", f"/containers/{CONTAINER}/stop?t=1&t=2"),
    ("POST", f"/containers/{CONTAINER}/stop?t=1&signal=SIGKILL"),
    ("POST", f"/containers/{CONTAINER}/stop?signal=SIGKILL"),
    ("POST", f"/containers/{CONTAINER}/restart?t=1?t=2"),
    ("POST", f"/containers/{CONTAINER}/restart?T=1"),
    ("POST", f"/containers/{CONTAINER}/restart#x"),
    # largo
    ("GET", "/containers/" + "a" * 300 + "/json"),
]


@pytest.mark.parametrize("method,target", DENIED_TARGETS)
def test_denegados(env, method, target):
    fake, proxy = env
    status, data = call(proxy, method, target)
    assert status == 403, (method, target, data)
    assert fake.requests == []


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "FOO"])
def test_metodos_no_permitidos(env, method):
    fake, proxy = env
    for target in (
        f"/containers/{CONTAINER}/json",
        f"/containers/{CONTAINER}/start",
        f"/containers/{CONTAINER}/archive?path=/x",
        f"/containers/{CONTAINER}",
    ):
        status, _ = call(proxy, method, target)
        assert status == 403, (method, target)
    assert fake.requests == []


@pytest.mark.parametrize(
    "raw",
    [
        # body en POST
        f"POST /containers/{CONTAINER}/start HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{{}}",
        f"POST /containers/{CONTAINER}/start HTTP/1.1\r\nHost: x\r\n"
        "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        f"POST /containers/{CONTAINER}/start HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
        "Content-Length: 5\r\n\r\n",
        f"POST /containers/{CONTAINER}/start HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n"
        "Content-Length: 0\r\n\r\n",
        # body en GET
        f"GET /containers/{CONTAINER}/json HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc",
        # no-ASCII
        f"GET /containers/{CONTAINER}/jsoñ HTTP/1.1\r\nHost: x\r\n\r\n",
        f"GET /containers/{CONTAINER}/json\x00 HTTP/1.1\r\nHost: x\r\n\r\n",
    ],
)
def test_body_y_targets_crudos_denegados(env, raw):
    fake, proxy = env
    resp = raw_call(proxy, raw.encode("utf-8"))
    status = int(resp.split(b" ", 2)[1])
    assert status in (400, 403), resp[:200]
    assert fake.requests == []


def test_request_line_gigante_rechazado(env):
    fake, proxy = env
    resp = raw_call(proxy, b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    assert int(resp.split(b" ", 2)[1]) in (400, 403, 414)
    assert fake.requests == []


def test_headers_hacia_docker_son_fijos(env):
    fake, proxy = env
    call(
        proxy,
        "POST",
        f"/containers/{CONTAINER}/start",
        headers={"X-Registry-Auth": "secreto", "Authorization": "Bearer x", "Host": "evil"},
    )
    hdrs = {k.lower(): v for k, v in fake.requests[-1]["headers"].items()}
    assert hdrs["host"] == "docker"
    assert "x-registry-auth" not in hdrs
    assert "authorization" not in hdrs


# --- errores de Docker -------------------------------------------------------------------


def _docker_caido(fake, proxy):
    fake.shutdown()
    fake.server_close()
    os.unlink(proxy.cfg.socket_path)


@pytest.mark.parametrize(
    "method,target",
    [("GET", f"/containers/{CONTAINER}/json"), ("POST", f"/containers/{CONTAINER}/restart?t=20")],
)
def test_docker_caido_502(env, method, target):
    # no se pudo conectar: Docker seguro no actuo -> respuesta HTTP 502
    fake, proxy = env
    _docker_caido(fake, proxy)
    status, _ = call(proxy, method, target)
    assert status == 502


def test_timeout_inspect_504(env):
    fake, proxy = env
    fake.delay = threading.Event()
    try:
        status, _ = call(proxy, "GET", f"/containers/{CONTAINER}/json")
        assert status == 504
    finally:
        fake.delay.set()


def test_timeout_accion_corta_sin_responder(env):
    # la accion ya se envio: no se sabe si Docker la ejecuto -> sin respuesta HTTP
    fake, proxy = env
    fake.delay = threading.Event()
    try:
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            call(proxy, "POST", f"/containers/{CONTAINER}/restart?t=0")
        assert fake.requests[-1]["path"] == f"/containers/{CONTAINER}/restart?t=0"
    finally:
        fake.delay.set()


def test_inspect_respuesta_invalida_502(env):
    fake, proxy = env
    fake.responses[("GET", f"/containers/{CONTAINER}/json")] = (200, b"no-json")
    status, _ = call(proxy, "GET", f"/containers/{CONTAINER}/json")
    assert status == 502


def _gigante():
    return b'{"message":"' + b"x" * (docker_proxy.MAX_RESPONSE_BYTES + 10) + b'"}'


def test_inspect_respuesta_gigante_502(env):
    fake, proxy = env
    fake.responses[("GET", f"/containers/{CONTAINER}/json")] = (200, _gigante())
    status, _ = call(proxy, "GET", f"/containers/{CONTAINER}/json")
    assert status == 502


def test_accion_respuesta_gigante_corta_sin_responder(env):
    fake, proxy = env
    fake.responses[("POST", f"/containers/{CONTAINER}/start")] = (500, _gigante())
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
        call(proxy, "POST", f"/containers/{CONTAINER}/start")


# --- config ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", " ", "-x", "a/b", "a b", "../x", "a%2Fb", "a" * 200])
def test_config_nombre_invalido(name):
    with pytest.raises(ValueError):
        docker_proxy.Config(container=name)


def test_config_from_env_requiere_contenedor():
    with pytest.raises(ValueError):
        docker_proxy.config_from_env({})
    cfg = docker_proxy.config_from_env({"PROXY_CONTAINER": CONTAINER, "PROXY_MAX_STOP_T": "30"})
    assert cfg.container == CONTAINER and cfg.max_stop_t == 30


# --- integracion con el cliente real del panel -------------------------------------------


def test_cliente_del_panel_funciona_via_proxy(env):
    bot_control = pytest.importorskip("futuresboard.bot_control")
    fake, proxy = env
    fake.responses[("GET", f"/containers/{CONTAINER}/json")] = (200, json.dumps(INSPECT).encode())
    client = bot_control.DockerClient(f"http://127.0.0.1:{proxy.server_address[1]}")

    info = client.inspect(CONTAINER)
    assert info["running"] is True and info["status"] == "running"
    assert info["started_at"] == INSPECT["State"]["StartedAt"]
    assert client.start(CONTAINER) == "ok"
    assert client.stop(CONTAINER) == "ok"
    assert client.restart(CONTAINER) == "ok"

    t = bot_control.STOP_TIMEOUT_SECONDS
    assert [(r["method"], r["path"]) for r in fake.requests] == [
        ("GET", f"/containers/{CONTAINER}/json"),
        ("POST", f"/containers/{CONTAINER}/start"),
        ("POST", f"/containers/{CONTAINER}/stop?t={t}"),
        ("POST", f"/containers/{CONTAINER}/restart?t={t}"),
    ]

    fake.responses[("POST", f"/containers/{CONTAINER}/start")] = (304, b"")
    assert client.start(CONTAINER) == "sin_cambios"
    fake.responses[("GET", f"/containers/{CONTAINER}/json")] = (404, b'{"message":"no"}')
    assert client.inspect(CONTAINER)["status"] == "not_found"

    # el panel apuntando a otro contenedor recibe 403 -> DockerError
    with pytest.raises(bot_control.DockerError):
        client.inspect("otro-passivbot")


def test_cliente_del_panel_distingue_rechazo_de_incierto(env):
    bot_control = pytest.importorskip("futuresboard.bot_control")
    fake, proxy = env
    client = bot_control.DockerClient(f"http://127.0.0.1:{proxy.server_address[1]}")

    # Docker colgado despues de recibir el restart: el panel lo ve como incierto (no revierte)
    fake.delay = threading.Event()
    try:
        with pytest.raises(bot_control.DockerError) as exc_info:
            client._action(CONTAINER, "restart", {"t": 0})
        assert exc_info.value.uncertain is True
    finally:
        fake.delay.set()

    # Docker caido (no se pudo conectar): rechazo seguro, el panel puede revertir
    _docker_caido(fake, proxy)
    with pytest.raises(bot_control.DockerError) as exc_info:
        client.restart(CONTAINER)
    assert exc_info.value.uncertain is False
