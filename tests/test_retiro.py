"""Retiro periódico de ganancias del rescate (futuros -> spot): períodos, monto, guardas, modos, conciliación,
préstamo pendiente, tope, C3 y panel.

Cuenta de prueba por default: wallet 12.000 (sin uPnL), mantenimiento 100 (ratio 120: ni inyección ni devolución
parcial), maxWithdrawAmount 9.000, spot 10.000. Base del retiro 10.000: excedente 2.000. El reloj arranca el
2023-11-14 22:13:20 UTC (martes): con frecuencia diaria el primer período nuevo es el 2023-11-15.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
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
from test_rescate_dashboard import _home
from test_rescate_dashboard import _tarjeta
from test_rescate_dashboard import hd  # noqa: F401

RETIRO_DIARIO = {"retiro_frecuencia": "diaria", "retiro_base_usdt": 10000}


def _cuenta(fake, wallet="12000", upnl="0", maint="100", max_withdraw="9000", spot="10000"):  # noqa: F811
    fake.upnl = Decimal(upnl)
    fake.margin = Decimal(wallet) + fake.upnl
    fake.maint, fake.max_withdraw, fake.spot = Decimal(maint), Decimal(max_withdraw), Decimal(spot)


def _configurar(h, retiro=RETIRO_DIARIO, upnl=None, **kw):  # noqa: F811
    body = _cfg_body(**{"reserva_max_usdt": 10000, "umbral_devolucion_ratio": 32, **kw})
    if retiro is not None:
        body.update(retiro)
    if upnl is not None:
        body.update(upnl)
    return rescate.save_config(h.data_dir, rescate.validate_config_body(body), "cliente17")


def _servicio(h, modo="real", retiro_mode="real", **env):  # noqa: F811
    return h.service(modo, env={rescate.ENV_RETIRO_MODE: retiro_mode, **env})


def _ir_a(h, iso):  # noqa: F811
    t = dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc).timestamp()
    assert t >= h.clock.t
    h.avanzar(t - h.clock.t)


def _retiros(h):  # noqa: F811
    return [p["amount"] for p in h.fake.posts() if p["type"] == "UMFUTURE_MAIN"]


def _arrancar(h, modo="real", retiro_mode="real", **kw):  # noqa: F811
    """Config + cuenta + primer ciclo (activa el retiro: el período en curso queda cerrado)."""
    _configurar(h, **kw)
    _cuenta(h.fake)
    svc = _servicio(h, modo, retiro_mode)
    svc.tick()
    rs = h.state()["retiro"]
    frecuencia = (kw.get("retiro") or RETIRO_DIARIO)["retiro_frecuencia"]
    assert rs["periodo_cerrado"] == rescate.retiro_periodo(h.clock.t, frecuencia) and rs["eval"]["vencido"] is False
    assert _retiros(h) == []
    return svc


# ---------------------------------------------------------------- estado, períodos y config


@pytest.mark.parametrize("servicio,retiro,esperado", [
    ("off", "real", rescate.RETIRO_OFF), ("real", "off", rescate.RETIRO_OFF), (None, "real", rescate.RETIRO_OFF),
    ("real", None, rescate.RETIRO_OFF), ("real", "real", rescate.RETIRO_REAL),
    ("real", "dry_run", rescate.RETIRO_SIMULADO), ("dry_run", "real", rescate.RETIRO_SIMULADO),
    ("dry_run", "dry_run", rescate.RETIRO_SIMULADO),
])
def test_retiro_estado_efectivo(servicio, retiro, esperado):
    assert rescate.retiro_estado(servicio, retiro) == esperado


@pytest.mark.parametrize("raw,modo,error", [
    ("", "off", False), ("OFF", "off", False), ("dry_run", "dry_run", False), (" Real ", "real", False),
    ("si", "off", True), ("1", "off", True),
])
def test_retiro_modo_por_entorno_fail_closed(raw, modo, error):
    settings = rescate.RescateSettings({rescate.ENV_RETIRO_MODE: raw})
    assert settings.retiro_mode == modo
    assert (settings.retiro_mode_error is not None) is error


def test_retiro_apagado_por_default():
    cfg = rescate.default_config()
    assert cfg["retiro_frecuencia"] == "off" and cfg["retiro_base_usdt"] == "10000.00" and cfg["retiro_desde"] is None
    assert rescate.RescateSettings({}).retiro_mode == "off"
    assert rescate.FRECUENCIA_RECOMENDADA in rescate.FRECUENCIAS and rescate.FRECUENCIA_RECOMENDADA != "off"
    # Retira por el mismo tipo de Binance que la devolución: no hace falta otro permiso de la API key.
    assert rescate.TIPO_API[rescate.TIPO_RETIRO] == rescate.TIPO_API[rescate.TIPO_DEVOLUCION] == "UMFUTURE_MAIN"


@pytest.mark.parametrize("ts,frecuencia,clave,proximo", [
    ("2023-11-14 22:13:20", "diaria", "2023-11-14", "2023-11-15T00:00:00+00:00"),
    ("2023-11-14 22:13:20", "semanal", "2023-S46", "2023-11-20T00:00:00+00:00"),
    ("2023-11-20 00:00:00", "semanal", "2023-S47", "2023-11-27T00:00:00+00:00"),
    ("2024-12-30 10:00:00", "semanal", "2025-S01", "2025-01-06T00:00:00+00:00"),
    ("2023-12-31 23:59:59", "mensual", "2023-12", "2024-01-01T00:00:00+00:00"),
    ("2024-02-29 12:00:00", "mensual", "2024-02", "2024-03-01T00:00:00+00:00"),
    ("2023-11-14 22:13:20", "trimestral", "2023-T4", "2024-01-01T00:00:00+00:00"),
    ("2024-03-31 23:59:59", "trimestral", "2024-T1", "2024-04-01T00:00:00+00:00"),
    ("2024-07-01 00:00:00", "trimestral", "2024-T3", "2024-10-01T00:00:00+00:00"),
    ("2023-11-14 22:13:20", "anual", "2023", "2024-01-01T00:00:00+00:00"),
])
def test_periodos_calendario_utc(ts, frecuencia, clave, proximo):
    t = dt.datetime.fromisoformat(ts).replace(tzinfo=dt.timezone.utc).timestamp()
    assert rescate.retiro_periodo(t, frecuencia) == clave
    assert rescate.retiro_proximo_inicio(t, frecuencia).isoformat() == proximo


def test_config_retiro_validacion_estricta():
    ok = rescate.validate_config_body(_cfg_body(**RETIRO_DIARIO))
    assert ok["retiro"] == {"retiro_frecuencia": "diaria", "retiro_base_usdt": Decimal("10000")}
    assert rescate.validate_config_body(_cfg_body())["retiro"] is None  # sin los campos: se conservan los guardados
    malos = [
        {"retiro_frecuencia": "diaria"},                                   # van juntos
        {"retiro_base_usdt": 10000},
        {"retiro_frecuencia": "quincenal", "retiro_base_usdt": 10000},
        {"retiro_frecuencia": None, "retiro_base_usdt": 10000},
        {"retiro_frecuencia": True, "retiro_base_usdt": 10000},
        {"retiro_frecuencia": "diaria", "retiro_base_usdt": None},
        {"retiro_frecuencia": "diaria", "retiro_base_usdt": "10000"},
        {"retiro_frecuencia": "diaria", "retiro_base_usdt": True},
        {"retiro_frecuencia": "diaria", "retiro_base_usdt": 9.99},
        {"retiro_frecuencia": "diaria", "retiro_base_usdt": 1000001},
        {"retiro_frecuencia": "diaria", "retiro_base_usdt": float("inf")},
    ]
    for extra in malos:
        with pytest.raises(ValueError):
            rescate.validate_config_body(_cfg_body(**extra))


def test_config_retiro_desde_cambia_solo_con_la_frecuencia(h):  # noqa: F811
    cfg = _configurar(h)
    desde = cfg["retiro_desde"]
    assert cfg["retiro_frecuencia"] == "diaria" and desde
    cfg = _configurar(h, retiro={"retiro_frecuencia": "diaria", "retiro_base_usdt": 12000})
    assert cfg["retiro_desde"] == desde and cfg["retiro_base_usdt"] == "12000.00"
    cfg = _configurar(h, retiro=None)  # sin los campos del retiro: se conservan
    assert cfg["retiro_frecuencia"] == "diaria" and cfg["retiro_base_usdt"] == "12000.00"
    cfg = _configurar(h, retiro={"retiro_frecuencia": "off", "retiro_base_usdt": 12000})
    assert cfg["retiro_desde"] is None


def test_config_vieja_sin_retiro_lo_toma_apagado(h):  # noqa: F811
    path = h.data_dir / rescate.CONFIG_FILE_NAME
    viejo = {k: v for k, v in rescate.default_config().items() if not k.startswith("retiro") and k != "perfil"}
    path.write_text(json.dumps(viejo))
    cfg, err = rescate.load_config(h.data_dir)
    assert err is None and cfg["retiro_frecuencia"] == "off"


@pytest.mark.parametrize("campo,valor", [("retiro_frecuencia", "semestral"), ("retiro_base_usdt", "5"),
                                         ("retiro_desde", 3), ("perfil", "conservador")])
def test_config_retiro_invalida_deshabilita_todo(h, campo, valor):  # noqa: F811
    _configurar(h)
    path = h.data_dir / rescate.CONFIG_FILE_NAME
    raw = json.loads(path.read_text())
    raw[campo] = valor
    path.write_text(json.dumps(raw))
    cfg, err = rescate.load_config(h.data_dir)
    assert err and cfg["habilitado"] is False and cfg["retiro_frecuencia"] == "off"


def test_monto_y_limites():
    calc = rescate.retiro_monto(Decimal("12000"), Decimal("10000"), Decimal("9000"), Decimal("12000"), Decimal("100"),
                                Decimal("32"), None, None, None)
    assert calc["monto"] == Decimal("2000.00") and calc["limite"] == "excedente"
    calc = rescate.retiro_monto(Decimal("12000"), Decimal("10000"), Decimal("100.009"), Decimal("12000"),
                                Decimal("100"), Decimal("32"), None, None, None)
    assert calc["monto"] == Decimal("100.00") and calc["limite"] == "maxWithdrawAmount"
    calc = rescate.retiro_monto(Decimal("12000"), Decimal("10000"), Decimal("-50"), Decimal("12000"), Decimal("100"),
                                Decimal("32"), None, None, None)
    assert calc["monto"] == Decimal("0.00") and calc["limite"] == "maxWithdrawAmount"
    calc = rescate.retiro_monto(Decimal("12000"), Decimal("10000"), Decimal("9000"), Decimal("12000"), Decimal("320"),
                                Decimal("32"), None, None, None)
    assert calc["monto"] == Decimal("1760.00") and calc["limite"] == "guarda de margen"
    calc = rescate.retiro_monto(Decimal("9000"), Decimal("10000"), Decimal("9000"), Decimal("9000"), Decimal("0"),
                                Decimal("32"), None, None, None)
    assert calc["excedente"] == Decimal("-1000") and calc["monto"] == Decimal("0.00")
    # Sin posiciones (mantenimiento 0) no hay guardas.
    calc = rescate.retiro_monto(Decimal("12000"), Decimal("10000"), Decimal("12000"), Decimal("12000"), Decimal("0"),
                                Decimal("32"), Decimal("0"), Decimal("70"), Decimal("1000"))
    assert calc["monto"] == Decimal("2000.00") and calc["guarda_margen"] is None and calc["guarda_upnl"] is None


# ---------------------------------------------------------------- retiro real


def test_activar_no_retira_hasta_el_periodo_siguiente_y_despues_retira_el_excedente(h):  # noqa: F811
    svc = _arrancar(h)
    assert any("Retiro periódico activo (real, diaria" in t for t in h.sent)
    h.avanzar(3600)
    svc.tick()  # mismo día: nada
    assert _retiros(h) == []
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert _retiros(h) == ["2000.00"]
    st = h.state()
    fila = st["ledger"][-1]
    assert fila["tipo"] == "retiro" and fila["estado"] == "confirmada" and fila["prestamo_despues"] == "0.00"
    assert st["retiro"]["acumulado"]["real"] == "2000.00" and st["retiro"]["ultimo"]["monto"] == "2000.00"
    assert st["prestamo"]["real"] == "0.00" and st["en_vuelo"] is None
    assert any("Retiro de ganancias 2000.00 USDT futuros → spot" in t and "Retirado acumulado: 2000.00" in t
               for t in h.sent)
    assert h.fake.spot == Decimal("12000") and h.fake.margin == Decimal("10000")
    # El préstamo de passivbot no cambia: loan_state.json sigue en 0 (sin contrato nuevo).
    assert h.loan_file()["loan_outstanding_usdt"] == 0.0
    assert set(h.loan_file()) == {"version", "loan_outstanding_usdt", "updated_at", "mode"}
    # El ciclo siguiente ve el excedente en 0 y cierra el período.
    h.avanzar(15)
    svc.tick()
    rs = h.state()["retiro"]
    assert rs["periodo_cerrado"] == "2023-11-15" and rs["eval"]["vencido"] is False
    assert rs["eval"]["proximo_en"] == "2023-11-16T00:00:00+00:00"
    assert _retiros(h) == ["2000.00"]


@pytest.mark.parametrize("frecuencia,antes,limite", [
    ("semanal", "2023-11-19 23:59:50", "2023-11-20 00:00:05"),
    ("mensual", "2023-11-30 23:59:50", "2023-12-01 00:00:05"),
    ("trimestral", "2023-12-31 23:59:50", "2024-01-01 00:00:05"),
    ("anual", "2023-12-31 23:59:50", "2024-01-01 00:00:05"),
])
def test_retiro_por_frecuencia_al_comienzo_del_periodo(h, frecuencia, antes, limite):  # noqa: F811
    svc = _arrancar(h, retiro={"retiro_frecuencia": frecuencia, "retiro_base_usdt": 10000})
    _ir_a(h, antes)
    svc.tick()
    assert _retiros(h) == [] and h.state()["retiro"]["eval"]["proximo_en"] == limite[:10] + "T00:00:00+00:00"
    _ir_a(h, limite)
    svc.tick()
    assert _retiros(h) == ["2000.00"]


def test_varios_periodos_sin_servicio_hacen_un_solo_retiro(h):  # noqa: F811
    svc = _arrancar(h)
    _ir_a(h, "2023-11-20 08:00:00")  # 6 días después
    svc.tick()
    h.avanzar(15)
    svc.tick()
    assert _retiros(h) == ["2000.00"]
    assert h.state()["retiro"]["periodo_cerrado"] == "2023-11-20"


def test_excedente_chico_cierra_el_periodo_sin_retirar(h):  # noqa: F811
    svc = _arrancar(h)
    _cuenta(h.fake, wallet="10009.99")
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert _retiros(h) == []
    st = h.state()
    assert st["retiro"]["periodo_cerrado"] == "2023-11-15"
    assert any("no hay ganancia para retirar" in e["texto"] for e in st["eventos"])
    _cuenta(h.fake, wallet="9000")  # en pérdida: tampoco
    _ir_a(h, "2023-11-16 00:00:05")
    svc.tick()
    assert _retiros(h) == [] and h.state()["retiro"]["periodo_cerrado"] == "2023-11-16"


def test_limitado_por_max_withdraw_sigue_vencido_y_reintenta(h):  # noqa: F811
    svc = _arrancar(h)
    _cuenta(h.fake, max_withdraw="500")
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert _retiros(h) == ["500.00"]
    ev = h.state()["retiro"]["eval"]
    assert ev["vencido"] is True
    assert "limitado por maxWithdrawAmount" in h.state()["ledger"][-1]["detalle"]
    h.avanzar(60)
    svc.tick()  # sin retirable (el fake bajó maxWithdrawAmount a 0) y dentro del cooldown
    assert _retiros(h) == ["500.00"]
    assert "esperando entre retiros" in h.state()["retiro"]["eval"]["motivo_espera"]
    h.avanzar(rescate.RETIRO_COOLDOWN_MS / 1000)
    svc.tick()
    assert _retiros(h) == ["500.00"]
    assert "lo limita maxWithdrawAmount" in h.state()["retiro"]["eval"]["motivo_espera"]
    h.fake.max_withdraw = Decimal("5000")
    h.avanzar(15)
    svc.tick()
    assert _retiros(h) == ["500.00", "1500.00"]
    h.avanzar(15)
    svc.tick()
    assert h.state()["retiro"]["periodo_cerrado"] == "2023-11-15"
    assert h.state()["retiro"]["acumulado"]["real"] == "2000.00"


def test_guarda_de_margen_deja_el_ratio_en_el_umbral_de_devolucion(h):  # noqa: F811
    svc = _arrancar(h)
    h.fake.maint = Decimal("320")  # ratio 37,5: retirar 2.000 lo dejaría en 31,25 < 32
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert _retiros(h) == ["1760.00"]
    assert h.fake.margin / h.fake.maint == Decimal("32")
    h.avanzar(rescate.RETIRO_COOLDOWN_MS / 1000 + 1)
    svc.tick()
    assert _retiros(h) == ["1760.00"]
    assert "lo limita guarda de margen" in h.state()["retiro"]["eval"]["motivo_espera"]
    h.fake.maint = Decimal("100")
    h.avanzar(15)
    svc.tick()
    assert _retiros(h) == ["1760.00", "240.00"]


def test_guarda_del_disparo_por_upnl(h):  # noqa: F811
    upnl_on = {"upnl_habilitado": True, "upnl_x_pct": 70, "upnl_paso_usdt": 1000}
    _configurar(h, upnl=upnl_on)
    _cuenta(h.fake, wallet="20000", upnl="-10000", maint="100", max_withdraw="9000")
    svc = h.service("real", env={rescate.ENV_RETIRO_MODE: "real", rescate.ENV_UPNL_MODE: "real"})
    svc.tick()
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    # Línea U con wallet 20.000: -14.000 (no dispara). Retirando más de 4.285,71 la línea subiría a menos de un
    # paso del uPnL y el disparo U inyectaría enseguida.
    assert _retiros(h) == ["4285.71"]
    assert "guarda del disparo por uPnL" in h.state()["ledger"][-1]["detalle"]
    h.avanzar(61)
    svc.tick()
    assert [p for p in h.fake.posts() if p["type"] == "MAIN_UMFUTURE"] == []
    ev = h.state()["upnl"]["eval"]
    assert ev["dispara"] is False


def test_con_prestamo_pendiente_espera_a_que_se_devuelva(h):  # noqa: F811
    svc = _arrancar(h)
    st = h.state()
    st["prestamo"]["real"] = "1000.00"
    rescate.save_state(h.data_dir, st)
    h.fake.maint = Decimal("1000")  # ratio 12: ni inyección ni devolución parcial
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert _retiros(h) == []
    assert "préstamo pendiente (1000.00 USDT)" in h.state()["retiro"]["eval"]["motivo_espera"]
    st = h.state()
    st["prestamo"]["real"] = "0.00"
    rescate.save_state(h.data_dir, st)
    h.avanzar(15)
    svc.tick()
    assert _retiros(h) == []  # sin préstamo, pero con ratio 12 la guarda de margen (32) no deja retirar nada
    assert "lo limita guarda de margen" in h.state()["retiro"]["eval"]["motivo_espera"]
    h.fake.maint = Decimal("100")
    h.avanzar(15)
    svc.tick()
    assert _retiros(h) == ["2000.00"]


def test_con_tope_en_confirmacion_no_retira(h):  # noqa: F811
    svc = _arrancar(h, tope_perdida_pct=30)  # base 22.000, piso 15.400
    h.fake.spot = Decimal("3000")  # capital 15.000: tope en confirmación (la wallet de futuros no cambia)
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    st = h.state()
    assert st["tope_confirmacion"] is not None
    assert _retiros(h) == []
    assert st["retiro"]["eval"]["motivo_espera"] == "el tope de pérdida está en confirmación"


def test_rechazo_de_binance_espera_y_reintenta(h):  # noqa: F811
    svc = _arrancar(h)
    h.fake.transfer_mode = "reject"
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    st = h.state()
    assert st["ledger"][-1]["estado"] == "rechazada" and st["retiro"].get("acumulado", {}).get("real") in (None, "0.00")
    assert any("Retiro de ganancias de 2000.00 USDT rechazada por Binance" in t for t in h.sent)
    h.fake.transfer_mode = "ok"
    h.avanzar(60)
    svc.tick()
    assert len(_retiros(h)) == 1  # el rechazado: espera 5 min
    h.avanzar(rescate.COOLDOWN_RECHAZO_MS / 1000)
    svc.tick()
    assert _retiros(h) == ["2000.00", "2000.00"]
    assert h.state()["retiro"]["acumulado"]["real"] == "2000.00"


def test_resultado_incierto_se_concilia_y_recien_ahi_cuenta(h):  # noqa: F811
    svc = _arrancar(h)
    st = h.state()
    st["c3"] = {"base": {"capital": "12000.00", "modo": "real"}, "historial": []}
    rescate.save_state(h.data_dir, st)
    h.fake.transfer_mode, h.fake.ejecutar_ambigua = "timeout", True
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    st = h.state()
    assert st["en_vuelo"]["tipo"] == "retiro" and st["en_vuelo"]["estado"] == "incierto"
    assert st["retiro"].get("acumulado", {}).get("real") in (None, "0.00")
    assert st["c3"]["base"]["capital"] == "12000.00"
    h.fake.transfer_mode = "ok"
    h.avanzar(15)
    svc.tick()  # el historial muestra la transferencia: se confirma
    st = h.state()
    assert st["en_vuelo"] is None and st["ledger"][-1]["estado"] == "confirmada"
    assert st["retiro"]["acumulado"]["real"] == "2000.00"
    assert st["c3"]["base"]["capital"] == "10000.00" and st["c3"]["base"]["retirado"] == "2000.00"
    assert st["c3"]["historial"][-1]["tipo"] == "base_retiro"
    assert len(_retiros(h)) == 1  # no se reintentó a ciegas


def test_spot_caido_despues_de_un_retiro_no_es_una_perdida(h):  # noqa: F811
    svc = _arrancar(h, tope_perdida_pct=4.5)  # base 22.000, piso 21.010
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()  # retiro de 2.000 confirmado en este ciclo (spot_ref es de antes)
    assert _retiros(h) == ["2000.00"]
    h.fake.read_fail.add("/api/v3/account")
    h.avanzar(15)
    svc.tick()
    st = h.state()
    # Sin sumar el retiro, el estimado sería 10.000 + 10.000 = 20.000 <= 21.010 y abriría una confirmación falsa.
    assert st["tope_confirmacion"] is None
    assert "último saldo spot leído (12000.00 USDT" in st["ultimo"]["error"]


def test_spot_caido_con_un_retiro_en_vuelo(h):  # noqa: F811
    svc = _arrancar(h, tope_perdida_pct=4.5)
    h.fake.transfer_mode, h.fake.ejecutar_ambigua, h.fake.history_visible = "timeout", True, False
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert h.state()["en_vuelo"]["tipo"] == "retiro"
    h.fake.read_fail.add("/api/v3/account")
    h.avanzar(15)
    svc.tick()
    st = h.state()
    assert st["tope_confirmacion"] is None and "12000.00 USDT" in st["ultimo"]["error"]


def test_otra_transferencia_en_el_ciclo_espera(h):  # noqa: F811
    _configurar(h)
    _cuenta(h.fake)
    svc = _servicio(h, "dry_run")
    svc.tick()
    st = h.state()
    st["prestamo"]["dry_run"] = "500.00"
    rescate.save_state(h.data_dir, st)
    h.fake.positions = {}  # sin posiciones: devolución total (simulada) del préstamo en el mismo ciclo
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    st = h.state()
    assert st["prestamo"]["dry_run"] == "0.00"
    assert st["retiro"]["eval"]["motivo_espera"] == "hubo otra transferencia en este ciclo"
    h.avanzar(15)
    svc.tick()
    assert h.state()["retiro"]["acumulado"]["dry_run"] == "2000.00"


def test_sin_wallet_en_la_lectura_queda_en_pausa(h):  # noqa: F811
    svc = _arrancar(h)
    h.fake.omitir_upnl = True
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    ev = h.state()["retiro"]["eval"]
    assert _retiros(h) == [] and "totalWalletBalance" in ev["error"]
    assert ev["motivo_espera"] == "sin lectura de la wallet de futuros"


# ---------------------------------------------------------------- modos


@pytest.mark.parametrize("modo,retiro_mode", [("off", "real"), ("real", "off"), ("dry_run", "off")])
def test_retiro_apagado_no_hace_nada(h, modo, retiro_mode):  # noqa: F811
    _configurar(h)
    _cuenta(h.fake)
    svc = _servicio(h, modo, retiro_mode)
    svc.tick()
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert _retiros(h) == []
    rs = h.state()["retiro"]
    assert rs.get("eval") is None and rs.get("acumulado") is None
    st = rescate.panel_status(h.data_dir)
    if modo != "off":
        assert st["retiro"]["estado"] == "off" and st["retiro"]["modo"] == retiro_mode


def test_frecuencia_off_en_el_panel_no_hace_nada(h):  # noqa: F811
    _configurar(h, retiro={"retiro_frecuencia": "off", "retiro_base_usdt": 10000})
    _cuenta(h.fake)
    svc = _servicio(h)
    svc.tick()
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert _retiros(h) == [] and h.state()["retiro"].get("eval") is None


@pytest.mark.parametrize("modo,retiro_mode,causa", [("dry_run", "real", "servicio en dry_run"),
                                                     ("dry_run", "dry_run", "servicio en dry_run"),
                                                     ("real", "dry_run", "FUTURESBOARD_RESCATE_RETIRO_MODE=dry_run")])
def test_retiro_simulado_no_transfiere_y_no_repite_el_excedente(h, modo, retiro_mode, causa):  # noqa: F811
    svc = _arrancar(h, modo, retiro_mode)
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert _retiros(h) == []
    st = h.state()
    fila = st["ledger"][-1]
    assert fila["tipo"] == "retiro" and fila["estado"] == "simulada" and fila["modo"] == "dry_run"
    assert st["retiro"]["acumulado"]["dry_run"] == "2000.00" and st["retiro"].get("acumulado", {}).get("real") is None
    assert any(t.startswith("🧪 <b>[SIMULACIÓN]</b>") and "HARÍA retiro de ganancias 2000.00" in t and causa in t
               for t in h.sent)
    h.avanzar(15)
    svc.tick()  # descuenta lo simulado: el excedente queda en 0 y el período se cierra
    assert h.state()["retiro"]["periodo_cerrado"] == "2023-11-15"
    _ir_a(h, "2023-11-16 00:00:05")
    svc.tick()  # la wallet real no bajó, pero el excedente simulado ya se "retiró": no se cuenta dos veces
    st = h.state()
    assert st["retiro"]["acumulado"]["dry_run"] == "2000.00" and st["retiro"]["periodo_cerrado"] == "2023-11-16"
    # El simulado no mueve el préstamo ni la base de C3.
    assert st["prestamo"] == {"real": "0.00", "dry_run": "0.00"}
    assert "c3" not in st or not (st["c3"] or {}).get("base")


def test_retiro_simulado_no_baja_la_base_de_c3(h):  # noqa: F811
    svc = _arrancar(h, "dry_run", "real")
    st = h.state()
    st["c3"] = {"base": {"capital": "12000.00", "modo": "dry_run"}, "historial": []}
    rescate.save_state(h.data_dir, st)
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    st = h.state()
    assert st["retiro"]["acumulado"]["dry_run"] == "2000.00" and st["c3"]["base"]["capital"] == "12000.00"


def test_cambio_de_modo_reancla_sin_retirar_en_el_momento(h):  # noqa: F811
    svc = _arrancar(h, "real", "dry_run")
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert h.state()["retiro"]["acumulado"]["dry_run"] == "2000.00"
    svc = _servicio(h, "real", "real")  # pasa a real: el período en curso se da por cerrado
    _ir_a(h, "2023-11-15 12:00:00")
    svc.tick()
    assert _retiros(h) == []
    _ir_a(h, "2023-11-16 00:00:05")
    svc.tick()
    assert _retiros(h) == ["2000.00"]  # la wallet real: lo simulado no se descuenta en real


# ---------------------------------------------------------------- panel y tarjeta


def test_api_config_guarda_y_muestra_el_retiro(client, app):  # noqa: F811
    body = dict(_cfg_body(), retiro_frecuencia="mensual", retiro_base_usdt=12000)
    resp = _post_raw(client, "/api/rescate/config", json.dumps(body))
    assert resp.status_code == 200
    status = resp.get_json()["status"]
    assert status["config"]["retiro_frecuencia"] == "mensual" and status["config"]["retiro_base_usdt"] == "12000.00"
    assert status["retiro"]["estado"] == "off" and status["retiro"]["recomendada"] == rescate.FRECUENCIA_RECOMENDADA
    assert audit_lines(app)[-1]["action"] == "rescate_config"
    resp = _post_raw(client, "/api/rescate/config", json.dumps(dict(_cfg_body(), retiro_frecuencia="mensual")))
    assert resp.status_code == 400 and "van juntos" in resp.get_json()["error"]
    page = client.get("/rescate").get_data(as_text=True)
    assert 'id="rescateRetiroFrecuencia"' in page and 'data-r="retiro"' in page


def test_tarjeta_del_retiro(client, hd):  # noqa: F811
    html = _home(client)
    assert 'data-tarjeta="retiro"' not in html  # sin frecuencia ni retiros: no se muestra
    _configurar(hd, tope_perdida_pct=30)
    _cuenta(hd.fake)
    svc = _servicio(hd)
    svc.tick()
    tarjeta = _tarjeta(_home(client), "retiro")
    assert "$0,00" in tarjeta and "diaria · base $10.000,00" in tarjeta and "próximo retiro" in tarjeta
    # Período nuevo: retira.
    hd.avanzar(86400)
    svc.tick()
    tarjeta = _tarjeta(_home(client), "retiro")
    assert "$2.000,00" in tarjeta and "último $2.000,00" in tarjeta and 'data-value="2000.00"' in tarjeta
    r = resumen_rescate.tarjetas(pathlib.Path(hd.data_dir), now=hd.clock.t)["retiro"]
    assert r["acumulado"] == "2000.00" and r["estado"] == "real"


def test_tarjeta_retiro_off_en_el_servicio(client, hd):  # noqa: F811
    _configurar(hd)
    _cuenta(hd.fake)
    _servicio(hd, retiro_mode="off").tick()
    tarjeta = _tarjeta(_home(client), "retiro")
    assert "FUTURESBOARD_RESCATE_RETIRO_MODE=off" in tarjeta


def test_simulado_no_recuenta_al_cambiar_la_frecuencia(h):  # noqa: F811
    svc = _arrancar(h, "dry_run", "real")
    _ir_a(h, "2023-11-15 00:00:05")
    svc.tick()
    assert h.state()["retiro"]["acumulado"]["dry_run"] == "2000.00"
    _configurar(h, retiro={"retiro_frecuencia": "semanal", "retiro_base_usdt": 10000})  # re-ancla
    h.avanzar(15)
    svc.tick()
    _ir_a(h, "2023-11-20 00:00:05")
    svc.tick()
    st = h.state()
    assert st["retiro"]["periodo_cerrado"] == "2023-S47" and st["retiro"]["acumulado"]["dry_run"] == "2000.00"
    assert st["retiro"]["eval"]["simulado_descontado"] == "2000.00"
