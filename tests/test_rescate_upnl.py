"""Disparo anticipado por uPnL (U) del rescate: escalera, prioridad del disparo de margen, guarda de la devolución
parcial, modos (off / registra / simulado / real), config del panel y tarjetas.

Los números de las cuentas de prueba: wallet 10.000, mantenimiento 100 (el ratio queda lejos de 8, así el disparo de
margen no se cumple salvo que el test lo busque), spot 10.000 y tope 50 % (base 20.000, piso 10.000).
"""
from __future__ import annotations

import json
import pathlib
import re
from decimal import Decimal

import pytest

from futuresboard import rescate
from futuresboard import resumen_rescate

from test_bot_control import app  # noqa: F401
from test_bot_control import audit_lines
from test_bot_control import client  # noqa: F401
from test_rescate import _cfg_body
from test_rescate import _post_raw
from test_rescate import data_dir  # noqa: F401
from test_rescate import fake  # noqa: F401
from test_rescate import h  # noqa: F401
from test_rescate import loan_path  # noqa: F401
from test_rescate_dashboard import hd  # noqa: F401

U_ON = {"upnl_habilitado": True, "upnl_x_pct": 70, "upnl_paso_usdt": 1000}
U_OFF = {"upnl_habilitado": False, "upnl_x_pct": 70, "upnl_paso_usdt": 1000}


def _cuenta(fake, wallet="10000", upnl="0", maint="100", max_withdraw="5000", spot="10000"):  # noqa: F811
    fake.upnl = Decimal(upnl)
    fake.margin = Decimal(wallet) + fake.upnl
    fake.maint, fake.max_withdraw, fake.spot = Decimal(maint), Decimal(max_withdraw), Decimal(spot)


def _upnl_a(fake, valor):  # noqa: F811
    """Cambia el uPnL sin tocar la wallet (margin = wallet + uPnL, como en Binance)."""
    wallet = fake.margin - fake.upnl
    fake.upnl = Decimal(valor)
    fake.margin = wallet + fake.upnl


def _servicio(h, modo="real", upnl_mode="real", **env):  # noqa: F811
    return h.service(modo, env={rescate.ENV_UPNL_MODE: upnl_mode, **env})


def _configurar(h, upnl=U_ON, **kw):  # noqa: F811
    cfg = dict(reserva=10000, paso=1000, umbral=8, devolucion=32, tope=50)
    cfg.update(kw)
    return h.configurar(upnl=upnl, **cfg)


def _inyecciones(h):  # noqa: F811
    return [p["amount"] for p in h.fake.posts() if p["type"] == "MAIN_UMFUTURE"]


def _devoluciones(h):  # noqa: F811
    return [p["amount"] for p in h.fake.posts() if p["type"] == "UMFUTURE_MAIN"]


def _arrancar(h, modo="real", upnl_mode="real", upnl=U_ON, **kw):  # noqa: F811
    """Config + cuenta sana + primer ciclo (fija la base 20.000; piso 10.000)."""
    _configurar(h, upnl=upnl, **kw)
    _cuenta(h.fake)
    svc = _servicio(h, modo, upnl_mode)
    svc.tick()
    assert h.state()["base"]["capital"] == "20000.00"
    return svc


# ---------------------------------------------------------------- estados y defaults


@pytest.mark.parametrize("servicio,upnl,esperado", [
    ("off", "real", rescate.UPNL_OFF),
    ("real", "off", rescate.UPNL_OFF),
    ("dry_run", "off", rescate.UPNL_OFF),
    (None, "real", rescate.UPNL_OFF),
    ("real", None, rescate.UPNL_OFF),
    ("real", "dry_run", rescate.UPNL_REGISTRA),
    ("real", "real", rescate.UPNL_REAL),
    ("dry_run", "dry_run", rescate.UPNL_REGISTRA),
    ("dry_run", "real", rescate.UPNL_SIMULADO),
])
def test_upnl_estado_efectivo(servicio, upnl, esperado):
    assert rescate.upnl_estado(servicio, upnl) == esperado


def test_upnl_defaults_apagado_y_del_backtest():
    cfg = rescate.default_config()
    assert cfg["upnl_habilitado"] is False
    assert cfg["upnl_x_pct"] == "70" and cfg["upnl_paso_usdt"] == "1000.00"
    settings = rescate.RescateSettings({})
    assert settings.upnl_mode == "off" and settings.upnl_mode_error is None
    assert settings.upnl_cooldown_seconds == 60


@pytest.mark.parametrize("raw,modo,error", [
    ("", "off", False), ("OFF", "off", False), ("dry_run", "dry_run", False), (" Real ", "real", False),
    ("si", "off", True), ("true", "off", True),
])
def test_upnl_modo_por_entorno_fail_closed(raw, modo, error):
    settings = rescate.RescateSettings({rescate.ENV_UPNL_MODE: raw})
    assert settings.upnl_mode == modo
    assert (settings.upnl_mode_error is not None) is error


