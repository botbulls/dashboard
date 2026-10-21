# Rescate como colchón

!!! warning "Herramienta, no asesor"
    La reserva sirve para aguantar margen cerca de la liquidación: **no garantiza evitar pérdidas**.
    El tope de pérdida total puede cerrar todo a mercado y realizar la pérdida. Configurar los valores
    es una decisión de quien opera la cuenta.

## Qué hace

Una parte del USDT queda en la wallet **spot** como reserva. passivbot dimensiona sobre el capital
propio, y la reserva solo se presta a futuros cuando la cuenta está cerca de la liquidación. Cuando
ya no hay posiciones abiertas, el préstamo se devuelve solo.

```
                 ┌──────────── dashboard (Flask) ────────────┐
 Browser ──────> │ /rescate  /api/rescate/*  (login + CSRF)   │── escribe rescate_config.json
                 │ /api/bot/start  (409 si hay tope sin ack)  │── lee rescate_state.json
                 └────────────────────────────────────────────┘
                                ./config (mismo directorio)
                 ┌──────────── rescate (python -m futuresboard.rescate) ─┐
 Binance <────── │ /fapi/v2/account, /api/v3/account, /sapi/v1/asset/... │── escribe rescate_state.json
                 │ tope => Apagar (bot_control.stop_bot, mismo lock)      │── escribe ./rescate/loan_state.json
                 └──────────────────────────┬─────────────────────────────┘
                                            │ docker-proxy (stop) + Binance (cierre)
                 passivbot <── lee (ro) ./rescate/loan_state.json  (PASSIVBOT_LOAN_STATE)
```

- Código: `src/futuresboard/rescate.py` (servicio, cliente Binance y validaciones) y las rutas en
  `src/futuresboard/blueprint.py`. UI: `templates/rescate.html` (menú lateral "Rescate (colchón)").
- El servicio corre en un contenedor aparte con la **misma imagen** del dashboard.

## Contrato con passivbot: `loan_state.json`

Lo escribe **solo** el servicio rescate, de forma atómica (tmp en el mismo directorio + `fsync` +
`os.replace`, permisos `0644`), en `/rescate/loan_state.json` (`./rescate` en el host). passivbot lo
monta read-only en `/passivbot/rescate/loan_state.json` y lo encuentra con `PASSIVBOT_LOAN_STATE`.

```json
{"version": 1, "loan_outstanding_usdt": 1000.0, "updated_at": "2026-10-21T15:04:05+00:00", "mode": "real"}
```

| Campo | Tipo | Significado |
|---|---|---|
| `version` | int | `1` |
| `loan_outstanding_usdt` | float `>= 0` | Préstamo pendiente (spot → futuros) del modo activo. |
| `updated_at` | ISO 8601 UTC | Última escritura. |
| `mode` | `"real"` \| `"dry_run"` | `dry_run` = préstamo **simulado** (solo sirve para probar el dimensionamiento). |

Semántica en passivbot: `balance_para_dimensionar = max(0, wallet_balance - loan_outstanding_usdt)`.
Sin `PASSIVBOT_LOAN_STATE`, comportamiento idéntico al de hoy. Archivo inexistente = préstamo 0.
Archivo inválido/ilegible: se conserva el último valor válido (y se loguea); si nunca hubo uno, 0. Se
relee en cada actualización de balance.

!!! note "Montar el directorio, no el archivo"
    `os.replace` crea un inode nuevo. Con el archivo montado suelto, passivbot seguiría leyendo el
    inode viejo para siempre. Es el mismo problema que con el HJSON de forager
    (ver [panel-passivbot.md](panel-passivbot.md)).

El archivo refleja el préstamo del modo activo, y el préstamo se lleva por separado por modo
(`real` / `dry_run`). En las dos direcciones:

- Un préstamo simulado **nunca** pasa a real: al arrancar en `real` el archivo se reescribe desde el
  ledger real, y la devolución real solo mira el ledger real.
- Un préstamo real **nunca** se deja de publicar: en `dry_run` con plata real prestada (o una
  transferencia real en vuelo) el archivo sigue con el préstamo real y `"mode": "real"`, no se simulan
  transferencias (bloqueo visible en el panel) y la CLI de simulación se niega. Hay que volver a `real`
  para devolverlo.

## Reglas

Un ciclo cada `FUTURESBOARD_RESCATE_INTERVAL` segundos (default 15). En cada ciclo:

1. Lee la cuenta de futuros (`GET /fapi/v2/account`: `totalMarginBalance`, `totalMaintMargin`,
   `maxWithdrawAmount`, posiciones con `positionAmt != 0`) y el USDT libre de **spot**
   (`GET /api/v3/account`, campo `free`). No se usa ccxt `binanceusdm.fetch_balance({'type': 'spot'})`,
   que en ccxt 4.1.72 igual consulta futuros. Una respuesta incompleta o inválida es error: nunca se toma
   como 0.
2. **Conciliación** de una transferencia en vuelo (ver abajo). Mientras haya una, no se hace otra.
3. **Base**: al habilitar (o volver a habilitar) en el panel se abre una generación nueva. El primer
   ciclo con lectura OK fija `base = capital_total` (`equity de futuros + USDT libre en spot`). Mientras no
   hay base el tope no está armado y **no se presta nada**; el panel lo muestra como "base pendiente".
4. **Tope de pérdida total**: si `capital_total <= base × (1 - tope%)`:
   - se registra el disparo y se persiste **antes** de actuar (desde ese momento el panel bloquea START);
   - se ejecuta **exactamente** la rutina de Apagar del panel (`bot_control.stop_bot(..., "apagar")`:
     detener passivbot, cancelar órdenes normales y condicionales, cerrar posiciones a mercado y
     verificar), tomando el **mismo lock de archivo** que las acciones del panel (espera hasta 180 s si
     hay una acción en curso). Queda en `bot_actions.log` con usuario `rescate (tope de pérdida)`;
   - aviso por Telegram (disparo + resultado del Apagar con el resumen del cierre);
   - si Apagar falla, se reintenta cada 2 min hasta completar 3 intentos; después, aviso de intervención
     manual;
   - START queda bloqueado (409) hasta **reconocerlo** en el panel. Reconocer deshabilita el rescate:
     volver a habilitarlo fija una base nueva.
5. **Inyección** (spot → futuros, `MAIN_UMFUTURE`) solo si el rescate está habilitado, hay base, no hay
   tope activo, hay posiciones y `totalMarginBalance / totalMaintMargin < umbral` (estricto: en el umbral
   exacto no inyecta). Monto = `min(paso, reserva_max - préstamo, USDT libre en spot)`, redondeado hacia
   abajo a centavos; menos de 1 USDT no se inyecta (aviso único de "reserva agotada" o "sin USDT en spot").
6. **Devolución** (futuros → spot, `UMFUTURE_MAIN`) cuando la cuenta de futuros no tiene posiciones y
   hay préstamo: `min(préstamo, maxWithdrawAmount)`. Si no alcanza, sigue en los ciclos siguientes. La
   devolución sigue aunque el rescate esté deshabilitado o con el tope disparado (es plata prestada).

### Préstamo y transferencias (sin idempotencia en Binance)

`POST /sapi/v1/asset/transfer` no es idempotente: un reintento a ciegas puede duplicar la
transferencia. Por eso:

- Antes del POST se persiste la transferencia "en vuelo" en `rescate_state.json`. Un reinicio en el
  medio no la pierde: al arrancar se concilia.
- **Rechazo claro** (4xx con código de Binance): no se ejecutó. Se registra, se avisa y ese tipo de
  transferencia espera 5 min.
- **Resultado ambiguo** (sin respuesta, timeout, 5xx, 200 sin `tranId`, códigos `-1006`/`-1007`): **no se
  reintenta**. Queda "incierta" y se concilia con `GET /sapi/v1/asset/transfer` (tipo, monto, timestamp,
  `tranId` no conocido). Si después de 2 min y 2 chequeos no aparece en el historial, decide la variación
  del USDT libre en spot: bajó/subió exactamente el monto → ejecutada; no cambió → no ejecutada; cambió
  otra cosa → aviso de conciliación manual (y no se hacen transferencias nuevas).
- Con `tranId`, el historial confirma el estado: `CONFIRMED` cierra, `FAILED` revierte, `PENDING` espera.
- **Inyección**: el préstamo se suma al recibir el `tranId` (o ante un resultado ambiguo, por las dudas)
  y se reescribe `loan_state.json`; si la conciliación prueba que no se ejecutó, se revierte.
- **Devolución**: el préstamo baja **solo** con la transferencia confirmada (`CONFIRMED` en el historial
  o variación del saldo spot). Hasta entonces passivbot sigue viendo el préstamo completo (lado conservador).

El ledger (últimas 200 transferencias) y los eventos (últimos 100) quedan en `rescate_state.json`.

## Modos (`FUTURESBOARD_RESCATE_MODE`)

