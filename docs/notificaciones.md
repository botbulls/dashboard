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
| `NOTIFIER_DB_PATH` | `./config/futures.db` | Ruta de la DB del dashboard (también `--db`). Se abre en solo lectura. |
| `NOTIFIER_STATE_PATH` | `notifier_state.json` junto a la DB | Archivo de estado del notifier (también `--state`). Tiene que ser escribible. |
| `FUTURESBOARD_DOCKER_URL` / `FUTURESBOARD_PASSIVBOT_CONTAINER` | (vacía) / `client17-passivbot` | Si `FUTURESBOARD_DOCKER_URL` está, el notifier chequea el contenedor de passivbot. Sin ella ese chequeo se omite. En el notifier tiene que apuntar a un proxy **de solo lectura** (ver compose), no al del panel. |

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
python -m futuresboard.notifier                                  # loop, DB en ./config/futures.db
python -m futuresboard.notifier --db /data/futures.db --once     # un ciclo y salir (prueba manual)
```

No lee `config.json` ni carga la config del dashboard: solo necesita la ruta de la DB (`--db` o
`NOTIFIER_DB_PATH`), que abre en solo lectura. Así no recibe las API keys de Binance. Si en el
dashboard se cambió `DATABASE` en `config.json`, pasar esa misma ruta al notifier.

### Chequeos

| Chequeo | Cómo | Caído si |
|---|---|---|
| Contenedor passivbot | `GET /containers/<nombre>/json` vía docker-socket-proxy | no existe, no está `running`, o el proxy no responde |
| Scrape de la DB | último scrape OK según el latido del scraper (`scraper_latido.json`) o `scrape_state.json`, que solo escribe el scraper. Si no están a la vista (DB montada como archivo suelto), mtime de la DB. No se usa el mtime de `futures.db-wal`: cualquier conexión lo crea al abrir. | más viejo que `NOTIFIER_SCRAPE_MAX_AGE`, o sin ningún scrape OK registrado |
| Último trade (opcional) | `MAX(time)` de la tabla `income` | más viejo que `NOTIFIER_TRADE_MAX_AGE` |

**Antispam:** solo se avisa en las transiciones. Un chequeo pasa a caído después de
`NOTIFIER_FAIL_THRESHOLD` fallos seguidos (un aviso `🔴 … caído`) y vuelve con un único
`✅ … recuperado`. Mientras sigue caído no se repite. Si el envío a Telegram falla, la transición no se
registra y el aviso se reintenta en el ciclo siguiente.

El estado se guarda en `NOTIFIER_STATE_PATH` (default `notifier_state.json` junto a la DB) para que
reiniciar el notifier no repita una alerta ya enviada ni el resumen del día. En Docker el estado va a
un volumen propio (ver compose), no al directorio de datos de la DB.

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

Snippet para sumar al compose del panel (ver [panel-passivbot.md](panel-passivbot.md)). El notifier
usa la misma imagen que el dashboard, pero **no** comparte con él ni `config.json` ni el docker-socket-proxy.
Supone la DB en su propio directorio de datos (`./data/futures.db`, ver
[produccion.md](produccion.md#scraper-en-su-propio-contenedor-y-sqlite-en-wal)), separado de `./config`:

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
    # La imagen trae un HEALTHCHECK contra /health del dashboard (gunicorn); el notifier no sirve
    # HTTP, así que se desactiva para que el contenedor no quede "unhealthy".
    healthcheck:
      disable: true
    restart: unless-stopped
    environment:
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
      TELEGRAM_PREFIX: "[client17]"
      NOTIFIER_DB_PATH: /data/futures.db
      NOTIFIER_STATE_PATH: /state/notifier_state.json
      FUTURESBOARD_DOCKER_URL: http://docker-proxy-ro:2375   # proxy propio, solo GET
      FUTURESBOARD_PASSIVBOT_CONTAINER: client17-passivbot
      NOTIFIER_INTERVAL: "60"
      NOTIFIER_DAILY_SUMMARY: "21:00"
    volumes:
      # Solo el directorio de DATOS (DB, -wal/-shm, scrape_state.json, latido del scraper). Nunca
      # ./config: ahí está config.json con las API keys de Binance. Escribible (sin :ro) porque con
      # WAL SQLite tiene que poder crear futures.db-shm/-wal si no hay otra conexión abierta; el
      # código igual abre la DB con mode=ro (ver nota abajo).
      - ./data:/data
      # Estado del notifier.
      - notifier-state:/state
    # default: salida a internet (api.telegram.org). notifier-docker es internal: sin internet.
    networks: [default, notifier-docker]
    depends_on: [docker-proxy-ro]

  # Proxy aparte para el notifier: solo lectura (POST=0). NO usar el docker-proxy del panel (POST=1).
  docker-proxy-ro:
    image: tecnativa/docker-socket-proxy   # fijar tag/digest en prod
    environment:
      CONTAINERS: 1
      POST: 0          # sin POST/PUT/DELETE: no puede crear, arrancar ni parar contenedores
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [notifier-docker]

networks:
  notifier-docker:
    internal: true

volumes:
  notifier-state:
```