@pytest.mark.parametrize("raw,esperado", [("", 60), ("0", 0), ("120", 120), ("-5", 60), ("x", 60), ("99999", 3600)])
def test_upnl_cooldown_por_entorno(raw, esperado):
    assert rescate.RescateSettings({rescate.ENV_UPNL_COOLDOWN: raw}).upnl_cooldown_seconds == esperado


@pytest.mark.parametrize("cfg_upnl,upnl_mode", [(U_OFF, "real"), (U_ON, "off"), (U_OFF, "off")])
def test_upnl_apagado_no_inyecta(h, cfg_upnl, upnl_mode):  # noqa: F811
    svc = _arrancar(h, upnl_mode=upnl_mode, upnl=cfg_upnl)
    _upnl_a(h.fake, "-9000")  # muy por debajo de la línea (-7.000); ratio 10 >= 8: el de margen no dispara
    h.avanzar(15)
    svc.tick()
    assert h.fake.posts() == []
    st = h.state()
    assert st["upnl"]["eval"] is None and st["prestamo"]["real"] == "0.00"
    assert st["servicio"]["upnl_modo"] == upnl_mode


# ---------------------------------------------------------------- disparo y escalera


def test_upnl_primer_disparo_a_menos_x_del_capital_propio(h):  # noqa: F811
    svc = _arrancar(h)
    _upnl_a(h.fake, "-6999.99")
    h.avanzar(15)
    svc.tick()
    assert h.fake.posts() == []
    ev = h.state()["upnl"]["eval"]
    assert ev["estado"] == "real" and ev["dispara"] is False
    assert ev["capital_propio"] == "10000.00" and ev["linea"] == "-7000.00" and ev["upnl_dispara"] == "-7000.00"
    _upnl_a(h.fake, "-7000")  # uPnL + préstamo = -7.000 <= -70 % × 10.000
    h.avanzar(15)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"]
    st = h.state()
    assert st["prestamo"]["real"] == "1000.00" and st["en_vuelo"] is None
    fila = st["ledger"][-1]
    assert fila["disparo"] == "upnl" and fila["estado"] == "confirmada" and fila["tipo"] == "inyeccion"
    assert h.loan_file()["loan_outstanding_usdt"] == 1000.0 and h.loan_file()["mode"] == "real"
    # Sin cambios de contrato con passivbot (docs/contratos.md): loan_state.json sigue en versión 1, mismos campos.
    assert h.loan_file()["version"] == 1
    assert set(h.loan_file()) == {"version", "loan_outstanding_usdt", "updated_at", "mode"}
    assert st["upnl"]["ultima_inyeccion"]["monto"] == "1000.00" and st["upnl"]["ultima_inyeccion"]["modo"] == "real"
    assert any("Inyección 1000.00 USDT" in t and "rescate anticipado por uPnL" in t for t in h.sent)


def test_upnl_escalera_el_segundo_paso_recien_un_paso_mas_abajo(h):  # noqa: F811
    svc = _arrancar(h)
    _upnl_a(h.fake, "-7000")
    h.avanzar(15)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"]
    # La wallet subió 1.000 y el préstamo también: el capital propio sigue en 10.000 y lo prestado cuenta como
    # cubierto. Con la condición literal (sin descontar lo prestado) se vaciaría la reserva en minutos.
    h.avanzar(61)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"]
    assert h.state()["upnl"]["eval"]["upnl_dispara"] == "-8000.00"
    _upnl_a(h.fake, "-7999.99")
    h.avanzar(61)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"]
    _upnl_a(h.fake, "-8000")
    h.avanzar(61)
    svc.tick()
    assert _inyecciones(h) == ["1000.00", "1000.00"]
    assert h.state()["prestamo"]["real"] == "2000.00"


def test_upnl_un_minuto_entre_inyecciones(h):  # noqa: F811
    svc = _arrancar(h)
    h.fake.maint = Decimal("10")  # ratio 50: el de margen no se cumple
    _upnl_a(h.fake, "-9500")  # alcanzaría para tres pasos seguidos
    h.avanzar(15)
    svc.tick()
    for _ in range(3):  # 45 s: dentro del minuto
        h.avanzar(15)
        svc.tick()
    assert _inyecciones(h) == ["1000.00"]
    h.avanzar(16)  # 61 s desde la inyección
    svc.tick()
    assert _inyecciones(h) == ["1000.00", "1000.00"]


def test_upnl_cooldown_cero_por_entorno(h):  # noqa: F811
    _configurar(h)
    _cuenta(h.fake)
    svc = _servicio(h, **{rescate.ENV_UPNL_COOLDOWN: "0"})
    svc.tick()
    h.fake.maint = Decimal("10")
    _upnl_a(h.fake, "-9500")
    for _ in range(4):
        h.avanzar(15)
        svc.tick()
    assert _inyecciones(h) == ["1000.00", "1000.00", "1000.00"]  # la tercera deja uPnL + préstamo = -6.500


