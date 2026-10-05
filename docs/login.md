# Login: usuarios, sesiones y rate limit

## Usuarios

Ya no se siembra ningún usuario por defecto (antes: `cliente17` / `123456`).

Alta o cambio de contraseña con el comando de Flask. Hay que correrlo **desde el directorio
de config** (el mismo que usa `futuresboard`, por default `config/`), porque `futuresboard.wsgi`
toma `config.json` y `futures.db` del directorio actual:

```bash
# local
cd config && flask --app futuresboard.wsgi set-password <usuario>

# docker
docker compose exec -w /usr/src/futuresboard/config futuresboard \
  flask --app futuresboard.wsgi set-password <usuario>
```

- Pide la contraseña por prompt sin eco, con confirmación.
- Si está definida `FUTURESBOARD_ADMIN_PASSWORD`, usa ese valor (solo en este comando, no se loguea).
- Si el usuario no existe lo crea; si existe le cambia la contraseña.
- Rechaza contraseña vacía y `123456`.
- Ojo: el comando instancia la app; si `config.json` no tiene `DISABLE_AUTO_SCRAPE: true`
  arranca el hilo del scraper mientras dura el comando (inofensivo, pero hace llamadas al exchange).

**Bootstrap opcional**: si la tabla `users` está vacía al arrancar y están definidas
`FUTURESBOARD_ADMIN_USER` y `FUTURESBOARD_ADMIN_PASSWORD`, se crea ese usuario. Si la tabla ya
tiene usuarios, las variables se ignoran (no pisan contraseñas). Sin usuarios ni variables, la app
arranca y loguea un warning.

### Bases existentes con `123456`

Un usuario cuya contraseña sea `123456` **no puede loguearse** aunque la ingrese bien: el login
responde 403 con el aviso de usar `flask set-password`, y se loguea un warning. Al arrancar, la app
también loguea un warning por cada usuario en esa situación. Se destraba fijando una contraseña
nueva con el comando. Desde `/settings` tampoco se puede volver a poner `123456`.

## Redirect después del login

`next` solo acepta rutas internas relativas: empieza con `/`, no con `//`, sin `\`, sin esquema
ni host y sin caracteres de control. Cualquier otra cosa redirige a `/`.

## Cookies de sesión y secret key

| Variable | Default | Descripción |
|---|---|---|
| `FUTURESBOARD_SECRET_KEY` | efímera + warning | Firma las sesiones (y el token CSRF del panel). Sin ella se genera una al azar en cada arranque: todas las sesiones se pierden al reiniciar. **Definirla en prod y rotarla al deployar este cambio** (ver abajo). |
| `FUTURESBOARD_COOKIE_SECURE` | `1` | Marca la cookie `Secure`. Correcto detrás de HTTPS (Cloudflare Tunnel, también en dev). **Si se accede por HTTP plano** (ej. `http://IP:80`) el navegador no devuelve la cookie y el login queda en loop: poner `0` en ese caso. `http://localhost` funciona igual en Chrome/Firefox. |

Siempre: `HttpOnly` y `SameSite=Lax`.

## Revocación de sesiones

Las sesiones son cookies firmadas, sin estado en el servidor. Para que un cambio de contraseña
las corte, al loguearse se guarda en la sesión una huella (`pw_fp`, HMAC-SHA256 con la secret key
del hash de contraseña) y en cada request protegida se compara contra el hash actual en la base
(una consulta sqlite por request; no se recalcula scrypt).

- `flask set-password`, el cambio de contraseña desde `/settings` o borrar el usuario invalidan
  todas las sesiones previas de ese usuario. Quien cambia la clave desde `/settings` conserva la suya.
- Una sesión inválida se limpia (`session.clear()`, incluido el token CSRF del panel) y redirige
  al login.
- Las cookies emitidas **antes** de este cambio no tienen huella y se rechazan: cualquier sesión
  obtenida con `cliente17` / `123456` deja de servir apenas se deploya, aunque
  `FUTURESBOARD_SECRET_KEY` no cambie.
- Igual se recomienda **rotar `FUTURESBOARD_SECRET_KEY` en este deploy** como defensa en profundidad
  (por si la clave anterior se filtró: con ella se podrían firmar cookies arbitrarias). Desloguea a
  todos.

## Rate limit del login

- Máximo **5 intentos fallidos por (usuario, IP) en 15 minutos**. El 6.º intento (aunque sea con
  la contraseña correcta) responde **429** y se loguea. Un login exitoso resetea el contador.
- El intento se cuenta de forma atómica **antes** de verificar la contraseña (bajo un candado), así
  que una ráfaga de requests concurrentes no puede evaluar más de 5 contraseñas por ventana.
- En memoria del proceso: se pierde al reiniciar y no se comparte entre workers. Alcanza para el
  servidor actual (un solo proceso de Flask); con gunicorn multi-worker habría que moverlo a la
  sqlite o a Redis.

### IP del cliente y proxies

Por default se usa la IP de la conexión (`remote_addr`) y se **ignoran** `X-Forwarded-For` y
`CF-Connecting-IP`, porque cualquiera puede mandarlos.

| Variable | Default | Descripción |
|---|---|---|
| `FUTURESBOARD_PROXY_FIX` | (vacía) | Cantidad de proxies de confianza delante de la app. Con `1` se activa `werkzeug.middleware.proxy_fix.ProxyFix(x_for=1, x_proto=1)` y la IP sale de `X-Forwarded-For` (cloudflared lo envía). |

- **Sin** `FUTURESBOARD_PROXY_FIX` detrás de un proxy/tunnel: todas las requests llegan con la IP
  del proxy, así que 5 fallos sobre un usuario lo bloquean para todos durante 15 min (DoS de login
  sobre ese usuario, no bypass).
- **Con** `FUTURESBOARD_PROXY_FIX` y la app alcanzable también directo (ej. puerto 80 publicado):
  un atacante puede falsear `X-Forwarded-For` y saltarse el límite. Activarlo solo si la app es
  accesible únicamente a través del proxy.
- `CF-Connecting-IP` no se usa: con cloudflared, `X-Forwarded-For` ya trae la IP real.

### Decisión para prod (cloudflared + puerto publicado)

Con el `docker-compose.yaml` actual (`ports: "80:5000"`, alcanzable directo) **ninguna de las dos
configuraciones es segura**:

- **Sin** `FUTURESBOARD_PROXY_FIX` (default): detrás de cloudflared todos los clientes comparten la
  IP del tunnel, así que la clave del rate limit es en la práctica solo el usuario. Cualquier
  visitante anónimo puede bloquear a `cliente17` mandando 5 POST cada 15 minutos (DoS de login).
- **Con** `FUTURESBOARD_PROXY_FIX=1` y el puerto 80 publicado: se puede falsear `X-Forwarded-For`
  pegándole directo al puerto y saltear el límite.

Configuración recomendada para prod: que la app sea alcanzable **solo** a través del tunnel y
activar `FUTURESBOARD_PROXY_FIX=1`. Si cloudflared corre en el host, alcanza con publicar el puerto
solo en loopback:

```yaml
    ports:
      - "127.0.0.1:80:5000"
```

Si cloudflared corre como contenedor en la misma red de compose, quitar `ports` y apuntarlo a
`futuresboard:5000`. Este PR **no** cambia el compose porque depende de cómo esté levantado
cloudflared en el server; se aplica en el deploy, después de confirmar que cloudflared manda
`X-Forwarded-For`.
