# Rescate como colchón

!!! warning "Herramienta, no asesor"
    La reserva sirve para aguantar margen cerca de la liquidación: **no garantiza evitar pérdidas**.
    El tope de pérdida total puede cerrar todo a mercado y realizar la pérdida. Configurar los valores
    es una decisión de quien opera la cuenta.

## Qué hace

Una parte del USDT queda en la wallet **spot** como reserva. passivbot dimensiona sobre el capital
propio, y la reserva solo se presta a futuros cuando la cuenta está cerca de la liquidación. Cuando
ya no hay posiciones abiertas, el préstamo se devuelve solo; con posiciones abiertas, se devuelve en partes
cuando el margen está sano.

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
   - Si falla **futuros**: no se hace nada más ese ciclo y el aviso dice que el tope no se puede evaluar.
   - Si falla **solo spot**: no hay transferencias ni conciliación, pero el tope se sigue evaluando con
     `equity de futuros + último spot leído OK` y el aviso lo dice. El spot estimado supone ejecutadas las
     transferencias reales posteriores a esa lectura: resta lo que creció el préstamo real (una inyección
     cuenta en el préstamo antes del POST) y suma una devolución real en vuelo enviada después de la lectura
     (el préstamo baja recién confirmada, pero la plata ya salió de futuros; sin sumarla, una devolución
     parcial pendiente durante una caída de spot parecería una pérdida y podría disparar el tope). No se usa solo la equity de futuros: con la reserva en spot, una caída de la API de
     spot cerraría todo a mercado. Sin ningún spot leído antes, el aviso dice que el tope quedó desarmado.
2. **Conciliación** de una transferencia en vuelo (ver abajo). Mientras haya una, no se hace otra.
3. **Base**: al habilitar (o volver a habilitar) en el panel se abre una generación nueva. El primer
   ciclo con lectura OK fija `base = capital_total` (`equity de futuros + USDT libre en spot`). Mientras no
   hay base el tope no está armado y **no se presta nada**; el panel lo muestra como "base pendiente".
   La base guarda también el % de tope vigente. Volver a habilitar **con un préstamo real pendiente** no
   fija una base nueva sobre el capital ya perdido: se mantiene la anterior.