def test_upnl_monto_limitado_por_la_reserva(h):  # noqa: F811
    svc = _arrancar(h, reserva=1500, paso=500)
    h.fake.maint = Decimal("1")  # ratio 100: el de margen (paso 500) no se cumple
    _upnl_a(h.fake, "-9900")
    for _ in range(4):
        h.avanzar(61)
        svc.tick()
    assert _inyecciones(h) == ["1000.00", "500.00"]
    assert h.state()["prestamo"]["real"] == "1500.00"
    assert len([t for t in h.sent if "ya está prestada" in t and "uPnL" in t]) == 1


def test_upnl_el_de_margen_tiene_prioridad(h):  # noqa: F811
    svc = _arrancar(h, paso=500)
    # Las dos condiciones a la vez: ratio (10.000 - 7.000) / 400 = 7,5 < 8 y uPnL en la línea.
    h.fake.maint = Decimal("400")
    _upnl_a(h.fake, "-7000")
    h.avanzar(15)
    svc.tick()
    assert _inyecciones(h) == ["500.00"]  # una sola transferencia, la de margen (paso 500)
    fila = h.state()["ledger"][-1]
    assert "disparo" not in fila and fila["monto"] == "500.00"
    # Lo prestado por el de margen también cuenta como cubierto: uPnL + 500 = -6.500 > -7.000.
    h.avanzar(61)
    svc.tick()
    assert _inyecciones(h) == ["500.00"]


@pytest.mark.parametrize("caso", ["sin_posiciones", "deshabilitado", "tope_en_confirmacion", "sin_base"])
def test_upnl_no_dispara(h, caso, monkeypatch):  # noqa: F811
    if caso == "tope_en_confirmacion":
        svc = _arrancar(h, tope=30)  # piso 14.000: con uPnL -7.000 el capital es 13.000
    elif caso == "sin_base":
        _configurar(h)
        _cuenta(h.fake)
        svc = _servicio(h)
        monkeypatch.setattr(svc, "_fijar_base", lambda *a, **k: None)
    else:
        svc = _arrancar(h)
    if caso == "sin_posiciones":
        h.fake.positions = {}
    if caso == "deshabilitado":
        _configurar(h, habilitado=False)
    _upnl_a(h.fake, "-7500")
    h.avanzar(15)
    svc.tick()
    assert _inyecciones(h) == []
    if caso == "tope_en_confirmacion":
        assert h.state()["tope_confirmacion"] is not None


def test_upnl_sin_capital_propio_no_dispara(h):  # noqa: F811
    svc = _servicio(h)
    cfg = rescate.default_config()
    cfg.update(upnl_habilitado=True)
    st = rescate.default_state()
    st["prestamo"]["real"] = "1000.00"
    snap = {"upnl": Decimal("-5000"), "wallet_eff": Decimal("1000")}  # wallet - préstamo = 0
    valores = svc._upnl_valores(st, cfg, snap)
    assert valores["capital_propio"] == 0 and valores["cubierto"] <= valores["linea"]
    assert valores["dispara"] is False


def test_upnl_con_transferencia_en_vuelo_no_dispara_otra(h):  # noqa: F811
    svc = _arrancar(h)
    h.fake.transfer_mode = "timeout"  # la primera queda incierta, en vuelo
    _upnl_a(h.fake, "-7000")
    h.avanzar(15)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"] and h.state()["en_vuelo"]["estado"] == "incierto"
    _upnl_a(h.fake, "-9500")
    h.avanzar(61)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"]  # una transferencia en vuelo a la vez


