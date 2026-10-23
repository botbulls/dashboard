# Contratos de archivos con passivbot

El servicio `python -m futuresboard.rescate` (rescate + C3) y passivbot se hablan solo por **archivos JSON** en un
directorio compartido (`./rescate` en el host). El dashboard escribe y passivbot lee, nunca al revés. Esta página
fija qué hay en cada archivo, quién lo escribe y cómo lo interpreta el lector. Sale del código, no de la prosa de
otras páginas:

- escritores: `src/futuresboard/rescate.py` (`loan_state_payload`, `write_loan_state`) y `src/futuresboard/c3.py`
  (`heartbeat_payload`, `write_heartbeat`);
- lectores (repo `botbulls/passivbot`, rama `dev/c3-stop-reinicio`): `rescate_loan.py` y `watcher_heartbeat.py`;
- tests: `tests/test_rescate.py::test_loan_state_contrato_y_permisos`, `tests/test_c3.py` (latido) y, del lado de
  passivbot, `tests/test_rescate_loan.py` y `tests/test_watcher_heartbeat.py`.

!!! warning "Herramienta, no asesor"
    Estos archivos cambian cómo dimensiona y cuándo abre entradas un bot que opera plata real. Cualquier cambio de
    contrato va con su PR del lado de passivbot y se prueba primero en `dry_run`.

## Reglas comunes

| Regla | Detalle |
|---|---|
| Escritura atómica | `write_json_atomic`: archivo temporal en el **mismo** directorio, `fsync`, `chmod 0644` y `os.replace`. El lector nunca ve un archivo a medias. |
| Permisos | `0644`: passivbot corre con otro uid y monta el directorio read-only. |
| Montar el directorio | `os.replace` crea un inode nuevo. Con el archivo montado suelto, passivbot seguiría leyendo el inode viejo. Se monta `./rescate:/passivbot/rescate:ro`. |
| Un escritor por archivo | Cada archivo lo escribe un solo componente del servicio. El servicio corre en una sola instancia (`.rescate_service.lock`). |
| Horas | ISO 8601 en UTC con offset (`2026-10-22T12:00:00+00:00`, `now_iso`). |
| Sin secretos | Ningún archivo compartido lleva credenciales, ids de transferencia ni montos de spot. |

## `loan_state.json` (versión 1, vigente)

- **Escribe:** el servicio rescate, solo él (también la CLI `simular-*` en `dry_run`).
- **Ruta:** `FUTURESBOARD_RESCATE_LOAN_STATE` (default `/rescate/loan_state.json`).
- **Lee:** passivbot con `PASSIVBOT_LOAN_STATE=/passivbot/rescate/loan_state.json`, en cada actualización de
  balance.

```json
{"version": 1, "loan_outstanding_usdt": 1000.0, "updated_at": "2026-10-21T15:04:05+00:00", "mode": "real"}
```

| Campo | Tipo | Significado |
|---|---|---|
| `version` | int, exactamente `1` | El lector rechaza cualquier otro valor (también `true`). |
| `loan_outstanding_usdt` | float finito `>= 0` | Préstamo pendiente spot → futuros del modo publicado, en centavos hacia abajo. |
| `updated_at` | texto ISO 8601 no vacío | Última escritura. El lector no lo usa para decidir (solo valida que exista). |
| `mode` | `"real"` \| `"dry_run"` | `dry_run` = préstamo simulado. |

**Semántica del lector:** `balance_para_dimensionar = max(0, wallet_balance - loan_outstanding_usdt)`. Si hay
préstamo y ese balance da 0, passivbot no calcula entradas (`sin_capital_propio`) y conserva los cierres.

| Situación | Lector |
|---|---|
| Sin `PASSIVBOT_LOAN_STATE` (o vacía) | No lee nada: comportamiento de siempre. |
| Archivo ausente | Préstamo 0. |
| Archivo inválido o ilegible (incluye `version != 1`) | Conserva el último valor válido (0 si nunca hubo uno) y loguea el error en cada lectura. |
| Claves de más | Se ignoran: no hace falta subir `version` para agregar un campo informativo. |

**Qué publica el escritor** (`loan_publicado`):

- el préstamo del modo del servicio;
- con el servicio en `dry_run` y un préstamo **real** pendiente (o una transferencia real en vuelo), el préstamo
  real con `"mode": "real"`: un préstamo real nunca se deja de publicar;
