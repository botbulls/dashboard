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
                                              └─ /var/run/docker.sock (solo en el proxy)
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
| GET | `/api/bot/status` | – | Estado del contenedor, preset de riesgo deducido (`bajo`/`medio`/`alto`/`personalizado`/`desconocido`), `twe_long`, `twe_short`, `long_mode`, `short_mode`, `enabled`, `modes_supported`, `config_pending`. |
| POST | `/api/bot/start` | `{"riesgo": "bajo"\|"medio"\|"alto"}` | Escribe `twe_long`/`twe_short` del preset, `long_mode=normal`, `short_mode` (ver abajo); `start` si está detenido, `restart` si corre. |
| POST | `/api/bot/stop` | `{"modo": "graceful"\|"apagar"}` | `graceful`: `long_mode=short_mode=graceful_stop` + start o restart (requiere `FUTURESBOARD_FORAGER_SUPPORTS_MODES=1`, si no 409). `apagar`: `stop` del contenedor y después cancela **todas** las órdenes y cierra **todas** las posiciones de USDⓈ-M Futures a mercado (ver "Apagar" abajo); no toca la config. |

Presets (históricos del guardian): bajo 4/1, medio 6/2, alto 8/3 (`twe_long`/`twe_short`).

Códigos: 400 parámetro inválido · 403 CSRF · 409 graceful no soportado / exchange distinto de
Binance en Apagar · 415 sin JSON · 502 docker-proxy/contenedor o cierre parcial en Binance ·
503 panel no configurado o faltan credenciales de Binance.

### Lo que muestra el estado

Riesgo, TWE y modos se leen del HJSON, no del proceso: forager carga la config una sola vez al
arrancar. Para no mostrar como vigente algo que no lo está:

- `config_pending`: `true` si el mtime del HJSON es posterior a `State.StartedAt` del contenedor
  (la config cambió y passivbot todavía no la cargó). La UI marca el riesgo como
  "pendiente de reinicio". `null` si el contenedor no corre (se aplica al próximo arranque).
- Con `FUTURESBOARD_FORAGER_SUPPORTS_MODES` apagado, la UI muestra los modos como
  "n/a (forager no los lee)" y nunca muestra el badge "Graceful stop", aunque el HJSON tenga
  `graceful_stop` (edición manual, o el flag estuvo prendido y se apagó).

### Config y acción Docker: qué pasa si falla

START y Graceful stop siguen este orden:

1. `GET /containers/{name}/json` antes de tocar nada. Si el proxy no responde o el contenedor no
   existe, se corta acá con 502 y la config queda intacta.
2. Escritura del HJSON (con backup) y del estado local.
3. `start` o `restart`.
   - Si Docker responde con error (4xx/5xx): no aplicó la acción. Se restaura el HJSON desde el
     backup (atómico, conservando el mtime previo) y el estado local; el error lo dice.
   - Si no hay respuesta (timeout, conexión cortada): no se sabe si el reinicio ocurrió, así que
     **no** se revierte (podría dejar el archivo distinto de lo que forager ya cargó). El error
     dice que la config quedó escrita y nombra el backup; `config_pending` muestra si está en
     efecto.

El log de auditoría guarda el mensaje de error completo, incluida la restauración o el backup.

### Apagar: detener y cerrar todo en Binance

`POST /api/bot/stop {"modo": "apagar"}` hace, en este orden y bajo el lock de acciones:

1. **Detiene passivbot** con el cliente Docker (`POST /containers/{name}/stop`, `304` = ya estaba
   detenido) y confirma con `GET /containers/{name}/json` que no corre. Si el stop falla (error
   HTTP, timeout, contenedor inexistente) o sigue corriendo: **502 y no se toca Binance**. Cerrar
   con el bot vivo haría que vuelva a abrir.
2. **Cancela todas las órdenes abiertas**: `GET /fapi/v1/openOrders` (sin `symbol`) y, por cada
   símbolo con órdenes, `DELETE /fapi/v1/allOpenOrders?symbol=`. Lo mismo con las órdenes
   condicionales (STOP/TP/trailing), que Binance movió al servicio algo:
   `GET /fapi/v1/openAlgoOrders` + `DELETE /fapi/v1/algoOpenOrders?symbol=`.