4. **Tope de pérdida total**: el piso es `base × (1 - tope%)`. Se evalúa con el rescate habilitado y
   también, mientras haya un **préstamo real pendiente**, con el rescate deshabilitado o con
   `rescate_config.json` inválido (usa la última base y su %: la reserva prestada sigue en futuros). Una
   base cuyo tope ya se disparó no se vuelve a armar así, para que reconocerlo no lo re-dispare.

   **Confirmación.** Al primer cruce (`capital_total <= piso`) **no** se apaga: el tope queda *en
   confirmación*. Se persiste en `rescate_state.json` (hora y capital del cruce), se avisa por Telegram y,
   mientras dure, **no se inyecta**. Cada lectura posterior se compara con el piso vigente en ese momento:
   - si el capital vuelve arriba del piso, la confirmación se cancela (aviso) y no se apaga;
   - si una lectura separada al menos `FUTURESBOARD_RESCATE_TOPE_CONFIRM_SECONDS` (default 60 s) del primer
     cruce sigue en o debajo del piso, se dispara el tope (abajo). Con los defaults dispara entre 60 y 75 s
     después del primer cruce. Con `0` se vuelve al disparo inmediato.

   El motivo: en la simulación de 3.330 corridas, con el disparo inmediato se vendía el fondo de mechas de
   1 minuto que rebotaban enseguida (por ejemplo 2026-01-31 18:43).
   - La confirmación sobrevive reinicios: el proceso nuevo la retoma de `rescate_state.json`. Una
     confirmación guardada ilegible se descarta con aviso y la próxima lectura debajo del piso abre otra.
   - **Lecturas caídas durante la confirmación** (misma lógica del punto 1): con spot caído se observa con
     el último saldo spot leído, y esa observación puede confirmar o cancelar. Con futuros caídos (o spot
     caído sin un saldo spot leído antes) no hay observación: la confirmación no se confirma ni se cancela.
   - **Con futuros caídos no se dispara a ciegas.** Apagar detendría passivbot pero no podría cancelar ni
     cerrar en Binance: gastaría los 3 intentos durante la caída y quedarían el bot parado, las posiciones
     abiertas y START bloqueado. Además se perdería el filtro de mechas, que suelen coincidir con las caídas de
     la API. La confirmación espera y decide la próxima lectura, como antes de que existiera la confirmación.
   - **Espera máxima**, solo con spot caído sin un saldo spot leído antes (futuros responde y Apagar puede
     cerrar): si pasan `max(5 × confirmación, 300 s)` desde el primer cruce (5 min con el default) sin una
     observación que la resuelva, se dispara con la última observación, que estaba en o debajo del piso. El
     aviso de lectura caída dice hasta cuándo.
   - **Arranca de cero** (la observación debajo del piso cuenta como un primer cruce nuevo, con su aviso):
     - si pasaron más de `confirmación + 2 × FUTURESBOARD_RESCATE_INTERVAL` (90 s con los defaults) desde la
       observación anterior. Con el servicio o futuros caídos no se sabe si el capital se quedó debajo o rebotó
       y volvió a caer: dos mechas no son "sigue debajo";
     - si cambió el modo del tope (por ejemplo de `dry_run` a `real`): un Apagar real necesita antes su propio
       aviso real de confirmación.

     Con `FUTURESBOARD_RESCATE_TOPE_CONFIRM_SECONDS=0` dispara en el momento, como cualquier primer cruce.
   - En modo `off` no se evalúa: una confirmación en curso se cancela con aviso. Si el servicio no late, el
     panel aclara que nadie la está evaluando.
   - Si el host atrasa el reloj durante una confirmación, la cuenta se reinicia desde ese momento.
   - Si el capital oscila alrededor del piso (cruza, vuelve arriba antes de confirmarse, cruza de nuevo),
     cada rebote cancela y el cruce siguiente abre una confirmación nueva: dispara recién cuando el capital
     se queda debajo una confirmación completa. Para no llenar Telegram, una confirmación que se abre a menos
     de 5 min de la última cancelación no avisa ni al abrirse ni al cancelarse (queda en los eventos y en el
     panel). El disparo y el resultado del Apagar se avisan siempre.
   - Si el tope se desarma durante la confirmación (rescate deshabilitado sin préstamo real pendiente), la
     confirmación se cancela con aviso.

   **Disparo** (confirmado, o inmediato con `FUTURESBOARD_RESCATE_TOPE_CONFIRM_SECONDS=0`):
   - se registra el disparo y se persiste **antes** de actuar (desde ese momento el panel bloquea START);
   - se ejecuta **exactamente** la rutina de Apagar del panel (`bot_control.stop_bot(..., "apagar")`:
     detener passivbot, cancelar órdenes normales y condicionales, cerrar posiciones a mercado y
     verificar), tomando el **mismo lock de archivo** que las acciones del panel (espera hasta 180 s si
     hay una acción en curso). Queda en `bot_actions.log` con usuario `rescate (tope de pérdida)`;
   - aviso por Telegram (disparo + resultado del Apagar con el resumen del cierre);
   - cada intento se persiste **antes** de ejecutarlo. Si el proceso muere en medio (deploy, OOM, reinicio
     del host), el ciclo siguiente lo cuenta como intento cortado, avisa por Telegram y reintenta en el
     momento;
   - si Apagar falla, se reintenta cada 2 min hasta completar 3 intentos; después, aviso de intervención
     manual. El reintento no depende de poder leer la cuenta;
   - el tope es **real** si el servicio está en `real` o si hay un préstamo real pendiente (aunque el
     servicio esté en `dry_run`), y ahí siempre ejecuta el Apagar real. Que sea simulado lo decide el modo
     del tope, nunca el del proceso que lo reintenta;
   - START queda bloqueado (409) hasta **reconocerlo** en el panel. Reconocer deshabilita el rescate:
     volver a habilitarlo fija una base nueva.