| Modo | Qué hace |
|---|---|
| `off` (default) | No opera. Solo, al arrancar, si `loan_state.json` quedó con un préstamo **simulado**, lo reescribe con el préstamo real (para no achicar a passivbot para siempre). |
| `dry_run` | Lee la cuenta y calcula todo; **no llama a `/sapi`**. Registra lo que HARÍA, actualiza el préstamo **simulado** y escribe `loan_state.json` con `"mode": "dry_run"` (sirve en dev para probar el dimensionamiento de passivbot). Para decidir, el margen y lo retirable se calculan como si lo simulado se hubiera movido. El tope **no ejecuta Apagar**: registra "tope disparado (simulado)", avisa y bloquea START igual (para probar el reconocimiento). |
| `real` | Transfiere de verdad. Al arrancar verifica `GET /sapi/v1/account/apiRestrictions` y se niega a transferir si no se cumple `ipRestrict=true`, `permitsUniversalTransfer=true`, `enableWithdrawals=false` y `enableInternalTransfer=false` (se verifica una vez por arranque: corregir la key y reiniciar). Bloqueado en testnet/Demo (Demo no tiene `/sapi`) y sin `FUTURESBOARD_DOCKER_URL` (el tope no podría apagar). |

Un modo bloqueado (permisos, testnet) frena las **transferencias**; el tope de pérdida sigue vigilando
con el rescate habilitado.

## Panel

Sección **Rescate (colchón)** del menú lateral (`/rescate`, con login). Muestra modo y latido del
servicio, ratio margen/mantenimiento vs umbral, posiciones, préstamo pendiente, USDT libre en spot,
capital total vs base y límite del tope, la transferencia en vuelo, el motivo de bloqueo y los últimos
eventos. Si hay un tope disparado, lo muestra con el resultado del Apagar y el botón para reconocerlo.

| Método | Ruta | Body | Efecto |
|---|---|---|---|
| GET | `/api/rescate/status` | – | Estado para la UI (lee los archivos, no consulta Binance). |
| POST | `/api/rescate/config` | `{"habilitado", "reserva_max_usdt", "paso_usdt", "umbral_ratio", "tope_perdida_pct"}` | Guarda la config. Habilitar (false → true) abre una generación nueva = base nueva. |
| POST | `/api/rescate/reconocer-tope` | `{"id": "<id del tope>"}` | Desbloquea START y deshabilita el rescate. |

Todos los POST exigen sesión, `X-CSRF-Token` y JSON (igual que el panel de passivbot) y quedan en
`bot_actions.log` (`rescate_config` / `rescate_reconocer_tope`).

Validación estricta (400 si no se cumple): todos los campos presentes y ninguno desconocido;
`habilitado` booleano; números JSON finitos (no `NaN`/`Infinity`, no texto, no booleanos).

| Campo | Rango | Default |
|---|---|---|
| `reserva_max_usdt` | 10 – 1.000.000 | (obligatorio para habilitar) |
| `paso_usdt` | 10 – 1.000.000 y ≤ reserva | 1000 |
| `umbral_ratio` | 1,2 – 10 (Binance liquida en 1) | 3 |
| `tope_perdida_pct` | 1 – 80 | (obligatorio para habilitar) |

409: habilitar con un tope disparado sin reconocer, o reconocer un id que no es el activo.
`POST /api/bot/start` responde 409 mientras haya un tope sin reconocer (se chequea al validar y otra vez
dentro del job, con el lock tomado); `/api/bot/status` trae `rescate_start_bloqueado` y el botón START
del panel queda deshabilitado con el motivo. Graceful stop y Apagar no se bloquean.

## Variables de entorno (servicio rescate)

Todas fail-closed: un valor inválido nunca habilita más de lo pedido.

| Variable | Default | Uso |
|---|---|---|
| `FUTURESBOARD_RESCATE_MODE` | `off` | `off` \| `dry_run` \| `real`. Cualquier otro valor = `off` + error en el log. |
| `FUTURESBOARD_RESCATE_INTERVAL` | `15` | Segundos entre ciclos (5–300; inválido = 15). |
| `FUTURESBOARD_RESCATE_LOAN_STATE` | `/rescate/loan_state.json` | Dónde se escribe el contrato para passivbot. |
| `FUTURESBOARD_RESCATE_SPOT_URL` | `https://api.binance.com` (`https://demo-api.binance.com` con testnet) | Base de spot y `/sapi`. |
| `FUTURESBOARD_DOCKER_URL` / `FUTURESBOARD_PASSIVBOT_CONTAINER` | (vacía) / `client17-passivbot` | Necesarias para que el tope ejecute Apagar. Sin `FUTURESBOARD_DOCKER_URL`, `real` no transfiere. |
| `FUTURESBOARD_BINANCE_TESTNET` | (vacía) | Igual que en el dashboard (o `BINANCE_TESTNET` en `config.json`): futuros en `demo-fapi`, spot en `demo-api`, `real` bloqueado. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `TELEGRAM_PREFIX` | (vacías) | Avisos (ver [notificaciones.md](notificaciones.md)). |
| `FUTURESBOARD_CONFIG_DIR` | `./config` | Directorio de `config.json` (API key/secret, `EXCHANGE`, testnet). El estado y el lock van junto a la DB. |

