# Notificaciones por Telegram

Reemplazan los avisos que mandaba el guardian. Hay dos partes:

1. **Eventos del panel** (dentro del dashboard): START, Graceful stop y Apagar, con su resultado.
2. **Notifier** (proceso aparte y opcional): alertas de salud y resumen diario de PnL.

Las dos usan la misma configuración de Telegram. Sin `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` no se
envía nada: el dashboard y el notifier lo dicen con una línea de log al arrancar y siguen normalmente.

## Variables de entorno

| Variable | Default | Uso |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (vacía) | Token del bot (BotFather). Sin token, deshabilitado. |
| `TELEGRAM_CHAT_ID` | (vacía) | Chat o grupo destino (ej. `-1001234567890`). Sin chat, deshabilitado. |
| `TELEGRAM_PREFIX` | (vacía) | Texto al inicio de cada mensaje, ej. `[DEV client17]`. |
| `NOTIFIER_INTERVAL` | `60` | Segundos entre chequeos del notifier (mínimo 10). |
| `NOTIFIER_FAIL_THRESHOLD` | `2` | Chequeos fallidos seguidos antes de avisar "caído" (evita avisos por un parpadeo). |
| `NOTIFIER_SCRAPE_MAX_AGE` | `900` | Segundos sin escrituras en la DB para considerar caído el scrape. `0` lo deshabilita. |
| `NOTIFIER_TRADE_MAX_AGE` | `0` | Segundos sin trades/income nuevos para avisar. `0` (default) lo deshabilita: horas sin trades puede ser normal. |
| `NOTIFIER_DAILY_SUMMARY` | `21:00` | Hora (HH:MM, Buenos Aires) del resumen diario. `off` lo deshabilita. |
| `FUTURESBOARD_DOCKER_URL` / `FUTURESBOARD_PASSIVBOT_CONTAINER` | (las del panel) | Si están, el notifier chequea el contenedor de passivbot. Sin `FUTURESBOARD_DOCKER_URL` ese chequeo se omite. |

Los mensajes se mandan con `parse_mode=HTML`; todo texto variable (prefijo, errores, símbolos, usuario)
se escapa. Los mensajes de más de 4096 caracteres se recortan.

## 1. Eventos del panel

Después de cada acción del panel (la misma que queda en `bot_actions.log`) se manda un mensaje:

- `✅ START riesgo medio: OK` / `❌ … FALLÓ` con el error.
- `Graceful stop: OK` / `FALLÓ`.
- `APAGAR (stop + cierre de posiciones)`: OK o FALLÓ, con el resumen del cierre: posiciones cerradas
  (símbolo, lado, cantidad), órdenes canceladas (por símbolo y tipo), errores, lo que quedó abierto y
  los avisos de "sin verificar" (estado final o condicionales en demo/testnet). El resumen también
  viaja en el caso de cierre parcial (502).

No se notifican los rechazos de validación (400, ej. `riesgo` inválido): no llegan a tocar nada.

El envío es **asíncrono** (hilo aparte) y no puede cambiar la respuesta: si Telegram no responde,
da error o el hilo no se puede crear, la acción devuelve exactamente lo mismo y queda un warning en el log.
Timeout de cada envío: 10 s.

## 2. Notifier (salud + resumen diario)

```bash
python -m futuresboard.notifier            # loop, usa ./config como el dashboard
python -m futuresboard.notifier -c /ruta/config --once   # un ciclo y salir (prueba manual)
```

Lee `config/config.json` igual que el dashboard para encontrar la DB (`DATABASE`, default
`config/futures.db`). No necesita las API keys de Binance para nada, pero `config.json` debe ser válido.

### Chequeos

| Chequeo | Cómo | Caído si |
|---|---|---|
| Contenedor passivbot | `GET /containers/<nombre>/json` vía docker-socket-proxy | no existe, no está `running`, o el proxy no responde |
| Scrape de la DB | mtime del archivo de la DB (proxy: el scraper escribe `positions`/`account` en cada ciclo) | más viejo que `NOTIFIER_SCRAPE_MAX_AGE` |
| Último trade (opcional) | `MAX(time)` de la tabla `income` | más viejo que `NOTIFIER_TRADE_MAX_AGE` |

**Antispam:** solo se avisa en las transiciones. Un chequeo pasa a caído después de
`NOTIFIER_FAIL_THRESHOLD` fallos seguidos (un aviso `🔴 … caído`) y vuelve con un único
`✅ … recuperado`. Mientras sigue caído no se repite. Si el envío a Telegram falla, la transición no se
registra y el aviso se reintenta en el ciclo siguiente.

El estado se guarda en `notifier_state.json`, en el mismo directorio que la DB, para que reiniciar el
notifier no repita una alerta ya enviada ni el resumen del día.

### Resumen diario

A la hora `NOTIFIER_DAILY_SUMMARY` (zona `America/Argentina/Buenos_Aires`; sin base de zonas se usa
UTC-3 fijo), una vez por día:

- Ingreso realizado del día (00:00–23:59 de Buenos Aires) desde la tabla `income`, con el mismo
  criterio que la home: sin `TRANSFER`, `COIN_SWAP_*` ni asset `BNB`; desglosado por tipo
  (`REALIZED_PNL`, `COMMISSION`, `FUNDING_FEE`, …).
- Balance de la wallet, posiciones abiertas (símbolo, lado, cantidad, UPNL) y UPNL total, según el
  último scrape.

Si el notifier arranca después de la hora y el resumen de ese día no se mandó, se manda en ese momento.

## docker-compose

Snippet para sumar al compose del panel (ver [panel-passivbot.md](panel-passivbot.md)). Usa la misma
imagen y el mismo volumen `./config` que el dashboard:

```yaml
services:
  dashboard:
    # ... lo de panel-passivbot.md, más:
    environment:
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
      TELEGRAM_PREFIX: "[client17]"

  notifier:
    build: .                      # misma imagen que el dashboard
    command: ["python", "-m", "futuresboard.notifier"]
    restart: unless-stopped
    environment:
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
      TELEGRAM_PREFIX: "[client17]"
      FUTURESBOARD_DOCKER_URL: http://docker-proxy:2375
      FUTURESBOARD_PASSIVBOT_CONTAINER: client17-passivbot
      NOTIFIER_INTERVAL: "60"
      NOTIFIER_DAILY_SUMMARY: "21:00"
    volumes:
      - ./config:/usr/src/futuresboard/config   # misma DB que el dashboard (solo lectura de la DB)
    # default: salida a internet (api.telegram.org). docker-api es internal: sin internet.
    networks: [default, docker-api]
    depends_on: [docker-proxy]
```

Notas:

- El token va en un `.env` junto al compose (`chmod 600`), no en el YAML versionado.
- La red `docker-api` es `internal: true` (sin salida a internet): el notifier necesita además la red
  `default` para llegar a `api.telegram.org`.
- Sin Telegram configurado el notifier no termina (para no entrar en un loop de reinicios con
  `restart: unless-stopped`): loguea que está deshabilitado y queda inactivo.

## Seguridad

- El token nunca se loguea: los errores de red se registran solo con el tipo de excepción (el texto de
  `requests` incluye la URL, que lleva el token) y los errores de la API solo con el status y la
  descripción. Además se fija el logger `urllib3.connectionpool` en INFO, porque a nivel DEBUG
  loguea el path del request.
- Los mensajes incluyen el usuario del panel y los errores tal como quedan en la auditoría; no incluyen
  API keys ni la config de forager.
