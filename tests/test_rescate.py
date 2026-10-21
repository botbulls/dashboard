"""Rescate como colchón: inyección cerca de la liquidación, devolución, tope de pérdida y panel."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import stat
from decimal import Decimal
from urllib.parse import parse_qsl
from urllib.parse import urlparse

import pytest
import requests

from futuresboard import bot_control
from futuresboard import rescate

from test_bot_control import app  # noqa: F401
from test_bot_control import audit_lines
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import post
from test_bot_control import router  # noqa: F401
from test_bot_control import FakeResponse

FUT = "https://fapi.binance.com"
SPOT = "https://api.binance.com"
RESTRICCIONES_OK = {"ipRestrict": True, "permitsUniversalTransfer": True, "enableWithdrawals": False,
                    "enableInternalTransfer": False, "enableReading": True, "enableFutures": True}


class FakeRescateBinance:
    """Cuenta de futuros + spot + transferencias universales. Mueve saldos como Binance."""

    def __init__(self):
        self.margin = Decimal("2000")
        self.maint = Decimal("1000")
        self.max_withdraw = Decimal("1500")
        self.positions = {"BTCUSDT": "0.5"}
        self.spot = Decimal("20000")
        self.restrictions = dict(RESTRICCIONES_OK)
        self.history = []          # filas de GET /sapi/v1/asset/transfer
        self.transfer_mode = "ok"  # ok | timeout | reject | 5xx | sin_tranid | code_1007
        self.ejecutar_ambigua = False  # si una transferencia ambigua igual se ejecutó en Binance
        self.history_status = "CONFIRMED"
        self.history_visible = True    # False: el historial todavía no la muestra
        self.read_fail = set()     # paths que fallan con 500
        self.calls = []
        self._tran = 5000
        self.now_ms = 1_700_000_000_000

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method is None or m == method]

    def posts(self):
        return [params for m, p, params in self.calls if m == "POST" and p == "/sapi/v1/asset/transfer"]

    def _execute(self, tipo, amount):
        if tipo == "MAIN_UMFUTURE":
            self.spot -= amount
            self.margin += amount
            self.max_withdraw += amount
        else:
            self.spot += amount
            self.margin -= amount
            self.max_withdraw -= amount
        self._tran += 1
        row = {"asset": "USDT", "amount": str(amount), "type": tipo, "status": self.history_status,
               "tranId": self._tran, "timestamp": self.now_ms}
        if self.history_visible:
            self.history.append(row)
        else:
            self._oculta = row
        return self._tran

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        u = urlparse(url)
        params = dict(parse_qsl(u.query))
        assert headers and headers.get("X-MBX-APIKEY") and "signature" in params
        self.calls.append((method, u.path, params))
        base = f"{u.scheme}://{u.netloc}"
        if u.path in self.read_fail:
            return FakeResponse(500, {"code": -1001, "msg": "Internal error"})
        if (method, u.path) == ("GET", "/fapi/v2/account"):
            assert base == FUT
            return FakeResponse(200, {
                "totalMarginBalance": str(self.margin), "totalMaintMargin": str(self.maint),
                "maxWithdrawAmount": str(self.max_withdraw), "availableBalance": str(self.max_withdraw),
                "totalWalletBalance": str(self.margin),
                "positions": [{"symbol": s, "positionAmt": a} for s, a in self.positions.items()]
                + [{"symbol": "ETHUSDT", "positionAmt": "0"}],
            })
        assert base == SPOT, f"{u.path} tiene que ir a spot, fue a {base}"
        if (method, u.path) == ("GET", "/api/v3/account"):
            return FakeResponse(200, {"balances": [{"asset": "BNB", "free": "1", "locked": "0"},
                                                   {"asset": "USDT", "free": str(self.spot), "locked": "5"}]})
        if (method, u.path) == ("GET", "/sapi/v1/account/apiRestrictions"):
            return FakeResponse(200, dict(self.restrictions))
        if (method, u.path) == ("GET", "/sapi/v1/asset/transfer"):
            rows = [r for r in self.history if r["type"] == params["type"]
                    and r["timestamp"] >= int(params["startTime"])]
            return FakeResponse(200, {"total": len(rows), "rows": rows} if rows else {"total": 0})
        if (method, u.path) == ("POST", "/sapi/v1/asset/transfer"):
            assert params["asset"] == "USDT"
            amount = Decimal(params["amount"])
            mode = self.transfer_mode
            if mode == "reject":
                return FakeResponse(400, {"code": -5013, "msg": "Asset transfer failed: insufficient balance"})
            if mode in ("timeout", "5xx", "sin_tranid", "code_1007"):
                if self.ejecutar_ambigua:
                    self._execute(params["type"], amount)
                if mode == "timeout":
                    raise requests.ReadTimeout("timeout")
                if mode == "5xx":
                    return FakeResponse(502, {})
                if mode == "code_1007":
                    return FakeResponse(400, {"code": -1007, "msg": "Timeout waiting for response"})
                return FakeResponse(200, {})
            return FakeResponse(200, {"tranId": self._execute(params["type"], amount)})
        return FakeResponse(404, {"code": -5000, "msg": "Path not found"})


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def fake():
    return FakeRescateBinance()


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def loan_path(tmp_path):
    return tmp_path / "rescate" / "loan_state.json"


class Harness:
    def __init__(self, data_dir, loan_path, fake):
        self.data_dir = data_dir
        self.loan_path = loan_path
        self.fake = fake
        self.sent = []
        self.apagados = []
        self.apagar_result = ("ok", {"ok": True, "modo": "apagar", "resumen": {
            "completo": True, "posiciones_cerradas": [], "ordenes_canceladas": [], "errores": [],
            "restante": {"verificado": True}}})
        self.clock = Clock()

    def apagar(self):
        self.apagados.append(1)
        return self.apagar_result

    def service(self, mode="real", testnet=False, apagar=True, exchange="binance"):
        settings = rescate.RescateSettings({rescate.ENV_MODE: mode, rescate.ENV_LOAN_STATE: str(self.loan_path)})
        client = rescate.RescateBinance(FUT, SPOT, "key-de-test", "secret-de-test", session=self.fake)
        return rescate.RescateService(self.data_dir, settings, client, testnet=testnet, exchange=exchange,
                                      apagar=self.apagar if apagar else None,
                                      send=lambda t: self.sent.append(t) or True, clock=self.clock)

    def configurar(self, habilitado=True, reserva=5000, paso=1000, umbral=3, tope=30):
        parsed = rescate.validate_config_body({"habilitado": habilitado, "reserva_max_usdt": reserva,
                                               "paso_usdt": paso, "umbral_ratio": umbral,
                                               "tope_perdida_pct": tope})
        return rescate.save_config(self.data_dir, parsed, "cliente17")

    def state(self):
        state, err = rescate.load_state(self.data_dir)
        assert err is None
        return state

    def loan_file(self):
        return json.loads(self.loan_path.read_text())

    def avanzar(self, segundos):
        self.clock.t += segundos
        self.fake.now_ms = int(self.clock.t * 1000)


@pytest.fixture
def h(data_dir, loan_path, fake):
    harness = Harness(data_dir, loan_path, fake)
    fake.now_ms = int(harness.clock.t * 1000)
    return harness


# ---------------------------------------------------------------- contrato loan_state.json


def test_loan_state_contrato_y_permisos(loan_path):
    payload = rescate.write_loan_state(loan_path, Decimal("1234.567"), "real", ts=1_700_000_000)
    data = json.loads(loan_path.read_text())
    assert data == payload
    assert set(data) == {"version", "loan_outstanding_usdt", "updated_at", "mode"}
    assert data["version"] == 1 and data["mode"] == "real"
    assert data["loan_outstanding_usdt"] == 1234.56 and isinstance(data["loan_outstanding_usdt"], float)
    parsed = dt.datetime.fromisoformat(data["updated_at"])
    assert parsed.utcoffset() == dt.timedelta(0)
    # passivbot corre con otro usuario: el archivo tiene que ser legible por todos.
    assert stat.S_IMODE(os.stat(loan_path).st_mode) == 0o644
    rescate.write_loan_state(loan_path, Decimal(0), "dry_run")
    assert json.loads(loan_path.read_text())["mode"] == "dry_run"
    with pytest.raises(ValueError):
        rescate.write_loan_state(loan_path, Decimal(1), "off")
    with pytest.raises(ValueError):
        rescate.write_loan_state(loan_path, Decimal(-1), "real")


def test_loan_state_escritura_atomica(loan_path, monkeypatch):
    rescate.write_loan_state(loan_path, Decimal("100"), "real")
    original = loan_path.read_text()

    def boom(src, dst):
        raise OSError("disco lleno")

    monkeypatch.setattr(rescate.os, "replace", boom)
    with pytest.raises(OSError):
        rescate.write_loan_state(loan_path, Decimal("999"), "real")
    assert loan_path.read_text() == original
    assert [p.name for p in loan_path.parent.iterdir()] == ["loan_state.json"]  # sin tmp huérfanos


# ---------------------------------------------------------------- inyección


def test_umbral_exacto_no_inyecta_y_debajo_si(h):
    h.configurar(umbral=3)
    h.fake.margin, h.fake.maint = Decimal("3000"), Decimal("1000")  # ratio exactamente 3
    svc = h.service()
    svc.tick()
    assert h.fake.posts() == []
    assert h.state()["base"]["capital"] == "23000.00"
    h.fake.margin = Decimal("2999.99")
    svc.tick()
    assert [p["amount"] for p in h.fake.posts()] == ["1000.00"]
    assert h.fake.posts()[0]["type"] == "MAIN_UMFUTURE"
    st = h.state()
    assert st["prestamo"]["real"] == "1000.00" and st["en_vuelo"] is None
    assert st["ledger"][-1]["estado"] == "confirmada" and st["ledger"][-1]["tranId"] == 5001
    assert h.loan_file()["loan_outstanding_usdt"] == 1000.0 and h.loan_file()["mode"] == "real"
    assert any("Inyección 1000.00 USDT" in t and "tranId 5001" in t for t in h.sent)


def test_sin_posiciones_no_inyecta(h):
    h.configurar()
    h.fake.positions = {}
    h.fake.margin, h.fake.maint = Decimal("100"), Decimal("1000")
    h.service().tick()
    assert h.fake.posts() == []


def test_paso_y_tope_de_reserva(h):
    h.configurar(reserva=2500, paso=1000)
    h.fake.maint = Decimal("3000")  # el ratio sigue < 3 aunque entre plata
    svc = h.service()
    for _ in range(5):
        svc.tick()
    assert [p["amount"] for p in h.fake.posts()] == ["1000.00", "1000.00", "500.00"]
    assert h.state()["prestamo"]["real"] == "2500.00"
    assert h.loan_file()["loan_outstanding_usdt"] == 2500.0
    avisos = [t for t in h.sent if "ya está prestada" in t]
    assert len(avisos) == 1  # el aviso no se repite cada ciclo


def test_tope_por_usdt_libre_en_spot(h):
    h.configurar(reserva=5000, paso=1000)
    h.fake.spot = Decimal("300.509")
    h.service().tick()
    assert [p["amount"] for p in h.fake.posts()] == ["300.50"]


def test_rechazo_claro_no_reintenta_y_avisa(h):
    h.configurar()
    h.fake.transfer_mode = "reject"
    svc = h.service()
    svc.tick()
    svc.tick()
    assert len(h.fake.posts()) == 1  # cooldown tras el rechazo
    st = h.state()
    assert st["prestamo"]["real"] == "0.00" and st["en_vuelo"] is None
    assert st["ledger"][-1]["estado"] == "rechazada"
    assert any("rechazada por Binance" in t for t in h.sent)
    h.avanzar(rescate.COOLDOWN_RECHAZO_MS / 1000 + 1)
    svc.tick()
    assert len(h.fake.posts()) == 2


# ---------------------------------------------------------------- una a la vez y conciliación


@pytest.mark.parametrize("modo", ["timeout", "5xx", "sin_tranid", "code_1007"])
def test_ambigua_queda_en_vuelo_y_no_reintenta(h, modo):
    h.configurar()
    h.fake.transfer_mode = modo
    svc = h.service()
    svc.tick()
    st = h.state()
    assert st["en_vuelo"]["estado"] == "incierto"
    # Conservador: el préstamo se cuenta ya, passivbot no dimensiona sobre lo que pudo entrar.
    assert st["prestamo"]["real"] == "1000.00" and h.loan_file()["loan_outstanding_usdt"] == 1000.0
    h.fake.transfer_mode = "ok"
    svc.tick()
    svc.tick()
    assert len(h.fake.posts()) == 1  # una transferencia en vuelo a la vez
    assert any("resultado incierto" in t for t in h.sent)


def test_conciliacion_timeout_ejecutada_por_historial(h):
    h.configurar()
    h.fake.transfer_mode, h.fake.ejecutar_ambigua = "timeout", True
    svc = h.service()
    svc.tick()
    h.fake.transfer_mode = "ok"
    h.avanzar(15)
    svc.tick()
    st = h.state()
    assert st["en_vuelo"] is None
    assert st["ledger"][-1]["estado"] == "confirmada" and st["ledger"][-1]["tranId"] == 5001
    assert st["prestamo"]["real"] == "1000.00"
    assert len(h.fake.posts()) == 1


def test_conciliacion_timeout_no_ejecutada_revierte(h):
    h.configurar()
    h.fake.transfer_mode = "timeout"
    svc = h.service()
    svc.tick()
    h.avanzar(15)
    svc.tick()
    assert h.state()["en_vuelo"]["estado"] == "incierto"  # antes del margen de gracia no se decide
    h.avanzar(rescate.RECONCILE_GRACE_MS / 1000)
    h.fake.transfer_mode = "ok"
    svc.tick()
    st = h.state()
    assert st["en_vuelo"] is None and st["ledger"][-1]["estado"] == "no_ejecutada"
    assert st["prestamo"]["real"] == "0.00" and h.loan_file()["loan_outstanding_usdt"] == 0.0
    assert len(h.fake.posts()) == 1  # cooldown corto antes de volver a intentar


def test_conciliacion_por_variacion_de_saldo(h):
    h.configurar()
    h.fake.transfer_mode, h.fake.ejecutar_ambigua, h.fake.history_visible = "timeout", True, False
    svc = h.service()
    svc.tick()
    h.avanzar(rescate.RECONCILE_GRACE_MS / 1000 + 1)
    h.fake.transfer_mode = "ok"
    svc.tick()
    assert h.state()["en_vuelo"]["estado"] == "incierto"  # un solo chequeo todavía
    svc.tick()
    st = h.state()
    assert st["ledger"][-1]["estado"] == "confirmada" and st["ledger"][-1]["tranId"] is None
    assert st["prestamo"]["real"] == "1000.00"


def test_reinicio_con_transferencia_enviando_se_concilia(h):
    h.configurar()
    h.fake.transfer_mode, h.fake.ejecutar_ambigua = "timeout", True
    svc = h.service()
    svc.tick()
    st = h.state()
    st["en_vuelo"]["estado"] = "enviando"  # se cortó el proceso antes de la respuesta
    st["en_vuelo"]["prestamo_aplicado"] = False
    st["prestamo"]["real"] = "0.00"
    rescate.save_state(h.data_dir, st)
    h.fake.transfer_mode = "ok"
    h.service().tick()  # proceso nuevo
    st = h.state()
    assert st["en_vuelo"] is None and st["ledger"][-1]["estado"] == "confirmada"
    assert st["prestamo"]["real"] == "1000.00" and h.loan_file()["loan_outstanding_usdt"] == 1000.0


# ---------------------------------------------------------------- devolución


def test_devolucion_parcial_por_max_withdraw(h):
    h.configurar()
    st = h.state()
    st["prestamo"]["real"] = "1000.00"
    rescate.save_state(h.data_dir, st)
    h.fake.positions = {}
    h.fake.max_withdraw = Decimal("400")
    svc = h.service()
    svc.tick()
    assert [(p["type"], p["amount"]) for p in h.fake.posts()] == [("UMFUTURE_MAIN", "400.00")]
    assert h.state()["prestamo"]["real"] == "600.00" and h.loan_file()["loan_outstanding_usdt"] == 600.0
    h.fake.max_withdraw = Decimal("5000")
    svc.tick()
    assert [p["amount"] for p in h.fake.posts()] == ["400.00", "600.00"]
    assert h.state()["prestamo"]["real"] == "0.00" and h.loan_file()["loan_outstanding_usdt"] == 0.0
    svc.tick()
    assert len(h.fake.posts()) == 2
    assert sum("Devolución" in t for t in h.sent) == 2


def test_devolucion_solo_baja_el_prestamo_confirmada(h):
    st = h.state()
    st["prestamo"]["real"] = "1000.00"
    rescate.save_state(h.data_dir, st)
    h.fake.positions = {}
    h.fake.history_status = "PENDING"
    svc = h.service()
    svc.tick()
    st = h.state()
    assert st["en_vuelo"]["estado"] == "pendiente" and st["prestamo"]["real"] == "1000.00"
    assert h.loan_file()["loan_outstanding_usdt"] == 1000.0
    for row in h.fake.history:
        row["status"] = "CONFIRMED"
    svc.tick()
    assert h.state()["prestamo"]["real"] == "0.00" and h.loan_file()["loan_outstanding_usdt"] == 0.0


def test_devolucion_fallida_no_baja_el_prestamo(h):
    st = h.state()
    st["prestamo"]["real"] = "500.00"
    rescate.save_state(h.data_dir, st)
    h.fake.positions = {}
    h.fake.history_status = "FAILED"
    h.service().tick()
    st = h.state()
    assert st["prestamo"]["real"] == "500.00" and st["ledger"][-1]["estado"] == "fallida"


def test_devolucion_sigue_con_rescate_deshabilitado(h):
    h.configurar(habilitado=False, reserva=None, tope=None)
    st = h.state()
    st["prestamo"]["real"] = "300.00"
    rescate.save_state(h.data_dir, st)
    h.fake.positions = {}
    h.service().tick()
    assert [p["amount"] for p in h.fake.posts()] == ["300.00"]


# ---------------------------------------------------------------- tope de pérdida


def test_tope_dispara_apagar_y_bloquea_start(h):
    h.configurar(tope=10)
    svc = h.service()
    svc.tick()
    assert h.state()["base"]["capital"] == "22000.00"
    # Tras la inyección: spot 19000. Margen -200 => capital 18800 <= 19800 (base x 0.9).
    h.fake.margin = Decimal("-200")
    svc.tick()
    st = h.state()
    assert h.apagados == [1]
    assert st["tope"]["apagar"]["resultado"] == "ok" and st["tope"]["limite"] == "19800.00"
    assert rescate.start_bloqueado(h.data_dir)
    assert any("TOPE DE PÉRDIDA" in t for t in h.sent)
    assert any("APAGAR" in t for t in h.sent)
    posts = len(h.fake.posts())
    svc.tick()
    assert h.apagados == [1]  # no se repite
    assert len(h.fake.posts()) == posts  # con el tope activo no inyecta


def test_tope_apagar_fallido_reintenta_con_limite(h):
    h.configurar(tope=10)
    h.apagar_result = ("error", {"ok": False, "error": "docker-proxy caído"})
    svc = h.service()
    svc.tick()
    h.fake.margin = Decimal("-500")
    svc.tick()
    assert len(h.apagados) == 1
    svc.tick()
    assert len(h.apagados) == 1  # espera entre intentos
    for _ in range(5):
        h.avanzar(rescate.APAGAR_REINTENTO_MS / 1000 + 1)
        svc.tick()
    assert len(h.apagados) == rescate.APAGAR_MAX_INTENTOS
    assert any("intervención manual" in t for t in h.sent)


def test_tope_en_dry_run_es_simulado(h):
    h.configurar(tope=10)
    svc = h.service(mode="dry_run")
    svc.tick()
    h.fake.margin = Decimal("-500")
    svc.tick()
    st = h.state()
    assert h.apagados == []  # nada real: ni Docker ni órdenes
    assert st["tope"]["apagar"]["resultado"] == "simulado" and st["tope"]["modo"] == "dry_run"
    assert "simulado" in rescate.start_bloqueado(h.data_dir)
    assert all(t.startswith("🧪") for t in h.sent)


def test_sin_base_no_inyecta(h, monkeypatch):
    h.configurar()
    svc = h.service()
    # La lectura falla en el primer ciclo: no hay base ni tope armado.
    h.fake.read_fail.add("/api/v3/account")
    svc.tick()
    assert h.state()["base"] is None and h.fake.posts() == []
    assert any("No se pudo leer la cuenta" in t for t in h.sent)
    h.fake.read_fail.clear()
    svc.tick()
    assert h.state()["base"] is not None
    assert any("recuperada" in t for t in h.sent)


def test_apagar_real_reusa_rutina_y_lock(data_dir, env, router, monkeypatch):  # noqa: F811
    router.binance.add_position("BTCUSDT", "0.010")
    router.binance.orders = [{"symbol": "BTCUSDT"}]
    settings = bot_control.Settings()
    futures = bot_control.BinanceFuturesClient("https://fapi.binance.com", "k", "s")
    apagar = rescate.make_apagar(data_dir, settings, futures, lock_timeout=0.05)
    store = bot_control.Store(data_dir)

    handle = store.try_lock()  # el panel tiene una acción en curso
    try:
        outcome, payload = apagar()
    finally:
        handle.release()
    assert outcome == "error" and "lock" in payload["error"]
    assert router.docker.calls == []

    outcome, payload = apagar()
    assert outcome == "ok" and payload["resumen"]["completo"] is True
    assert router.docker.actions() == ["stop"]
    assert router.binance.positions == {} and router.binance.orders == []
    audit = [json.loads(line) for line in (data_dir / bot_control.AUDIT_LOG_NAME).read_text().splitlines()]
    assert audit[-1]["user"] == rescate.APAGAR_USUARIO and audit[-1]["result"] == "ok"
    assert audit[-1]["params"] == {"modo": "apagar", "origen": "rescate_tope"}
    assert store.try_lock() is not None  # el lock quedó libre


# ---------------------------------------------------------------- modos


def test_dry_run_no_llama_a_sapi(h):
    h.configurar()
    svc = h.service(mode="dry_run")
    svc.tick()
    assert not [p for p in h.fake.paths() if p.startswith("/sapi")]
    st = h.state()
    assert st["prestamo"]["dry_run"] == "1000.00" and st["prestamo"]["real"] == "0.00"
    assert h.loan_file() == {**h.loan_file(), "mode": "dry_run", "loan_outstanding_usdt": 1000.0}
    assert st["ledger"][-1]["estado"] == "simulada"
    # El ratio simulado ya cuenta lo inyectado (2000 + 1000) / 1000 = 3: no vuelve a inyectar.
    svc.tick()
    assert h.state()["prestamo"]["dry_run"] == "1000.00"
    assert st["ultimo"]["simulado"] is True
    h.fake.positions = {}
    svc.tick()
    assert h.state()["prestamo"]["dry_run"] == "0.00" and h.loan_file()["loan_outstanding_usdt"] == 0.0
    assert not [p for p in h.fake.paths() if p.startswith("/sapi")]


def test_real_bloqueado_en_testnet(h):
    h.configurar()
    h.service(mode="real", testnet=True).tick()
    st = h.state()
    assert "testnet" in st["bloqueo"] and h.fake.posts() == []
    assert not [p for p in h.fake.paths() if p.startswith("/sapi")]


@pytest.mark.parametrize("campo,valor", [("ipRestrict", False), ("permitsUniversalTransfer", False),
                                         ("enableWithdrawals", True), ("enableInternalTransfer", True)])
def test_real_sin_permisos_correctos_no_opera(h, campo, valor):
    h.configurar()
    h.fake.restrictions[campo] = valor
    svc = h.service()
    svc.tick()
    svc.tick()
    assert campo in h.state()["bloqueo"] and h.fake.posts() == []
    assert h.fake.paths().count("/sapi/v1/account/apiRestrictions") == 1  # se verifica al arrancar


def test_real_sin_apagar_disponible_no_opera(h):
    h.configurar()
    h.service(apagar=False).tick()
    assert "FUTURESBOARD_DOCKER_URL" in h.state()["bloqueo"] and h.fake.posts() == []


def test_modo_invalido_es_off():
    s = rescate.RescateSettings({rescate.ENV_MODE: "reaI"})
    assert s.mode == "off" and s.mode_error
    assert rescate.RescateSettings({}).mode == "off"
    assert rescate.RescateSettings({rescate.ENV_INTERVAL: "x"}).interval == rescate.DEFAULT_INTERVAL
    assert rescate.RescateSettings({rescate.ENV_INTERVAL: "1"}).interval == rescate.MIN_INTERVAL


def test_off_no_opera_y_limpia_prestamo_simulado(h):
    h.configurar()
    st = h.state()
    st["prestamo"] = {"real": "250.00", "dry_run": "900.00"}
    rescate.save_state(h.data_dir, st)
    rescate.write_loan_state(h.loan_path, Decimal("900"), "dry_run")
    h.service(mode="off").tick()
    assert h.fake.calls == []
    assert h.loan_file()["mode"] == "real" and h.loan_file()["loan_outstanding_usdt"] == 250.0


def test_prestamo_simulado_no_pasa_a_real(h):
    st = h.state()
    st["prestamo"] = {"real": "0.00", "dry_run": "1500.00"}
    st["servicio"] = {"modo": "dry_run"}
    rescate.save_state(h.data_dir, st)
    rescate.write_loan_state(h.loan_path, Decimal("1500"), "dry_run")
    h.fake.positions = {}
    h.service(mode="real").tick()
    assert h.loan_file()["mode"] == "real" and h.loan_file()["loan_outstanding_usdt"] == 0.0
    assert h.fake.posts() == []  # nunca "devuelve" un préstamo simulado con plata real


def test_estado_ilegible_no_opera_y_bloquea_start(h):
    h.configurar()
    (h.data_dir / rescate.STATE_FILE_NAME).write_text("{roto")
    h.service().tick()
    assert h.fake.calls == []
    assert (h.data_dir / rescate.STATE_FILE_NAME).read_text() == "{roto"
    assert "ilegible" in rescate.start_bloqueado(h.data_dir)


# ---------------------------------------------------------------- CLI de simulación


@pytest.fixture
def cli_env(tmp_path, monkeypatch, loan_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"API_KEY": "key-de-test", "API_SECRET": "secret-de-test"}))
    monkeypatch.setenv("FUTURESBOARD_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv(rescate.ENV_LOAN_STATE, str(loan_path))
    return cfg_dir


def test_cli_simular_solo_en_dry_run(cli_env, monkeypatch, loan_path, capsys):
    monkeypatch.setenv(rescate.ENV_MODE, "real")
    assert rescate.main(["simular-inyeccion", "500"]) == 2
    assert not loan_path.exists()
    monkeypatch.setenv(rescate.ENV_MODE, "dry_run")
    assert rescate.main(["simular-inyeccion", "500"]) == 0
    assert json.loads(loan_path.read_text())["loan_outstanding_usdt"] == 500.0
    assert json.loads(loan_path.read_text())["mode"] == "dry_run"
    assert rescate.main(["simular-devolucion", "200"]) == 0
    assert json.loads(loan_path.read_text())["loan_outstanding_usdt"] == 300.0
    assert rescate.main(["simular-devolucion", "1000"]) == 2
    assert rescate.main(["simular-devolucion"]) == 0
    assert json.loads(loan_path.read_text())["loan_outstanding_usdt"] == 0.0
    assert rescate.main(["simular-inyeccion", "nan"]) == 2
    state, _ = rescate.load_state(cli_env)
    assert [e["estado"] for e in state["ledger"]] == ["simulada"] * 3
    assert state["prestamo"]["real"] == "0.00"


# ---------------------------------------------------------------- panel


def _cfg_body(**kw):
    body = {"habilitado": True, "reserva_max_usdt": 5000, "paso_usdt": 1000, "umbral_ratio": 3,
            "tope_perdida_pct": 30}
    body.update(kw)
    return body


def _post_raw(client, url, raw, token="tok"):  # noqa: F811
    return client.post(url, data=raw, content_type="application/json", headers={"X-CSRF-Token": token})


def _data_dir(app):  # noqa: F811
    return pathlib.Path(app.config["DATABASE"]).parent


def test_panel_requiere_login_y_csrf(app, client):  # noqa: F811
    anon = app.test_client()
    assert anon.get("/rescate").status_code == 302
    assert anon.get("/api/rescate/status").status_code == 302
    assert anon.post("/api/rescate/config", json=_cfg_body()).status_code in (302, 403)
    assert _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body()), token="mal").status_code == 403
    assert _post_raw(client, "/api/rescate/reconocer-tope", json.dumps({"id": "x"}), token="").status_code == 403
    assert client.get("/api/rescate/config").status_code == 405


def test_panel_pagina_y_status(app, client):  # noqa: F811
    resp = client.get("/rescate")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Rescate (colchón)" in html and "no garantiza evitar pérdidas" in html
    status = client.get("/api/rescate/status").get_json()
    assert status["config"]["habilitado"] is False and status["tope"] is None
    assert status["start_bloqueado"] is None and "no garantiza" in status["aviso"]


@pytest.mark.parametrize("raw", [
    json.dumps(_cfg_body(reserva_max_usdt=float("nan"))),
    '{"habilitado": true, "reserva_max_usdt": NaN, "paso_usdt": 1000, "umbral_ratio": 3, "tope_perdida_pct": 30}',
    '{"habilitado": true, "reserva_max_usdt": Infinity, "paso_usdt": 1000, "umbral_ratio": 3, "tope_perdida_pct": 30}',
    json.dumps(_cfg_body(reserva_max_usdt=True)),
    json.dumps(_cfg_body(reserva_max_usdt="5000")),
    json.dumps(_cfg_body(habilitado="true")),
    json.dumps(_cfg_body(reserva_max_usdt=5)),
    json.dumps(_cfg_body(reserva_max_usdt=2_000_000)),
    json.dumps(_cfg_body(paso_usdt=6000)),
    json.dumps(_cfg_body(umbral_ratio=1)),
    json.dumps(_cfg_body(umbral_ratio=11)),
    json.dumps(_cfg_body(tope_perdida_pct=0.5)),
    json.dumps(_cfg_body(tope_perdida_pct=95)),
    json.dumps(_cfg_body(tope_perdida_pct=None)),
    json.dumps(_cfg_body(reserva_max_usdt=None)),
    json.dumps(_cfg_body(paso_usdt=None, habilitado=False)),
    json.dumps({**_cfg_body(), "extra": 1}),
    json.dumps({"habilitado": True}),
    json.dumps([1, 2]),
])
def test_panel_validacion_estricta(app, client, raw):  # noqa: F811
    resp = _post_raw(client, "/api/rescate/config", raw)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["ok"] is False
    assert not (_data_dir(app) / rescate.CONFIG_FILE_NAME).exists()
    assert audit_lines(app)[-1]["action"] == "rescate_config" and audit_lines(app)[-1]["result"] == "rechazado"


def test_panel_generaciones(app, client):  # noqa: F811
    ok = _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body()))
    assert ok.status_code == 200
    assert ok.get_json()["status"]["config"]["generacion"] == 1
    assert ok.get_json()["status"]["config"]["reserva_max_usdt"] == "5000"
    # Cambiar parámetros con el rescate habilitado no cambia la base.
    ok = _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body(paso_usdt=500.5)))
    assert ok.get_json()["status"]["config"]["generacion"] == 1
    assert ok.get_json()["status"]["config"]["paso_usdt"] == "500.5"
    _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body(habilitado=False, tope_perdida_pct=None)))
    ok = _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body()))
    assert ok.get_json()["status"]["config"]["generacion"] == 2  # habilitar de nuevo = base nueva
    assert audit_lines(app)[-1]["result"] == "ok"


def test_panel_start_bloqueado_por_tope_y_reconocimiento(app, client, env, docker, fake, loan_path):  # noqa: F811
    data_dir = _data_dir(app)
    assert _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body(tope_perdida_pct=10))).status_code == 200
    h = Harness(data_dir, loan_path, fake)
    fake.now_ms = int(h.clock.t * 1000)
    svc = h.service()
    svc.tick()
    fake.margin = Decimal("-500")
    svc.tick()
    tope = h.state()["tope"]
    assert h.apagados == [1]

    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 409 and "tope de pérdida" in resp.get_json()["error"]
    assert docker.actions() == []
    assert "tope de pérdida" in client.get("/api/bot/status").get_json()["rescate_start_bloqueado"]
    # No se puede volver a habilitar sin reconocer.
    assert _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body())).status_code == 409
    assert _post_raw(client, "/api/rescate/reconocer-tope", json.dumps({"id": "otro"})).status_code == 409
    assert _post_raw(client, "/api/rescate/reconocer-tope", json.dumps({"id": 5})).status_code == 400

    ack = _post_raw(client, "/api/rescate/reconocer-tope", json.dumps({"id": tope["id"]}))
    assert ack.status_code == 200
    status = ack.get_json()["status"]
    assert status["start_bloqueado"] is None and status["config"]["habilitado"] is False
    assert post(client, "/api/bot/start", {"riesgo": "bajo"}).status_code == 200
    svc.tick()
    assert h.state()["tope"] is None
    assert h.apagados == [1]


def test_start_rechequea_el_bloqueo_dentro_del_job(app, client, env, docker, monkeypatch):  # noqa: F811
    from futuresboard import blueprint

    llamadas = []

    def bloqueo(data_dir):
        llamadas.append(data_dir)
        return None if len(llamadas) == 1 else "START bloqueado: tope de pérdida"

    monkeypatch.setattr(blueprint.rescate, "start_bloqueado", bloqueo)
    resp = post(client, "/api/bot/start", {"riesgo": "bajo"})
    assert resp.status_code == 409 and len(llamadas) == 2
    assert docker.actions() == []


def test_telegram_en_cada_evento(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("3000")
    svc = h.service()
    svc.tick()                       # base + inyección
    h.fake.transfer_mode = "reject"
    h.avanzar(1)
    svc.tick()                       # error de transferencia
    h.fake.margin = Decimal("-500")
    svc.tick()                       # tope + Apagar
    textos = "\n".join(h.sent)
    for esperado in ("Rescate habilitado", "Inyección 1000.00", "rechazada por Binance", "TOPE DE PÉRDIDA", "APAGAR"):
        assert esperado in textos
    assert "key-de-test" not in textos and "secret-de-test" not in textos and "signature" not in textos

