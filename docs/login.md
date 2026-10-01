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
| `FUTURESBOARD_SECRET_KEY` | efímera + warning | Firma las sesiones (y el token CSRF del panel). Sin ella se genera una al azar en cada arranque: todas las sesiones se pierden al reiniciar. **Definirla en prod.** |
| `FUTURESBOARD_COOKIE_SECURE` | `1` | Marca la cookie `Secure`. Correcto detrás de HTTPS (Cloudflare Tunnel, también en dev). **Si se accede por HTTP plano** (ej. `http://IP:80`) el navegador no devuelve la cookie y el login queda en loop: poner `0` en ese caso. `http://localhost` funciona igual en Chrome/Firefox. |

Siempre: `HttpOnly` y `SameSite=Lax`.

## Rate limit del login

- Máximo **5 intentos fallidos por (usuario, IP) en 15 minutos**. El 6.º intento (aunque sea con
  la contraseña correcta) responde **429** y se loguea. Un login exitoso resetea el contador.
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