def test_upnl_respeta_el_cooldown_de_rechazo(h):  # noqa: F811
    svc = _arrancar(h)
    h.fake.transfer_mode = "reject"
    _upnl_a(h.fake, "-7000")
    h.avanzar(15)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"] and h.state()["prestamo"]["real"] == "0.00"
    h.fake.transfer_mode = "ok"
    h.avanzar(61)  # pasó el minuto del disparo U, no los 5 min del rechazo
    svc.tick()
    assert _inyecciones(h) == ["1000.00"]
    h.avanzar(rescate.COOLDOWN_RECHAZO_MS // 1000)
    svc.tick()
    assert _inyecciones(h) == ["1000.00", "1000.00"]


# ---------------------------------------------------------------- dry_run y modo "registra"


def test_upnl_simulado_con_el_servicio_en_dry_run(h):  # noqa: F811
    """Servicio en dry_run y U en real: inyecciones simuladas que se publican como préstamo simulado."""
    svc = _arrancar(h, modo="dry_run", upnl_mode="real")
    _upnl_a(h.fake, "-7000")
    h.avanzar(15)
    svc.tick()
    assert h.fake.posts() == []  # dry_run no llama a /sapi
    st = h.state()
    assert st["prestamo"]["dry_run"] == "1000.00" and st["prestamo"]["real"] == "0.00"
    assert st["ledger"][-1]["estado"] == "simulada" and st["ledger"][-1]["disparo"] == "upnl"
    assert h.loan_file() == {**h.loan_file(), "loan_outstanding_usdt": 1000.0, "mode": "dry_run"}
    # La wallet efectiva suma lo simulado: el capital propio sigue siendo la wallet real (10.000), no 9.000.
    h.avanzar(61)
    svc.tick()
    ev = h.state()["upnl"]["eval"]
    assert ev["estado"] == "simulado" and ev["capital_propio"] == "10000.00" and ev["prestamo"] == "1000.00"
    assert ev["upnl_dispara"] == "-8000.00"
    assert h.state()["prestamo"]["dry_run"] == "1000.00"
    _upnl_a(h.fake, "-8000")
    h.avanzar(61)
    svc.tick()
    assert h.state()["prestamo"]["dry_run"] == "2000.00"
    assert all("[SIMULACIÓN]" in t for t in h.sent if "Inyección" in t)


def test_upnl_registra_no_transfiere_y_avisa_una_vez_por_escalon(h):  # noqa: F811
    svc = _arrancar(h, modo="real", upnl_mode="dry_run")
    loan_antes = h.loan_file()
    _upnl_a(h.fake, "-7000")
    for _ in range(3):
        h.avanzar(15)
        svc.tick()
    assert h.fake.posts() == []
    st = h.state()
    assert st["prestamo"] == {"real": "0.00", "dry_run": "0.00"} and st["ledger"] == []
    assert h.loan_file()["loan_outstanding_usdt"] == loan_antes["loan_outstanding_usdt"] == 0.0
    avisos = [t for t in h.sent if "HARÍA inyección por uPnL" in t]
    assert len(avisos) == 1 and "escalón 1" in avisos[0] and "[SIMULACIÓN]" in avisos[0]
    assert st["upnl"]["eval"]["estado"] == "registra"
    _upnl_a(h.fake, "-8000")  # el real ya habría inyectado el segundo paso
    h.avanzar(15)
    svc.tick()
    avisos = [t for t in h.sent if "HARÍA inyección por uPnL" in t]
    assert len(avisos) == 2 and "escalón 2" in avisos[1]
    _upnl_a(h.fake, "-5000")  # se recupera más de un paso arriba de la línea: el próximo cruce vuelve a avisar
    h.avanzar(15)
    svc.tick()
    _upnl_a(h.fake, "-7000")
    h.avanzar(15)
    svc.tick()
    avisos = [t for t in h.sent if "HARÍA inyección por uPnL" in t]
    assert len(avisos) == 3 and "escalón 1" in avisos[2]
    assert [e for e in h.state()["eventos"] if e["tipo"] == "upnl"][-1]["modo"] == "dry_run"


# ---------------------------------------------------------------- guarda de la devolución parcial


def test_upnl_guarda_evita_el_ping_pong(h):  # noqa: F811
    svc = _arrancar(h)
    h.fake.maint = Decimal("50")
    _upnl_a(h.fake, "-7000")
    h.avanzar(15)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"]
    # Después de inyectar: margen 4.000, mantenimiento 50 => ratio 80 >= 32. Sin la guarda, pasado el cooldown de
    # 10 min se devolvería todo lo inyectado y el disparo U lo volvería a pedir al minuto.
    h.avanzar(rescate.DEFAULT_DEVOLUCION_COOLDOWN_S + 1)
    svc.tick()
    assert _devoluciones(h) == []
    assert h.state()["upnl"]["eval"]["devolucion_max_guarda"] == "0.00"
    for _ in range(5):
        h.avanzar(61)
        svc.tick()
    assert _devoluciones(h) == [] and _inyecciones(h) == ["1000.00"]  # sin idas y vueltas
    # Con el uPnL recuperado la guarda deja devolver.
    _upnl_a(h.fake, "-5000")
    h.avanzar(61)
    svc.tick()
    assert _devoluciones(h) == ["1000.00"]
    assert h.state()["prestamo"]["real"] == "0.00"


def test_sin_guarda_hay_ping_pong(h):  # noqa: F811
    """Control: lo que la guarda evita (el disparo U encendido y la guarda forzada a no limitar)."""
    svc = _arrancar(h)
    svc._upnl_guarda = lambda valores: Decimal("1e9")
    h.fake.maint = Decimal("50")
    _upnl_a(h.fake, "-7000")
    h.avanzar(15)
    svc.tick()
    h.avanzar(rescate.DEFAULT_DEVOLUCION_COOLDOWN_S + 1)
    svc.tick()
    assert _devoluciones(h) == ["1000.00"]
    h.avanzar(61)
    svc.tick()
    assert _inyecciones(h) == ["1000.00", "1000.00"]  # vuelve a entrar
    h.avanzar(rescate.DEFAULT_DEVOLUCION_COOLDOWN_S + 1)
    svc.tick()
    assert _devoluciones(h) == ["1000.00", "1000.00"]  # y vuelve a salir: ping-pong


def _con_prestamo(h, prestamo="3000.00", wallet="13000", upnl_valor="-6500", upnl_mode="real", **cfg):  # noqa: F811
    """Préstamo real pendiente con posiciones (capital propio 10.000, ratio 65 >= 32) y base fijada."""
    _configurar(h, **cfg)
    st = h.state()
    st["prestamo"]["real"] = prestamo
    rescate.save_state(h.data_dir, st)
    rescate.write_loan_state(h.loan_path, Decimal(prestamo), "real")
    _cuenta(h.fake, wallet=wallet, upnl=upnl_valor, max_withdraw="10000")
    return _servicio(h, upnl_mode=upnl_mode)


def test_upnl_guarda_limita_la_devolucion_parcial(h):  # noqa: F811
    svc = _con_prestamo(h)
    svc.tick()
    # Sin guarda: min(3.000, 10.000, 6.500 - 32 × 100 = 3.300) = 3.000. Con guarda: -6.500 + 3.000 + 7.000 - 1.000.
    assert _devoluciones(h) == ["2500.00"]
    assert h.state()["prestamo"]["real"] == "500.00"
    texto = [e["texto"] for e in h.state()["eventos"] if e["tipo"] == "devolucion"][-1]
    assert "guarda del disparo por uPnL" in texto


@pytest.mark.parametrize("upnl_mode,cfg_upnl", [("dry_run", U_ON), ("off", U_ON), ("real", U_OFF)])
def test_sin_disparo_que_transfiera_no_hay_guarda(h, upnl_mode, cfg_upnl):  # noqa: F811
    """En "registra" (U en dry_run con el servicio en real), con U en off o deshabilitado, la devolución parcial
    real no cambia."""
    svc = _con_prestamo(h, upnl_mode=upnl_mode, upnl=cfg_upnl)
    svc.tick()
    assert _devoluciones(h) == ["3000.00"]


# ---------------------------------------------------------------- lectura sin uPnL


def test_upnl_sin_campos_en_la_lectura_pausa_u_y_la_parcial_pero_no_el_de_margen(h):  # noqa: F811
    svc = _con_prestamo(h)
    h.fake.omitir_upnl = True
    svc.tick()
    assert _devoluciones(h) == []  # la guarda no se puede evaluar: no se devuelve
    st = h.state()
    assert st["upnl"]["eval"]["error"] and "en pausa" in st["upnl"]["eval"]["error"]
    avisos = [t for t in h.sent if "totalUnrealizedProfit" in t]
    assert len(avisos) == 1
    h.avanzar(15)
    svc.tick()
    assert len([t for t in h.sent if "totalUnrealizedProfit" in t]) == 1  # sin spam
    # El disparo de margen sigue: ratio 6.500 / 1.000 = 6,5 < 8.
    h.fake.maint = Decimal("1000")
    h.avanzar(15)
    svc.tick()
    assert _inyecciones(h) == ["1000.00"]
    assert "disparo" not in h.state()["ledger"][-1]
    h.fake.omitir_upnl = False
    h.avanzar(15)
    svc.tick()
    assert any("lectura de wallet y uPnL recuperada" in t for t in h.sent)


# ---------------------------------------------------------------- config del panel


def test_config_guardada_sin_campos_upnl_carga_apagado(data_dir):  # noqa: F811
    viejo = {k: v for k, v in rescate.default_config().items() if not k.startswith("upnl_")}
    viejo.update(habilitado=True, generacion=1, reserva_max_usdt="5000", tope_perdida_pct="30")
    (data_dir / rescate.CONFIG_FILE_NAME).write_text(json.dumps(viejo))
    cfg, err = rescate.load_config(data_dir)
    assert err is None and cfg["habilitado"] is True
    assert cfg["upnl_habilitado"] is False and cfg["upnl_x_pct"] == "70" and cfg["upnl_paso_usdt"] == "1000.00"


@pytest.mark.parametrize("campo,valor", [("upnl_habilitado", "si"), ("upnl_x_pct", "5"), ("upnl_x_pct", None),
                                         ("upnl_paso_usdt", "0")])
def test_config_upnl_invalida_en_disco_deshabilita(data_dir, campo, valor):  # noqa: F811
    cfg = {**rescate.default_config(), "habilitado": True, "generacion": 1, "reserva_max_usdt": "5000",
           "tope_perdida_pct": "30", campo: valor}
    (data_dir / rescate.CONFIG_FILE_NAME).write_text(json.dumps(cfg))
    loaded, err = rescate.load_config(data_dir)
    assert err and "rescate deshabilitado" in err and loaded["habilitado"] is False


@pytest.mark.parametrize("extra,error", [
    ({"upnl_habilitado": True}, "van juntos: faltan upnl_x_pct, upnl_paso_usdt"),
    ({"upnl_x_pct": 70, "upnl_paso_usdt": 1000}, "van juntos: faltan upnl_habilitado"),
    ({**U_ON, "upnl_habilitado": "true"}, "upnl_habilitado tiene que ser true o false"),
    ({**U_ON, "upnl_x_pct": 5}, "upnl_x_pct fuera de rango"),
    ({**U_ON, "upnl_x_pct": 96}, "upnl_x_pct fuera de rango"),
    ({**U_ON, "upnl_x_pct": "70"}, "upnl_x_pct tiene que ser un número."),
    ({**U_ON, "upnl_x_pct": None}, "upnl_x_pct es obligatorio"),
    ({**U_ON, "upnl_paso_usdt": 5}, "upnl_paso_usdt fuera de rango"),
    ({**U_ON, "upnl_paso_usdt": 6000}, "El paso del disparo por uPnL no puede ser mayor que la reserva"),
    ({**U_ON, "upnl_otro": 1}, "Campos desconocidos: upnl_otro"),
])
def test_panel_validacion_upnl(app, client, extra, error):  # noqa: F811
    resp = _post_raw(client, "/api/rescate/config", json.dumps({**_cfg_body(), **extra}))
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert error in resp.get_json()["error"]
    assert not (pathlib.Path(app.config["DATABASE"]).parent / rescate.CONFIG_FILE_NAME).exists()
    assert audit_lines(app)[-1]["result"] == "rechazado"


def test_panel_guarda_upnl_y_un_body_sin_upnl_lo_conserva(app, client):  # noqa: F811
    ok = _post_raw(client, "/api/rescate/config", json.dumps({**_cfg_body(), **U_ON, "upnl_x_pct": 50.5}))
    assert ok.status_code == 200
    cfg = ok.get_json()["status"]["config"]
    assert cfg["upnl_habilitado"] is True and cfg["upnl_x_pct"] == "50.5" and cfg["upnl_paso_usdt"] == "1000"
    # Una pestaña abierta antes del deploy (sin los campos U) no prende ni apaga el disparo.
    ok = _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body(paso_usdt=500)))
    assert ok.status_code == 200
    cfg = ok.get_json()["status"]["config"]
    assert cfg["upnl_habilitado"] is True and cfg["upnl_x_pct"] == "50.5" and cfg["paso_usdt"] == "500"
    # Pero tampoco puede dejar el paso U guardado por encima de la reserva nueva.
    resp = _post_raw(client, "/api/rescate/config", json.dumps(_cfg_body(reserva_max_usdt=800, paso_usdt=500)))
    assert resp.status_code == 400 and "mayor que la reserva" in resp.get_json()["error"]
    ok = _post_raw(client, "/api/rescate/config", json.dumps({**_cfg_body(), **U_OFF}))
    assert ok.get_json()["status"]["config"]["upnl_habilitado"] is False
    assert audit_lines(app)[-1]["action"] == "rescate_config" and audit_lines(app)[-1]["result"] == "ok"


