# Números en vivo (sin recargar la página)

Inicio, Posiciones y Moneda se actualizan solos cada 15 s sin recargar la página. Mismo stack que el resto:
Flask + Jinja arman los datos y `static/js/en_vivo.js` los pone en su lugar. No hay WebSocket ni otro proceso.

## Qué se actualiza

| Página | Qué cambia en el lugar |
|---|---|
| Todas las de abajo | Menú izquierdo: monedas activas (exposición y órdenes), inactivas y las cuentas de los badges. |
| Inicio (`/`, `/dashboard/<desde>/<hasta>`) | Balance y PnL no realizado (con su fuente), Hoy / Desde el lunes / mes / Total con su % del balance, último pedido, estado del bot, modo del rescate en la franja, tarjetas del rescate (semáforo, avisos, C3), los tres gráficos del período (con `chart.update('none')`), sus tablas de "Ver los números" y el subtítulo del período (en `/` es la semana en curso: el lunes pasa sola a la nueva). |
| Posiciones | Filas de la tabla, sin perder el orden, la búsqueda ni el scroll de la DataTable. |
| Moneda (`/coins/<par>`) | Precio actual, posición y órdenes (tabla), PnL no realizado de la moneda y la calculadora de promedio (posición, objetivos y precio). |

No se actualizan: las velas de Moneda (son 4 pedidos de 1000 velas a Binance; quedan como al cargar), los KPI
de ganancia de Moneda, Historial, Proyección, Rescate y Ajustes.

## Cómo funciona

- **Pedido**: `GET /api/vivo/inicio[?start=AAAA-MM-DD&end=AAAA-MM-DD]`, `GET /api/vivo/posiciones` y
  `GET /api/vivo/moneda/<par>` (ver `futuresboard/vivo.py`). Requieren login como el resto de `/api` (sin sesión:
  redirect al login); son GET de lectura, sin CSRF, con `Cache-Control: no-store`. Una moneda que no existe da
  404 antes de cualquier pedido a Binance. Fechas inválidas: 400. La base bloqueada: 503 JSON.
- **Mismas cuentas, mismos macros**: la página y el JSON salen de las mismas funciones
  (`blueprint._datos_inicio`, `_datos_posiciones`, `_posicion_moneda`) y lo que cambia de forma (listas, tarjetas,
  filas, el % con su ícono) de los mismos macros de Jinja (`_sidebar.html`, `_inicio.html`, `_posiciones.html`,
  `_moneda.html`, `_rescate_tarjetas.html`). El JS no formatea números ni arma HTML: el formato es-AR es el de
  `formato.py`.
- **Forma del JSON**:
  - `valores[k] = {"crudo", "texto", "tono"}` para los elementos `[data-vivo="k"]`: el crudo va a `data-value`, el
    texto formateado al contenido y el tono (`gana`/`pierde`/`cero`) a la clase `is-*` si el elemento tiene
    `data-vivo-tono`;
  - `bloques[k]`: el HTML del elemento `[data-vivo-bloque="k"]`;
  - `tablas[id]`: las `<tr>` de la DataTable `#id`, cada una con `data-fila`;
  - `graficos` (Inicio), `bot` (Inicio: lo mismo que `/api/bot/status`), `datos` (latido del scraper) y `fuentes`
    (Inicio: de dónde salen balance y PnL).
  Un test (`tests/test_en_vivo.py`) compara la página con el JSON en las dos direcciones: todo gancho de la página
  tiene su valor y todo valor tiene dónde aplicarse, con el mismo crudo, texto, clase y HTML.
- **Balance y PnL no realizado de Inicio**: si el servicio rescate tiene una lectura de la cuenta de menos de 60 s
  (late, no está en `off` y la lectura no tiene error), se usa esa: el rescate lee cada 15 s y el scraper cada 60 s.
  Si no, la base. La tarjeta dice la fuente ("Balance y PnL: lectura del rescate" / "base del scraper"). En
  `dry_run` la lectura trae la wallet con el préstamo simulado: ahí la wallet real sale de `margin_balance − upnl`
  (cuenta USDT-M de un solo activo, el mismo supuesto del disparo por uPnL). La página al cargar usa la misma
  regla, así el primer poll no salta.

