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

    def service(self, mode="real", testnet=False, apagar=True, exchange="binance", env=None):
        settings = rescate.RescateSettings({rescate.ENV_MODE: mode, rescate.ENV_LOAN_STATE: str(self.loan_path),
                                            **(env or {})})
        client = rescate.RescateBinance(FUT, SPOT, "key-de-test", "secret-de-test", session=self.fake)
        return rescate.RescateService(self.data_dir, settings, client, testnet=testnet, exchange=exchange,
                                      apagar=self.apagar if apagar else None,
                                      send=lambda t: self.sent.append(t) or True, clock=self.clock)

    def configurar(self, habilitado=True, reserva=5000, paso=1000, umbral=8, devolucion=16, tope=30):
        parsed = rescate.validate_config_body({"habilitado": habilitado, "reserva_max_usdt": reserva,
                                               "paso_usdt": paso, "umbral_ratio": umbral,
                                               "umbral_devolucion_ratio": devolucion,
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


def confirmar_tope(h, svc=None):
    """Segunda lectura del cruce del piso, separada por la confirmación por defecto: ejecuta el tope."""
    h.avanzar(rescate.DEFAULT_TOPE_CONFIRM_S + 1)
    (svc or h.service()).tick()


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
    h.configurar()  # umbral por defecto: 8
    assert h.state()["base"] is None
    h.fake.margin, h.fake.maint = Decimal("8000"), Decimal("1000")  # ratio exactamente 8
    svc = h.service()
    svc.tick()
    assert h.fake.posts() == []
    assert h.state()["base"]["capital"] == "28000.00"
    h.fake.margin = Decimal("7999.99")
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
    h.fake.maint = Decimal("3000")  # el ratio sigue < 8 aunque entre plata
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
    h.fake.margin = Decimal("7500")  # ratio 7.5: tras inyectar 1000 queda en 8.5 (no inyecta más)
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
    h.fake.margin = Decimal("7500")  # ratio 7.5: tras inyectar 1000 queda en 8.5 (no inyecta más)
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
    h.fake.margin = Decimal("7500")  # ratio 7.5: tras inyectar 1000 queda en 8.5 (no inyecta más)
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
    assert h.apagados == [] and h.state()["tope_confirmacion"] is not None  # primer cruce: en confirmación
    confirmar_tope(h, svc)
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
    confirmar_tope(h, svc)
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
    confirmar_tope(h, svc)
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
    h.fake.margin = Decimal("7500")  # ratio 7.5 < 8
    svc = h.service(mode="dry_run")
    svc.tick()
    assert not [p for p in h.fake.paths() if p.startswith("/sapi")]
    st = h.state()
    assert st["prestamo"]["dry_run"] == "1000.00" and st["prestamo"]["real"] == "0.00"
    assert h.loan_file() == {**h.loan_file(), "mode": "dry_run", "loan_outstanding_usdt": 1000.0}
    assert st["ledger"][-1]["estado"] == "simulada"
    # El ratio simulado ya cuenta lo inyectado (7500 + 1000) / 1000 = 8.5: no vuelve a inyectar.
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


def test_prestamo_real_no_se_deja_de_publicar_en_dry_run(h):
    h.configurar()
    st = h.state()
    st["prestamo"] = {"real": "1000.00", "dry_run": "0.00"}
    st["servicio"] = {"modo": "real"}
    rescate.save_state(h.data_dir, st)
    rescate.write_loan_state(h.loan_path, Decimal("1000"), "real")
    h.fake.margin, h.fake.maint = Decimal("1000"), Decimal("1000")  # cerca de la liquidación
    svc = h.service(mode="dry_run")
    svc.tick()
    h.fake.positions = {}
    svc.tick()
    st = h.state()
    assert h.loan_file()["mode"] == "real" and h.loan_file()["loan_outstanding_usdt"] == 1000.0
    assert "préstamo REAL pendiente" in st["bloqueo"]
    assert st["prestamo"] == {"real": "1000.00", "dry_run": "0.00"} and st["ledger"] == []
    assert not [p for p in h.fake.paths() if p.startswith("/sapi")]
    status = rescate.panel_status(h.data_dir)
    assert status["prestamo"] == "1000.00" and status["prestamo_modo"] == "real"


def test_transferencia_real_en_vuelo_queda_en_dry_run(h):
    h.configurar()
    h.fake.transfer_mode = "timeout"
    h.service().tick()
    assert h.state()["en_vuelo"]["modo"] == "real"
    calls = len(h.fake.calls)
    svc = h.service(mode="dry_run")
    svc.tick()
    st = h.state()
    assert st["en_vuelo"]["estado"] == "incierto" and st["prestamo"]["real"] == "1000.00"
    assert not [p for _, p, _ in h.fake.calls[calls:] if p.startswith("/sapi")]
    assert h.loan_file() == {**h.loan_file(), "mode": "real", "loan_outstanding_usdt": 1000.0}


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


def test_cli_no_simula_con_prestamo_real_pendiente(cli_env, monkeypatch, loan_path):
    state = rescate.default_state()
    state["prestamo"]["real"] = "500.00"
    rescate.save_state(cli_env, state)
    rescate.write_loan_state(loan_path, Decimal("500"), "real")
    monkeypatch.setenv(rescate.ENV_MODE, "dry_run")
    assert rescate.main(["simular-inyeccion", "100"]) == 2
    assert rescate.main(["simular-devolucion"]) == 2
    assert json.loads(loan_path.read_text())["mode"] == "real"
    assert json.loads(loan_path.read_text())["loan_outstanding_usdt"] == 500.0


# ---------------------------------------------------------------- panel


def _cfg_body(**kw):
    body = {"habilitado": True, "reserva_max_usdt": 5000, "paso_usdt": 1000, "umbral_ratio": 8,
            "umbral_devolucion_ratio": 16, "tope_perdida_pct": 30}
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


def test_umbral_por_defecto_es_8(app, client):  # noqa: F811
    assert rescate.DEFAULT_UMBRAL == Decimal("8")
    assert rescate.default_config()["umbral_ratio"] == "8"
    assert client.get("/api/rescate/status").get_json()["config"]["umbral_ratio"] == "8"
    # Una config guardada antes del cambio conserva su umbral (el default no migra lo guardado).
    data_dir = _data_dir(app)
    (data_dir / rescate.CONFIG_FILE_NAME).write_text(json.dumps({**rescate.default_config(), "umbral_ratio": "3"}))
    assert client.get("/api/rescate/status").get_json()["config"]["umbral_ratio"] == "3"


_RAW = '"paso_usdt": 1000, "umbral_ratio": 8, "umbral_devolucion_ratio": 16, "tope_perdida_pct": 30}'


@pytest.mark.parametrize("raw,error", [
    (json.dumps(_cfg_body(reserva_max_usdt=float("nan"))), "reserva_max_usdt tiene que ser un número finito"),
    ('{"habilitado": true, "reserva_max_usdt": NaN, ' + _RAW, "reserva_max_usdt tiene que ser un número finito"),
    ('{"habilitado": true, "reserva_max_usdt": Infinity, ' + _RAW, "reserva_max_usdt tiene que ser un número finito"),
    (json.dumps(_cfg_body(reserva_max_usdt=True)), "reserva_max_usdt tiene que ser un número."),
    (json.dumps(_cfg_body(reserva_max_usdt="5000")), "reserva_max_usdt tiene que ser un número."),
    (json.dumps(_cfg_body(habilitado="true")), "habilitado tiene que ser true o false"),
    (json.dumps(_cfg_body(reserva_max_usdt=5)), "reserva_max_usdt fuera de rango"),
    (json.dumps(_cfg_body(reserva_max_usdt=2_000_000)), "reserva_max_usdt fuera de rango"),
    (json.dumps(_cfg_body(paso_usdt=6000)), "El paso no puede ser mayor que la reserva"),
    (json.dumps(_cfg_body(umbral_ratio=1)), "umbral_ratio fuera de rango"),
    (json.dumps(_cfg_body(umbral_ratio=11)), "umbral_ratio fuera de rango"),
    (json.dumps(_cfg_body(tope_perdida_pct=0.5)), "tope_perdida_pct fuera de rango"),
    (json.dumps(_cfg_body(tope_perdida_pct=95)), "tope_perdida_pct fuera de rango"),
    (json.dumps(_cfg_body(tope_perdida_pct=None)), "hay que fijar el tope de pérdida"),
    (json.dumps(_cfg_body(reserva_max_usdt=None)), "hay que fijar la reserva máxima"),
    (json.dumps(_cfg_body(paso_usdt=None, habilitado=False)), "paso_usdt es obligatorio"),
    (json.dumps({**_cfg_body(), "extra": 1}), "Campos desconocidos: extra"),
    (json.dumps({"habilitado": True}), "Faltan campos"),
    (json.dumps([1, 2]), "Body inválido"),
    # Umbral de devolución parcial: obligatorio, en rango y mayor que el de inyección (histéresis).
    (json.dumps({k: v for k, v in _cfg_body().items() if k != "umbral_devolucion_ratio"}),
     "Faltan campos: umbral_devolucion_ratio"),
    (json.dumps(_cfg_body(umbral_devolucion_ratio=None)), "umbral_devolucion_ratio es obligatorio"),
    (json.dumps(_cfg_body(umbral_devolucion_ratio="16")), "umbral_devolucion_ratio tiene que ser un número."),
    (json.dumps(_cfg_body(umbral_devolucion_ratio=1.5)), "umbral_devolucion_ratio fuera de rango"),
    (json.dumps(_cfg_body(umbral_devolucion_ratio=101)), "umbral_devolucion_ratio fuera de rango"),
    (json.dumps(_cfg_body(umbral_devolucion_ratio=8)), "tiene que ser mayor que el de inyección"),
    (json.dumps(_cfg_body(umbral_ratio=10, umbral_devolucion_ratio=9)), "tiene que ser mayor que el de inyección"),
])
def test_panel_validacion_estricta(app, client, raw, error):  # noqa: F811
    resp = _post_raw(client, "/api/rescate/config", raw)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["ok"] is False
    assert error in resp.get_json()["error"]  # cada caso falla por su motivo, no por otro
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
    confirmar_tope(h, svc)
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
    svc.tick()                       # tope en confirmación
    confirmar_tope(h, svc)           # tope + Apagar
    textos = "\n".join(h.sent)
    for esperado in ("Rescate habilitado", "Inyección 1000.00", "rechazada por Binance", "EN CONFIRMACIÓN",
                     "TOPE DE PÉRDIDA", "APAGAR"):
        assert esperado in textos
    assert "key-de-test" not in textos and "secret-de-test" not in textos and "signature" not in textos



# ---------------------------------------------------------------- revisión adversarial (tope y préstamo)


def _tope_pendiente_en_disco(h, intentos):
    """Estado tal como queda en disco si el proceso muere durante el Apagar del tope (SIGTERM con el handler
    por defecto: no corre ningún finally)."""
    st = h.state()
    st["tope"] = {"id": "t0pe", "generacion": st["base"]["generacion"], "disparado_en": "2023-11-14T22:13:20+00:00",
                  "modo": "real", "capital": "100.00", "base": st["base"]["capital"], "limite": "19800.00",
                  "pct": "10", "apagar": {"resultado": "pendiente", "intentos": intentos,
                                          "ultimo_ms": None if intentos == 0 else h.fake.now_ms, "detalle": ""}}
    rescate.save_state(h.data_dir, st)


def test_apagar_cortado_antes_de_arrancar_se_ejecuta_en_el_proceso_nuevo(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("100")  # lejos de la liquidación: no inyecta
    h.service().tick()
    _tope_pendiente_en_disco(h, intentos=0)
    h.avanzar(15)
    h.service().tick()  # proceso nuevo (restart: unless-stopped)
    ap = h.state()["tope"]["apagar"]
    assert h.apagados == [1]
    assert ap["resultado"] == "ok" and ap["intentos"] == 1


def test_apagar_cortado_en_medio_cuenta_el_intento_reintenta_y_avisa(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("100")
    h.service().tick()
    _tope_pendiente_en_disco(h, intentos=1)
    h.avanzar(15)
    svc = h.service()
    svc.tick()
    ap = h.state()["tope"]["apagar"]
    assert h.apagados == [1]  # se reintenta ya, sin esperar APAGAR_REINTENTO_MS
    assert ap["resultado"] == "ok" and ap["intentos"] == 2
    assert any("cortado por un reinicio" in t for t in h.sent)


def test_apagar_cortado_en_el_ultimo_intento_pide_intervencion_manual(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("100")
    h.service().tick()
    _tope_pendiente_en_disco(h, intentos=rescate.APAGAR_MAX_INTENTOS)
    h.avanzar(15)
    h.service().tick()
    assert h.apagados == []
    assert h.state()["tope"]["apagar"]["resultado"] == "error"
    assert any("intervención manual" in t for t in h.sent)


def test_apagar_persiste_el_intento_antes_de_ejecutar(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("100")
    svc = h.service()
    svc.tick()
    vistos = []

    def apagar():
        vistos.append(h.state()["tope"]["apagar"])
        return h.apagar_result

    svc._apagar = apagar
    h.fake.margin = Decimal("-500")
    svc.tick()
    confirmar_tope(h, svc)
    # Si el proceso muere acá adentro, el próximo ve el intento contado (no reintenta para siempre).
    assert vistos and vistos[0]["resultado"] == "pendiente" and vistos[0]["intentos"] == 1


def test_tope_real_no_queda_simulado_por_pasar_por_dry_run(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("100")
    h.apagar_result = ("error", {"ok": False, "error": "docker-proxy caído"})
    svc = h.service()
    svc.tick()
    h.fake.margin = Decimal("-500")
    svc.tick()
    confirmar_tope(h, svc)
    assert len(h.apagados) == 1 and h.state()["tope"]["modo"] == "real"
    h.avanzar(rescate.APAGAR_REINTENTO_MS / 1000 + 10)
    h.service(mode="dry_run").tick()  # el operador reinicia en dry_run para diagnosticar
    tope = h.state()["tope"]
    assert tope["apagar"]["resultado"] == "error" and tope["modo"] == "real"
    assert len(h.apagados) == 2  # el tope es real: el reintento es el Apagar real
    h.apagar_result = ("ok", {"ok": True, "modo": "apagar"})
    h.avanzar(rescate.APAGAR_REINTENTO_MS / 1000 + 10)
    h.service().tick()
    assert len(h.apagados) == 3 and h.state()["tope"]["apagar"]["resultado"] == "ok"


@pytest.mark.parametrize("apagar", [True, False])
def test_dry_run_con_prestamo_real_el_tope_es_real(h, apagar):
    h.configurar(tope=30)
    h.fake.maint = Decimal("100")
    h.service().tick()  # base 22000 en real
    st = h.state()
    st["prestamo"]["real"] = "2000.00"
    rescate.save_state(h.data_dir, st)
    h.fake.margin, h.fake.spot = Decimal("10"), Decimal("10")
    h.avanzar(15)
    svc = h.service(mode="dry_run", apagar=apagar)
    svc.tick()
    confirmacion = next(t for t in h.sent if "EN CONFIRMACIÓN" in t)
    assert not confirmacion.startswith("🧪")  # plata real: la confirmación tampoco es simulada
    confirmar_tope(h, svc)
    tope = h.state()["tope"]
    assert tope["modo"] == "real"
    assert "simulado" not in rescate.start_bloqueado(h.data_dir)
    disparo = next(t for t in h.sent if "TOPE DE PÉRDIDA" in t)
    assert not disparo.startswith("🧪")  # es plata real: sin [SIMULACIÓN]
    if apagar:
        assert h.apagados == [1] and tope["apagar"]["resultado"] == "ok"
    else:
        assert tope["apagar"]["resultado"] == "error" and "no disponible" in tope["apagar"]["detalle"]


def test_inyeccion_publica_el_prestamo_antes_del_post(h):
    h.configurar(reserva=5000, paso=1000)
    h.fake.maint = Decimal("3000")  # sigue cerca de la liquidación aunque entre plata
    vistos = []
    original = h.fake._execute

    def execute(tipo, amount):
        vistos.append(h.loan_file()["loan_outstanding_usdt"])  # lo que lee passivbot cuando Binance acredita
        return original(tipo, amount)

    h.fake._execute = execute
    svc = h.service()
    svc.tick()
    svc.tick()
    assert vistos == [1000.0, 2000.0]


def test_rechazo_revierte_el_prestamo_publicado(h):
    h.configurar()
    h.fake.transfer_mode = "reject"
    vistos = []
    original = h.fake.request

    def request(method, url, **kw):
        if method == "POST":
            vistos.append(h.loan_file()["loan_outstanding_usdt"])
        return original(method, url, **kw)

    h.fake.request = request
    h.service().tick()
    assert vistos == [1000.0]
    assert h.loan_file()["loan_outstanding_usdt"] == 0.0 and h.state()["prestamo"]["real"] == "0.00"
    assert h.state()["ledger"][-1]["prestamo_despues"] == "0.00"


@pytest.mark.parametrize("historial_caido", [False, True])
def test_reinicio_con_inyeccion_enviando_aplica_el_prestamo(h, historial_caido):
    h.configurar()
    h.fake.transfer_mode, h.fake.ejecutar_ambigua, h.fake.history_visible = "timeout", True, False
    h.service().tick()
    st = h.state()
    st["en_vuelo"]["estado"] = "enviando"  # estado escrito antes del POST por una versión anterior
    st["en_vuelo"]["prestamo_aplicado"] = False
    st["prestamo"]["real"] = "0.00"
    rescate.save_state(h.data_dir, st)
    rescate.write_loan_state(h.loan_path, Decimal(0), "real")
    h.fake.transfer_mode = "ok"
    if historial_caido:
        h.fake.read_fail.add("/sapi/v1/asset/transfer")
    h.avanzar(15)
    svc = h.service()
    svc.tick()
    assert h.loan_file()["loan_outstanding_usdt"] == 1000.0  # conservador desde el primer ciclo
    assert h.state()["prestamo"]["real"] == "1000.00" and h.state()["en_vuelo"]["estado"] == "incierto"
    h.avanzar(60)
    svc.tick()
    assert h.loan_file()["loan_outstanding_usdt"] == 1000.0


def _inyectar_real(h, tope=30):
    h.configurar(tope=tope)
    svc = h.service()
    svc.tick()  # base 22000 + inyección de 1000 (ratio 2 < 8)
    st = h.state()
    assert st["base"]["capital"] == "22000.00" and st["prestamo"]["real"] == "1000.00"
    # Ratio 3000 / 250 = 12: entre el umbral de inyección (8) y el de devolución parcial (16), así el préstamo
    # queda pendiente (ni se inyecta más ni se devuelve).
    h.fake.maint = Decimal("250")
    return svc


@pytest.mark.parametrize("como", ["deshabilitado", "config_invalida"])
def test_tope_sigue_armado_con_prestamo_real_pendiente(h, como):
    svc = _inyectar_real(h)
    if como == "deshabilitado":
        h.configurar(habilitado=False, reserva=None, tope=None)  # lo que manda el panel al destildar
    else:
        (h.data_dir / rescate.CONFIG_FILE_NAME).write_text("{roto")
    h.fake.margin = Decimal("-4000")  # capital 19000 - 4000 = 15000 <= 15400 (22000 x 0.7)
    h.avanzar(15)
    svc.tick()
    confirmar_tope(h, svc)
    tope = h.state()["tope"]
    assert tope is not None and tope["limite"] == "15400.00" and tope["modo"] == "real"
    assert h.apagados == [1]
    status = rescate.panel_status(h.data_dir)
    assert status["tope"]["id"] == tope["id"]


def test_tope_armado_por_prestamo_se_ve_en_el_panel(h):
    _inyectar_real(h)
    h.configurar(habilitado=False, reserva=None, tope=None)
    h.service().tick()
    status = rescate.panel_status(h.data_dir)
    assert status["base"]["capital"] == "22000.00" and status["limite_tope"] == "15400.00"
    assert status["tope_pct"] == "30" and status["tope_por_prestamo"] is True


def test_rehabilitar_con_prestamo_real_mantiene_la_base(h):
    svc = _inyectar_real(h)
    h.configurar(habilitado=False, reserva=None, tope=None)
    h.fake.margin = Decimal("-2000")  # capital 17000: perdió plata, todavía arriba de 15400
    h.avanzar(15)
    svc.tick()
    h.configurar(tope=30)  # generación nueva
    h.avanzar(15)
    svc.tick()
    st = h.state()
    assert st["base"]["capital"] == "22000.00" and st["base"]["generacion"] == 2
    assert st["tope"] is None
    h.fake.margin = Decimal("-4000")
    h.avanzar(15)
    svc.tick()
    confirmar_tope(h, svc)
    assert h.state()["tope"]["limite"] == "15400.00" and h.apagados == [1]


def test_tope_reconocido_con_prestamo_pendiente_no_se_redispara(h):
    svc = _inyectar_real(h)
    h.fake.margin = Decimal("-4000")
    h.avanzar(15)
    svc.tick()
    confirmar_tope(h, svc)
    tope = h.state()["tope"]
    assert h.apagados == [1]
    rescate.reconocer_tope(h.data_dir, tope["id"], "cliente17")
    for _ in range(3):
        h.avanzar(15)
        svc.tick()
    assert h.state()["tope"] is None and h.apagados == [1]
    assert h.state()["prestamo"]["real"] == "1000.00"


def test_spot_caido_evalua_el_tope_con_el_ultimo_spot(h):
    h.configurar(tope=30)
    h.fake.margin, h.fake.spot, h.fake.maint = Decimal("20000"), Decimal("2000"), Decimal("100")
    svc = h.service()
    svc.tick()
    assert h.state()["base"]["capital"] == "22000.00"
    h.fake.read_fail.add("/api/v3/account")
    h.fake.margin = Decimal("13000")  # 13000 + 2000 = 15000 <= 15400
    h.avanzar(15)
    svc.tick()
    confirmar_tope(h, svc)  # sigue sin spot: también se confirma con el último saldo spot leído
    tope = h.state()["tope"]
    assert tope is not None and h.apagados == [1]
    assert any("último saldo spot" in t for t in h.sent)


def test_spot_caido_con_margen_sano_no_dispara(h):
    h.configurar(tope=30)
    h.fake.maint = Decimal("100")
    svc = h.service()
    svc.tick()  # base 22000 = 2000 de margen + 20000 de spot
    h.fake.read_fail.add("/api/v3/account")
    for _ in range(3):
        h.avanzar(15)
        svc.tick()
    # Con "solo margin_balance" (2000 <= 15400) se cerraría todo a mercado por una caída de la API de spot.
    assert h.state()["tope"] is None and h.apagados == []
    assert h.fake.posts() == []


def test_spot_caido_descuenta_la_inyeccion_posterior_a_la_lectura(h):
    h.configurar(tope=30)
    h.fake.margin, h.fake.spot, h.fake.maint = Decimal("20000"), Decimal("2000"), Decimal("7000")
    svc = h.service()
    svc.tick()  # lee spot 2000, después inyecta 1000 (ratio 2.86 < 8): spot real 1000
    assert [p["amount"] for p in h.fake.posts()] == ["1000.00"]
    h.fake.read_fail.add("/api/v3/account")
    h.fake.margin = Decimal("14400")  # capital real 14400 + 1000 = 15400 <= 15400
    h.avanzar(15)
    svc.tick()
    confirmar_tope(h, svc)
    assert h.state()["tope"] is not None and h.apagados == [1]


def test_futuros_caidos_avisa_que_el_tope_no_se_evalua(h):
    h.configurar(tope=30)
    svc = h.service()
    svc.tick()
    h.fake.read_fail.add("/fapi/v2/account")
    h.avanzar(15)
    svc.tick()
    aviso = next(t for t in h.sent if "No se pudo leer la cuenta" in t)
    assert "tope de pérdida" in aviso


# ---------------------------------------------------------------- confirmación del tope


def _base_sin_prestamo(h, tope=10):
    h.configurar(tope=tope)
    h.fake.maint = Decimal("100")  # lejos de la liquidación: no inyecta
    svc = h.service()
    svc.tick()
    assert h.state()["base"]["capital"] == "22000.00"  # límite 19800 con tope 10%
    return svc


def _cruzar(h, svc, margin="-500"):
    h.fake.margin = Decimal(margin)  # capital = margin + 20000 de spot
    h.avanzar(15)
    svc.tick()


def test_tope_confirmacion_cancelada_por_rebote(h):
    svc = _base_sin_prestamo(h)
    _cruzar(h, svc)  # mecha: capital 19500 <= 19800 (y ratio < 8)
    st = h.state()
    conf = st["tope_confirmacion"]
    assert st["tope"] is None and h.apagados == []
    assert conf["capital"] == "19500.00" and conf["limite"] == "19800.00" and conf["confirmar_ms"] == 60_000
    assert h.fake.posts() == []  # durante la confirmación no se inyecta
    assert rescate.start_bloqueado(h.data_dir) is None
    assert any("EN CONFIRMACIÓN" in t for t in h.sent)
    h.fake.margin = Decimal("2000")  # rebota
    h.avanzar(15)
    svc.tick()
    st = h.state()
    assert st["tope_confirmacion"] is None and st["tope"] is None
    assert any("cancelada" in t and "No se ejecutó Apagar" in t for t in h.sent)
    for _ in range(3):
        h.avanzar(61)
        svc.tick()
    assert h.apagados == [] and h.state()["tope"] is None
    eventos = [e["texto"] for e in h.state()["eventos"]]
    assert any("EN CONFIRMACIÓN" in e for e in eventos) and any("cancelada" in e for e in eventos)


def test_tope_confirmado_ejecuta_apagar(h):
    svc = _base_sin_prestamo(h)
    _cruzar(h, svc)
    h.fake.margin = Decimal("-600")
    h.avanzar(30)
    svc.tick()  # 30 s después: sigue debajo, pero todavía no pasó la confirmación
    assert h.apagados == [] and h.state()["tope_confirmacion"]["observaciones"] == 2
    h.avanzar(30)
    svc.tick()  # 60 s después del primer cruce, sigue debajo: se ejecuta
    st = h.state()
    assert h.apagados == [1] and st["tope_confirmacion"] is None
    assert st["tope"]["capital"] == "19400.00" and st["tope"]["apagar"]["resultado"] == "ok"
    assert rescate.start_bloqueado(h.data_dir)
    disparo = next(t for t in h.sent if "TOPE DE PÉRDIDA" in t)
    assert "Confirmado: 60 s" in disparo
    assert h.fake.posts() == []


@pytest.mark.parametrize("rebota", [False, True])
def test_tope_confirmacion_sobrevive_reinicio(h, rebota):
    svc = _base_sin_prestamo(h)
    en_disco = []
    svc.send = lambda t: en_disco.append((t, h.state()["tope_confirmacion"])) or True
    _cruzar(h, svc)
    aviso, conf = next((t, c) for t, c in en_disco if "EN CONFIRMACIÓN" in t)
    assert conf is not None  # persistida antes de avisar
    if rebota:
        h.fake.margin = Decimal("2000")
    h.avanzar(90)
    h.service().tick()  # proceso nuevo (deploy, OOM): retoma la confirmación de rescate_state.json
    st = h.state()
    assert st["tope_confirmacion"] is None
    if rebota:
        assert h.apagados == [] and st["tope"] is None
        assert any("cancelada" in t for t in h.sent)
    else:
        assert h.apagados == [1] and st["tope"]["capital"] == "19500.00"


@pytest.mark.parametrize("caida", ["futuros", "spot_sin_referencia"])
def test_tope_confirmacion_espera_maxima_sin_lecturas(h, caida):
    svc = _base_sin_prestamo(h)
    _cruzar(h, svc)
    conf = h.state()["tope_confirmacion"]
    assert conf["max_ms"] == rescate.tope_espera_max_s(rescate.DEFAULT_TOPE_CONFIRM_S) * 1000 == 300_000
    if caida == "futuros":
        h.fake.read_fail.add("/fapi/v2/account")
    else:
        h.fake.read_fail.add("/api/v3/account")
        st = h.state()
        st["spot_ref"] = None
        rescate.save_state(h.data_dir, st)
    for _ in range(19):  # 285 s sin poder observar: no se confirma ni se cancela
        h.avanzar(15)
        svc.tick()
    assert h.apagados == [] and h.state()["tope_confirmacion"] is not None
    aviso = next(t for t in h.sent if "No se pudo leer la cuenta" in t)
    assert "a más tardar" in aviso
    h.avanzar(15)
    svc.tick()  # 300 s desde el primer cruce: se ejecuta con la última observación
    st = h.state()
    assert h.apagados == [1] and st["tope_confirmacion"] is None and st["tope"]["capital"] == "19500.00"
    assert "espera máxima" in next(t for t in h.sent if "TOPE DE PÉRDIDA" in t)


def test_tope_confirmacion_con_spot_caido_cancela_con_el_ultimo_spot(h):
    svc = _base_sin_prestamo(h)
    _cruzar(h, svc)
    h.fake.read_fail.add("/api/v3/account")
    h.fake.margin = Decimal("2000")  # 2000 + último spot leído (20000) > 19800
    h.avanzar(15)
    svc.tick()
    assert h.state()["tope_confirmacion"] is None and h.apagados == []
    assert any("cancelada" in t and "último saldo spot" in t for t in h.sent)


def test_tope_sin_confirmacion_con_cero_apaga_al_primer_cruce(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("100")
    svc = h.service(env={rescate.ENV_TOPE_CONFIRM: "0"})
    svc.tick()
    _cruzar(h, svc)
    assert h.apagados == [1] and h.state()["tope_confirmacion"] is None
    assert not any("EN CONFIRMACIÓN" in t for t in h.sent)


def test_tope_confirm_seconds_por_entorno():
    def seg(valor):
        return rescate.RescateSettings({rescate.ENV_TOPE_CONFIRM: valor}).tope_confirm_seconds

    assert rescate.RescateSettings({}).tope_confirm_seconds == rescate.DEFAULT_TOPE_CONFIRM_S == 60
    # Inválido o negativo: el default (nunca desactiva la confirmación por un typo); muy alto: el máximo.
    assert seg("") == seg("x") == seg("-60") == seg("1.5") == 60
    assert seg("0") == 0 and seg("90") == 90 and seg("99999") == rescate.MAX_TOPE_CONFIRM_S
    assert rescate.tope_espera_max_s(0) == rescate.tope_espera_max_s(60) == 300
    assert rescate.tope_espera_max_s(600) == 3000


@pytest.mark.parametrize("conf", [
    "roto",
    {"desde_ms": "x"},
    {"desde_ms": 1, "confirmar_ms": 60_000, "max_ms": 1, "ultimo_ms": 1, "ultimo_capital": "1"},
    {"desde_ms": 1, "confirmar_ms": 60_000, "max_ms": 300_000, "ultimo_ms": 1, "ultimo_capital": "NaN"},
])
def test_tope_confirmacion_invalida_en_disco_no_frena_el_tope(h, conf):
    svc = _base_sin_prestamo(h)
    st = h.state()
    st["tope_confirmacion"] = conf
    rescate.save_state(h.data_dir, st)
    h.fake.margin = Decimal("-500")
    h.avanzar(15)
    assert svc.tick() == {"ok": True}
    assert h.state()["tope_confirmacion"]["capital"] == "19500.00"  # se descartó y se abrió una nueva
    assert any("inválida" in t for t in h.sent)
    confirmar_tope(h, svc)
    assert h.apagados == [1]


def test_tope_confirmacion_cancelada_si_se_desarma(h):
    svc = _base_sin_prestamo(h)
    _cruzar(h, svc)
    h.configurar(habilitado=False, reserva=None, tope=None)  # sin préstamo real: el tope se desarma
    h.avanzar(61)
    svc.tick()
    assert h.state()["tope_confirmacion"] is None and h.apagados == []
    assert any("cancelada" in t and "desarmado" in t for t in h.sent)


def test_tope_confirmacion_con_reloj_hacia_atras_no_queda_colgada(h):
    svc = _base_sin_prestamo(h)
    _cruzar(h, svc)
    h.avanzar(-3600)  # el reloj del host se corrige una hora para atrás
    svc.tick()
    assert h.apagados == [] and h.state()["tope_confirmacion"] is not None
    confirmar_tope(h, svc)
    assert h.apagados == [1]


def test_tope_confirmacion_en_dry_run_es_simulada(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("100")
    svc = h.service(mode="dry_run")
    svc.tick()
    _cruzar(h, svc)
    confirmar_tope(h, svc)
    assert h.apagados == [] and h.state()["tope"]["apagar"]["resultado"] == "simulado"
    assert all(t.startswith("🧪") for t in h.sent)


def test_panel_muestra_tope_en_confirmacion(app, client, fake, loan_path):  # noqa: F811
    data_dir = _data_dir(app)
    assert _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body(tope_perdida_pct=10))).status_code == 200
    h = Harness(data_dir, loan_path, fake)
    fake.now_ms = int(h.clock.t * 1000)
    fake.maint = Decimal("100")
    svc = h.service()
    svc.tick()
    _cruzar(h, svc)
    status = client.get("/api/rescate/status").get_json()
    conf = status["tope_confirmacion"]
    assert conf["limite"] == "19800.00" and conf["capital"] == "19500.00"
    assert conf["ejecuta_desde_en"] and conf["ejecuta_maximo_en"]
    assert status["tope"] is None and status["start_bloqueado"] is None
    assert 'id="rescateConfirmacion"' in client.get("/rescate").get_data(as_text=True)


# ---------------------------------------------------------------- devolución parcial con margen sano


def _con_prestamo(h, prestamo="5000.00", margin="20000", maint="1000", max_withdraw="10000", **cfg):
    """Préstamo real pendiente con posiciones abiertas (default: ratio 20 >= 16)."""
    h.configurar(**cfg)
    st = h.state()
    st["prestamo"]["real"] = prestamo
    rescate.save_state(h.data_dir, st)
    rescate.write_loan_state(h.loan_path, Decimal(prestamo), "real")
    h.fake.margin, h.fake.maint, h.fake.max_withdraw = Decimal(margin), Decimal(maint), Decimal(max_withdraw)


def _devoluciones(h):
    return [p["amount"] for p in h.fake.posts() if p["type"] == "UMFUTURE_MAIN"]


def test_devolucion_parcial_respeta_el_ratio_objetivo(h):
    _con_prestamo(h)
    svc = h.service()
    svc.tick()
    # r = min(5000, 10000, 20000 - 16 x 1000) = 4000: el ratio queda justo en 16.
    assert _devoluciones(h) == ["4000.00"]
    assert h.fake.margin / h.fake.maint == 16
    st = h.state()
    assert st["prestamo"]["real"] == "1000.00" and h.loan_file()["loan_outstanding_usdt"] == 1000.0
    assert st["ledger"][-1]["estado"] == "confirmada" and st["ledger"][-1]["tipo"] == "devolucion"
    aviso = next(t for t in h.sent if "Devolución 4000.00" in t)
    assert "devolución parcial con margen sano" in aviso and "Préstamo pendiente: 1000.00" in aviso
    h.avanzar(15)
    svc.tick()
    assert _devoluciones(h) == ["4000.00"]  # ratio 16: no inyecta ni devuelve
    assert [p for p in h.fake.posts() if p["type"] == "MAIN_UMFUTURE"] == []


def test_devolucion_parcial_redondea_hacia_abajo_y_no_baja_del_umbral(h):
    _con_prestamo(h, margin="9000", maint="333.337")
    h.service().tick()
    # 9000 - 16 x 333.337 = 3666.608 -> 3666.60 (centavos hacia abajo): el ratio queda >= 16.
    assert _devoluciones(h) == ["3666.60"]
    assert h.fake.margin / h.fake.maint >= 16


def test_devolucion_parcial_limitada_por_max_withdraw(h):
    _con_prestamo(h, max_withdraw="1500.509")
    h.service().tick()
    assert _devoluciones(h) == ["1500.50"]
    assert h.state()["prestamo"]["real"] == "3499.50"
    assert any("maxWithdrawAmount 1500.50" in t for t in h.sent)


def test_devolucion_parcial_respeta_el_minimo(h):
    _con_prestamo(h, margin="16099.99")  # r = 99.99 < 100
    svc = h.service()
    svc.tick()
    assert _devoluciones(h) == [] and h.state()["prestamo"]["real"] == "5000.00"
    h.fake.margin = Decimal("16100")  # r = 100
    h.avanzar(15)
    svc.tick()
    assert _devoluciones(h) == ["100.00"]


def test_devolucion_parcial_residuo_menor_al_minimo_espera_a_quedar_sin_posiciones(h):
    _con_prestamo(h, prestamo="50.00")  # el ratio alcanza, pero r = 50 < 100
    svc = h.service()
    svc.tick()
    assert _devoluciones(h) == []
    h.fake.positions = {}
    h.avanzar(15)
    svc.tick()  # sin posiciones: devolución total, sin mínimo
    assert _devoluciones(h) == ["50.00"] and h.state()["prestamo"]["real"] == "0.00"


def test_devolucion_parcial_respeta_el_cooldown(h):
    _con_prestamo(h, max_withdraw="1000")
    svc = h.service()
    svc.tick()
    assert _devoluciones(h) == ["1000.00"]
    h.fake.max_withdraw = Decimal("1000")
    for _ in range(39):  # 585 s: sigue sano pero dentro del cooldown
        h.avanzar(15)
        svc.tick()
    assert _devoluciones(h) == ["1000.00"]
    h.avanzar(15)  # 600 s desde la anterior
    svc.tick()
    assert _devoluciones(h) == ["1000.00", "1000.00"]
    assert sum("devolución parcial" in t for t in h.sent) == 2  # Telegram en cada una


def test_devolucion_parcial_minimo_y_cooldown_por_entorno(h):
    _con_prestamo(h, margin="16050", max_withdraw="20")
    env = {rescate.ENV_DEVOLUCION_MIN: "10", rescate.ENV_DEVOLUCION_COOLDOWN: "0"}
    svc = h.service(env=env)
    svc.tick()
    h.avanzar(15)
    h.fake.max_withdraw = Decimal("20")
    svc.tick()
    assert _devoluciones(h) == ["20.00", "20.00"]
    s = rescate.RescateSettings({})
    assert s.devolucion_min_usdt == 100 and s.devolucion_cooldown_seconds == 600
    s = rescate.RescateSettings({rescate.ENV_DEVOLUCION_MIN: "0", rescate.ENV_DEVOLUCION_COOLDOWN: "-1"})
    assert s.devolucion_min_usdt == 100 and s.devolucion_cooldown_seconds == 600


def test_devolucion_parcial_solo_baja_el_prestamo_confirmada(h):
    _con_prestamo(h)
    h.fake.history_status = "PENDING"
    svc = h.service()
    svc.tick()
    st = h.state()
    assert _devoluciones(h) == ["4000.00"] and st["en_vuelo"]["estado"] == "pendiente"
    # La wallet ya bajó 4000 pero el préstamo publicado sigue completo: passivbot dimensiona de menos, nunca de más.
    assert st["prestamo"]["real"] == "5000.00" and h.loan_file()["loan_outstanding_usdt"] == 5000.0
    for row in h.fake.history:
        row["status"] = "CONFIRMED"
    h.avanzar(15)
    svc.tick()
    assert h.state()["prestamo"]["real"] == "1000.00" and h.loan_file()["loan_outstanding_usdt"] == 1000.0


def test_histeresis_inyeccion_devolucion_sin_ping_pong(h):
    h.configurar()
    h.fake.margin, h.fake.maint, h.fake.max_withdraw = Decimal("7500"), Decimal("1000"), Decimal("10000")
    svc = h.service()
    svc.tick()  # ratio 7.5 < 8: inyecta 1000 -> 8.5
    assert [p["type"] for p in h.fake.posts()] == ["MAIN_UMFUTURE"]
    for margin in ("8500", "12000", "15999.99", "9000"):  # entre 8 y 16: ni inyecta ni devuelve
        h.fake.margin = Decimal(margin)
        h.avanzar(15)
        svc.tick()
    assert len(h.fake.posts()) == 1 and h.state()["prestamo"]["real"] == "1000.00"
    h.fake.margin = Decimal("17000")  # sube el precio: ratio 17 >= 16
    h.avanzar(15)
    svc.tick()
    assert _devoluciones(h) == ["1000.00"] and h.state()["prestamo"]["real"] == "0.00"
    for _ in range(5):  # ratio 16 después de devolver: lejos del umbral de inyección
        h.avanzar(15)
        svc.tick()
    assert [p["type"] for p in h.fake.posts()] == ["MAIN_UMFUTURE", "UMFUTURE_MAIN"]


def test_devolucion_parcial_sigue_con_rescate_deshabilitado(h):
    _con_prestamo(h, habilitado=False, reserva=None, tope=None)
    h.service().tick()
    assert _devoluciones(h) == ["4000.00"]  # es plata prestada


def test_devolucion_parcial_no_con_tope_en_confirmacion_o_disparado(h):
    h.configurar(tope=10)
    h.fake.maint = Decimal("100")
    svc = h.service()
    svc.tick()  # base 22000, límite 19800
    st = h.state()
    st["prestamo"]["real"] = "1000.00"
    rescate.save_state(h.data_dir, st)
    h.fake.spot = Decimal("17000")  # capital 19000 <= 19800 con ratio 20 (margen sano)
    h.avanzar(15)
    svc.tick()
    assert h.state()["tope_confirmacion"] is not None and _devoluciones(h) == []
    confirmar_tope(h, svc)
    assert h.apagados == [1] and h.state()["tope"] is not None
    h.avanzar(15)
    svc.tick()  # el fake no cierra posiciones: con el tope disparado tampoco se devuelve parcial
    assert _devoluciones(h) == []


def test_devolucion_parcial_en_dry_run_es_simulada(h):
    h.configurar()
    st = h.state()
    st["prestamo"]["dry_run"] = "5000.00"
    st["servicio"] = {"modo": "dry_run"}
    rescate.save_state(h.data_dir, st)
    h.fake.margin, h.fake.maint, h.fake.max_withdraw = Decimal("15000"), Decimal("1000"), Decimal("10000")
    h.service(mode="dry_run").tick()  # margen simulado 15000 + 5000 = 20000: ratio 20
    assert not [p for p in h.fake.paths() if p.startswith("/sapi")]
    st = h.state()
    assert st["prestamo"]["dry_run"] == "1000.00" and st["ledger"][-1]["estado"] == "simulada"
    assert h.loan_file()["mode"] == "dry_run" and h.loan_file()["loan_outstanding_usdt"] == 1000.0


def test_config_guardada_con_umbral_de_devolucion_invalido_deshabilita(data_dir):
    cfg = {**rescate.default_config(), "habilitado": True, "reserva_max_usdt": "5000", "tope_perdida_pct": "30",
           "umbral_ratio": "8", "umbral_devolucion_ratio": "8"}
    (data_dir / rescate.CONFIG_FILE_NAME).write_text(json.dumps(cfg))
    loaded, err = rescate.load_config(data_dir)
    assert loaded["habilitado"] is False and "mayor que el de inyección" in err
    # Una config anterior a este campo toma el default (16), que siempre es mayor que el umbral máximo (10).
    del cfg["umbral_devolucion_ratio"]
    cfg["umbral_ratio"] = "10"
    (data_dir / rescate.CONFIG_FILE_NAME).write_text(json.dumps(cfg))
    loaded, err = rescate.load_config(data_dir)
    assert err is None and loaded["habilitado"] is True and loaded["umbral_devolucion_ratio"] == "16"


def test_panel_umbral_de_devolucion(app, client):  # noqa: F811
    status = client.get("/api/rescate/status").get_json()
    assert status["config"]["umbral_devolucion_ratio"] == "16"
    assert status["limites"]["umbral_devolucion_ratio"] == ["2", "100"]
    ok = _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body(umbral_devolucion_ratio=20.5)))
    assert ok.status_code == 200 and ok.get_json()["status"]["config"]["umbral_devolucion_ratio"] == "20.5"
    assert 'id="rescateUmbralDevolucion"' in client.get("/rescate").get_data(as_text=True)