# ---------------------------------------------------------------- panel y tarjetas


def test_panel_y_tarjeta_muestran_el_disparo(client, hd):  # noqa: F811
    html = client.get("/").get_data(as_text=True)
    assert 'data-tarjeta="upnl"' not in html  # deshabilitado: sin tarjeta
    page = client.get("/rescate").get_data(as_text=True)
    assert 'id="rescateUpnlHabilitado"' in page and 'id="rescateUpnlX"' in page and 'data-r="upnl"' in page
    assert 'min="10" max="95"' in page

    hd.configurar(reserva=10000, paso=1000, umbral=8, devolucion=32, tope=50, upnl=U_ON)
    _cuenta(hd.fake)
    svc = hd.service("real", env={rescate.ENV_UPNL_MODE: "real"})
    svc.tick()
    _upnl_a(hd.fake, "-7000")
    hd.avanzar(15)
    svc.tick()
    hd.avanzar(15)
    svc.tick()

    status = client.get("/api/rescate/status").get_json()
    up = status["upnl"]
    assert up["habilitado"] is True and up["modo"] == "real" and up["estado"] == "real"
    assert up["eval"]["upnl"] == "-7000.00" and up["eval"]["upnl_dispara"] == "-8000.00"
    assert up["ultima_inyeccion"]["monto"] == "1000.00"
    assert status["limites"]["upnl_x_pct"] == ["10", "95"]

    html = client.get("/").get_data(as_text=True)
    m = re.search(r'data-tarjeta="upnl".*?</div>\s*<div class="card-footer', html, re.S)
    assert m, "falta la tarjeta upnl"
    tarjeta = m.group(0)
    assert 'data-upnl-estado="real"' in tarjeta and "$-7000.00" in tarjeta
    assert "X 70% · paso $1000" in tarjeta and "próximo paso con uPnL ≤ $-8000.00" in tarjeta
    assert "última inyección uPnL $1000.00" in tarjeta


