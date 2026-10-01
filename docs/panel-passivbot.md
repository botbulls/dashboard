# Panel de control de passivbot

Reemplaza al guardian, al check y al servicio SCC (puerto 9009, `server_call_commands.py`),
que se eliminan del producto. El dashboard controla directamente el contenedor de passivbot
(forager, passivbot v6.1) y su config HJSON.

## Bloqueante conocido: forager no lee `long_mode` / `short_mode`

`forager.py` (botbulls/passivbot) **no** lee modos desde el HJSON:

- `-lm` / `-sm` de cada bot se derivan internamente: `"n"` si el símbolo está activo y la
  exposición es > 0, `"gs"` si no.
- El graceful stop solo existe como flag de CLI (`-gs`, `-gsl`, `-gss`), y forager pisa
  `config["graceful_stop*"]` con los valores de argparse después de cargar el HJSON.
- El contenedor corre `python forager.py configs/forager/new.json` sin flags, y la Docker API
  no permite cambiar el `Cmd` de un contenedor existente.

Por eso el botón **Graceful stop** queda deshabilitado por defecto: escribir
`long_mode: graceful_stop` y reiniciar no tendría efecto (el bot volvería a operar normal
mientras la UI dice "graceful stop"). Se habilita con `FUTURESBOARD_FORAGER_SUPPORTS_MODES=1`
**solo** cuando forager esté parcheado. Patch propuesto (no aplicado, va en botbulls/passivbot),
después de las líneas que setean `config["graceful_stop_*"]` desde argparse:

```python
    # Modos desde el HJSON (panel del dashboard). Semántica -gsl/-gss: mantiene WE limit = twe / n.
    config["graceful_stop_long"] = config["graceful_stop_long"] or config.get("long_mode") == "graceful_stop"
    config["graceful_stop_short"] = config["graceful_stop_short"] or config.get("short_mode") == "graceful_stop"
```

Se usan los flags por lado (`-gsl` / `-gss`) y no `-gs`: `-gs` redistribuye la TWE entre las
posiciones que quedan abiertas y sube la exposición por posición.

No se usa `twe = 0` ni `n_longs = 0` como "graceful stop": cambian el WE limit que passivbot ve
sobre las posiciones abiertas (0 o el fallback fijo 0.1) y pueden disparar unstuck o realizar
pérdidas.

START (presets de riesgo) sí funciona con forager sin cambios: `twe_long` / `twe_short` se leen
del HJSON. forager carga la config una sola vez al arrancar, por eso cada acción reinicia el
contenedor.

## Diseño

```
Browser ──(sesión + CSRF)──> dashboard (Flask)
                               │  lee/escribe  configs/forager/new.json (bind mount del DIRECTORIO, RW)
                               │  auditoría    <data>/bot_actions.log (JSONL)
                               └─ HTTP ──> docker-proxy:2375 (tecnativa/docker-socket-proxy, CONTAINERS=1, POST=1)
                                              └─ /var/run/docker.sock (ro, solo en el proxy)
                                                    └─ contenedor <name>-passivbot (forager)
```

- Código: `src/futuresboard/bot_control.py` (lógica) y rutas en `src/futuresboard/blueprint.py`.
- Autenticación: el `before_request` global de `auth.py` (sin sesión → 302 a `/login`).
- CSRF: token por sesión (`session["csrf_token"]`), expuesto en `<meta name="csrf-token">`.
  Las mutaciones exigen header `X-CSRF-Token` (comparación en tiempo constante) y body JSON.
- Las acciones se serializan con un lock de archivo en el directorio de datos.
- Cliente Docker: solo `GET /containers/{name}/json` y `POST /containers/{name}/{start,restart,stop}`.
  El nombre del contenedor se valida contra `^[A-Za-z0-9][A-Za-z0-9_.-]*$`. Sin `subprocess`.

### Endpoints

| Método | Ruta | Body | Efecto |
|---|---|---|---|
| GET | `/api/bot/status` | – | Estado del contenedor, preset de riesgo deducido (`bajo`/`medio`/`alto`/`personalizado`/`desconocido`), `twe_long`, `twe_short`, `long_mode`, `short_mode`, `enabled`, `modes_supported`. |
| POST | `/api/bot/start` | `{"riesgo": "bajo"\|"medio"\|"alto"}` | Escribe `twe_long`/`twe_short` del preset, `long_mode=normal`, `short_mode` (ver abajo); `start` si está detenido, `restart` si corre. |
| POST | `/api/bot/stop` | `{"modo": "graceful"\|"apagar"}` | `graceful`: `long_mode=short_mode=graceful_stop` + start o restart (requiere `FUTURESBOARD_FORAGER_SUPPORTS_MODES=1`, si no 409). `apagar`: `stop` del contenedor, no toca la config. |