Telegram: un aviso por inyección, devolución, transferencia rechazada o incierta, disparo del tope y
resultado del Apagar, y por cambios de estado (lectura caída / recuperada, bloqueo, reserva agotada),
sin repetir el mismo aviso cada ciclo. En `dry_run` todos llevan `[SIMULACIÓN]`.

## docker-compose

El servicio rescate usa la misma imagen del dashboard. Necesita **el mismo `./config`** que el
dashboard (credenciales, el lock de acciones `.bot_control.lock` y los archivos del rescate) y el
**mismo docker-proxy** que el panel (Apagar hace `stop`), no el proxy de solo lectura del notifier.

```yaml
services:
  dashboard:
    # ... lo de panel-passivbot.md (./config:/usr/src/futuresboard/config, red docker-api)

  rescate:
    build: .                      # misma imagen que el dashboard
    command: ["python", "-m", "futuresboard.rescate"]
    healthcheck:
      disable: true               # no sirve HTTP
    restart: unless-stopped
    environment:
      FUTURESBOARD_RESCATE_MODE: dry_run            # off | dry_run | real (real: bloqueado en Demo)
      FUTURESBOARD_RESCATE_INTERVAL: "15"
      FUTURESBOARD_RESCATE_LOAN_STATE: /rescate/loan_state.json
      FUTURESBOARD_DOCKER_URL: http://docker-proxy:2375
      FUTURESBOARD_PASSIVBOT_CONTAINER: client17-passivbot
      # FUTURESBOARD_BINANCE_TESTNET: "1"           # igual que el dashboard en dev
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
      TELEGRAM_PREFIX: "[client17]"
    volumes:
      - ./config:/usr/src/futuresboard/config       # el MISMO directorio que el dashboard
      - ./rescate:/rescate                          # rw: acá se escribe loan_state.json
    networks: [default, docker-api]
    depends_on: [docker-proxy]

  passivbot:
    # ... lo existente, más:
    environment:
      PASSIVBOT_LOAN_STATE: /passivbot/rescate/loan_state.json
    volumes:
      - ./rescate:/passivbot/rescate:ro             # el DIRECTORIO, read-only
```

## Permisos de la API key (modo real)

- **Enable Reading** y **Enable Futures** (Apagar opera futuros, igual que el panel).
- **Permits Universal Transfer** (spot ⇄ USDⓈ-M).
- **Restringida por IP** (`ipRestrict=true`): sin IP fija el servicio no transfiere.
- **Sin** retiros (`enableWithdrawals=false`) y **sin** transferencia interna (`enableInternalTransfer=false`).

Spot/margin trading no hace falta. Si algo no cumple, el servicio queda bloqueado, lo muestra en el
panel y avisa por Telegram.

## CLI (solo `dry_run`)

```bash
docker compose exec rescate python -m futuresboard.rescate simular-inyeccion 500   # préstamo simulado +500
docker compose exec rescate python -m futuresboard.rescate simular-devolucion 200  # -200
docker compose exec rescate python -m futuresboard.rescate simular-devolucion      # todo
python -m futuresboard.rescate --once                                              # un ciclo y salir
```

Con otro modo responden con error (código 2) sin tocar nada. Sirven para probar en dev que passivbot
achica el dimensionamiento leyendo `loan_state.json`.

## Limitaciones conocidas

- `capital_total` no cambia con las transferencias propias (spot ⇄ futuros), pero **sí** con depósitos,
  retiros, transferencias a otras wallets o trading en spot: eso puede disparar el tope (o alejarlo).
- La base se fija en el primer ciclo después de habilitar (hasta `FUTURESBOARD_RESCATE_INTERVAL` s).
- Una transferencia que no se puede conciliar sola (sin rastro y con el saldo spot movido por otra cosa)
  frena las transferencias nuevas hasta resolverla a mano en `rescate_state.json`.
- En Binance Demo no hay `/sapi`: en dev solo se puede probar `dry_run`.
- Un solo servicio por cuenta: una segunda instancia queda esperando el lock (`.rescate_service.lock`).