def test_tarjeta_upnl_habilitado_sin_modo_en_el_servicio(client, hd):  # noqa: F811
    hd.configurar(reserva=10000, paso=1000, tope=50, upnl=U_ON)
    _cuenta(hd.fake)
    hd.service("real").tick()  # FUTURESBOARD_RESCATE_UPNL_MODE sin definir = off
    r = resumen_rescate.tarjetas(pathlib.Path(hd.data_dir), now=hd.clock.t)
    assert r["upnl"]["estado"] == "off" and r["upnl"]["modo"] == "off" and r["upnl"]["upnl"] is None
    html = client.get("/").get_data(as_text=True)
    assert "el servicio no lo evalúa (FUTURESBOARD_RESCATE_UPNL_MODE=off)" in html


# ---------------------------------------------------------------- revisión adversarial de efb7e8e


def _ciclo(h, valor, segundos=15):  # noqa: F811
    _upnl_a(h.fake, valor)
    h.avanzar(segundos)
    h.svc.tick()


def _avisos_registra(h):  # noqa: F811
    return [t for t in h.sent if "HARÍA inyección por uPnL" in t]


def test_upnl_registra_oscilando_sobre_la_linea_avisa_una_vez(h):  # noqa: F811
    """Hallazgo 1: un uPnL que oscila sobre la línea (-7.010 / -6.990 con la línea en -7.000) mandaba un aviso y un
    evento por cada cruce (20 en 10 min) y la historia de `eventos` (últimos 100) se perdía."""
    h.svc = _arrancar(h, modo="real", upnl_mode="dry_run")
    eventos_antes = len(h.state()["eventos"])
    for i in range(40):
        _ciclo(h, "-7010" if i % 2 == 0 else "-6990")
    assert len(_avisos_registra(h)) == 1
    eventos = h.state()["eventos"]
    assert len([e for e in eventos if e["tipo"] == "upnl"]) == 1
    assert len(eventos) == eventos_antes + 1
    assert h.fake.posts() == []


