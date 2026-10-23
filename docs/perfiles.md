# Perfiles de riesgo y TWE manual

!!! warning "Herramienta, no asesor"
    Los números de esta página son de un **backtest** (2021–2026), no una promesa. Elegir un perfil, una TWE o una
    frecuencia de retiro es una decisión de quien opera la cuenta. Un perfil puede liquidar la cuenta de futuros y,
    con rescate, también la reserva.

El panel de control del bot (menú de la rueda) tiene, además de los presets de riesgo de siempre (bajo 4/1, medio 6/2,
alto 8/3, que no cambian):

- **PERFILES**: las dos opciones medidas en el backtest multianual, aplicadas en una sola acción;
- **TWE MANUAL**: `twe_long` a mano, con validación de rango y advertencia.

Código: `src/futuresboard/perfiles.py` (perfiles), `bot_control.start_bot(..., twe_long=)` (TWE manual) y las rutas en
`src/futuresboard/blueprint.py`. El retiro periódico vive en el servicio rescate ([rescate.md](rescate.md#retiro-periodico-de-ganancias)).

## Los dos perfiles

| | Conservador | Con rescate |
|---|---|---|
| `twe_long` (forager) | 3 (lw 0,75 con 4 pares) | 4 (lw 1,0 con 4 pares) |
| `twe_short` | 1 (sin efecto con `n_shorts` 0) | 1 (sin efecto con `n_shorts` 0) |
| Rescate | deshabilitado (se conservan reserva, umbrales y tope guardados) | habilitado: reserva 10.000, paso 1.000, inyecta con equity/MM < 8, devuelve desde 32, tope de pérdida total 60 % |
| Disparo por uPnL | off | X 70 %, paso 1.000 |
| Retiro periódico | off | anual (la frecuencia que fija el backtest de retiro), base 10.000 |
| Capital en riesgo | 10.000 (futuros) | 20.000 (futuros + reserva) |

"Con rescate" con TWE 4/1 es el mismo `twe_long`/`twe_short` que el preset **bajo**: el estado del bot lo muestra como
riesgo `bajo`. "Conservador" (3/1) aparece como `personalizado`.

### Números del backtest

Backtest multianual 2021–2026 (`botbulls-lab/multianual`; passivbot v6.1 clock en wallet compartida, BNB/BTC/ETH/XRP,
apalancamiento 10). OOS = ventanas de 12 meses que arrancan desde 2023-11-10, retorno sobre los 10.000 de futuros
(wiki `BotBulls_Mejoras_Riesgo_Retorno_Rescate_uPnL_2026-10-23`, recalculado en `revision/final/recalc_final.py`):

| Perfil | Mediana | Media | Peor ventana | Peor drawdown | Sobre |
|---|---|---|---|---|---|
| Conservador (TWE 3 sin rescate) | +59,5 % | +41,9 % | −9,4 % | 45 % | 20.000 (10.000 de futuros + 10.000 quietos) |
| Con rescate (TWE 4 + rescate + U70) | +86,3 % | +60 % | −12 % | 63–65 % | 20.000 (10.000 de futuros + 10.000 de reserva) |

Esas filas se midieron sin tope de pérdida total; con el tope 60 % del perfil las ventanas OOS dan lo mismo (mediana
86,5 %, media 60,2 %, peor −11,6 % a −12,0 % según el escenario, drawdown 61,7 % a 64,8 %): en ninguna ventana se
llega al tope.

Lo que la tabla no dice:

- **Conservador**: en las ventanas OOS la cuenta de futuros llegó a bajar 71 % (3.369 de 10.000) sin liquidarse.
- **Con rescate**: en may-21 la cuenta de futuros llegó a 401 USDT (−96 %) y la salvó la reserva. Para planificar,
  ~60 %/año sobre 10.000 (~30 % sobre 20.000) como techo.
- **Con rescate supone retirar la ganancia.** Cada ventana de 12 meses arranca de nuevo en 10.000. Sin retiro, el
  camino continuo compone y, con mechas 1,5 % más profundas, se liquida el 2026-02-06 y pierde también la reserva. Por
  eso el perfil prende el [retiro periódico](rescate.md#retiro-periodico-de-ganancias). En el mismo camino continuo,
  con retiro anual termina en 95.631 desde 20.000 sin liquidarse, mientras que TWE 3 sin retiro compone y termina en
  295.896: el retiro protege la ganancia ya hecha, no agrega retorno.
- Desde TWE 6 aparecen liquidaciones en el backtest; los perfiles solo cubren TWE 3 y 4.

## Aplicar un perfil

En el panel: **PERFILES** → elegir → **Aplicar perfil**. El servidor arma el plan y lo muestra para confirmar (con
una casilla). El plan dice:

- qué cambia en forager (`twe_long` actual → nuevo, la lw con los pares de la config);
- si passivbot está **operando** (se reinicia: `docker restart`, y forager tarda `FUTURESBOARD_FORAGER_WARMUP_SECONDS`,
  ~90 s, en volver a abrir los pares; las posiciones y órdenes abiertas siguen en Binance) o **detenido** (se inicia);
- qué queda en el rescate, el disparo por uPnL y el retiro;
- los modos del servicio rescate: con el servicio en `off` o sin latido, "Con rescate" **no protege nada** hasta
  levantarlo; con `FUTURESBOARD_RESCATE_UPNL_MODE` o `FUTURESBOARD_RESCATE_RETIRO_MODE` en `off`, esa parte no corre;
  con `dry_run`, es simulada;
- un préstamo pendiente (pasar a Conservador deshabilita el rescate, pero lo prestado se sigue devolviendo y, si es
  real, el tope sigue armado hasta devolverlo);
- si prender el disparo por uPnL en plena pérdida inyectaría ya (el mismo aviso que el panel del rescate);
- si C3 está habilitado o parado (el perfil no lo cambia; para C3 es un START a mano);
- los números del backtest y que no son una promesa.

Al confirmar corre como cualquier acción del panel: un job con progreso (`Validar perfil, forager y rescate`,
`Guardar rescate, disparo por uPnL y retiro`, y los pasos de START), **el mismo lock de acciones** (una acción a la vez:
`409` con el `job_id` de la que está en curso) y la espera informativa a forager.

### Orden y vuelta atrás

1. Se valida todo antes de escribir: contenedor existente (`502` si el proxy no responde), `short_mode` válido (`409`),
   `n_shorts` 0 (`409`: los perfiles se midieron solo con longs), `n_longs` válido, ningún tope del rescate sin
   reconocer (`409`, igual que START) y el cuerpo del rescate (la validación de siempre).
2. Se guarda la config del rescate (`rescate_config.json`), con una copia del archivo anterior.
3. START con la TWE del perfil: backup y escritura atómica del HJSON, `start` o `restart`, verificación y espera.
4. Si el START falla y la config de forager quedó como estaba (Docker respondió con error y se restauró, contenedor
   inexistente, error de escritura), **se restaura también el rescate**: no se aplica nada. La generación del rescate
   no baja (si el servicio llegó a fijar una base con la generación nueva, la próxima habilitación abre otra).
5. Si Docker no respondió (resultado incierto), quedan las dos configs escritas, igual que con START: el error lo dice
   y `/api/bot/status` muestra "pendiente de reinicio".

Con `n_longs` distinto de 4 el perfil se aplica, pero el plan avisa que los números se midieron con 4 pares.

### API

| Método | Ruta | Body | Efecto |
|---|---|---|---|
| POST | `/api/bot/perfil` | `{"perfil": "conservador" \| "con_rescate"}` | `409` con `requiere_confirmacion: true`, el texto del plan en `error` y `plan` (`texto`, `lineas`, `corriendo`). No escribe nada. |
| POST | `/api/bot/perfil` | `{"perfil": ..., "confirmar": true}` | `202` + `job_id` (acción `perfil`). El resultado del job trae lo de START más `perfil` y `rescate`. |
| GET | `/api/bot/status` | – | Además de lo de siempre: `perfiles` (definiciones y números), `fuente`, `perfil_vigente`, `perfil_ultimo`, `n_longs`, `n_shorts` y `twe_manual` (límites). |

Validación estricta (`400`): solo `perfil` y `confirmar`, `perfil` exacto, `confirmar` booleano. Sesión y
`X-CSRF-Token` como el resto del panel.

**Auditoría** en `bot_actions.log` con la acción `perfil` y `params: {"perfil": ...}`: `requiere_confirmacion` (el plan
completo en `detail`), `rechazado`, y el resultado del job (`ok` / `error`, o `interrumpido` si el dashboard se
reinició en el medio). Telegram avisa el resultado del job (`PERFIL con_rescate: OK`).

### Perfil vigente

`perfil_vigente` sale de los valores, no del último aplicado: forager con `n_shorts` 0 y el `twe_long` del perfil, y el
rescate igual al del perfil (Conservador: rescate, disparo por uPnL y retiro apagados; Con rescate: todos los valores,
con cualquier frecuencia de retiro distinta de `off`, que es configurable). `twe_short` no cuenta (forager no lo usa
sin shorts). Si se toca algo a mano deja de ser el perfil; `perfil_ultimo` (`rescate_config.json` → `perfil`) dice
cuál se aplicó por última vez y lo borra el próximo guardado del panel del rescate.

## TWE manual

Campo numérico **TWE LONG**: de 0,5 a 8, en pasos de 0,25, validado en el navegador y en el servidor. Por encima de 4
el panel muestra la advertencia en el momento ("en el backtest 2021–2026 aparecen liquidaciones desde TWE 6") y la
confirmación pide marcar una casilla.

`POST /api/bot/start` con `{"twe_long": n}` en vez de `{"riesgo": ...}` (los dos juntos: `400`):

- número JSON finito (no texto, no booleano, no `NaN`), entre 0,5 y 8 y múltiplo de 0,25 (`400` si no);
- por encima de 4 hay que mandar `"confirmar_twe_alta": true`; sin eso, `409` con `requiere_confirmacion` (y queda
  en `bot_actions.log`), sin escribir nada. El resultado del job trae `warning`;
- escribe `twe_long` (entero si no tiene decimales) y, con `n_shorts` 0, `twe_short` 1. Con shorts deja el
  `twe_short` que está;
- el resto es START: modos normales, backup, escritura atómica, `start` o `restart`, START bloqueado por un tope sin
  reconocer, auditoría con `params: {"twe_long": n}`.

Una TWE manual que no coincide con un preset queda como `personalizado`: el reinicio automático de C3 la conserva
(START sin tocar `twe_long`/`twe_short`).

## Puesta en marcha (dev primero)

"Con rescate" necesita el servicio rescate corriendo con sus modos. En dev, todo en `dry_run`:

```yaml
  rescate:
    environment:
      FUTURESBOARD_RESCATE_MODE: dry_run
      FUTURESBOARD_RESCATE_UPNL_MODE: real      # con el servicio en dry_run: simulado
      FUTURESBOARD_RESCATE_RETIRO_MODE: real    # con el servicio en dry_run: simulado
```

y después aplicar el perfil desde el panel. En la cuenta real, `real` en los tres solo después de revisar los avisos
de `dry_run` (ver [rescate.md](rescate.md)).