- un préstamo simulado nunca pasa a real: al arrancar en `real` o en `off` se reescribe con el ledger real;
- una inyección real se publica **antes** del `POST` de la transferencia; una devolución baja el préstamo recién
  confirmada (lado conservador: passivbot nunca dimensiona sobre la reserva).

### El disparo por uPnL no cambia este contrato

El disparo anticipado por uPnL ([rescate.md](rescate.md#disparo-anticipado-por-upnl)) es otra regla para decidir
**cuándo** inyectar. Lo que inyecta es préstamo como cualquier otro: suma a `loan_outstanding_usdt` y passivbot
sigue dimensionando sobre `wallet - préstamo`. No hay campo nuevo ni PR de passivbot.

## `watcher_heartbeat.json` (versión 1, vigente)

- **Escribe:** C3, dentro del mismo servicio (`c3.write_heartbeat`).
- **Ruta:** `FUTURESBOARD_C3_HEARTBEAT` (default `/rescate/watcher_heartbeat.json`).
- **Lee:** passivbot con `PASSIVBOT_WATCHER_HEARTBEAT=/passivbot/rescate/watcher_heartbeat.json` y
  `PASSIVBOT_WATCHER_MAX_AGE_SECONDS` (default 120, mínimo 45; en el deploy `>= 3 × intervalo + 30`). Lo lee una vez
  por ciclo de órdenes.

```json
{"version": 1, "ts": "2026-10-22T12:00:00+00:00", "c3_mode": "real", "c3_parado": false}
```

| Campo | Tipo | Significado |
|---|---|---|
| `version` | int `1` | Lo escribe el dashboard; el lector hoy lo ignora. |
| `ts` | ISO 8601 UTC | Último latido. Significa "C3 está protegiendo", no "el proceso vive" (ver [c3.md](c3.md#dead-man-watcher_heartbeatjson-contrato-con-passivbot)). El lector acepta también segundos epoch y `Z`. |
| `c3_mode` | `"off"` \| `"dry_run"` \| `"real"` | Modo **efectivo** de C3. |
| `c3_parado` | bool | Informativo; el lector no lo usa. |

**Semántica del lector (dead-man):**

| Situación | Lector |
|---|---|
| `c3_mode` `off` o `dry_run` | Sin efecto. |
| `c3_mode` `real` y `ts` con más de `MAX_AGE` (o más de `MAX_AGE` en el futuro) | No abre entradas nuevas, cancela las abiertas y conserva los cierres. Al volver a latir, reanuda. |
| Archivo ausente o inválido | Bloquea solo si el último archivo válido leído por ese proceso estaba en `real`. |
| Claves de más | Se ignoran. |

## Lo que NO se introdujo: `loan_state` v2 / `sizing_state`

El plan de mejoras de riesgo (botbulls-lab, `multianual/mejoras/preregistro.json`) preveía un `loan_state.json`
versión 2 (o un `sizing_state` aparte) con factor de dimensionamiento, tope de dimensionamiento y bloqueo de
entradas por exposición o por régimen. Lo iban a escribir el dashboard y leer passivbot, que ante un archivo
viejo o inválido cerraría entradas (fail-closed) y conservaría cierres.

**No se implementó**, porque las cuatro mejoras que lo usaban quedaron descartadas en el backtest:

| Mejora | Campo que necesitaba | Resultado |
|---|---|---|
| G: freno de cuenta por exposición (gate TWEL) | bloqueo de entradas | descartada en IS (lab `dbf2a1c`) |
| V: factor por volatilidad | `sizing_factor` | descartada en IS (lab `e6ef86f`) |
| R: filtro de régimen (EMA200 / ATR) | bloqueo de entradas por símbolo | R_atr3 pasó IS y no confirmó OOS (lab `06ce90c`) |
| S: "10k siempre" (colchón / barrido) | `sizing_cap_usdt` | descartada en IS (lab `3a54f89`) |

Si alguna vuelve a estar sobre la mesa:

- subir `version` a 2 **rompe** al lector actual de `loan_state.json`: lo rechaza y se queda con el último valor
  válido, en silencio salvo por el log;
- un campo que pueda **bloquear** entradas necesita lógica nueva en passivbot (con su fail-closed) y un PR
  coordinado. No alcanza con agregar la clave;
- hasta entonces, los dos archivos quedan en versión 1 tal como están acá.