5. **Inyección** (spot → futuros, `MAIN_UMFUTURE`) solo si el rescate está habilitado, hay base, no hay
   tope activo ni en confirmación, hay posiciones y `totalMarginBalance / totalMaintMargin < umbral` (estricto: en el umbral
   exacto no inyecta). Monto = `min(paso, reserva_max - préstamo, USDT libre en spot)`, redondeado hacia
   abajo a centavos; menos de 1 USDT no se inyecta (aviso único de "reserva agotada" o "sin USDT en spot").
   Si esta condición no se cumple, se mira el [disparo anticipado por uPnL](#disparo-anticipado-por-upnl)
   (opcional, apagado por default).
6. **Devolución** (futuros → spot, `UMFUTURE_MAIN`) cuando la cuenta de futuros no tiene posiciones y
   hay préstamo: `min(préstamo, maxWithdrawAmount)`. Si no alcanza, sigue en los ciclos siguientes. La
   devolución sigue aunque el rescate esté deshabilitado o con el tope disparado (es plata prestada).
7. **Devolución parcial con margen sano** (futuros → spot, `UMFUTURE_MAIN`) con préstamo y posiciones
   abiertas, si `totalMarginBalance / totalMaintMargin >= umbral_devolucion_ratio` (default 32; tiene que ser
   mayor que `umbral_ratio`). Monto
   `r = min(préstamo, maxWithdrawAmount, totalMarginBalance - umbral_devolucion × totalMaintMargin)`, en
   centavos hacia abajo: después de devolver, el ratio queda en el umbral de devolución o arriba. Solo si
   `r >= FUTURESBOARD_RESCATE_DEVOLUCION_MIN_USDT` (100) y pasaron
   `FUTURESBOARD_RESCATE_DEVOLUCION_COOLDOWN_SECONDS` (600 s) desde el último intento de devolución parcial
   **o desde la última inyección**. Sin el cooldown después de inyectar, un paso grande frente al
   mantenimiento (por ejemplo 1.000 USDT con un mantenimiento de 50) deja el ratio muy arriba del umbral de
   devolución y el ciclo siguiente devolvía casi 400 USDT recién inyectados, justo antes de una posible segunda
   caída. El motivo de la devolución parcial: en caídas largas la cuenta casi nunca queda sin posiciones (en la
   simulación, en febrero quedaron 8.000 USDT prestados).
   - **Histéresis**: se inyecta debajo de 8 y se devuelve desde 32. Como lo devuelto deja el ratio en 32 o
     más, no vuelve a entrar enseguida (sin ping-pong); entre los dos umbrales no se mueve nada. La validación
     exige solo que el de devolución sea mayor: con umbrales pegados (por ejemplo 8 y 8,01) hay ping-pong cada
     10 min.
   - **Por qué 32** (simulación de 1.350 corridas: 30 caminos × TWE 4/8/12 × tope 50 %/70 %/sin tope × umbral
     de devolución 12/16/24/32/sin devolución; inyección debajo de 8, paso 1.000, reserva 10.000, apalancamiento
     10):
     - Ningún valor mejora la supervivencia. 32 es el único que no la empeora: con 12, 16 y 24, con TWE 8 y
       tope 50 %, un camino que sobrevivía terminó en stop.
     - Con 12 y 16 lo que limita la devolución es `maxWithdrawAmount` en el 82–98 % de las devoluciones: con
       apalancamiento 10 el margen inicial es ~17–25 veces el de mantenimiento. Se devuelve todo lo retirable,
       el margen libre queda en ~0 y las recompras de passivbot se rechazan por margen. Por eso 12 y 16 dan
       resultados idénticos. Con otro apalancamiento ese rango se corre.
     - Costo: aun con 32 el capital final baja (mediana pareada −253 y −299 USDT por camino con TWE 8, tope
       70 % y sin tope; ningún camino mejora). Sin devolución parcial el préstamo sigue sirviendo de margen
       para las recompras. La simulación no tenía el cooldown después de inyectar, que solo hace las
       devoluciones más raras.
   - No cambia el dimensionamiento de passivbot: dimensiona sobre `wallet - préstamo` y devolver `r` baja
     las dos cosas. El préstamo publicado baja **solo** con la transferencia confirmada, igual que en la
     devolución total.
   - Un préstamo residual menor al mínimo espera a quedar sin posiciones (la devolución total no tiene
     mínimo).
   - Sigue con el rescate deshabilitado (es plata prestada), pero **no** con el tope en confirmación o
     disparado: ahí no se mueve plata entre wallets.

## Disparo anticipado por uPnL

!!! warning "Apagado por default. Herramienta, no asesor"
    Se validó en backtest solo con TWE 2, 3 y 4 sobre 10k (más 10k de reserva). Prod hoy corre con `twe_long` 12, y
    con eso **no está validado**. En el backtest no salvó ninguna ventana más que el disparo de margen, y el capital
    final dio igual con cualquier X y paso. Solo cambia cuánta reserva ya está en futuros cuando llega la mecha.

Es un segundo disparo de inyección, además del de margen (`equity / mantenimiento < umbral`). El de margen tiene
prioridad: en un ciclo se mira el de uPnL solo si el de margen no se cumple, y nunca hay dos transferencias a la vez.

**Regla.** Con el rescate habilitado, la base fijada, sin tope disparado ni en confirmación, con posiciones abiertas
y sin transferencia en vuelo:

```
capital_propio = totalWalletBalance - préstamo          (lo mismo con lo que dimensiona passivbot)
inyecta upnl_paso_usdt si  capital_propio > 0  y  totalUnrealizedProfit + préstamo <= -X% × capital_propio
```

- **Escalera.** Lo ya prestado (por cualquiera de los dos disparos) cuenta como cubierto. La primera inyección sale
  con `uPnL <= -X% × capital_propio`; la `n+1`, con `uPnL <= -X% × capital_propio - n × paso`. Con la condición
  literal, sin descontar lo prestado, se seguiría cumpliendo después de inyectar y vaciaría la reserva en minutos.
- **Monto:** `min(upnl_paso_usdt, reserva_max - préstamo, USDT libre en spot)`, en centavos hacia abajo; menos de
  1 USDT no se inyecta (aviso único).
- **Espera:** `FUTURESBOARD_RESCATE_UPNL_COOLDOWN_SECONDS` (60 s) entre inyecciones de este disparo. Un rechazo de
  Binance frena las dos reglas 5 min, como siempre.
- La transferencia es una inyección más: el mismo ledger (con `"disparo": "upnl"`), la misma conciliación, el
  préstamo publicado antes del `POST` y el mismo `loan_state.json`
  ([sin cambios de contrato](contratos.md#el-disparo-por-upnl-no-cambia-este-contrato)).

**Guarda de la devolución parcial.** Con el disparo transfiriendo (estados `simulado` o `real`), la devolución
parcial no devuelve lo que la regla volvería a pedir:

```
r <= uPnL + préstamo + X% × capital_propio - upnl_paso_usdt
```

Sin la guarda hay ping-pong: a -50 % de uPnL el ratio suele quedar arriba de 32, lo inyectado vuelve a spot a los
10 min y se re-inyecta al minuto siguiente. En el backtest, con la devolución parcial de siempre y sin guarda, una
ventana llegó a 10.322 inyecciones (6.238 idas y vueltas); con la guarda, como mucho 44. Con X 70 % casi no aparece
(como mucho 1 ida y vuelta por ventana), pero la guarda queda igual por si se baja X. El cooldown de 10 min después
de cada inyección sigue igual.

**Lectura.** Usa `totalWalletBalance` y `totalUnrealizedProfit` de `GET /fapi/v2/account`, que el resto del rescate
no necesita. Si faltan o son inválidos, el disparo por uPnL queda en pausa con un aviso. La devolución parcial
también se pausa, porque depende de la guarda. El disparo de margen y el tope siguen igual.

### Modos (`FUTURESBOARD_RESCATE_UPNL_MODE`)

Tiene su propio modo, aparte de `FUTURESBOARD_RESCATE_MODE`, para probarlo en `dry_run` con el rescate ya en `real`.
Además hay que habilitarlo en el panel (`upnl_habilitado`).

| Servicio | `UPNL_MODE` | Estado | Qué hace |
|---|---|---|---|
| cualquiera | `off` (default) | off | No se evalúa. |
| `off` | cualquiera | off | El rescate no opera. |
| `real` | `dry_run` | registra | No transfiere ni aplica la guarda: nada cambia en la cuenta real. Registra y avisa por Telegram (`[SIMULACIÓN]`) una vez por escalón lo que HARÍA: "escalón n" = cuántos pasos habría inyectado ya el disparo real. |
| `dry_run` | `dry_run` o `real` | simulado | Inyecciones simuladas sobre el préstamo simulado, con escalera y guarda, como el resto del `dry_run`. |
| `real` | `real` | real | Transfiere de verdad. |

Orden sugerido: `registra` unos días con la cuenta real y, si los avisos tienen sentido, `real`.

### Diferencias con el backtest

| Backtest (`botbulls-lab/multianual/rescate_upnl`) | Servicio |
|---|---|
| Decide al inicio de cada minuto con el close (last price) del minuto anterior | Cada 15 s con el `totalUnrealizedProfit` de Binance, que se calcula con el mark price |
| Monto `min(paso, reserva, tope de préstamo - prestado)` | Monto `min(paso, reserva - préstamo, USDT libre en spot)` |
| Devolución parcial: 10 min desde la última inyección | Igual (`FUTURESBOARD_RESCATE_DEVOLUCION_COOLDOWN_SECONDS`, contado desde la confirmación) |

### Por qué estos defaults (X 70 %, paso 1.000)

Backtest multianual 2021-2026: passivbot v6.1 clock en wallet compartida BNB/BTC/ETH/XRP, apalancamiento 10,
10k en futuros + 10k de reserva. 36 ventanas de 12 meses más el camino continuo, en tres escenarios: base, demora
de 5 min en las transferencias y mechas de mark 1,5 % más profundas (`mark985`). Grilla: TWE 3 y 4 × X 30/50/70 ×
paso 1.000/2.500 × sin tope / tope 60 %. Con la regla fijada antes de correr, sobre IS (hasta 2023-11-10), salió
**X 70 %, paso 1.000**: mayor mediana del capital final y, como desempate, menos reserva usada. En OOS se confirmó:
0 liquidaciones en los tres escenarios y una mediana pareada contra el disparo de margen solo de 0.

- No salva ninguna ventana más que el disparo de margen. En `mark985`, ene-feb 2026, TWE 4 sin rescate se
  liquidó en 3 de 36 ventanas. Las salvaron por igual el de margen solo y todas las variantes con uPnL. En el
  camino continuo `mark985` se liquidan todas igual el 2026-02-06: 10k de reserva es poco frente a la wallet ya
  compuesta.
- El capital final es el mismo con cualquier X y paso, porque passivbot dimensiona sin el préstamo. X y paso solo
  deciden cuánta reserva ya está en futuros cuando llega la mecha, cuánto tiempo queda prestada y cuántas
  transferencias hay.
- Colchón de margen, como equity / mantenimiento mínimo en la peor ventana de `mark985` con TWE 4:

  | Disparo | eq/MM mínimo |
  |---|---|
  | Margen solo | 2,2 |
  | uPnL X 70 % / 1.000 | 3,5 |
  | uPnL X 50 % / 1.000 | 14,2 |
  | uPnL X 30 % / 1.000 | 32,9 |

  Un X más bajo presta antes y más seguido: más colchón, más tiempo con la reserva en futuros y más transferencias.
- Con TWE 2 no inyectó nunca: 0 inyecciones en las 36 ventanas.

Las mejoras de riesgo que se evaluaron después ([contratos.md](contratos.md#lo-que-no-se-introdujo-loan_state-v2-sizing_state))
se midieron todas con este disparo (X 70 %, paso 1.000) como rescate de referencia.

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
- **Inyección**: el préstamo se suma y se reescribe `loan_state.json` **antes** del POST (passivbot puede
  ver la plata en la wallet por websocket antes de que llegue la respuesta). Si Binance la rechaza, o la
  conciliación prueba que no se ejecutó o quedó `FAILED`, se revierte. Al arrancar, una inyección real en
  vuelo siempre cuenta en el préstamo publicado.
- **Devolución**: el préstamo baja **solo** con la transferencia confirmada (`CONFIRMED` en el historial
  o variación del saldo spot). Hasta entonces passivbot sigue viendo el préstamo completo (lado conservador).

El ledger (últimas 200 transferencias) y los eventos (últimos 100) quedan en `rescate_state.json`.

## Modos (`FUTURESBOARD_RESCATE_MODE`)

| Modo | Qué hace |
|---|---|
| `off` (default) | No opera. Solo, al arrancar, si `loan_state.json` quedó con un préstamo **simulado**, lo reescribe con el préstamo real (para no achicar a passivbot para siempre). |
| `dry_run` | Lee la cuenta y calcula todo; **no llama a `/sapi`**. Registra lo que HARÍA, actualiza el préstamo **simulado** y escribe `loan_state.json` con `"mode": "dry_run"` (sirve en dev para probar el dimensionamiento de passivbot). Para decidir, el margen y lo retirable se calculan como si lo simulado se hubiera movido. El tope **no ejecuta Apagar**: registra "tope disparado (simulado)", avisa y bloquea START igual (para probar el reconocimiento). Excepción: con un préstamo real pendiente el tope es real y ejecuta el Apagar real (si no está disponible, queda en error con aviso, nunca "simulado"). |
| `real` | Transfiere de verdad. Al arrancar verifica `GET /sapi/v1/account/apiRestrictions` y se niega a transferir si no se cumple `ipRestrict=true`, `permitsUniversalTransfer=true`, `enableWithdrawals=false` y `enableInternalTransfer=false` (se verifica una vez por arranque: corregir la key y reiniciar). Bloqueado en testnet/Demo (Demo no tiene `/sapi`) y sin `FUTURESBOARD_DOCKER_URL` (el tope no podría apagar). |

Un modo bloqueado (permisos, testnet) frena las **transferencias**; el tope de pérdida sigue vigilando
con el rescate habilitado (o con un préstamo real pendiente).

## Panel

El dashboard principal (`/`) muestra una fila **Rescate** con la billetera de rescate, el préstamo, el capital
total vs la base, el ratio con semáforo y el estado (ver [c3.md](c3.md#dashboard-principal)). El stop con
reinicio sobre futuros (C3) corre en este mismo servicio: ver [c3.md](c3.md).

Sección **Rescate (colchón)** del menú lateral (`/rescate`, con login). Muestra modo y latido del
servicio, ratio margen/mantenimiento vs los umbrales de inyección y de devolución, posiciones, préstamo
pendiente, USDT libre en spot, capital total vs base y límite del tope, la transferencia en vuelo, el motivo de bloqueo y los últimos
eventos. La fila **Disparo por uPnL** muestra su estado (off / registra / simulado / real), el uPnL, el uPnL en el
que sale el próximo paso, hasta cuánto deja devolver la guarda y la última inyección por uPnL; en la configuración
están el interruptor, X y el paso. En el dashboard principal aparece la tarjeta **Disparo por uPnL** solo si está
habilitado en el panel. Si hay un tope en confirmación, lo muestra con el capital del cruce, la última lectura y desde
cuándo se puede ejecutar (y aclara si el servicio no late y nadie la está evaluando). Si hay un tope
disparado, lo muestra con el resultado del Apagar y el
botón para reconocerlo.

| Método | Ruta | Body | Efecto |
|---|---|---|---|
| GET | `/api/rescate/status` | – | Estado para la UI (lee los archivos, no consulta Binance). |
| POST | `/api/rescate/config` | `{"habilitado", "reserva_max_usdt", "paso_usdt", "umbral_ratio", "umbral_devolucion_ratio", "tope_perdida_pct"}` y, opcionales y juntos, `"upnl_habilitado", "upnl_x_pct", "upnl_paso_usdt"` | Guarda la config. Habilitar (false → true) abre una generación nueva = base nueva. Sin los campos `upnl_*` se conservan los guardados. |
| POST | `/api/rescate/reconocer-tope` | `{"id": "<id del tope>"}` | Desbloquea START y deshabilita el rescate. |

Todos los POST exigen sesión, `X-CSRF-Token` y JSON (igual que el panel de passivbot) y quedan en
`bot_actions.log` (`rescate_config` / `rescate_reconocer_tope`).

Validación estricta (400 si no se cumple): todos los campos presentes y ninguno desconocido;
`habilitado` booleano; números JSON finitos (no `NaN`/`Infinity`, no texto, no booleanos).

| Campo | Rango | Default |
|---|---|---|
| `reserva_max_usdt` | 10 – 1.000.000 | (obligatorio para habilitar) |
| `paso_usdt` | 10 – 1.000.000 y ≤ reserva | 1000 |
| `umbral_ratio` | 1,2 – 10 (Binance liquida en 1) | 8 |
| `umbral_devolucion_ratio` | 2 – 100 y **mayor** que `umbral_ratio` | 32 |
| `tope_perdida_pct` | 1 – 80 | (obligatorio para habilitar) |
| `upnl_habilitado` | booleano | `false` |
| `upnl_x_pct` | 10 – 95 (% del capital propio de futuros) | 70 |
| `upnl_paso_usdt` | 10 – 1.000.000 y ≤ reserva (también con los guardados) | 1000 |

Los tres campos `upnl_*` van juntos o no van: si falta alguno, 400. Sin ninguno se conservan los guardados, así una
pestaña abierta antes del deploy no puede prender ni apagar el disparo por uPnL. Una config guardada antes de que
existieran lo toma apagado con los defaults de la tabla. Un valor `upnl_*` inválido en el archivo invalida la config
entera: rescate deshabilitado con el error en el panel, como con cualquier otro campo.

Los defaults solo se usan mientras no hay nada guardado: una config ya guardada conserva sus valores
(por ejemplo, un umbral 3 guardado antes de que el default pasara a 8 sigue en 3 hasta cambiarlo en el panel).
Una config guardada antes de `umbral_devolucion_ratio` toma 32 (siempre mayor que el umbral máximo de
inyección). Una config guardada con `umbral_devolucion_ratio <= umbral_ratio` es inválida: rescate
deshabilitado con el error en el panel. Una pestaña del panel abierta antes del deploy manda el body sin el
campo nuevo y recibe 400: recargar la página.

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
| `FUTURESBOARD_RESCATE_DEVOLUCION_MIN_USDT` | `100` | Mínimo por devolución parcial (1–1.000.000). Vacío, no entero o menor que 1 = 100. |
| `FUTURESBOARD_RESCATE_DEVOLUCION_COOLDOWN_SECONDS` | `600` | Espera entre devoluciones parciales (0–86.400). Vacío, no entero o negativo = 600. |
| `FUTURESBOARD_RESCATE_UPNL_MODE` | `off` | Disparo anticipado por uPnL: `off` \| `dry_run` (con el servicio en real, solo registra) \| `real`. Cualquier otro valor = `off` + error en el log. Ver [Modos](#modos-futuresboard_rescate_upnl_mode). |
| `FUTURESBOARD_RESCATE_UPNL_COOLDOWN_SECONDS` | `60` | Espera entre inyecciones del disparo por uPnL (0–3.600). Vacío, no entero o negativo = 60; más de 3.600 = 3.600. |
| `FUTURESBOARD_RESCATE_TOPE_CONFIRM_SECONDS` | `60` | Confirmación del tope (0–600). `0` = disparo inmediato. Vacío, no entero o negativo = 60; más de 600 = 600. Espera máxima sin lecturas: `max(5 × valor, 300)` s. |
| `FUTURESBOARD_RESCATE_SPOT_URL` | `https://api.binance.com` (`https://demo-api.binance.com` con testnet) | Base de spot y `/sapi`. |
| `FUTURESBOARD_DOCKER_URL` / `FUTURESBOARD_PASSIVBOT_CONTAINER` | (vacía) / `client17-passivbot` | Necesarias para que el tope ejecute Apagar. Sin `FUTURESBOARD_DOCKER_URL`, `real` no transfiere. |
| `FUTURESBOARD_BINANCE_TESTNET` | (vacía) | Igual que en el dashboard (o `BINANCE_TESTNET` en `config.json`): futuros en `demo-fapi`, spot en `demo-api`, `real` bloqueado. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `TELEGRAM_PREFIX` | (vacías) | Avisos (ver [notificaciones.md](notificaciones.md)). |
| `FUTURESBOARD_CONFIG_DIR` | `./config` | Directorio de `config.json` (API key/secret, `EXCHANGE`, testnet). El estado y el lock van junto a la DB. |

Telegram: un aviso por inyección (de margen o por uPnL, con el motivo), devolución (total o parcial, con el ratio antes y después),
transferencia rechazada o incierta, tope en confirmación,
confirmación cancelada, disparo del tope y resultado del Apagar, y por cambios de estado (lectura caída / recuperada, bloqueo, reserva agotada),
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
      FUTURESBOARD_RESCATE_TOPE_CONFIRM_SECONDS: "60"   # 0 = tope sin confirmación (Apagar inmediato)
      FUTURESBOARD_RESCATE_UPNL_MODE: "off"         # disparo por uPnL: off | dry_run (solo registra) | real
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

Para C3 (stop con reinicio) el mismo servicio suma `FUTURESBOARD_C3_*`, el directorio de forager y el latido
para passivbot: ver [c3.md](c3.md#docker-compose).

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
- La confirmación del tope es un trade-off: filtra mechas que rebotan, pero si la caída sigue, el cierre a
  mercado llega 60–75 s más tarde (con los defaults) y puede salir a un precio peor que el del primer cruce.
- Una transferencia que no se puede conciliar sola (sin rastro y con el saldo spot movido por otra cosa)
  frena las transferencias nuevas hasta resolverla a mano en `rescate_state.json`.
- En Binance Demo no hay `/sapi`: en dev solo se puede probar `dry_run`.
- Un solo servicio por cuenta: una segunda instancia queda esperando el lock (`.rescate_service.lock`).