def test_upnl_registra_histeresis_de_un_paso(h):  # noqa: F811
    """Un aviso por escalón en cada episodio: el episodio termina recién cuando la pérdida se recupera al menos un paso
    arriba de la línea (el mismo margen que deja la guarda de la devolución parcial)."""
    h.svc = _arrancar(h, modo="real", upnl_mode="dry_run")
    _ciclo(h, "-8000")  # déficit 1.000 sobre la línea de -7.000: escalón 2
    assert len(_avisos_registra(h)) == 1 and "escalón 2" in _avisos_registra(h)[-1]
    for valor in ("-7990", "-8010") * 5:  # borde entre el escalón 1 y el 2
        _ciclo(h, valor)
    _ciclo(h, "-6500")  # 500 arriba de la línea: menos de un paso, el episodio sigue
    _ciclo(h, "-8000")
    assert len(_avisos_registra(h)) == 1
    _ciclo(h, "-9000")  # escalón 3: nuevo
    assert len(_avisos_registra(h)) == 2 and "escalón 3" in _avisos_registra(h)[-1]
    _ciclo(h, "-5990")  # 1.010 arriba de la línea: más de un paso, se rearma
    _ciclo(h, "-7000")
    assert len(_avisos_registra(h)) == 3 and "escalón 1" in _avisos_registra(h)[-1]
    assert h.fake.posts() == []  # el ratio nunca baja de 8: el de margen no se mete


def test_upnl_en_dry_run_solo_registra_tambien_con_el_servicio_en_dry_run(h):  # noqa: F811
    """Hallazgo 2: con el servicio y U en dry_run, U generaba préstamo simulado que se publicaba en loan_state.json
    y achicaba a passivbot de verdad (dimensionaba sobre 7.000 en vez de 10.000) aunque los textos decían "solo
    registra". U en dry_run es siempre "registra": nada cambia en la cuenta ni en loan_state.json."""
    h.svc = _arrancar(h, modo="dry_run", upnl_mode="dry_run")
    for valor in ("-7000", "-8000", "-9000"):
        _ciclo(h, valor, segundos=61)
    st = h.state()
    assert st["upnl"]["eval"]["estado"] == "registra"
    assert st["prestamo"] == {"real": "0.00", "dry_run": "0.00"} and st["ledger"] == []
    assert h.loan_file()["loan_outstanding_usdt"] == 0.0
    assert h.fake.posts() == []
    assert [a.split("(")[1].split(",")[0] for a in _avisos_registra(h)] == ["escalón 1", "escalón 2", "escalón 3"]
    assert "ultima_inyeccion" not in st["upnl"]


def test_upnl_con_el_rescate_deshabilitado_queda_off(client, hd):  # noqa: F811
    """Hallazgo 3: con el rescate deshabilitado el panel y la tarjeta mostraban el disparo U "real · condición
    cumplida" (no transfería nada) y una lectura sin wallet/uPnL avisaba igual por Telegram."""
    hd.configurar(reserva=10000, paso=1000, umbral=8, devolucion=32, tope=50, upnl=U_ON)
    _cuenta(hd.fake)
    svc = hd.service("real", env={rescate.ENV_UPNL_MODE: "real"})
    svc.tick()
    hd.configurar(habilitado=False, reserva=10000, paso=1000, umbral=8, devolucion=32, tope=50, upnl=U_ON)
    _upnl_a(hd.fake, "-9000")
    hd.avanzar(15)
    svc.tick()
    assert hd.fake.posts() == []
    up = client.get("/api/rescate/status").get_json()["upnl"]
    assert up["habilitado"] is True and up["rescate_habilitado"] is False
    assert up["estado"] == "off" and up["eval"] is None
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'data-tarjeta="upnl".*?</div>\s*<div class="card-footer', html, re.S)
    assert m, "falta la tarjeta upnl"
    tarjeta = m.group(0)
    assert 'data-upnl-estado="off"' in tarjeta and "el rescate está deshabilitado" in tarjeta
    assert "condición cumplida" not in tarjeta and "FUTURESBOARD_RESCATE_UPNL_MODE" not in tarjeta
    assert "rescate_habilitado" in client.get("/rescate").get_data(as_text=True)  # el JS del panel distingue el motivo
    hd.fake.omitir_upnl = True
    hd.avanzar(15)
    svc.tick()
    assert not [t for t in hd.sent if "totalUnrealizedProfit" in t]