Notas:

- El token va en un `.env` junto al compose (`chmod 600`), no en el YAML versionado.
- El notifier **no** se conecta a la red `docker-api` del panel: ese proxy tiene `POST=1`, que permite
  `POST /containers/create` con `Privileged` (root en el host, ver
  [panel-passivbot.md](panel-passivbot.md#alcance-real-del-docker-socket-proxy-leer-antes-de-prod)).
  El notifier solo necesita `GET /containers/<nombre>/json`.
- Riesgo residual del proxy de solo lectura: `CONTAINERS=1` con `POST=0` sigue permitiendo **GET** sobre
  `/containers/*` de cualquier contenedor, o sea leer `Config.Env` (`/json`) y archivos (`/archive`),
  incluidas las API keys de passivbot. Ya no hay escritura ni root, pero un RCE en el notifier podría
  exfiltrar esas keys. Si eso no es aceptable, hay dos salidas: un proxy con allowlist que solo deje
  pasar `GET /containers/client17-passivbot/json` (el mismo follow-up de panel-passivbot.md), o
  correr el notifier sin `FUTURESBOARD_DOCKER_URL` ni la red `notifier-docker` (se omite el chequeo del
  contenedor y quedan el scrape, el trade y el resumen diario).
- **DB en WAL: se monta el directorio de datos, no el archivo suelto.** Con WAL los commits van a
  `futures.db-wal` y la coordinación entre procesos a `futures.db-shm`, ambos junto a la DB: un archivo
  montado solo leería con un `-wal`/`-shm` propio dentro del contenedor y vería datos viejos (hasta el
  último checkpoint). Por eso la DB vive en `./data`, aparte de `./config`.
- **Por qué `./data` va escribible.** Probado con SQLite 3.46 (el de la imagen) y un bind mount `:ro`:
  si en ese momento no hay ninguna otra conexión abierta (el scraper y el web cierran la suya en cada
  vuelta/request, y SQLite borra `-wal`/`-shm` al cerrar la última), la apertura falla con
  `unable to open database file`. Con el directorio escribible SQLite crea el `-shm` y la lectura
  funciona. El código sigue abriendo con `mode=ro` (no puede escribir la DB por SQL), pero un proceso
  comprometido sí podría escribir en `./data`, y el costo frente al `:ro` de antes es concreto:
  - **cuenta del panel**: en `futures.db` está la tabla `users` (hashes del login). Un notifier comprometido
    (corre como root y tiene el directorio escribible) podría reescribir un hash y entrar al panel, que
    controla el bot, el rescate y C3. Lo amortigua que el panel está detrás de Cloudflare Access;
  - **su propia alerta**: podría pisar `scraper_latido.json` o `scrape_state.json` y así no avisar que el
    scraper se cayó;
  - borrar o pisar la DB y sus `-wal`/`-shm`.

  Es una regresión de **integridad**, no de secretos: el notifier sigue sin ver `config.json` ni las API
  keys. Mitigaciones a futuro (no hechas): que el scraper mantenga una conexión abierta para que `-wal`/
  `-shm` existan siempre y probar el notifier con `:ro`, o sacar `users` de `futures.db`. No usar
  `immutable=1` (leería páginas a medio checkpoint).
- Con journal de rollback (DB vieja, sin WAL) el mismo montaje de directorio también funciona.
- Sin Telegram configurado el notifier no termina (para no entrar en un loop de reinicios con
  `restart: unless-stopped`): loguea que está deshabilitado y queda inactivo.

## Seguridad

- El token nunca se loguea: los errores de red se registran solo con el tipo de excepción (el texto de
  `requests` incluye la URL, que lleva el token) y los errores de la API solo con el status y la
  descripción. Además se fija el logger `urllib3.connectionpool` en INFO, porque a nivel DEBUG
  loguea el path del request.
- Los mensajes incluyen el usuario del panel y los errores tal como quedan en la auditoría; no incluyen
  API keys ni la config de forager.
- El notifier no carga `config.json` (no recibe `API_KEY`/`API_SECRET` de Binance) y no tiene acceso de
  escritura a Docker: usa su propio proxy con `POST=0` (ver arriba el riesgo residual de lectura).