Presets (históricos del guardian): bajo 4/1, medio 6/2, alto 8/3 (`twe_long`/`twe_short`).

Códigos: 400 parámetro inválido · 403 CSRF · 409 graceful no soportado · 415 sin JSON ·
502 docker-proxy/contenedor · 503 panel no configurado.

**Apagar**: las posiciones y órdenes abiertas quedan en el exchange sin gestión del bot. La UI
exige tildar esa advertencia antes de confirmar.

### Decisión sobre `short_mode` en START

`short_mode` se mantiene tal cual, salvo que sea un modo de stop (`graceful_stop`). En ese caso
vuelve al valor que tenía antes del graceful stop (guardado en
`<data>/bot_control_state.json`, fuera del HJSON de forager) o, si no hay valor guardado, a
`normal`. Así un START no pisa un `short_mode` puesto a mano (por ejemplo `tp_only`) y tampoco
deja los shorts frenados después de un graceful stop.

### Escritura del HJSON

1. Backup exacto con timestamp junto al archivo (`new.json.bak-YYYYmmdd-HHMMSS-ffffff`); se
   conservan los últimos 20.
2. Escritura a un tmp en el mismo directorio + `fsync` + mismos permisos.
3. Se relee el tmp y se verifica que parsea y tiene los valores nuevos; si no, se aborta y el
   original queda intacto.
4. `os.replace` atómico.

El round-trip de `hjson` **pierde los comentarios** del archivo (el backup los conserva). Si el
original es JSON estricto se reescribe como JSON.

`os.replace` crea un inode nuevo: hay que montar el **directorio** `configs/forager/` (no el
archivo) tanto en el dashboard como en passivbot. Con el archivo montado suelto, el rename
falla (`EBUSY`) o passivbot sigue viendo el inode viejo.

### Auditoría

`<directorio de la DB>/bot_actions.log`, una línea JSON por acción:
`ts`, `user`, `action`, `params`, `result` (`ok`/`rechazado`/`error`), `detail`, `remote_addr`.

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `FUTURESBOARD_DOCKER_URL` | (vacía) | URL del docker-socket-proxy, ej. `http://docker-proxy:2375`. Sin ella el panel se muestra deshabilitado. |
| `FUTURESBOARD_PASSIVBOT_CONTAINER` | `client17-passivbot` | Nombre del contenedor de passivbot. |
| `FUTURESBOARD_FORAGER_CONFIG` | (vacía) | Ruta del HJSON de forager dentro del contenedor del dashboard. Sin ella START y graceful quedan deshabilitados; Apagar funciona. |
| `FUTURESBOARD_FORAGER_SUPPORTS_MODES` | `0` | Habilita Graceful stop. Activar solo con forager parcheado (ver arriba). |
| `FUTURESBOARD_SECRET_KEY` | aleatoria | Ya existente. Fijarla para que las sesiones (y el token CSRF) sobrevivan reinicios. |

Se eliminan `FUTURESBOARD_ADMIN_URL`, `FUTURESBOARD_BOT_URL`, `FUTURESBOARD_BOT_SERVICE_NAME`
y `FUTURESBOARD_BOT_SERVICE_PORT`.

## docker-compose (dev)

```yaml
services:
  dashboard:
    build: .
    environment:
      FUTURESBOARD_SECRET_KEY: cambiar-esto
      FUTURESBOARD_DOCKER_URL: http://docker-proxy:2375
      FUTURESBOARD_PASSIVBOT_CONTAINER: client17-passivbot
      FUTURESBOARD_FORAGER_CONFIG: /forager/new.json
      # FUTURESBOARD_FORAGER_SUPPORTS_MODES: "1"   # solo con forager parcheado
    volumes:
      - ./config:/usr/src/futuresboard/config
      # directorio (no el archivo) para que os.replace funcione; RW
      - /root/botbulls/client17/passivbot/configs/forager:/forager
    ports:
      - "80:5000"
    networks: [default, docker-api]
    depends_on: [docker-proxy]

  docker-proxy:
    image: tecnativa/docker-socket-proxy   # fijar tag/digest en prod
    environment:
      CONTAINERS: 1
      POST: 1
      # todo lo demás queda en 0 (default): sin images, exec, volumes, networks, etc.
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [docker-api]
    # sin "ports": solo accesible desde la red interna

networks:
  docker-api:
    internal: true
```

`POST=1` + `CONTAINERS=1` permite POST sobre cualquier endpoint `/containers/*` (incluido
create/kill/delete) para cualquier contenedor del host: el proxy reduce la superficie pero no
limita a un contenedor. Por eso la red `docker-api` es interna y solo el dashboard la comparte.

El contenedor de passivbot debe montar el mismo directorio:
`/root/botbulls/client17/passivbot/configs/forager:/<passivbot_root>/configs/forager`.