3. **Cierra todas las posiciones a mercado**: `GET /fapi/v2/positionRisk`, filas con
   `positionAmt != 0`. `POST /fapi/v1/order` con `type=MARKET`, `side` opuesto al signo y
   `newOrderRespType=RESULT`:
   - one-way (`positionSide=BOTH`): `reduceOnly=true`, sin `positionSide`;
   - hedge (`positionSide` LONG/SHORT): `positionSide` de la fila y **sin** `reduceOnly` (Binance
     no lo acepta en hedge). Un símbolo con LONG y SHORT genera dos cierres.

   El modo se deduce del `positionSide` de cada fila (no hace falta consultar
   `/fapi/v1/positionSide/dual`). Cantidad = `abs(positionAmt)` redondeada **hacia abajo** al
   `stepSize` de `MARKET_LOT_SIZE` (o `LOT_SIZE` si el primero falta o es 0), partida en varias
   órdenes de a lo sumo `maxQty` (alineado al step). `exchangeInfo` se pide una sola vez.
4. **Verifica**: hasta 3 rondas de leer → cancelar → cerrar, con 1 s entre rondas, y una lectura
   final. Terminado = 0 posiciones, 0 órdenes y 0 órdenes algo.
5. **Resumen** en la respuesta (`resumen`) y en la auditoría (`detail`): `ordenes_canceladas`
   (símbolo, tipo `normal`/`algo`, cantidad), `posiciones_cerradas` (símbolo, lado, modo,
   cantidad, órdenes enviadas), `errores`, `rondas`, `restante` (lo que quedó abierto +
   `verificado`) y `algo_verificado`.

Resultado: **200** si quedó todo en cero. **502** si quedó algo abierto o no se pudo verificar
(passivbot igual queda detenido); el JSON trae `error` + `resumen.restante` y la UI lo muestra.

Cliente Binance: las mismas credenciales y base URL que el scraper (`API_KEY`, `API_SECRET`,
`API_BASE_URL` de `config.json`; con `BINANCE_TESTNET` / `FUTURESBOARD_BINANCE_TESTNET=1`
apunta a `https://demo-fapi.binance.com`). Firma HMAC-SHA256 del query string, header
`X-MBX-APIKEY`, `recvWindow=5000`, timeout de 10 s por request. Los errores muestran solo método,
ruta y `code`/`msg` de Binance: nunca la URL firmada, la key ni el secret. Si `EXCHANGE` no es
`binance` → 409; si faltan credenciales → 503 (en ambos casos antes de detener el contenedor).

Decisiones:

- Las órdenes se cancelan **antes** de cerrar posiciones, para que ninguna orden pendiente
  reabra una posición mientras se cierra.
- Si `GET /fapi/v1/openAlgoOrders` devuelve 404:
  - **producción** (sin `BINANCE_TESTNET`): el endpoint existe en Binance, así que un 404 indica
    base URL o proxy mal configurados. Las órdenes condicionales quedan **sin verificar** →
    `restante.verificado=false` y **502** ("No se pudo verificar órdenes condicionales"). Se
    reintenta la lectura en cada ronda;
  - **demo/testnet** (`BINANCE_TESTNET` activo): se omiten, se anota en `errores` y el resumen trae
    `algo_verificado=false`. Puede terminar en 200, pero con `warning` y la UI **no** dice "se
    cerraron todas": avisa que las condicionales no se verificaron.
- Una respuesta que no es la esperada **nunca** cuenta como "vacío": un 200 sin JSON (proxy, WAF,
  página de mantenimiento) es error, y `openOrders`/`openAlgoOrders`/`positionRisk` tienen que
  devolver una lista de objetos (un objeto u otra cosa es error). Esas lecturas fallidas dejan
  `verificado=false` → 502.
- Cualquier excepción inesperada durante el cierre (no solo errores de Binance) se anota en
  `errores` por paso y el resumen parcial se conserva: la respuesta es 502 con `resumen` y la
  auditoría lo registra, en lugar de un 500 "Error interno." sin detalle.
- Un remanente menor que `minQty`/`stepSize` no se puede cerrar con `quantity` y queda en
  `restante` → 502. En la práctica `positionAmt` siempre es múltiplo del step.
- Un error en una orden o cancelación no corta el proceso: se anota en `errores` y la ronda
  siguiente reintenta sobre lo que siga abierto.
- Doble click: el lock de archivo serializa; la segunda ejecución encuentra todo cerrado
  (`stop` → 304) y devuelve 200 con 0 acciones.

