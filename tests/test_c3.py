"""C3 (stop con reinicio): piso sobre futuros, corte con Apagar, reinicio automático, dead-man y panel.

Todo con dobles: Binance (cuenta y velas), docker-proxy, config de forager y reloj.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import stat
from decimal import Decimal

import pytest

from futuresboard import bot_control
from futuresboard import c3
from futuresboard import rescate

from test_bot_control import app  # noqa: F401
from test_bot_control import audit_lines
from test_bot_control import binance  # noqa: F401
from test_bot_control import client  # noqa: F401
from test_bot_control import docker  # noqa: F401
from test_bot_control import env  # noqa: F401
from test_bot_control import forager_cfg  # noqa: F401
from test_bot_control import router  # noqa: F401
from test_bot_control import FakeResponse
from test_rescate import FakeRescateBinance
from test_rescate import Harness

H = c3.MS_HORA
PARES = ("BNBUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT")


class C3Harness:
    """Servicio rescate + C3 con dobles. Capital de futuros = ``fake.margin`` (sin préstamo)."""

    def __init__(self, data_dir, tmp_path):
        self.r = Harness(data_dir, tmp_path / "rescate" / "loan_state.json", FakeRescateBinance())
        self.r.fake.now_ms = int(self.r.clock.t * 1000)
        self.r.fake.margin = Decimal("10000")
        self.r.fake.maint = Decimal("100")
        self.data_dir = data_dir
        self.heartbeat = tmp_path / "rescate" / "watcher_heartbeat.json"
        self.apagados = []
        self.starts = []
        self.running = True
        self.apagar_result = ("ok", {"ok": True, "modo": "apagar", "resumen": {
            "completo": True, "posiciones_cerradas": [], "ordenes_canceladas": [], "errores": [],
            "restante": {"verificado": True}}})
        self.start_result = ("ok", {"ok": True, "docker_action": "start"})
        self.forager_cfg = {"twe_long": 6, "twe_short": 2, "long_mode": "normal", "short_mode": "normal"}
        self.precio = {s: (lambda t: Decimal(100)) for s in PARES}  # precio(t_ms) por par
        self.klines_calls = []
        self.klines_fail = False

    # -- dobles -----------------------------------------------------------------------
    @property
    def clock(self):
        return self.r.clock

    @property
    def sent(self):
        return self.r.sent

    def apagar(self):
        self.apagados.append(self.clock.t)
        if self.apagar_result[0] == "ok":
            self.running = False
        return self.apagar_result

    def start(self, plan):
        self.starts.append(dict(plan))
        if self.start_result[0] == "ok":
            self.running = True
        return self.start_result

    def contenedor(self):
        return self.running

    def forager(self):
        return dict(self.forager_cfg)

    def klines(self, symbol, interval, start_ms, limit):
        self.klines_calls.append((symbol, interval, start_ms, limit))
        if self.klines_fail:
            raise bot_control.BinanceError("Binance respondió 502 en GET /fapi/v1/klines: None")
        paso = {"1m": 60_000, "1h": H}[interval]
        now_ms = int(self.clock.t * 1000)
        rows, o = [], start_ms
        while len(rows) < limit and o <= now_ms:  # como Binance: incluye la vela abierta
            close = self.precio[symbol](min(o + paso, now_ms))
            rows.append([o, "0", "0", "0", str(close), "0", o + paso - 1])
            o += paso
        return rows

    # -- armado -----------------------------------------------------------------------
    def watcher(self, mode="real", env=None, apagar=True, testnet=False, **kw):
        settings = c3.C3Settings({c3.ENV_MODE: mode, c3.ENV_HEARTBEAT: str(self.heartbeat), **(env or {})})
        return c3.C3Watcher(self.data_dir, settings, interval=15, testnet=testnet,
                            apagar=self.apagar if apagar else None, start=self.start, contenedor=self.contenedor,
                            forager=self.forager, klines=self.klines,
                            send=lambda t: self.sent.append(t) or True, clock=self.clock, **kw)

    def service(self, mode="real", rescate_mode="off", **kw):
        svc = self.r.service(rescate_mode)
        svc.c3 = self.watcher(mode, **kw)
        return svc

    def configurar(self, habilitado=True, tope=30, politica="rebote", rebote=10, cooldown=7, max_reinicios=3,
                   ventana=30):
        parsed = c3.validate_config_body({"habilitado": habilitado, "tope_pct": tope, "politica": politica,
                                          "rebote_pct": rebote, "cooldown_dias": cooldown,
                                          "max_reinicios": max_reinicios, "ventana_dias": ventana})
        return c3.save_config(self.data_dir, parsed, "cliente17")

    def c3(self):
        state, err = rescate.load_state(self.data_dir)
        assert err is None
        return state.get("c3") or {}

    def latido(self):
        return json.loads(self.heartbeat.read_text())

    def avanzar(self, segundos):
        self.r.avanzar(segundos)

    def capital(self, valor):
        self.r.fake.margin = Decimal(valor)


@pytest.fixture
def h(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return C3Harness(d, tmp_path)


def operando(h, mode="real", **cfg):
    """C3 habilitado con base 10000 (piso 7000 con el tope por defecto)."""
    h.configurar(**cfg)
    svc = h.service(mode)
    svc.tick()
    assert h.c3()["base"]["capital"] == "10000.00"
    return svc


def cortar(h, svc, capital="6900"):
    """Cruza el piso y confirma 61 s después: corte."""
    h.capital(capital)
    h.avanzar(15)
    svc.tick()
    assert h.c3()["confirmacion"] is not None
    h.avanzar(c3.DEFAULT_CONFIRM_S + 1)
    svc.tick()
    assert h.c3()["c3_parado"] is True
    return h.c3()["corte"]


# ---------------------------------------------------------------- dead-man: watcher_heartbeat.json


def test_latido_contrato_atomico_y_legible(h):
    svc = operando(h)
    data = h.latido()
    assert set(data) == {"version", "ts", "c3_mode", "c3_parado"}
    assert data["version"] == 1 and data["c3_mode"] == "real" and data["c3_parado"] is False
    assert dt.datetime.fromisoformat(data["ts"]).timestamp() == int(h.clock.t)
    assert data["ts"].endswith("+00:00")
    assert stat.S_IMODE(os.stat(h.heartbeat).st_mode) == 0o644
    assert [p.name for p in h.heartbeat.parent.iterdir() if p.name.endswith(".tmp")] == []
    h.avanzar(15)
    svc.tick()
    assert dt.datetime.fromisoformat(h.latido()["ts"]).timestamp() == int(h.clock.t)  # late en cada ciclo


def test_latido_escritura_atomica_con_os_replace(h, monkeypatch):
    reemplazos = []
    real_replace = os.replace
    monkeypatch.setattr(rescate.os, "replace", lambda a, b: reemplazos.append(pathlib.Path(b)) or real_replace(a, b))
    c3.write_heartbeat(h.heartbeat, "real", True, 1_700_000_000)
    assert reemplazos == [h.heartbeat]
    assert h.latido() == {"version": 1, "ts": "2023-11-14T22:13:20+00:00", "c3_mode": "real", "c3_parado": True}


@pytest.mark.parametrize("mode,habilitado,esperado", [
    ("off", True, "off"), ("dry_run", True, "dry_run"), ("real", True, "real"), ("real", False, "off"),
])
def test_latido_con_el_modo_efectivo(h, mode, habilitado, esperado):
    h.configurar(habilitado=habilitado)
    h.service(mode).tick()
    assert h.latido()["c3_mode"] == esperado


def test_latido_real_sin_apagar_disponible_es_off_y_bloquea(h):
    h.configurar()
    svc = h.r.service("off")
    svc.c3 = h.watcher("real", apagar=False)
    svc.tick()
    assert h.latido()["c3_mode"] == "off"
    assert "FUTURESBOARD_DOCKER_URL" in h.c3()["bloqueo"]


def test_error_en_c3_no_escribe_el_latido_y_no_tira_el_ciclo(h, monkeypatch):
    svc = operando(h)
    ts = h.latido()["ts"]
    monkeypatch.setattr(svc.c3, "_evaluar_piso", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    h.avanzar(15)
    assert svc.tick() == {"ok": True}
    assert h.latido()["ts"] == ts  # passivbot va a ver el latido viejo
    assert any("C3 con error" in t for t in h.sent)


def test_estado_del_rescate_ilegible_no_late(h):
    svc = operando(h)
    ts = h.latido()["ts"]
    (h.data_dir / rescate.STATE_FILE_NAME).write_text("{roto")
    h.avanzar(15)
    svc.tick()
    assert h.latido()["ts"] == ts


def test_latido_ruta_por_entorno_y_default():
    assert str(c3.C3Settings({}).heartbeat_path) == "/rescate/watcher_heartbeat.json"
    assert str(c3.C3Settings({c3.ENV_HEARTBEAT: "/x/hb.json"}).heartbeat_path) == "/x/hb.json"


# ---------------------------------------------------------------- base y capital de futuros


def test_c3_corre_con_el_rescate_en_off_y_lee_futuros(h):
    operando(h)
    assert "/fapi/v2/account" in h.r.fake.paths()
    assert "/api/v3/account" not in h.r.fake.paths()  # el rescate en off no lee spot


def test_c3_sigue_con_spot_caido(h):
    h.configurar()
    h.r.fake.read_fail.add("/api/v3/account")
    svc = h.service("real", rescate_mode="real")
    svc.tick()
    assert h.c3()["base"]["capital"] == "10000.00"
    assert h.r.fake.paths().count("/fapi/v2/account") == 1  # reusa la lectura del rescate


def test_c3_con_futuros_caidos_no_observa(h):
    svc = operando(h)
    h.capital("6000")
    h.r.fake.read_fail.add("/fapi/v2/account")
    h.avanzar(15)
    svc.tick()
    st = h.c3()
    assert st["confirmacion"] is None and st["c3_parado"] is False
    assert "No se pudo leer" in st["ultimo"]["error"]


def test_capital_descuenta_el_prestamo_real_y_suma_lo_que_esta_en_vuelo():
    state = rescate.default_state()
    rescate.set_loan(state, "real", Decimal("1000"))
    fut = {"margin_balance": Decimal("9000")}
    assert c3.capital_futuros(state, fut)[0] == Decimal("8000")
    state["en_vuelo"] = {"modo": "real", "tipo": "devolucion", "monto": "500.00"}
    assert c3.capital_futuros(state, fut)[0] == Decimal("8500")
    state["en_vuelo"] = {"modo": "dry_run", "tipo": "inyeccion", "monto": "500.00"}
    assert c3.capital_futuros(state, fut)[0] == Decimal("8000")


def test_inyeccion_del_rescate_no_tapa_la_perdida(h):
    h.configurar()
    st, _ = rescate.load_state(h.data_dir)
    rescate.set_loan(st, "real", Decimal("2000"))
    rescate.save_state(h.data_dir, st)
    h.capital("12000")  # 10000 propios + 2000 prestados
    svc = h.service("real")
    svc.tick()
    assert h.c3()["base"]["capital"] == "10000.00"


# ---------------------------------------------------------------- corte


def test_corte_confirmado_ejecuta_apagar_y_queda_parado(h):
    svc = operando(h)
    corte = cortar(h, svc)
    assert len(h.apagados) == 1
    assert corte["capital"] == "6900.00" and corte["piso"] == "7000.00" and corte["apagar"]["resultado"] == "ok"
    assert corte["config_al_corte"]["preset"] == "medio" and corte["contenedor_al_corte"] is True
    assert h.latido()["c3_parado"] is True
    assert any("EN CONFIRMACIÓN" in t for t in h.sent) and any("CORTE" in t for t in h.sent)
    assert any("APAGAR" in t and "C3 (stop con reinicio)" in t for t in h.sent)


def test_rebote_antes_de_confirmar_cancela(h):
    svc = operando(h)
    h.capital("6900")
    h.avanzar(15)
    svc.tick()
    h.capital("7100")
    h.avanzar(c3.DEFAULT_CONFIRM_S + 1)
    svc.tick()
    st = h.c3()
    assert st["confirmacion"] is None and st["c3_parado"] is False and h.apagados == []
    assert any("cancelada" in t for t in h.sent)


def test_corte_sin_confirmacion_con_cero(h):
    h.configurar()
    svc = h.service("real", env={c3.ENV_CONFIRM: "0"})
    svc.tick()
    h.capital("6900")
    h.avanzar(15)
    svc.tick()
    assert h.c3()["c3_parado"] is True and len(h.apagados) == 1


def test_confirmacion_tras_hueco_largo_arranca_de_cero(h):
    svc = operando(h)
    h.capital("6900")
    h.avanzar(15)
    svc.tick()
    h.avanzar(600)  # servicio caído
    svc.tick()
    assert h.c3()["c3_parado"] is False and h.apagados == []
    st, _ = rescate.load_state(h.data_dir)
    assert any("arrancó de cero" in e["texto"] for e in st["eventos"])
    h.avanzar(c3.DEFAULT_CONFIRM_S + 1)
    svc.tick()
    assert len(h.apagados) == 1


def test_corte_ejecuta_apagar_una_sola_vez_aunque_se_reinicie_el_servicio(h):
    svc = operando(h)
    cortar(h, svc)
    for _ in range(5):
        h.avanzar(15)
        h.service("real").tick()  # proceso nuevo cada vez
    assert len(h.apagados) == 1


def test_apagar_cortado_por_un_reinicio_se_reintenta(h, monkeypatch):
    svc = operando(h)
    h.capital("6900")
    h.avanzar(15)
    svc.tick()
    h.avanzar(c3.DEFAULT_CONFIRM_S + 1)

    def muere():
        raise SystemExit("deploy")

    monkeypatch.setattr(svc.c3, "_apagar", muere)
    with pytest.raises(SystemExit):
        svc.tick()
    corte = h.c3()["corte"]
    assert corte["apagar"]["resultado"] == "pendiente" and corte["apagar"]["intentos"] == 1
    h.avanzar(15)
    h.service("real").tick()
    corte = h.c3()["corte"]
    assert len(h.apagados) == 1 and corte["apagar"]["resultado"] == "ok" and corte["apagar"]["intentos"] == 2


def test_apagar_fallido_reintenta_cada_2_min_hasta_3(h):
    svc = operando(h)
    h.apagar_result = ("error", {"ok": False, "error": "docker-proxy caído"})
    cortar(h, svc)
    for _ in range(10):
        h.avanzar(60)
        svc.tick()
    assert len(h.apagados) == 3
    assert any("requiere intervención manual" in t for t in h.sent)
    assert h.starts == []


# ---------------------------------------------------------------- reinicio: política rebote


def _caida_y_rebote(h, t_corte_ms, minimo=Decimal("0.80"), rebote_h=5):
    """Todos los pares caen a ``minimo`` (relativo) las primeras horas y suben 15 % sobre el mínimo después."""
    def precio(t):
        horas = (t - t_corte_ms) / H
        if horas < 2:
            return Decimal(100)
        if horas < rebote_h:
            return Decimal(100) * minimo
        return Decimal(100) * minimo * Decimal("1.15")
    for s in PARES:
        h.precio[s] = precio


def test_rebote_reinicia_con_el_ultimo_preset_y_reancla(h):
    svc = operando(h)
    corte = cortar(h, svc)
    _caida_y_rebote(h, corte["desde_ms"])
    h.capital("6800")  # capital al reiniciar (sin posiciones)
    for _ in range(4):  # 4 h: índice 0,80 => mínimo, sin rebote
        h.avanzar(3600)
        svc.tick()
    assert h.starts == []
    idx = h.c3()["corte"]["indice"]
    assert idx["min"] == "0.8" and idx["muestras"] >= 3 and idx["rebote_en"] is None
    for _ in range(3):
        h.avanzar(3600)
        svc.tick()
    assert h.starts == [{"riesgo": "medio", "mantener_twe": False, "descripcion": "el preset medio"}]
    st = h.c3()
    assert st["c3_parado"] is False and st["corte"] is None
    assert st["base"]["capital"] == "6800.00" and st["base"]["motivo"] == "reinicio"
    assert len(st["reinicios"]) == 1
    assert any("Reinicio automático" in t and "piso 4760.00" in t for t in h.sent)
    assert h.latido()["c3_parado"] is False


def test_rebote_usa_el_minimo_posterior_al_corte_no_el_precio_del_corte(h):
    svc = operando(h)
    corte = cortar(h, svc)
    # cae 20 % y sube 12 % sobre el mínimo: 0,896 < 1 (sigue debajo del precio del corte) pero reinicia
    def precio(t):
        horas = (t - corte["desde_ms"]) / H
        return Decimal(80) if horas < 3 else Decimal("89.6")
    for s in PARES:
        h.precio[s] = precio
    for _ in range(6):
        h.avanzar(3600)
        svc.tick()
    assert len(h.starts) == 1
    hist = [x for x in h.c3()["historial"] if x["tipo"] == "reinicio"]
    assert "rebote" in hist[-1]["texto"]


def test_indice_equiponderado_con_pares_distintos(h):
    svc = operando(h)
    corte = cortar(h, svc)
    # BTC +40 %, el resto planos: índice 1,10 >= 1 × 1,10 => rebote
    h.precio["BTCUSDT"] = lambda t: Decimal(140) if t > corte["desde_ms"] + H else Decimal(100)
    for _ in range(3):
        h.avanzar(3600)
        svc.tick()
    assert len(h.starts) == 1


def test_indice_se_muestrea_cada_hora_al_cierre_de_la_vela(h):
    svc = operando(h)
    corte = cortar(h, svc)
    for s in PARES:  # rebote enorme apenas pasan 2 min del corte
        h.precio[s] = lambda t: Decimal(200) if t > corte["desde_ms"] + 120_000 else Decimal(100)
    h.klines_calls.clear()
    h.avanzar(60)  # la vela de 1 h del corte no cerró: no se muestrea
    svc.tick()
    assert [c for c in h.klines_calls if c[1] == "1h"] == [] and h.starts == []
    assert {c[1] for c in h.klines_calls} == {"1m"}  # referencia: la vela de 1 min del corte
    ref = h.c3()["corte"]["indice"]["ref"]
    assert set(ref) == set(PARES)
    proxima = (corte["desde_ms"] // H) * H + H
    h.clock.t = (proxima + c3.VELA_MARGEN_MS) / 1000 + 1
    svc.tick()
    assert len(h.starts) == 1
    assert all(c[2] == (corte["desde_ms"] // H) * H for c in h.klines_calls if c[1] == "1h")


def test_indice_tras_un_corte_del_servicio_procesa_las_velas_en_orden(h):
    svc = operando(h)
    corte = cortar(h, svc)
    _caida_y_rebote(h, corte["desde_ms"], minimo=Decimal("0.70"), rebote_h=4)
    h.avanzar(10 * 3600)  # 10 h sin ciclos
    svc.tick()
    idx = h.c3()["corte"] if h.c3()["corte"] else None
    assert len(h.starts) == 1
    hist = [x for x in h.c3()["historial"] if x["tipo"] == "reinicio"][-1]
    assert "0.805" in hist["texto"]  # 0,70 × 1,15 >= 0,70 × 1,10
    assert idx is None


def test_velas_caidas_no_reinician_y_avisan(h):
    svc = operando(h)
    cortar(h, svc)
    h.klines_fail = True
    h.avanzar(3 * 3600)
    svc.tick()
    assert h.starts == [] and "No se pudo actualizar el índice" in h.c3()["corte"]["indice"]["error"]


# ---------------------------------------------------------------- reinicio: cooldown y ambos


def test_cooldown_reinicia_a_los_n_dias(h):
    svc = operando(h, politica="cooldown", cooldown=7)
    cortar(h, svc)
    for s in PARES:
        h.precio[s] = lambda t: Decimal(500)  # un rebote no importa con cooldown
    h.avanzar(7 * 86400 - 120)
    svc.tick()
    assert h.starts == []
    h.avanzar(180)
    svc.tick()
    assert len(h.starts) == 1
    assert "cooldown de 7 días" in [x for x in h.c3()["historial"] if x["tipo"] == "reinicio"][-1]["texto"]


@pytest.mark.parametrize("rebota,horas,esperado", [(True, 3, "rebote"), (False, 24 + 1, "cooldown")])
def test_ambos_reinicia_con_lo_primero(h, rebota, horas, esperado):
    svc = operando(h, politica="ambos", cooldown=1)
    corte = cortar(h, svc)
    if rebota:
        for s in PARES:
            h.precio[s] = lambda t: Decimal(120) if t > corte["desde_ms"] + H else Decimal(100)
    for _ in range(horas):
        h.avanzar(3600)
        svc.tick()
    assert len(h.starts) == 1
    assert esperado in [x for x in h.c3()["historial"] if x["tipo"] == "reinicio"][-1]["texto"]


def test_tras_reiniciar_vuelve_a_vigilar_con_el_piso_nuevo(h):
    svc = operando(h, politica="cooldown", cooldown=1)
    cortar(h, svc)
    h.capital("6000")
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.c3()["base"]["capital"] == "6000.00"
    h.capital("4300")  # piso nuevo 4200: no corta
    h.avanzar(15)
    svc.tick()
    assert h.c3()["confirmacion"] is None
    h.capital("4100")
    h.avanzar(15)
    svc.tick()
    assert h.c3()["confirmacion"]["piso"] == "4200.00"


# ---------------------------------------------------------------- START con el último preset


def test_reinicio_con_config_personalizada_mantiene_twe(h):
    h.forager_cfg = {"twe_long": 12, "twe_short": 3, "long_mode": "normal"}
    svc = operando(h, politica="cooldown", cooldown=1)
    corte = cortar(h, svc)
    assert corte["config_al_corte"]["preset"] == "personalizado"
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.starts == [{"riesgo": None, "mantener_twe": True,
                         "descripcion": "la config personalizada (twe 12/3)"}]


def test_config_cambiada_desde_el_corte_no_reinicia_sola(h):
    h.forager_cfg = {"twe_long": 12, "twe_short": 3, "long_mode": "normal"}
    svc = operando(h, politica="cooldown", cooldown=1)
    cortar(h, svc)
    h.forager_cfg = {"twe_long": 10, "twe_short": 3, "long_mode": "normal"}
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.starts == []
    corte = h.c3()["corte"]
    assert corte["auto"] is False and "cambió desde el corte" in corte["motivo_manual"]


def test_corte_en_graceful_stop_no_reinicia_solo(h):
    h.forager_cfg = {"twe_long": 6, "twe_short": 2, "long_mode": "graceful_stop"}
    svc = operando(h, politica="cooldown", cooldown=1)
    corte = cortar(h, svc)
    assert corte["auto"] is False and "graceful_stop" in corte["motivo_manual"]
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.starts == []


def test_start_fallido_reintenta_y_despues_pide_accion_manual(h):
    svc = operando(h, politica="cooldown", cooldown=1)
    cortar(h, svc)
    h.start_result = ("error", {"ok": False, "error": "contenedor inexistente"})
    h.avanzar(86400 + 60)
    for _ in range(12):
        svc.tick()
        h.avanzar(60)
    assert len(h.starts) == 3 and h.c3()["c3_parado"] is True
    assert any("falló 3 veces" in t for t in h.sent)


# ---------------------------------------------------------------- límite de reinicios


def test_limite_de_reinicios_deja_parado_esperando_accion_manual(h):
    svc = operando(h, politica="cooldown", cooldown=1, max_reinicios=1, ventana=30)
    cortar(h, svc)
    h.avanzar(86400 + 60)
    svc.tick()
    assert len(h.starts) == 1
    st = h.c3()  # la base nueva es el capital al reiniciar (6900): piso 4830
    assert st["base"]["capital"] == "6900.00"
    cortar(h, svc, capital="4800")  # piso 4830
    h.avanzar(86400 + 60)
    svc.tick()
    assert len(h.starts) == 1
    corte = h.c3()["corte"]
    assert corte["auto"] is False and "límite de reinicios" in corte["motivo_manual"]
    assert any("límite de reinicios" in t for t in h.sent)
    assert "manual" in c3.panel_status(h.data_dir, now=h.clock.t)["proximo_reinicio"]


def test_limite_cero_nunca_reinicia_solo(h):
    svc = operando(h, politica="cooldown", cooldown=1, max_reinicios=0)
    cortar(h, svc)
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.starts == [] and h.c3()["corte"]["auto"] is False


def test_reinicios_viejos_no_cuentan_para_el_limite(h):
    svc = operando(h, politica="cooldown", cooldown=1, max_reinicios=1, ventana=2)
    cortar(h, svc)
    h.avanzar(86400 + 60)
    svc.tick()
    h.capital("6900")
    h.avanzar(3 * 86400)  # el reinicio anterior quedó fuera de la ventana de 2 días
    svc.tick()
    cortar(h, svc, capital="4800")
    h.avanzar(86400 + 60)
    svc.tick()
    assert len(h.starts) == 2


# ---------------------------------------------------------------- acciones manuales


def test_start_a_mano_con_c3_parado_reancla_sin_contar(h):
    svc = operando(h)
    cortar(h, svc)
    h.running = True  # START desde el panel
    h.capital("6500")
    h.avanzar(15)
    svc.tick()
    st = h.c3()
    assert st["c3_parado"] is False and st["base"]["capital"] == "6500.00"
    assert st["base"]["motivo"] == "reinicio_manual" and st["reinicios"] == [] and h.starts == []


def test_contenedor_corriendo_con_apagar_fallido_no_es_un_start_manual(h):
    svc = operando(h)
    h.apagar_result = ("error", {"ok": False, "error": "passivbot sigue corriendo"})
    cortar(h, svc)
    h.avanzar(15)
    svc.tick()
    assert h.c3()["c3_parado"] is True


def test_make_start_espera_el_lock_del_panel(tmp_path, env, docker):  # noqa: F811
    store = bot_control.Store(tmp_path)
    docker.running = False
    handle = store.try_lock()  # una acción del panel en curso
    start = c3.make_start(tmp_path, bot_control.Settings(), docker=bot_control.DockerClient("http://docker-proxy:2375"),
                          lock_timeout=0.05)
    outcome, payload = start({"riesgo": "medio", "mantener_twe": False})
    assert outcome == "error" and "lock" in payload["error"]
    assert docker.actions() == []
    handle.release()
    outcome, payload = start({"riesgo": "medio", "mantener_twe": False})
    assert outcome == "ok" and docker.actions() == ["start"]
    line = json.loads((tmp_path / bot_control.AUDIT_LOG_NAME).read_text().splitlines()[-1])
    assert line["user"] == c3.USUARIO_START and line["params"]["origen"] == "c3_reinicio"


def test_make_start_no_reinicia_si_ya_corre(tmp_path, env, docker):  # noqa: F811
    docker.running = True
    start = c3.make_start(tmp_path, bot_control.Settings(), docker=bot_control.DockerClient("http://docker-proxy:2375"))
    assert start({"riesgo": "medio", "mantener_twe": False})[0] == "ya_corriendo"
    assert docker.actions() == []


def test_make_start_respeta_el_tope_del_rescate(tmp_path, env, docker):  # noqa: F811
    docker.running = False
    st = rescate.default_state()
    st["tope"] = {"id": "abc", "disparado_en": "2026-10-22T10:00:00+00:00", "modo": "real"}
    rescate.save_state(tmp_path, st)
    start = c3.make_start(tmp_path, bot_control.Settings(), docker=bot_control.DockerClient("http://docker-proxy:2375"))
    outcome, payload = start({"riesgo": "medio", "mantener_twe": False})
    assert outcome == "bloqueado" and "START bloqueado" in payload["error"] and docker.actions() == []


def test_make_start_personalizado_no_toca_twe(tmp_path, env, docker, forager_cfg):  # noqa: F811
    import hjson

    forager_cfg.write_text(forager_cfg.read_text().replace("twe_long: 6", "twe_long: 12"))
    docker.running = False
    start = c3.make_start(tmp_path, bot_control.Settings(), docker=bot_control.DockerClient("http://docker-proxy:2375"))
    outcome, payload = start({"riesgo": None, "mantener_twe": True})
    assert outcome == "ok" and payload["riesgo"] == "personalizado"
    cfg = hjson.loads(forager_cfg.read_text())
    assert cfg["twe_long"] == 12 and cfg["twe_short"] == 2 and cfg["long_mode"] == "normal"


def test_apagar_de_c3_cierra_reduce_only_con_el_lock_y_su_usuario(tmp_path, env, router, binance):  # noqa: F811
    binance.add_position("BTCUSDT", "0.5")
    apagar = rescate.make_apagar(tmp_path, bot_control.Settings(),
                                 bot_control.BinanceFuturesClient("https://fapi.binance.com", "k", "s"),
                                 usuario=c3.USUARIO_APAGAR, origen="c3_corte")
    outcome, payload = apagar()
    assert outcome == "ok"
    orders = binance.market_orders()
    assert orders and all(o["reduceOnly"] == "true" for o in orders)
    line = json.loads((tmp_path / bot_control.AUDIT_LOG_NAME).read_text().splitlines()[-1])
    assert line["user"] == c3.USUARIO_APAGAR and line["params"]["origen"] == "c3_corte"


# ---------------------------------------------------------------- modos


def test_dry_run_calcula_y_registra_sin_cortar_ni_reiniciar(h):
    svc = operando(h, mode="dry_run", politica="cooldown", cooldown=1)
    corte = cortar(h, svc)
    assert h.apagados == [] and corte["modo"] == "dry_run" and corte["apagar"]["resultado"] == "simulado"
    assert any("[SIMULACIÓN]" in t and "CORTE" in t for t in h.sent)
    assert h.latido() == {**h.latido(), "c3_mode": "dry_run", "c3_parado": True}
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.starts == [] and h.c3()["c3_parado"] is False
    st, _ = rescate.load_state(h.data_dir)
    assert any("HARÍA START" in e["texto"] for e in st["eventos"])


def test_pasar_de_dry_run_a_real_descarta_el_corte_simulado(h):
    svc = operando(h, mode="dry_run")
    cortar(h, svc)
    h.avanzar(15)
    h.service("real").tick()
    st = h.c3()
    assert st["c3_parado"] is False and st["corte"] is None and h.apagados == []
    assert st["confirmacion"] is not None  # sigue debajo del piso: confirmación real nueva, con su aviso


def test_corte_real_no_se_pierde_al_pasar_a_dry_run(h):
    svc = operando(h, politica="cooldown", cooldown=1)
    cortar(h, svc)
    sim = h.service("dry_run")
    h.avanzar(86400 + 60)
    sim.tick()
    st = h.c3()
    assert st["c3_parado"] is True and h.starts == []
    assert "dry_run" in st["corte"]["reinicio"]["aviso"]


def test_real_permitido_en_testnet(h):
    h.configurar()
    svc = h.r.service("off")
    svc.c3 = h.watcher("real", testnet=True)
    svc.tick()
    assert h.c3()["bloqueo"] is None and h.latido()["c3_mode"] == "real"


def test_modo_off_no_lee_ni_corta(h):
    h.configurar()
    svc = h.service("off")
    h.capital("100")
    svc.tick()
    assert h.r.fake.paths() == [] and h.apagados == [] and h.latido()["c3_mode"] == "off"


def test_modo_invalido_es_off():
    s = c3.C3Settings({c3.ENV_MODE: "turbo"})
    assert s.mode == "off" and "inválido" in s.mode_error


def test_pares_por_entorno():
    assert c3.C3Settings({c3.ENV_PARES: "btcusdt, ETHUSDT"}).pares == ("BTCUSDT", "ETHUSDT")
    s = c3.C3Settings({c3.ENV_PARES: "BTC/USDT"})
    assert s.pares == c3.DEFAULT_PARES and s.pares_error


def test_deshabilitar_y_habilitar_abre_una_base_nueva(h):
    svc = operando(h)
    h.capital("8000")
    h.configurar(habilitado=False)
    h.avanzar(15)
    svc.tick()
    h.configurar(habilitado=True)
    h.avanzar(15)
    svc.tick()
    assert h.c3()["base"]["capital"] == "8000.00"


# ---------------------------------------------------------------- tope del rescate


def _tope_real(h):
    st, _ = rescate.load_state(h.data_dir)
    st["tope"] = {"id": "t1", "disparado_en": "2026-10-22T10:00:00+00:00", "modo": "real",
                  "apagar": {"resultado": "ok", "intentos": 1}}
    rescate.save_state(h.data_dir, st)


def test_tope_del_rescate_deja_a_c3_parado_en_manual_sin_otro_apagar(h):
    svc = operando(h, politica="cooldown", cooldown=1)
    _tope_real(h)
    h.running = False
    h.avanzar(15)
    svc.tick()
    corte = h.c3()["corte"]
    assert h.c3()["c3_parado"] is True and corte["origen"] == "tope_rescate" and corte["auto"] is False
    assert h.apagados == []
    h.avanzar(2 * 86400)
    svc.tick()
    assert h.starts == []
    # reconocer el tope + START a mano => C3 re-ancla
    cfg, _ = rescate.load_config(h.data_dir)
    cfg["tope_reconocido"] = "t1"
    rescate.write_json_atomic(h.data_dir / rescate.CONFIG_FILE_NAME, cfg)
    h.running = True
    h.capital("6000")
    h.avanzar(15)
    svc.tick()
    assert h.c3()["c3_parado"] is False and h.c3()["base"]["capital"] == "6000.00"


def test_reinicio_espera_mientras_start_este_bloqueado(h):
    svc = operando(h, politica="cooldown", cooldown=1)
    cortar(h, svc)
    st, _ = rescate.load_state(h.data_dir)
    st["tope"] = {"id": "t2", "disparado_en": "2026-10-22T10:00:00+00:00", "modo": "dry_run"}  # simulado
    rescate.save_state(h.data_dir, st)
    h.avanzar(86400 + 60)
    svc.tick()
    assert h.starts == []
    assert "START bloqueado" in h.c3()["corte"]["reinicio"]["aviso"]


# ---------------------------------------------------------------- panel


def _data_dir(app):  # noqa: F811
    return pathlib.Path(app.config["DATABASE"]).parent


def _body(**kw):
    body = {"habilitado": True, "tope_pct": 30, "politica": "rebote", "rebote_pct": 10, "cooldown_dias": 7,
            "max_reinicios": 3, "ventana_dias": 30}
    body.update(kw)
    return body


def _post(client, body, token="tok"):  # noqa: F811
    return client.post("/api/c3/config", data=json.dumps(body), content_type="application/json",
                       headers={"X-CSRF-Token": token})


def test_panel_c3_requiere_login_y_csrf(app, client):  # noqa: F811
    anon = app.test_client()
    assert anon.post("/api/c3/config", json=_body()).status_code in (302, 403)
    assert _post(client, _body(), token="mal").status_code == 403
    assert client.post("/api/c3/config", data="{}", headers={"X-CSRF-Token": "tok"}).status_code == 415
    assert client.get("/api/c3/config").status_code == 405


@pytest.mark.parametrize("cambio,error", [
    ({"tope_pct": 4}, "tope_pct fuera de rango"),
    ({"tope_pct": "30"}, "tope_pct tiene que ser un número"),
    ({"tope_pct": True}, "tope_pct tiene que ser un número"),
    ({"politica": "siempre"}, "politica inválida"),
    ({"rebote_pct": 0.5}, "rebote_pct fuera de rango"),
    ({"cooldown_dias": 91}, "cooldown_dias fuera de rango"),
    ({"max_reinicios": 2.5}, "max_reinicios tiene que ser un número entero"),
    ({"max_reinicios": 21}, "max_reinicios fuera de rango"),
    ({"ventana_dias": 0}, "ventana_dias fuera de rango"),
    ({"habilitado": "si"}, "habilitado tiene que ser true o false"),
    ({"extra": 1}, "Campos desconocidos: extra"),
])
def test_panel_c3_validacion_estricta(app, client, cambio, error):  # noqa: F811
    resp = _post(client, _body(**cambio))
    assert resp.status_code == 400 and error in resp.get_json()["error"]
    assert not (_data_dir(app) / c3.CONFIG_FILE_NAME).exists()


def test_panel_c3_rechaza_nan_y_faltantes(app, client):  # noqa: F811
    resp = client.post("/api/c3/config", data=json.dumps(_body()).replace("30,", "NaN,", 1),
                       content_type="application/json", headers={"X-CSRF-Token": "tok"})
    assert resp.status_code == 400
    body = _body()
    del body["ventana_dias"]
    assert "Faltan campos: ventana_dias" in _post(client, body).get_json()["error"]
    assert _post(client, [1, 2]).status_code == 400


def test_panel_c3_guarda_audita_y_abre_generacion(app, client):  # noqa: F811
    resp = _post(client, _body())
    assert resp.status_code == 200
    status = resp.get_json()["status"]
    assert status["c3"]["config"]["habilitado"] is True and status["c3"]["config"]["generacion"] == 1
    assert _post(client, _body(tope_pct=25)).get_json()["status"]["c3"]["config"]["generacion"] == 1
    _post(client, _body(habilitado=False))
    assert _post(client, _body()).get_json()["status"]["c3"]["config"]["generacion"] == 2
    lines = [x for x in audit_lines(app) if x["action"] == "c3_config"]
    assert len(lines) == 4 and lines[0]["user"] == "cliente17" and lines[0]["result"] == "ok"
    rechazo = _post(client, _body(tope_pct=99))
    assert rechazo.status_code == 400
    assert [x for x in audit_lines(app) if x["action"] == "c3_config"][-1]["result"] == "rechazado"


def test_panel_muestra_estado_de_c3(app, client, tmp_path):  # noqa: F811
    _post(client, _body())
    harness = C3Harness(_data_dir(app), tmp_path)
    svc = harness.service("real")
    svc.tick()
    harness.capital("6900")
    harness.avanzar(15)
    svc.tick()
    st = client.get("/api/rescate/status").get_json()["c3"]
    assert st["estado"] == "en_confirmacion" and st["piso"] == "7000.00" and st["distancia_usdt"] == "-100.00"
    harness.avanzar(61)
    svc.tick()
    st = client.get("/api/rescate/status").get_json()["c3"]
    assert st["estado"] == "parado" and st["c3_parado"] is True
    assert "cuando el índice llegue a" in st["proximo_reinicio"]
    assert st["ultimo_corte"]["tipo"] == "corte"
    html = client.get("/rescate").get_data(as_text=True)
    assert 'id="c3Form"' in html and "Stop con reinicio (C3)" in html


def test_tarjeta_c3_en_el_dashboard_principal(app, client, tmp_path):  # noqa: F811
    html = client.get("/").get_data(as_text=True)
    assert 'data-tarjeta="c3"' not in html  # deshabilitado: sin tarjeta
    _post(client, _body())
    harness = C3Harness(_data_dir(app), tmp_path)
    import time as _time

    harness.clock.t = _time.time()
    svc = harness.service("real")
    svc.tick()
    harness.capital("8000")
    harness.avanzar(15)
    svc.tick()
    html = client.get("/").get_data(as_text=True)
    m = html.split('data-tarjeta="c3"', 1)[1].split("card-footer", 1)[0]
    assert "Operando" in m and "piso $7000.00" in m and "a $1000.00 (12.50%) del piso" in m
    cortar(harness, svc, capital="6900")
    html = client.get("/").get_data(as_text=True)
    m = html.split('data-tarjeta="c3"', 1)[1].split("card-footer", 1)[0]
    assert "Parado" in m and "último corte" in m


# ---------------------------------------------------------------- bot_control


def test_start_bot_mantener_twe(tmp_path, env, docker, forager_cfg):  # noqa: F811
    import hjson

    out = bot_control.start_bot(bot_control.Settings(), bot_control.Store(tmp_path), None,
                                docker=bot_control.DockerClient("http://docker-proxy:2375"), mantener_twe=True)
    assert out["riesgo"] == "medio" and "twe_long" not in out["config"]
    assert hjson.loads(forager_cfg.read_text())["twe_long"] == 6
    with pytest.raises(ValueError):
        bot_control.start_bot(bot_control.Settings(), bot_control.Store(tmp_path), None,
                              docker=bot_control.DockerClient("http://docker-proxy:2375"))


def test_klines_publicas_sin_firma():
    class S:
        def __init__(self):
            self.urls = []

        def request(self, method, url, headers=None, timeout=None):
            self.urls.append((method, url, headers))
            return FakeResponse(200, [[1, "1", "1", "1", "2", "0", 2]])

    s = S()
    client = bot_control.BinanceFuturesClient("https://fapi.binance.com", "k", "s", session=s)
    assert client.klines("BTCUSDT", "1h", 123, 5) == [[1, "1", "1", "1", "2", "0", 2]]
    method, url, headers = s.urls[0]
    assert url.startswith("https://fapi.binance.com/fapi/v1/klines?") and "signature" not in url
    assert "startTime=123" in url and "limit=5" in url and not headers


def test_config_c3_invalida_en_disco_deshabilita(h):
    (h.data_dir / c3.CONFIG_FILE_NAME).write_text(json.dumps({"habilitado": True, "politica": "x"}))
    cfg, err = c3.load_config(h.data_dir)
    assert cfg["habilitado"] is False and "inválido" in err
    h.service("real").tick()
    assert h.latido()["c3_mode"] == "off"
