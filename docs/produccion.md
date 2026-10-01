# Producción: gunicorn, health checks, métricas y logs

## Servidor: gunicorn con 1 worker

La imagen ya no usa el servidor de desarrollo de Flask. El `CMD` es:

```bash
gunicorn --config python:futuresboard.gunicorn_conf futuresboard.wsgi:app
```

- **1 worker `gthread` con varios threads** (default 8). No se escala con workers porque:
  - el **scraper automático** es un hilo que arranca dentro de `init_app`: con N workers habría
    N scrapers en paralelo con la misma API key, gastando peso de API y escribiendo la misma SQLite;
  - el **rate limit del login** (PR de login-hardening) vive en memoria del proceso;
  - sin `FUTURESBOARD_SECRET_KEY`, cada proceso generaría su propia clave y las sesiones se romperían.
  - El lock de acciones del panel (`Store.lock`) es `fcntl.flock`, que sí sirve entre procesos; no
    es la razón, pero queda dicho.
- **Sin `preload_app`**: con preload el hilo del scraper nacería en el master y no sobreviviría al fork.
- El conf **rechaza** `workers != 1` o `--preload` al arrancar (también ignora `WEB_CONCURRENCY`).
- El trabajo es I/O (SQLite, Docker API, Binance), así que los threads alcanzan.

La config se lee de `./config/config.json` relativo al `WORKDIR` (`/usr/src/futuresboard`), igual
que el CLI. Se puede cambiar con `FUTURESBOARD_CONFIG_DIR`.

`HEALTHCHECK` de la imagen: consulta `/health` cada 30 s (con `python`, la imagen slim no trae curl).

### Scraper automático y `--disable-auto-scraper`

- Por defecto el scraper corre como hilo dentro del worker, cada `AUTO_SCRAPE_INTERVAL` segundos.
- **Antes**, `--disable-auto-scraper` se parseaba pero no tenía efecto: el `CMD` viejo lo pasaba y
  el scraper corría igual (así funciona hoy prod). Ahora el flag sí lo desactiva, y el `CMD` nuevo
  **ya no lo pasa**, así que el comportamiento de prod no cambia.
- Formas de desactivarlo: `FUTURESBOARD_DISABLE_AUTO_SCRAPE=1`, `DISABLE_AUTO_SCRAPE: true` en
  `config.json`, o `futuresboard --disable-auto-scraper` (dev server).
- `futuresboard --scrape-only` (cron) ya no arranca además el hilo en segundo plano.
  En Docker: `docker run --rm -v ./config:/usr/src/futuresboard/config <imagen> futuresboard --scrape-only`.

El servidor de desarrollo sigue disponible con `futuresboard` (útil en local).

## Endpoints de operación

| Endpoint | Auth | Uso |
|---|---|---|
| `GET /health` | ninguna | Liveness del proceso web. Responde `{"status": "ok"}`. No toca DB, Docker ni el exchange y no expone datos. |
| `GET /api/bot/health` | login | Estado detallado con `status` `ok`/`warn`/`critical`. Siempre HTTP 200; el estado va en el JSON. |
| `GET /metrics` | login **o** `Authorization: Bearer $FUTURESBOARD_METRICS_TOKEN` | Formato Prometheus text. Sin sesión ni token: 401. El token no se acepta por query string. |

### Qué mide `/api/bot/health`

- **bot**: contenedor de passivbot vía el `DockerClient` del panel (docker-socket-proxy):
  `running`, `status`, `started_at` (StartedAt) y `uptime_seconds`.
  - contenedor detenido o inexistente → `critical`;
  - proxy caído / sin respuesta → `warn` (nunca 500). Ojo: el timeout del cliente es 10 s, así que con el
    proxy caído la consulta tarda eso;
  - panel sin `FUTURESBOARD_DOCKER_URL` → `skipped` (no afecta el estado global).
- **last_trade**: último fill en la tabla `income` (`REALIZED_PNL`, `COMMISSION`, `ADLTRADE`,
  `BUSTTRADE`; funding y transfers no cuentan). También se informa `last_order_at` (la orden abierta
  más nueva de `orders`), sin umbral.
- **positions / upnl**: de la DB (`positions` con `positionAmt != 0`). Se eligió la DB y no Binance
  en vivo para no gastar peso de API en cada consulta; la frescura la da `scrape`. Como la DB la
  llena el scraper, respeta `BINANCE_TESTNET` (también se informa `binance_testnet`).
- **scrape**: edad del último scrape exitoso. `scrape()` deja `scrape_state.json` junto a la DB
  (`last_started_at`, `last_success_at`, `last_error_at`, `last_error` con el código, sin URL).
  Sirve también si el scrape corre por cron (`--scrape-only`). Si el hilo del scraper muere, la edad
  crece y la alerta salta.