def _body_panel(**upnl):  # noqa: F811
    return {**_cfg_body(reserva_max_usdt=10000, umbral_devolucion_ratio=32, tope_perdida_pct=50), **U_ON, **upnl}


def _servicio_en_perdida(hd, upnl_cfg=U_ON, upnl_mode="real", modo="real", upnl="-5000"):  # noqa: F811
    """Servicio con la config del panel y una lectura con uPnL -5.000 (capital propio 10.000): con X 70 no dispara."""
    hd.configurar(reserva=10000, paso=1000, umbral=8, devolucion=32, tope=50, upnl=upnl_cfg)
    _cuenta(hd.fake, upnl=upnl)
    svc = hd.service(modo, env={rescate.ENV_UPNL_MODE: upnl_mode})
    svc.tick()
    assert hd.fake.posts() == []
    return svc


@pytest.mark.parametrize("upnl_cfg", [U_ON, U_OFF])
def test_panel_bajar_x_con_la_perdida_cumplida_pide_confirmacion(app, client, hd, upnl_cfg):  # noqa: F811
    """Hallazgo 5: bajar X (o prender el disparo) con la cuenta ya en pérdida inyectaba varios pasos seguidos sin
    confirmación (X 70 -> 30 con uPnL -5.000: 3 pasos de 1.000 en ~2 min)."""
    _servicio_en_perdida(hd, upnl_cfg=upnl_cfg)
    body = _body_panel(upnl_x_pct=30)
    resp = _post_raw(client, "/api/rescate/config", json.dumps(body))
    assert resp.status_code == 409, resp.get_data(as_text=True)
    d = resp.get_json()
    assert d["ok"] is False and d["requiere_confirmacion"] is True
    assert "3 pasos" in d["error"] and "3000.00 USDT" in d["error"] and "¿Guardar igual?" in d["error"]
    cfg, _err = rescate.load_config(hd.data_dir)
    assert cfg["upnl_x_pct"] == str(upnl_cfg["upnl_x_pct"]) and cfg["upnl_habilitado"] is upnl_cfg["upnl_habilitado"]
    assert audit_lines(app)[-1]["result"] == "requiere_confirmacion"
    resp = _post_raw(client, "/api/rescate/config", json.dumps({**body, "confirmar_inyeccion": True}))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    cfg = resp.get_json()["status"]["config"]
    assert cfg["upnl_x_pct"] == "30" and cfg["upnl_habilitado"] is True
    assert audit_lines(app)[-1]["result"] == "ok"


@pytest.mark.parametrize("caso", ["no_dispara", "registra", "sin_campos_upnl", "upnl_apagado", "sin_posiciones"])
def test_panel_sin_inyeccion_inmediata_no_pide_confirmacion(client, hd, caso):  # noqa: F811
    _servicio_en_perdida(hd, upnl_mode="dry_run" if caso == "registra" else "real",
                         upnl="0" if caso == "sin_posiciones" else "-5000")
    if caso == "sin_posiciones":
        hd.fake.positions = {}
        _upnl_a(hd.fake, "-5000")
        hd.avanzar(15)
        hd.service("real", env={rescate.ENV_UPNL_MODE: "real"}).tick()
    body = {"no_dispara": _body_panel(upnl_x_pct=60),  # línea -6.000: con -5.000 no dispara
            "registra": _body_panel(upnl_x_pct=30),
            "sin_campos_upnl": _cfg_body(reserva_max_usdt=10000, umbral_devolucion_ratio=32, tope_perdida_pct=50),
            "upnl_apagado": _body_panel(upnl_habilitado=False, upnl_x_pct=30),
            "sin_posiciones": _body_panel(upnl_x_pct=30)}[caso]
    resp = _post_raw(client, "/api/rescate/config", json.dumps(body))
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_panel_confirmar_inyeccion_tiene_que_ser_booleano(client, hd):  # noqa: F811
    _servicio_en_perdida(hd)
    body = {**_body_panel(upnl_x_pct=30), "confirmar_inyeccion": 1}
    resp = _post_raw(client, "/api/rescate/config", json.dumps(body))
    assert resp.status_code == 400 and "confirmar_inyeccion tiene que ser true o false" in resp.get_json()["error"]


def test_upnl_ultima_lectura_guarda_wallet_y_upnl(h):  # noqa: F811
    """La confirmación del panel calcula con la última lectura: wallet efectiva y uPnL quedan en `ultimo` aunque el
    disparo U esté apagado."""
    h.svc = _arrancar(h, upnl=U_OFF)
    _ciclo(h, "-5000")
    ultimo = h.state()["ultimo"]
    assert ultimo["wallet_balance"] == "10000.00" and ultimo["upnl"] == "-5000.00"
