# Panel: progreso de las acciones

START, Graceful stop y Apagar ya no bloquean la request HTTP mientras trabajan. El POST valida,
crea un **job** que corre en un hilo del dashboard y responde enseguida con `202` y su id. La UI
abre un modal con barra de progreso y la lista de pasos, y consulta el job cada segundo.

## API

| Método | Ruta | Respuesta |
|---|---|---|
| POST | `/api/bot/start` · `/api/bot/stop` | `202 {"ok": true, "job_id": "...", "job": {...}}`. Las validaciones siguen siendo síncronas y no crean job: 400 parámetro inválido, 403 CSRF, 415 sin JSON, 409 graceful no soportado o exchange distinto de Binance en Apagar, 503 panel no configurado o faltan credenciales. **409 `{"job_id": "..."}`** si ya hay una acción en curso. |
| GET | `/api/bot/jobs/<id>` | Estado del job (abajo). 404 si no existe o venció. |
| GET | `/api/bot/jobs/activo` | `{"job": {...}}` con el job en curso, o `{"job": null}`. La UI lo consulta al cargar la página para retomar el modal. |

Las tres requieren sesión (sin sesión, 302 a `/login`, igual que el resto del panel).

Forma del job:

```json
{
  "id": "3f2a9c0d1e4b5a67",
  "accion": "start | graceful | apagar",
  "params": {"riesgo": "medio"},
  "estado": "en_curso | ok | error | interrumpido",
  "porcentaje": 66,
  "pasos": [
    {"clave": "cancelar_ordenes", "titulo": "Cancelar órdenes", "estado": "en_curso",
     "detalle": "2/5 símbolos (ETHUSDT)", "progreso": {"actual": 2, "total": 5}}
  ],
  "resultado": {"ok": true, "...": "el mismo JSON que antes devolvía la acción"},
  "http_status": 200,
  "inicio": "2026-10-05T15:00:00+00:00",
  "fin": "2026-10-05T15:01:31+00:00"
}
```

- Estados de paso: `pendiente`, `en_curso`, `ok`, `error`, `omitido`. `progreso` es opcional.
- `resultado` y `http_status` aparecen al terminar: son el JSON y el código HTTP que devolvía la
  acción síncrona (incluido el `resumen` de Apagar), así la UI y las integraciones leen lo mismo.
- `porcentaje`: pasos terminados sobre el total, más la fracción del paso en curso si informa
  `progreso`. Llega a 100 solo al terminar.

## Pasos

**START / Graceful stop**: validar configuración (contenedor existe, HJSON legible) → backup de la
config → escribir config → iniciar/reiniciar passivbot → verificar que el contenedor quedó
corriendo → esperando a forager.

"Esperando a forager" es informativo: forager tarda ~60-90 s en abrir los pares después del
reinicio. Es una cuenta regresiva de `FUTURESBOARD_FORAGER_WARMUP_SECONDS` (default 90, entre 0 y
900) que consulta el contenedor cada 5 s. Queda `ok` si al cumplirse el tiempo passivbot sigue
corriendo y `error` si se cayó. Si el contenedor no quedó corriendo después del reinicio, el job
termina en `error` (502) aunque la config ya esté escrita: el mensaje lo aclara.

**Apagar**: detener passivbot → verificar que está detenido → cancelar órdenes (x/y símbolos) →
cancelar órdenes condicionales (x/y símbolos; `omitido` en demo/testnet si el endpoint no existe)
→ cerrar posiciones (x/y) → verificación final (ronda n/3). Si una orden falla en la primera
ronda, el paso queda en `error` con "se reintenta en la verificación"; si la verificación termina
sin nada abierto, se re-marca `ok` ("resuelto al reintentar"). Si queda algo abierto, la
verificación queda en `error` y el modal muestra el resumen de lo que quedó.

Si un paso falla, ese paso queda en `error` con el mensaje y los siguientes en `omitido`.

## Una acción a la vez

El job toma el mismo lock de archivo que antes tomaba la acción síncrona (`.bot_control.lock` en
el directorio de datos) y lo suelta cuando terminó de tocar config, Docker y Binance. Mientras lo
tiene, cualquier otro POST de acción responde 409 con el `job_id` del job activo y la UI muestra
ese progreso.

Decisión: la espera a forager corre **sin** el lock y se puede cortar. Si durante esos 90 s se
pide otra acción (por ejemplo Apagar), el POST corta la espera (el paso queda `omitido`, el job de
START termina `ok`) y la acción nueva arranca. Bloquear un Apagar 90 s por una cuenta regresiva
informativa no tenía sentido.

## Registro, TTL y reinicios

- Los jobs viven en memoria del proceso (gunicorn corre con 1 worker, ver `docs/produccion.md` en
  la rama de prod), protegidos con un lock. Los terminados se borran después de 1 h y se guardan a
  lo sumo 50; el job en curso nunca se purga.
- El último job se persiste en `<directorio de datos>/bot_job_last.json` (escritura atómica) en
  cada cambio de paso.
- Si el proceso se reinicia con un job en curso (deploy, crash, OOM), al arrancar la app lo marca
  `interrumpido`: el paso que estaba en curso queda en `error` y los pendientes en `omitido`.
  Además escribe una línea `interrumpido` en `bot_actions.log` y manda la notificación de
  Telegram como fallo, porque una acción cortada a mitad (sobre todo Apagar) hay que revisarla a
  mano. La UI guarda el id del último job en `sessionStorage` y, al recargar, muestra ese estado.

## Auditoría y Telegram

La línea de auditoría y la notificación de Telegram se escriben **una vez, al terminar el job**
(no por paso), con el mismo resultado y resumen que antes. Los rechazos síncronos (400) se siguen
auditando como `rechazado` y no se notifican.

## Implementación

- `src/futuresboard/bot_control.py`: no conoce Flask ni los jobs. Las acciones reciben un
  `reporter` opcional (`Reporter.step(clave, estado, detalle, actual, total)`, por defecto no hace
  nada) y los catálogos de pasos están en `START_STEPS`, `GRACEFUL_STEPS` y `APAGAR_STEPS`.
- `src/futuresboard/jobs.py`: `Job` (es el reporter), `JobRegistry` (memoria + persistencia) y
  el runner (lock, auditoría, Telegram).
- `src/futuresboard/blueprint.py`: validación síncrona, 202/409 y los GET de jobs.
- `src/futuresboard/templates/base.html`: modal de progreso (`aria-live`, botones bloqueados
  mientras hay un job activo).