El estado global es el peor de los checks (ignorando `skipped`).

### Umbrales (env)

Edades en segundos. `0` o vacío deshabilita el umbral (en UPNL, vacío deshabilita y `0` es un valor válido).
Un valor no numérico se ignora con un warning y se usa el default.

| Variable | Default | Regla |
|---|---|---|
| `FUTURESBOARD_HEALTH_SCRAPE_WARN_SECONDS` | `900` | edad del último scrape OK ≥ valor → warn |
| `FUTURESBOARD_HEALTH_SCRAPE_CRIT_SECONDS` | `3600` | ≥ valor → critical |
| `FUTURESBOARD_HEALTH_TRADE_WARN_SECONDS` | `21600` (6 h) | edad del último fill ≥ valor → warn |
| `FUTURESBOARD_HEALTH_TRADE_CRIT_SECONDS` | `86400` (24 h) | ≥ valor → critical |
| `FUTURESBOARD_HEALTH_UPNL_WARN` | (off) | UPNL total ≤ valor → warn (ej. `-50`) |
| `FUTURESBOARD_HEALTH_UPNL_CRIT` | (off) | UPNL total ≤ valor → critical |
| `FUTURESBOARD_HEALTH_POSITIONS_WARN` | (off) | posiciones abiertas ≥ valor → warn |
| `FUTURESBOARD_HEALTH_POSITIONS_CRIT` | (off) | ≥ valor → critical |

Sin datos (nunca hubo scrape exitoso, no hay fills en la DB) → `warn`.

### Métricas

Todas son gauges con prefijo `futuresboard_`. Un valor desconocido se publica como `NaN`.

| Métrica | Significado |
|---|---|
| `futuresboard_health_status` | 0 ok, 1 warn, 2 critical |
| `futuresboard_bot_up` | 1 corriendo, 0 detenido/inexistente, NaN si no se pudo consultar |
| `futuresboard_positions_open` | posiciones abiertas (DB) |
| `futuresboard_upnl_total` | suma de UPNL de las posiciones abiertas (DB) |
| `futuresboard_last_trade_age_seconds` | segundos desde el último fill |
| `futuresboard_scrape_age_seconds` | segundos desde el último scrape exitoso |

Ejemplo de scrape config:

```yaml
- job_name: futuresboard
  metrics_path: /metrics
  authorization:
    type: Bearer
    credentials_file: /etc/prometheus/futuresboard_token
  static_configs:
    - targets: ["dashboard:5000"]
```

Generar el token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Logs

| Variable | Default | |
|---|---|---|
| `FUTURESBOARD_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `FUTURESBOARD_LOG_FORMAT` | `text` | `text`: `2026-10-01T18:48:30+0000 INFO [logger] mensaje`; `json`: una línea por evento con `ts` (UTC), `level`, `logger`, `msg` y `exc` |
| `FUTURESBOARD_ACCESS_LOG` | `1` | `0` silencia el access log de gunicorn |

App, scraper, `gunicorn.error` y `gunicorn.access` salen con el mismo formato por stderr
(`docker logs`). Antes de escribir se ocultan: `signature=` de las URLs firmadas, tokens Bearer,
headers de API key y los valores de `API_KEY`, `API_SECRET`, la secret key de Flask y
`FUTURESBOARD_METRICS_TOKEN`.

## Otras variables

| Variable | Default | |
|---|---|---|
| `FUTURESBOARD_HOST` / `FUTURESBOARD_PORT` | `0.0.0.0` / `5000` | bind de gunicorn (el HEALTHCHECK usa `FUTURESBOARD_PORT`) |
| `FUTURESBOARD_GUNICORN_THREADS` | `8` | threads del worker |
| `FUTURESBOARD_GUNICORN_TIMEOUT` | `60` | segundos (mínimo 10); una acción del panel puede esperar ~30 s a Docker |
| `FUTURESBOARD_CONFIG_DIR` | `./config` | directorio de `config.json` (y de la DB por defecto) |
| `FUTURESBOARD_DISABLE_AUTO_SCRAPE` | `0` | `1` desactiva el hilo de scraping |
| `FUTURESBOARD_SECRET_KEY` | aleatoria | gunicorn la pasa al worker por entorno. Fijarla en prod. La lógica de validación/aviso está en el PR de login-hardening. |
| `FUTURESBOARD_METRICS_TOKEN` | (vacía) | habilita `/metrics` con Bearer token |

Se eliminó `FUTURESBOARD_PUBLIC_IP` junto con la consulta a ipify/ifconfig.me que se hacía en
cada render (ningún template la usaba desde que se quitó el SCC).