## Comportamiento del JS

- No pide nada al cargar (la página llega con los datos); después, cada 15 s con `setTimeout` encadenado.
- Con la pestaña oculta no pide nada; al volver pide enseguida. También al volver desde la caché del navegador.
- Si falla, reintenta a los 30 s, 60 s, 120 s (tope 2 min) y muestra "Sin conexión con el servidor,
  reintentando…" (o "No se pudo actualizar…" si el server respondió con error), en un solo `role="status"`.
- Si la sesión venció o se cortó el acceso (redirect al login de la app o a Cloudflare Access, o una página que no
  es JSON), avisa "La sesión venció…" con un botón para volver a entrar y deja de pedir: nunca pinta esa respuesta.
- No toca nada con un modal abierto (confirmaciones, progreso del bot, velas, detalle) ni con el foco en un campo
  del panel del bot: espera y aplica cuando se cierra.
- Debajo del título: "Actualizado hace X s · datos de hace Y s" (la edad sale del latido del scraper; con más de
  3 vueltas sin dato, mínimo 3 min, se pinta en amarillo). Ese texto no es `aria-live`; cuando los datos pasan a
  viejos se avisa una vez en el `role="status"` ("Los datos tienen más de 3 min: el scraper no está actualizando
  la base"). Sin latido y con el scraper como hilo del web (compose sin servicio scraper) las vueltas son las del
  hilo (`AUTO_SCRAPE_INTERVAL`, 300 s): no se pone amarillo entre dos vueltas.
- Si una respuesta llega después de un pedido más nuevo (por ejemplo, volver a la pestaña con uno en vuelo), se
  descarta: cada pedido lleva un número y el nuevo aborta al anterior.
- Después de aplicar dispara `fb-vivo-aplicado`: en Moneda la calculadora recalcula el promedio y la cantidad
  para la distancia con la posición y el precio nuevos (si la posición se cerró, la primera fila "Posición"
  queda en 0).
- El estado del bot viaja en el JSON de Inicio con un tope de 3 s para el `inspect` a Docker (el pedido se corta a
  los 10 s): con el docker-proxy colgado los números se actualizan igual y el bot queda "Sin datos".
- Un número que cambió se resalta apenas (fondo que se apaga en 1,6 s), salvo con `prefers-reduced-motion`.
- Los bloques se reemplazan solo si cambiaron, sin sacarle el foco a nadie y sin cerrar los `<details>`
  abiertos. Un aviso del rescate (`role="alert"`) que sigue diciendo lo mismo y solo cambia el "hace N min" (o
  pasa de "89 s" a "2 min") se actualiza sin volver a anunciarse, también cuando se reemplaza el bloque entero
  porque cambió la cantidad de tarjetas. Con el foco adentro de un bloque que cambió de forma, ese bloque espera
  a que el foco salga (nunca se lo saca).
- En `file://` (los scripts de captura abren el HTML guardado por `file://`) no hace nada: no pide, no avisa y
  deja el texto que vino del server.

## Costo

Por pestaña abierta, cada 15 s: Inicio hace las mismas consultas a SQLite que la página y un `inspect` al
contenedor de passivbot por el docker-proxy (reemplaza el poll de 30 s que ya tenía la franja); Posiciones pide
`premiumIndex` (todas las monedas, peso 10) y Moneda `premiumIndex?symbol=` (peso 1) a la API pública de Binance,
como la página al cargar. Es peso de la misma IP que usa el scraper (que se regula con `X-MBX-USED-WEIGHT-1M`):
con una pestaña en Posiciones son 40 de peso por minuto contra un límite de 2400.
