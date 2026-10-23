# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [Unreleased]
- Perfiles de riesgo en el panel del bot: "Conservador" (TWE 3, sin rescate, disparo por uPnL y retiro off) y "Con rescate" (TWE 4 + rescate colchón con reserva 10.000, paso 1.000, umbral 8, devolución 32 y tope 60 % + disparo por uPnL X 70 % paso 1.000 + retiro periódico anual, base 10.000), aplicados en una sola acción (`POST /api/bot/perfil`, job con el lock del panel) con plan de confirmación armado por el servidor (reinicio y espera a forager, modos del servicio, préstamo pendiente, C3, números del backtest), solo con `n_shorts` 0, vuelta atrás del rescate si el START falla según lo que quedó en forager (sin pisar un guardado concurrente del rescate) y auditoría `perfil` en `bot_actions.log`; `perfil_vigente` en `/api/bot/status` (ver `docs/perfiles.md`)
- TWE manual en el panel del bot (`POST /api/bot/start` con `{"twe_long": n}`): 0,5 a 8 en pasos de 0,25, validación estricta, advertencia visible y `409` + `confirmar_twe_alta` por encima de 4; `twe_short` 1 con `n_shorts` 0. Los presets bajo/medio/alto no cambian
- Rescate: retiro periódico de ganancias futuros → spot (`UMFUTURE_MAIN`, sin permiso nuevo) de lo que exceda la base de futuros (`retiro_base_usdt`, 10.000), por períodos calendario UTC (`retiro_frecuencia` off/diaria/semanal/mensual/trimestral/anual), apagado por default (`FUTURESBOARD_RESCATE_RETIRO_MODE` `off`/`dry_run`/`real` y frecuencia `off`); sobre la wallet realizada, nunca con préstamo pendiente ni con el tope disparado o en confirmación, limitado por `maxWithdrawAmount` y por guardas de margen (equity/MM queda en el umbral de devolución) y del disparo por uPnL; misma conciliación sin idempotencia; ledger `retiro`, Telegram, fila en `/rescate` y tarjeta "Retiro de ganancias". Un retiro real confirmado baja la base de C3 en proporción al capital de futuros (el retiro solo nunca acerca el corte), y con spot caído el estimado del tope suma lo retirado. Frecuencia anual por default según el backtest de retiro del lab (`docs/rescate.md#retiro-periodico-de-ganancias`)
- Rescate: disparo anticipado por uPnL, apagado por default (`FUTURESBOARD_RESCATE_UPNL_MODE` `off`/`dry_run`/`real` y `upnl_habilitado` en el panel). Si el disparo de margen no se cumple, inyecta `upnl_paso_usdt` (1.000) cuando `uPnL + préstamo <= -X % × (wallet - préstamo)` (X 70 %, escalera por lo ya prestado, 60 s entre inyecciones), y con el disparo activo la devolución parcial no devuelve lo que volvería a pedir (guarda contra el ping-pong). Con `FUTURESBOARD_RESCATE_UPNL_MODE=dry_run` solo registra y avisa (no presta ni simulado, con el servicio en `dry_run` o `real`), una vez por escalón y con histéresis de un paso (un uPnL que oscila sobre la línea avisa una vez); con el rescate deshabilitado queda en off; bajar X o prenderlo con la cuenta en pérdida pide confirmación si inyectaría ya (uno o varios pasos seguidos) (`409` + `confirmar_inyeccion`). Supone una cuenta USDT-M de un solo activo. Fila en `/rescate` y tarjeta "Disparo por uPnL" en el dashboard principal. `loan_state.json` y `watcher_heartbeat.json` quedan en versión 1, documentados en `docs/contratos.md` (ver `docs/rescate.md#disparo-anticipado-por-upnl`)
- C3 (stop con reinicio): piso sobre la cuenta de futuros (equity menos el préstamo del rescate) con confirmación; al confirmarse ejecuta el Apagar del panel con el mismo lock y queda `c3_parado`; reinicio automático por rebote del índice equiponderado de los pares (velas de 1 h), cooldown o lo primero, re-anclando la base y haciendo START con la última config; límite de reinicios en una ventana; modos `FUTURESBOARD_C3_MODE` `off`/`dry_run`/`real` (real permitido en Demo); latido atómico `watcher_heartbeat.json` para el dead-man de passivbot, que en `real` solo se escribe mientras C3 protege (lectura de futuros reciente, sin Apagar pendiente propio o del tope, sin bloqueo ni config inválida); sin reinicio automático si al cortar falló la lectura de forager o del contenedor, y un START automático con error que deja passivbot corriendo cuenta para el límite; bajar el tope hasta cortar pide confirmación; sección "Stop con reinicio (C3)" en `/rescate` y `POST /api/c3/config` (ver `docs/c3.md`)
- Dashboard principal: fila "Rescate" con la billetera de rescate (USDT libre en spot), el préstamo, el capital total vs la base, el ratio margen/mantenimiento con semáforo, el estado del rescate y, si está habilitado, el de C3; solo lee el estado persistido (sin llamadas a Binance), marca la antigüedad del dato y pinta en gris una lectura vieja (rescate en off, servicio sin latido o más de 3 × intervalo + 30 s); la tarjeta de C3 muestra su latido (o suspendido) y el error de lectura
- Rescate como colchón: servicio `python -m futuresboard.rescate` (modos `off`/`dry_run`/`real`) que presta USDT de spot a futuros solo cerca de la liquidación, lo devuelve solo sin posiciones, publica el préstamo en `loan_state.json` para que passivbot dimensione sobre el capital propio y ejecuta Apagar al llegar al tope de pérdida total (START bloqueado hasta reconocerlo); sección "Rescate (colchón)" en el panel (ver `docs/rescate.md`). Umbral de inyección por defecto 8; el tope se confirma con una lectura posterior (`FUTURESBOARD_RESCATE_TOPE_CONFIRM_SECONDS`, 60 s) antes de Apagar, sin disparar a ciegas con futuros caídos y arrancando de cero tras un hueco de lecturas o un cambio de modo; devolución parcial con posiciones abiertas si el ratio llega a `umbral_devolucion_ratio` (32 por defecto según la simulación, mayor que el de inyección), con cooldown también después de cada inyección
- Panel de passivbot: START, Graceful stop y Apagar corren como job en segundo plano (`202` + `job_id`) con progreso paso a paso en un modal; nuevos `GET /api/bot/jobs/<id>` y `GET /api/bot/jobs/activo`, y `FUTURESBOARD_FORAGER_WARMUP_SECONDS` (ver `docs/panel-progreso.md`); durante la espera a forager el modal deja cerrar y ofrece Apagar, que la corta sin esperar al job anterior, y un worker nuevo de gunicorn no marca `interrumpido` un job que sigue vivo en otro proceso
- Match all of the details from [passivbot](https://github.com/enarjord/passivbot)s telegram commands `/position` and `/open_orders` commands
- Store and display historical unrealized PnL
- Tidy up repeated code into functions

## 2022-01-03
### Changed
- Added a composite key to the income table in the database to stop duplicates from being entered. This change will only be active on fresh databases/built from scratch. 
- Added the transactionId from Bybit to the list for future processing - this is a string whereas Binance uses integers. This needs to be managed to avoid more database deletions.

## 2021-12-27
### Changed
- Fixed an issue on history page where dowload buttons would cause 500 error when pressed
- Fixed an issue with bybit scraping to get latest trades. Still more to do here with regard to old trades looking back.

## 2021-12-25
### Changed
- Commas removed from `config.json.example` as the extra commas caused errors [@jontstaz](https://github.com/jontstaz)
- Inverse the colours for current buy/sells on the 15m, 1h, 4h and 1d charts to match green for buy and red for sell

## 2021-12-24
### Added
- Bybit now supported as an exchange, data will be scraped as best as possible however whole historical data doesn't seem possible currently.
- Candle chart with buy/sell lines now available on coin pages for 15m, 1h, 4h and 1d

### Changed
- Coin pages now have Average Down Calculator showing all the time, with specific distances away from position already calculated
- Income history page buttons fixed
- Positions page now has a table showing distance from next buys and all previous information

## 2021-12-10
### Changed
- Minor fixes and README updates regarding the latest changes made yesterday

## 2021-12-09
### Added
- The config file now has the ability to change the look and feel of the dashboard. The title can be changed using `NAVBAR_TITLE`, the background colour of the navigation bar can now be changed to a set of colours using `NAVBAR_BG` and the projection percentages can be changed to show more or less values as specified by the user with `PROJECTIONS`
- The config file can also be used to set the value of the `AUTO_SCRAPE_INTERVAL` between `60` and `3600` for more or less frequent updates depending on their needs
- Under the charts on the dashboard and coin pages there is now a summary showing either the `total profit` for that period or `number of coins active`
- Positions page now shows unrealised Profit and Loss for each coin

### Changed
- Tooltips were lost somewhere along the line for the values in the sidebar, these are reintroduced 
- Link to the repo in the footer was lost also, reintroduced

## 2021-12-07
### Changed
- Codebase overhauled by [@s0undt3ch](https://github.com/s0undt3ch) to use a blueprint which allows the multiple dashboards to be served 
- Hard coded URLs swapped to use `url_for` by  [@s0undt3ch](https://github.com/s0undt3ch)

## 2021-12-02
### Changed
- Timeframes no longer exist, instead there are start dates and end dates with a daterangepicker on all appropriate pages

## 2021-11-29
### Changed
- Added a fix for the graph on the dashboard that would only calculate the total - profit over a week and not the other tiemframes

## 2021-11-28
### Added
- Route added for balance projection over 365 days at fixed and average %, this can be accessed from the main dashboard page above the line chart
- Average down calculator now available on coin page (only shows when a coin has no more buy positions)

### Changed
- Changed 'Week' to be previous 7 days rather than 8

## 2021-11-21
### Changed
- Fixed issue that was reintroduced (new positions wouldn't show unless a buy/sell was present)
- Fixed issue where `positions` were not returned by API causing error which is now handled more gracefully
- Fixed issue where some coins were automatically hidden on the graph when too many were present - now resizes to fit

## 2021-11-20
### Added
- Table on history page to show all of the previously saved CSV files
- favicon2 (warning emoji) will replace favicon (rocket emoji) when no buys are left on an active coin
- New route added for `All positions` that shows every coin in one place. Accordian can be opened to see the orders relating to each position.

### Changed
- Active coins now have a red background if there are no buys left on an active coin
- Cards on the front page (profit for today, week, month and PnL) are now hyperlinked to their respective pages

## 2021-11-19
### Added
- Balance now displays on profit chart as requested by [@s0undt3ch](https://github.com/s0undt3ch)

## 2021-11-18
### Added
- Tooltips added on left menu for PBR, BUY, SELL by [@s0undt3ch](https://github.com/s0undt3ch)

### Changed
- Fixed issue highlighted by [@hungud](https://github.com/hungud) with no longer used route for `/coins/`
- Fixed issue with calculation for profit not including `FUNDING_FEES` so now if a coin is stuck and funding fees, it will show negative impact
- Changed the tooltips to use the bootstrap theme

## 2021-11-16
### Changed
- Fixed issue highlighted by [@ltorres6](https://github.com/ltorres6) where coins wouldn't show as active unless there was an open buy/sell order and realised PNL

## 2021-11-14
### Added
- Income history page added, allows you to pull CSVs out of the income database table depending on the common timeframes used (today, week...)
- Totals and PBR added to the sidebar for active coins

## 2021-11-13
### Added
- Buy and sell position indicators for each active coin in the menu
- Mark price now displays on the active coin pages

### Changed
- SQL for latest order switched from `ORDER BY / LIMIT` to `MAX()`
- Repeated code removed for lastupdate and put into `get_lastupdate()`
- All of the headline values for profit will display with 2dp i.e. `4.00` instead of `4.0`
- Rounded the position price on coins page to 5dp maximum
- Split the coin menu into `active` and `inactive`

## 2021-11-12
### Changed
- Auto refresh after 60 seconds now on a page by page basis rather than whole site
- Line charts now have padding at the top for when you hit ATH profits and can't see the numbers because the chart cuts off
- Positions/orders now uses datatables instead of a list within a card meaning it is now sortable and each value is individually identifiable (was previously volume@price)

## 2021-11-11
### Added
- Added `favicon.ico` and referenced in base template
- Add last update time to `dashboard` and `coin` pages (pulls the last order creation time)
- Added automatic page refresh every 60 seconds (to show the latest information from data pull)

### Changed
- Renamed variable `result` to `all_fees` in `app.py`
- Merge the scraper into the app through the asyncio library - completed in PR by [@s0undt3ch](https://github.com/s0undt3ch)

## 2021-11-10
### Added
- Added this `CHANGELOG.md`

## 2021-11-09
### Added
- Added recommended command for docker in `README.md` [@MiKE0#7135]
- Added hyperlinks to the bar charts to redirect to the page for that specific coin
- Added numeric values on top of the line and within bar charts

### Changed
- Changed the required libraries to include `requests` in `README.md` [@MiKE0#7135]

## 2021-11-08
### Added
- `README.md` and `LICENSE.md` created
- `app.py` and `scraper.py` python files uploaded
- `config.json` json file uploaded
- `static/styles/dashboard.css`and `static/styles/sidebars.css` CSS files uploaded
- `templates/base.html`, `templates/coin.html`, `templates/date.html`, `templates/error.html`, `templates/home.html` and `templates/showall.html` HTML files uploaded

### Changed
- README.md updated with suggested running instructions