La UI pide confirmación con un texto que aclara que se cierran **todas** las posiciones a mercado
y se cancelan **todas** las órdenes (irreversible) y exige tildar el checkbox. Al terminar
muestra el resumen (y, si fue parcial, lo que quedó abierto).

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
En Apagar, `detail` incluye `resumen: {...}` con el JSON del cierre.

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `FUTURESBOARD_DOCKER_URL` | (vacía) | URL del docker-socket-proxy, ej. `http://docker-proxy:2375`. Sin ella el panel se muestra deshabilitado. |
| `FUTURESBOARD_PASSIVBOT_CONTAINER` | `client17-passivbot` | Nombre del contenedor de passivbot. |
| `FUTURESBOARD_FORAGER_CONFIG` | (vacía) | Ruta del HJSON de forager dentro del contenedor del dashboard. Sin ella START y graceful quedan deshabilitados; Apagar funciona. |
| `FUTURESBOARD_FORAGER_SUPPORTS_MODES` | `0` | Habilita Graceful stop. Activar solo con forager parcheado (ver arriba). |
| `FUTURESBOARD_SECRET_KEY` | efímera + warning | Fijarla para que las sesiones (y el token CSRF) sobrevivan reinicios. Ver [login.md](login.md) para cookies, usuarios y rate limit. |

Apagar no agrega variables: usa `API_KEY` / `API_SECRET` / `API_BASE_URL` / `EXCHANGE` de
`config.json` y el modo de prueba existente (`FUTURESBOARD_BINANCE_TESTNET`). La API key
necesita permiso de **trading de futuros** (antes el dashboard solo leía).

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
      # ":ro" NO restringe la API (ver "Alcance real" abajo)
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [docker-api]
    # sin "ports": solo accesible desde la red interna

networks:
  docker-api:
    internal: true
```

### Alcance real del docker-socket-proxy (leer antes de prod)

`tecnativa/docker-socket-proxy` filtra por prefijo de ruta y por método, no por contenedor:

- `CONTAINERS=1` habilita **cualquier** ruta `/containers/*` para **cualquier** contenedor del host.
- `POST=1` habilita **todos** los métodos que no son GET/HEAD (POST, PUT, DELETE), no solo POST.

Desde el dashboard (o cualquier cosa en la red `docker-api`) eso permite, entre otras cosas:

- `GET /containers/client17-passivbot/archive?path=...`: leer cualquier archivo de passivbot,
  incluidas las **API keys del exchange**.
- `GET /containers/<x>/json`: `Config.Env` de todos los contenedores del host.
- `PUT /containers/<x>/archive`: escribir archivos dentro de cualquier contenedor.
- `POST /containers/create` con `Privileged` y `Binds: ["/:/host"]` + `start`: **root en el host**.

O sea: un RCE o SSRF en el dashboard equivale a root en el host más exfiltración de las keys de
Binance. El `:ro` del montaje de `docker.sock` **no limita nada** a nivel API (solo impide
reemplazar el archivo del socket); no es una medida de seguridad. La red interna sin puertos
publicados solo evita que el proxy sea alcanzable desde afuera.

**Pendiente antes de prod (follow-up):** reemplazar el proxy genérico por uno con allowlist
explícita (haproxy o nginx propio frente al socket) que permita exactamente, con o sin prefijo
`/vX.Y`, y deniegue todo lo demás:

```
GET  ^(/v[0-9.]+)?/containers/client17-passivbot/json$
POST ^(/v[0-9.]+)?/containers/client17-passivbot/(start|stop|restart)$   (query t=N permitida)
```

Esa config no está incluida ni probada en este PR.

## Prerrequisitos de deploy (fuera del alcance de este PR)

El panel le da a cualquier sesión logueada la capacidad de arrancar el bot con riesgo "alto" o
apagarlo cerrando todas las posiciones a mercado. Antes, eso requería además la contraseña de admin
separada (que este PR elimina). Hoy la app tiene:

- servidor de desarrollo de Flask en HTTP plano en `:80` (CMD del Dockerfile + `80:5000`).

Resuelto en `dev/login-hardening` (ver [login.md](login.md)): sin usuario por defecto (los que
tengan `123456` quedan bloqueados hasta `flask set-password`), cookie `Secure`/`HttpOnly`/`Lax`,
`next` sin open redirect y rate limit en el login. Mínimo pendiente antes de exponer el panel:
HTTPS adelante (Cloudflare Tunnel) y `FUTURESBOARD_SECRET_KEY` fija.

El contenedor de passivbot debe montar el mismo directorio:
`/root/botbulls/client17/passivbot/configs/forager:/<passivbot_root>/configs/forager`.
