/* bot_acciones.js: acciones del bot desde cualquier página (antes, el script de la rueda #menu_admin de base.html).
   Mismos IDs y data-bot-* que tenía la rueda: los botones [data-bot-accion] pueden estar en Inicio (control rápido), en
   Bot y en Estrategia. Confirmación (#botActionConfirmModal) y progreso del job (#botJobModal) siguen en base.html, para
   retomar una acción en curso desde cualquier página. Además pinta la píldora del bot ([data-bot-pildora]) y el punto
   del ítem Bot ([data-bot-punto]) con los cuatro estados que arma el server (estado_bot de /api/bot/status).
   Los presets bajo/medio/alto no se ofrecen (spec bot-estrategia S3); la API /api/bot/start {riesgo} se conserva.
   Herramienta, no asesor. */
(function () {
    'use strict';
    const confirmEl = document.getElementById('botActionConfirmModal');
    const jobEl = document.getElementById('botJobModal');
    if (!confirmEl || !jobEl || !window.bootstrap) return;

    const q = (sel) => document.querySelector(sel);
    const respuesta = q('[data-bot-aviso]');
    const estadoEl = q('[data-bot-panel="estado"]');
    const disabledLi = q('[data-bot-panel="deshabilitado"]');
    const disabledMsg = q('[data-bot-panel="deshabilitado-msg"]');
    const gracefulNota = q('[data-bot-panel="graceful-nota"]');
    const perfilSelect = document.getElementById('botPerfilSelect');
    const perfilDesc = q('[data-bot-panel="perfil-desc"]');
    const perfilVigente = q('[data-bot-panel="perfil-vigente"]');
    const tweInput = document.getElementById('botTweInput');
    const tweAdvertencia = document.getElementById('botTweAdvertencia');
    let tweEditado = false;
    const buttons = () => document.querySelectorAll('[data-bot-accion]');
    const confirmCheck = document.getElementById('botActionConfirmCheck');
    const confirmAccept = document.getElementById('botActionConfirmAccept');
    let pending = null;
    let lastStatus = null;
    let avisoTimer = null;
    const jobQ = (k) => jobEl.querySelector(`[data-bot-job="${k}"]`);
    const JOB_KEY = 'fbBotJobId';
    const TITULOS = { start: 'Iniciar', graceful: 'Parada gradual', apagar: 'Apagar', perfil: 'Perfil' };
    // Números que se muestran (no los que se mandan): es-AR.
    const n = (v) => (window.BB && window.BB.fmt ? window.BB.fmt.num(v) : String(v));
    const CHECK_APAGAR = 'Entiendo que se cierran TODAS las posiciones a mercado y se cancelan TODAS las órdenes abiertas en Binance Futures, y que no se puede deshacer.';
    const AVISO_TWE_ALTA = (t) => `TWE ${n(t)} está por encima de 4: en el backtest 2021–2026 aparecen liquidaciones desde TWE 6, y los perfiles medidos son TWE 3 y TWE 4.`;
    const ICONOS = { ok: '✓', error: '✗', omitido: '–', pendiente: '·' };
    const ESTADOS_PASO = { ok: 'listo', error: 'error', omitido: 'omitido', pendiente: 'pendiente', en_curso: 'en curso' };
    let jobId = null;       // job que muestra el modal
    let jobActivo = false;  // mientras sea true los botones quedan bloqueados
    let jobCancelable = false;  // espera a forager: Apagar sigue disponible y la corta
    let modalOculto = false;    // el modal se cerró con el job en curso (se sigue consultando)
    const NOTA_EN_CURSO = 'No cierres esta página si podés; si la recargás, el progreso se retoma.';
    const NOTA_CANCELABLE = 'La espera a forager es informativa: podés cerrar esta ventana o usar Apagar, que la corta.';
    let pollTimer = null;
    let ultimoEstadoAnunciado = '';
    let terminoUnJob = false;  // al cerrar el resumen, las páginas con [data-bot-recargar] se recargan

    function guardarJob(id) {
        try { if (id) sessionStorage.setItem(JOB_KEY, id); else sessionStorage.removeItem(JOB_KEY); } catch (e) { /* sin storage */ }
    }
    function jobGuardado() {
        try { return sessionStorage.getItem(JOB_KEY); } catch (e) { return null; }
    }

    function csrfToken() {
        const m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.getAttribute('content') : '';
    }

    function aviso(tipo, msg) {
        if (!respuesta) return;
        respuesta.className = `bb-bot-aviso ${tipo}`.trim();
        const p = document.createElement('p');
        p.textContent = msg;
        respuesta.replaceChildren(p);
        clearTimeout(avisoTimer);
        avisoTimer = setTimeout(limpiarAviso, 8000);
    }
    function limpiarAviso() {
        if (!respuesta) return;
        respuesta.replaceChildren();
        respuesta.className = 'bb-bot-aviso';
    }
    if (respuesta) respuesta.addEventListener('click', limpiarAviso);

    // ---- Píldora y punto del menú (cuatro estados: el server ya los calculó en estado_bot) ----
    const ESTADOS = {
        operando: { texto: 'Operando', cls: 'is-operando', icono: 'fas fa-check-circle', punto: 'ok', sr: 'operando' },
        gradual: { texto: 'Parada gradual', cls: 'is-gradual', icono: 'fas fa-exclamation-triangle', punto: 'aviso', sr: 'en parada gradual' },
        detenido: { texto: 'Detenido', cls: 'is-parado', icono: 'fas fa-stop-circle', punto: 'gris', sr: 'detenido, sin posiciones ni órdenes' },
        expuesto: { texto: '', cls: 'is-detenido', icono: 'fas fa-times-circle', punto: 'peligro', sr: 'detenido con posiciones u órdenes sin bot' },
        sin_datos: { texto: 'Bot sin datos', cls: 'is-parado', icono: 'fas fa-question-circle', punto: 'gris', sr: 'sin datos del estado' },
    };
    function textoExpuesto(expo) {
        const p = (expo && expo.posiciones) || 0;
        const o = (expo && expo.ordenes) || 0;
        return p ? `Sin bot · ${p} ${p === 1 ? 'posición' : 'posiciones'}` : `Sin bot · ${o} ${o === 1 ? 'orden' : 'órdenes'}`;
    }
    function pintarEstadoBot(st) {
        const clave = (st && st.estado_bot) || 'sin_datos';
        const e = ESTADOS[clave] || ESTADOS.sin_datos;
        const texto = clave === 'expuesto' ? textoExpuesto(st.exposicion) : e.texto;
        document.querySelectorAll('[data-bot-pildora]').forEach((a) => {
            a.className = `bb-pildora-bot ${e.cls}`;
            a.setAttribute('data-bot-estado', clave);
            const i = a.querySelector('i');
            if (i) i.className = e.icono;
            const t = a.querySelector('[data-bot-pildora-texto]');
            if (t && t.textContent !== texto) t.textContent = texto;
        });
        document.querySelectorAll('[data-bot-punto]').forEach((s) => {
            s.className = `bb-punto bb-punto--${e.punto}`;
            s.setAttribute('data-bot-estado', clave);
            const sr = s.nextElementSibling;
            if (sr && sr.hasAttribute('data-bot-punto-texto')) sr.textContent = `bot ${e.sr}`;
        });
    }

    // Resumen de Apagar: órdenes canceladas, posiciones cerradas, errores y lo que quedó abierto.
    function construirResumen(msg, r, warning) {
        const nodos = [];
        const add = (tag, texto) => {
            const el = document.createElement(tag);
            el.textContent = texto;
            nodos.push(el);
            return el;
        };
        const lista = (titulo, items) => {
            if (!items || !items.length) return;
            add('p', titulo);
            const ul = document.createElement('ul');
            items.forEach((t) => {
                const li = document.createElement('li');
                li.textContent = t;
                ul.appendChild(li);
            });
            nodos.push(ul);
        };
        add('p', msg);
        if (warning) add('p', warning);
        const canceladas = r.ordenes_canceladas || [];
        const cerradas = r.posiciones_cerradas || [];
        add('p', `Órdenes canceladas: ${canceladas.reduce((s, o) => s + (o.cantidad || 0), 0)} · posiciones cerradas: ${cerradas.length}`);
        lista('Canceladas:', canceladas.map((o) => `${o.symbol} (${o.tipo}): ${o.cantidad}`));
        lista('Cerradas:', cerradas.map((p) => `${p.symbol} ${p.lado} ${p.cantidad} (${p.modo}, ${p.ordenes} orden/es)`));
        const rest = r.restante || {};
        const quedan = [].concat(
            (rest.posiciones || []).map((p) => `Posición ${p.symbol} ${p.lado} ${p.cantidad}`),
            (rest.ordenes || []).map((o) => `Órdenes ${o.symbol}: ${o.cantidad}`),
            (rest.ordenes_algo || []).map((o) => `Órdenes condicionales ${o.symbol}: ${o.cantidad}`),
        );
        lista('QUEDÓ ABIERTO (revisar en Binance):', quedan);
        if (!r.completo && rest.verificado === false) add('p', 'No se pudo verificar el estado final en Binance: revisar a mano.');
        lista('Errores:', r.errores || []);
        return nodos;
    }

    // Un botón puede traer su propio motivo de deshabilitado del server (data-bot-bloqueado): queda deshabilitado.
    function setButtonsDisabled(disabled) {
        const bloquear = disabled || jobActivo;
        buttons().forEach((b) => {
            // Durante la espera a forager Apagar queda habilitado: el backend corta la espera.
            const accion = b.getAttribute('data-bot-accion');
            const apagarPermitido = !disabled && jobActivo && jobCancelable && (accion === 'apagar' || accion === 'cerrar_todo');
            b.disabled = (bloquear && !apagarPermitido) || b.hasAttribute('data-bot-bloqueado');
        });
        if (perfilSelect) perfilSelect.disabled = bloquear;
        if (tweInput) tweInput.disabled = bloquear;
    }
    const boton = (accion) => document.querySelector(`[data-bot-accion="${accion}"]`);
    const deshabilitar = (accion) => { const b = boton(accion); if (b) b.disabled = true; };

    // ---- Perfiles y TWE manual (Estrategia; docs/perfiles.md) ----
    function tweLimites() {
        const t = (lastStatus && lastStatus.twe_manual) || { min: '0.5', max: '8', paso: '0.25', advertencia: '4' };
        return { min: Number(t.min), max: Number(t.max), paso: Number(t.paso), adv: Number(t.advertencia) };
    }
    function tweValida(v) {
        const l = tweLimites();
        if (!Number.isFinite(v)) return 'Ingresá un número.';
        if (v < l.min || v > l.max) return `Tiene que estar entre ${n(l.min)} y ${n(l.max)}.`;
        if (Math.abs(v / l.paso - Math.round(v / l.paso)) > 1e-9) return `Va en pasos de ${n(l.paso)}.`;
        return null;
    }
    function actualizarTwe() {
        if (!tweInput || !tweAdvertencia) return;
        const v = tweInput.value === '' ? NaN : Number(tweInput.value);
        const error = tweInput.value === '' ? null : tweValida(v);
        const alta = !error && v > tweLimites().adv;
        tweAdvertencia.textContent = error ? error : (alta ? AVISO_TWE_ALTA(v) + ' Se pide confirmación.' : '');
        tweAdvertencia.classList.toggle('d-none', !error && !alta);
        tweAdvertencia.classList.toggle('text-danger', !!error);
        tweAdvertencia.classList.toggle('text-warning', !error && alta);
        tweInput.classList.toggle('is-invalid', !!error);
    }
    if (tweInput) tweInput.addEventListener('input', () => { tweEditado = true; actualizarTwe(); });
    function renderPerfil() {
        if (!perfilSelect || !perfilDesc) return;
        const st = lastStatus || {};
        const p = (st.perfiles || {})[perfilSelect.value];
        if (!p) { perfilDesc.textContent = ''; return; }
        const num = p.numeros || {};
        perfilDesc.textContent = `${p.resumen} ${p.riesgo} Backtest OOS (ventanas de 12 meses, sobre los 10.000 de futuros): mediana ${num.mediana_pct} %, peor ${num.peor_pct} %; peor drawdown ${num.peor_dd_pct} %; capital en riesgo ${num.capital_en_riesgo}. No es una promesa.`;
    }
    if (perfilSelect) perfilSelect.addEventListener('change', renderPerfil);
    function actualizarBotones() {
        setButtonsDisabled(!lastStatus || !lastStatus.enabled);
        if (lastStatus && lastStatus.enabled && !jobActivo) aplicarRestricciones(lastStatus);
    }

    // Lo que el estado deshabilita (además del job en curso): sin config, sin modos, tope del rescate sin reconocer.
    function aplicarRestricciones(status) {
        if (status.n_shorts !== 0 && status.config_available) {
            deshabilitar('perfil');
            if (perfilVigente) perfilVigente.textContent = `Perfiles no disponibles: se midieron solo con longs y la config de forager tiene n_shorts ${status.n_shorts}.`;
        }
        if (!status.config_available) {
            ['perfil', 'twe', 'iniciar', 'reanudar', 'reiniciar'].forEach(deshabilitar);
        }
        if (status.rescate_start_bloqueado) {
            // Tope de pérdida del rescate sin reconocer: el backend responde 409 a START (y a los perfiles).
            ['perfil', 'twe', 'iniciar', 'reanudar', 'reiniciar'].forEach(deshabilitar);
            if (disabledLi && disabledMsg) {
                disabledLi.classList.remove('d-none');
                disabledMsg.textContent = status.rescate_start_bloqueado;
            }
        }
        if (!status.modes_supported || !status.config_available) {
            deshabilitar('graceful');
            if (gracefulNota) gracefulNota.textContent = 'No disponible: requiere forager con soporte de long_mode/short_mode en el HJSON.';
        }
    }

    function render(status) {
        lastStatus = status;
        pintarEstadoBot(status);
        if (!status || !status.enabled) {
            if (estadoEl) estadoEl.textContent = 'Panel deshabilitado';
            if (disabledMsg) disabledMsg.textContent = (status && status.message) || 'Control del bot no configurado.';
            if (disabledLi) disabledLi.classList.remove('d-none');
            setButtonsDisabled(true);
            return;
        }
        if (disabledLi) disabledLi.classList.toggle('d-none', !status.message);
        if (disabledMsg) disabledMsg.textContent = status.message || '';
        setButtonsDisabled(false);
        if (jobActivo) return;  // con una acción en curso no se habilita nada
        if (perfilSelect) {
            const vigente = status.perfil_vigente;
            if (vigente && status.perfiles && status.perfiles[vigente]) {
                perfilSelect.value = vigente;
                if (perfilVigente) perfilVigente.textContent = `Vigente: ${status.perfiles[vigente].nombre}.`;
            } else if (perfilVigente) {
                perfilVigente.textContent = status.perfil_motivo
                    ? `Vigente: Personalizado. ${status.perfil_motivo}.`
                    : 'Vigente: Personalizado (la config no coincide con un perfil medido).';
            }
            renderPerfil();
        }
        if (tweInput && !tweEditado && status.twe_long !== null && status.twe_long !== undefined) {
            tweInput.value = status.twe_long;
        }
        actualizarTwe();
        aplicarRestricciones(status);
    }

    function cargarEstado() {
        return fetch('/api/bot/status?rapido=1', { cache: 'no-cache', credentials: 'same-origin' })
            .then((r) => {
                if (r.redirected) throw new Error('Sesión expirada: volvé a iniciar sesión.');
                return r.json().then((d) => ({ ok: r.ok, d }));
            })
            .then(({ ok, d }) => {
                // Con Docker caído el JSON trae estado_bot "sin_datos": la píldora lo dice y los botones se bloquean.
                window.dispatchEvent(new CustomEvent('fb-bot-status', { detail: d }));
                if (!ok && d.enabled !== false) throw new Error(d.error || 'Error al consultar el estado.');
            })
            .catch((e) => {
                if (estadoEl) estadoEl.textContent = e.message || 'Error al consultar el estado.';
                setButtonsDisabled(true);
            });
    }
    // El estado llega de este archivo, de en_vivo.js (Inicio, cada 15 s) o de la página que ya lo trae al render.
    window.addEventListener('fb-bot-status', (e) => { if (e.detail) render(e.detail); });

    function ultimoArranque() {
        return (lastStatus && lastStatus.container && lastStatus.container.running) ? 'REINICIARÁ' : 'INICIARÁ';
    }

    const ACCIONES = {
        twe: () => {
            const v = Number(tweInput.value);
            const alta = v > tweLimites().adv;
            const body = { twe_long: v };
            if (alta) body.confirmar_twe_alta = true;
            return {
                url: '/api/bot/start',
                body,
                titulo: `Confirmar TWE long ${n(v)}`,
                texto: `Se escribirá twe_long ${n(v)} en la config de forager (modo normal; twe_short 1 si n_shorts es 0) y passivbot se ${ultimoArranque()}: forager tarda ~90 s en volver a operar los pares.` + (alta ? '\n\n' + AVISO_TWE_ALTA(v) + ' Herramienta, no asesor.' : ''),
                check: alta,
                checkLabel: `Entiendo que TWE ${n(v)} está por encima de lo medido en el backtest y que puede liquidar la cuenta.`,
            };
        },
        graceful: () => ({
            url: '/api/bot/stop',
            body: { modo: 'graceful' },
            titulo: 'Confirmar parada gradual',
            texto: 'passivbot se reiniciará en parada gradual (graceful stop): no abrirá nuevas posiciones y seguirá gestionando las abiertas hasta cerrarlas.',
            check: false,
        }),
        // Reiniciar operando: la misma API que Reanudar; se confirma sin casilla (~90 s sin gestionar órdenes).
        reiniciar: () => ({
            titulo: 'Reiniciar passivbot',
            texto: 'passivbot se reinicia con la estrategia guardada (sin cambiar TWE, pares ni rescate): forager tarda ~90 s en volver a operar los pares y mientras tanto no gestiona órdenes.',
            check: false,
            boton: 'Reiniciar',
            alAceptar: () => pedirInicio(),
        }),
        // Detenido con posiciones u órdenes sin bot: es el mismo Apagar (funciona con passivbot ya detenido).
        cerrar_todo: () => ({
            url: '/api/bot/stop',
            body: { modo: 'apagar' },
            titulo: 'Cerrar todo en Binance',
            texto: 'passivbot ya está detenido (forager queda en parada gradual). Se cancelarán TODAS las órdenes abiertas (incluidas las condicionales) y se cerrarán TODAS las posiciones de USDⓈ-M Futures A MERCADO, en ambos lados. Es irreversible: el precio de cierre puede ser peor que el esperado (slippage) y se realizan las ganancias o pérdidas actuales.',
            check: true,
            boton: 'Cerrar todo',
        }),
        apagar: () => ({
            url: '/api/bot/stop',
            body: { modo: 'apagar' },
            titulo: 'Confirmar apagado de passivbot',
            texto: 'Primero deja forager en parada gradual (si algo lo vuelve a arrancar, no abre pares nuevos). Después se detendrá passivbot y se cancelarán TODAS las órdenes abiertas (incluidas las condicionales) y se cerrarán TODAS las posiciones de USDⓈ-M Futures A MERCADO, en ambos lados. Es irreversible: el precio de cierre puede ser peor que el esperado (slippage) y se realizan las ganancias o pérdidas actuales.',
            check: true,
        }),
    };

    function pedirConfirmacion(accion) {
        pending = typeof accion === 'string' ? ACCIONES[accion]() : accion;
        confirmEl.querySelector('[data-bot-confirm="titulo"]').textContent = pending.titulo;
        // Un plan con diff (Estrategia, Versiones) pide el modal ancho: ancho 'lg'.
        const dialogo = confirmEl.querySelector('.modal-dialog');
        if (dialogo) dialogo.classList.toggle('modal-lg', pending.ancho === 'lg');
        const textoEl = confirmEl.querySelector('[data-bot-confirm="texto"]');
        textoEl.textContent = pending.texto || '';
        textoEl.classList.toggle('d-none', !pending.texto);
        const detalle = confirmEl.querySelector('[data-bot-confirm="detalle"]');
        if (detalle) {
            detalle.replaceChildren(...(pending.nodos || []));
            detalle.classList.toggle('d-none', !(pending.nodos && pending.nodos.length));
        }
        confirmEl.querySelector('[data-bot-confirm="check-label"]').textContent = pending.checkLabel || CHECK_APAGAR;
        confirmEl.querySelector('[data-bot-confirm="check-wrap"]').classList.toggle('d-none', !pending.check);
        confirmCheck.checked = false;
        confirmAccept.disabled = pending.check;
        confirmAccept.textContent = pending.boton || 'Confirmar';
        bootstrap.Modal.getOrCreateInstance(confirmEl).show();
    }
    confirmCheck.addEventListener('change', () => {
        if (pending && pending.check) confirmAccept.disabled = !confirmCheck.checked;
    });

    // ---- Progreso de la acción (job) ----

    function mensajeFinal(job) {
        const d = job.resultado || {};
        if (job.estado === 'interrumpido') return { tipo: 'error', msg: d.error || 'La acción se interrumpió.' };
        if (d.resumen) {
            const okCierre = job.estado === 'ok' && d.ok;
            const algoSinVerificar = d.resumen.algo_verificado === false;
            let msg = d.error || 'Cierre parcial.';
            if (okCierre) {
                msg = algoSinVerificar
                    ? 'passivbot detenido. Se cerraron las posiciones y órdenes normales, pero las órdenes condicionales NO se pudieron verificar (demo/testnet): revisar en Binance.'
                    : 'passivbot detenido. Se cerraron todas las posiciones y órdenes.';
            }
            return { tipo: okCierre && !algoSinVerificar && !d.warning ? 'bien' : 'error', msg };
        }
        if (job.estado !== 'ok' || !d.ok) return { tipo: 'error', msg: d.error || 'La acción falló.' };
        let msg = 'Acción aplicada.';
        if (d.warning) msg += ' ' + d.warning;
        return { tipo: 'bien', msg };
    }

    function tituloJob(job) {
        const pr = job.params || {};
        if (pr.reanudar) return pr.como === 'reanudar' ? 'Reanudar' : (pr.como === 'reiniciar' ? 'Reiniciar' : 'Iniciar');
        if (job.accion === 'estrategia') {
            const cab = pr.volver ? 'Volver a una versión' : (pr.perfil ? `Perfil ${pr.perfil}` : 'Estrategia');
            return pr.twe_long !== undefined ? `${cab}: TWE ${n(pr.twe_long)}, ${pr.n_longs} a la vez` : cab;
        }
        const detalle = pr.riesgo ? ` (riesgo ${pr.riesgo})` : (pr.twe_long !== undefined && pr.twe_long !== null ? ` (TWE ${n(pr.twe_long)})` : (pr.perfil ? ` ${(lastStatus && lastStatus.perfiles && lastStatus.perfiles[pr.perfil] && lastStatus.perfiles[pr.perfil].nombre) || pr.perfil}` : ''));
        return `${TITULOS[job.accion] || job.accion}${detalle}`;
    }

    function renderJob(job) {
        const terminado = job.estado !== 'en_curso';
        jobQ('titulo').textContent = tituloJob(job);
        const pct = Math.max(0, Math.min(100, job.porcentaje || 0));
        const barra = jobQ('barra');
        barra.style.width = pct + '%';
        barra.textContent = pct + '%';
        barra.setAttribute('aria-valuenow', String(pct));
        barra.classList.toggle('progress-bar-animated', !terminado);
        barra.classList.toggle('progress-bar-striped', !terminado);
        barra.classList.toggle('bg-success', job.estado === 'ok');
        barra.classList.toggle('bg-danger', terminado && job.estado !== 'ok');

        const ol = jobQ('pasos');
        const items = (job.pasos || []).map((p) => {
            const li = document.createElement('li');
            li.setAttribute('data-estado', p.estado);
            const icono = document.createElement('span');
            icono.className = 'bot-job-icono';
            icono.setAttribute('aria-hidden', 'true');
            if (p.estado === 'en_curso') {
                const sp = document.createElement('span');
                sp.className = 'spinner-border spinner-border-sm';
                sp.setAttribute('role', 'presentation');
                icono.appendChild(sp);
            } else {
                icono.textContent = ICONOS[p.estado] || '·';
            }
            const cuerpo = document.createElement('span');
            const titulo = document.createElement('span');
            let texto = p.titulo;
            // La espera a forager muestra su cuenta regresiva en el detalle (no como x/y segundos).
            if (p.progreso && p.progreso.total && p.clave !== 'forager') texto += ` (${p.progreso.actual}/${p.progreso.total})`;
            titulo.textContent = texto;
            const sr = document.createElement('span');
            sr.className = 'visually-hidden';
            sr.textContent = ` — ${ESTADOS_PASO[p.estado] || p.estado}`;
            cuerpo.append(titulo, sr);
            if (p.detalle) {
                const det = document.createElement('span');
                det.className = 'bot-job-detalle';
                det.textContent = p.detalle;
                cuerpo.appendChild(det);
            }
            li.append(icono, cuerpo);
            return li;
        });
        ol.replaceChildren(...items);

        // aria-live: anunciar solo cambios de paso o el final, no cada segundo de la cuenta regresiva.
        const actual = (job.pasos || []).find((p) => p.estado === 'en_curso');
        const anuncio = terminado
            ? (job.estado === 'ok' ? 'Acción terminada.' : (job.estado === 'interrumpido' ? 'Acción interrumpida.' : 'La acción terminó con error.'))
            : (actual ? `En curso: ${actual.titulo}` : 'En curso…');
        if (anuncio !== ultimoEstadoAnunciado) {
            jobQ('estado').textContent = anuncio;
            ultimoEstadoAnunciado = anuncio;
        }

        const resumenEl = jobQ('resumen');
        const cerrar = jobQ('cerrar');
        const apagarBtn = jobQ('apagar');
        const cancelable = !terminado && job.cancelable === true;
        if (cancelable !== jobCancelable) {
            jobCancelable = cancelable;
            actualizarBotones();
        }
        apagarBtn.classList.toggle('d-none', !cancelable);
        apagarBtn.disabled = !cancelable;
        if (terminado) {
            const { tipo, msg } = mensajeFinal(job);
            const d = job.resultado || {};
            const nodos = d.resumen ? construirResumen(msg, d.resumen, d.warning) : [(() => { const el = document.createElement('p'); el.textContent = msg; return el; })()];
            resumenEl.replaceChildren(...nodos);
            resumenEl.className = `bot-job-resumen mt-3 alert ${tipo === 'bien' ? 'alert-success' : 'alert-danger'}`;
            jobQ('nota').textContent = '';
            cerrar.disabled = false;
        } else {
            resumenEl.replaceChildren();
            resumenEl.className = 'bot-job-resumen mt-3 d-none';
            jobQ('nota').textContent = cancelable ? NOTA_CANCELABLE : NOTA_EN_CURSO;
            cerrar.disabled = !cancelable;
        }
    }

    function terminarJob(job) {
        jobActivo = false;
        jobCancelable = false;
        terminoUnJob = true;
        clearTimeout(pollTimer);
        pollTimer = null;
        const { tipo, msg } = mensajeFinal(job);
        aviso(tipo, msg);
        // Si el modal se había cerrado durante la espera, volver a mostrarlo con el resumen
        // (salvo que el usuario esté confirmando otra acción).
        if (modalOculto && !confirmEl.classList.contains('show')) {
            modalOculto = false;
            bootstrap.Modal.getOrCreateInstance(jobEl).show();
        }
        refrescar();
    }

    // Después de una acción: el estado nuevo para la píldora y los botones; en Inicio, además, los bloques en vivo
    // (el control rápido) sin esperar 15 s.
    function refrescar() {
        cargarEstado();
        if (window.BB && window.BB.vivo && typeof window.BB.vivo.actualizar === 'function') window.BB.vivo.actualizar();
    }

    function pedirJob(id) {
        return fetch(`/api/bot/jobs/${encodeURIComponent(id)}`, { cache: 'no-cache', credentials: 'same-origin' })
            .then((r) => {
                if (r.redirected) {
                    const err = new Error('Sesión expirada: volvé a iniciar sesión para ver el progreso.');
                    err.sesion = true;
                    throw err;
                }
                if (r.status === 404) return null;
                return r.json();
            });
    }

    function poll() {
        if (!jobId) return;
        const id = jobId;
        pedirJob(id)
            .then((job) => {
                if (id !== jobId) return;
                if (!job) {
                    jobQ('estado').textContent = 'No se encontró la acción (vencida o reiniciada). Revisá el estado del bot.';
                    jobQ('cerrar').disabled = false;
                    jobActivo = false;
                    cargarEstado();
                    return;
                }
                renderJob(job);
                if (job.estado === 'en_curso') {
                    pollTimer = setTimeout(poll, 1000);
                } else {
                    terminarJob(job);
                }
            })
            .catch((e) => {
                if (e && e.sesion) {
                    // Sin sesión no se puede seguir: dejar cerrar el modal (la acción sigue en el servidor).
                    jobQ('estado').textContent = e.message;
                    jobQ('cerrar').disabled = false;
                    jobActivo = false;
                    return;
                }
                // Un error de red no corta el seguimiento: se reintenta.
                jobQ('estado').textContent = 'Sin conexión con el dashboard: reintentando…';
                pollTimer = setTimeout(poll, 2000);
            });
    }

    function mostrarJob(job) {
        clearTimeout(pollTimer);
        jobId = job.id;
        jobActivo = job.estado === 'en_curso';
        jobCancelable = false;
        modalOculto = false;
        ultimoEstadoAnunciado = '';
        guardarJob(job.id);
        setButtonsDisabled(true);
        renderJob(job);
        bootstrap.Modal.getOrCreateInstance(jobEl).show();
        if (jobActivo) {
            pollTimer = setTimeout(poll, 1000);
        } else {
            terminarJob(job);
        }
    }

    function ocultarJobEnCurso() {
        // El job sigue en el servidor: se sigue consultando y al terminar se muestra el resumen.
        modalOculto = true;
        bootstrap.Modal.getOrCreateInstance(jobEl).hide();
    }

    jobQ('apagar').addEventListener('click', () => {
        if (!jobActivo || !jobCancelable) return;
        // Primero se cierra el modal de progreso (no se apilan modales) y después se confirma.
        jobEl.addEventListener('hidden.bs.modal', () => pedirConfirmacion('apagar'), { once: true });
        ocultarJobEnCurso();
    });

    // Si se cancela la confirmación con un job en curso oculto, volver a mostrar su progreso.
    confirmEl.addEventListener('hidden.bs.modal', () => {
        if (pending && jobId && jobActivo && modalOculto) {
            modalOculto = false;
            bootstrap.Modal.getOrCreateInstance(jobEl).show();
        }
    });

    jobQ('cerrar').addEventListener('click', () => {
        if (jobActivo) {
            if (jobCancelable) ocultarJobEnCurso();
            return;
        }
        jobId = null;
        guardarJob(null);
        bootstrap.Modal.getOrCreateInstance(jobEl).hide();
        // Bot y Estrategia se arman en el server: con el resultado a la vista, se recargan para mostrar lo nuevo.
        if (terminoUnJob && document.querySelector('[data-bot-recargar]')) {
            location.reload();
            return;
        }
        cargarEstado();
    });

    // La acción nueva no arrancó: si había un job en curso con el modal cerrado, se vuelve a él.
    function volverAlJobOculto() {
        if (jobId && modalOculto) {
            jobActivo = true;
            modalOculto = false;
            bootstrap.Modal.getOrCreateInstance(jobEl).show();
            actualizarBotones();
            return;
        }
        jobActivo = false;
        cargarEstado();
    }

    function ejecutar(p) {
        jobActivo = true;
        setButtonsDisabled(true);
        aviso('', 'Procesando…');
        fetch(p.url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
            body: JSON.stringify(p.body),
        })
            .then((r) => {
                if (r.redirected) throw new Error('Sesión expirada: volvé a iniciar sesión.');
                return r.json().then((d) => ({ status: r.status, d }));
            })
            .then(({ status, d }) => {
                if (status === 202 && d.job) {
                    limpiarAviso();
                    mostrarJob(d.job);
                    return;
                }
                if (status === 409 && d.job_id) {
                    // Ya hay una acción en curso: mostrar su progreso.
                    aviso('error', d.error || 'Ya hay una acción en curso.');
                    return pedirJob(d.job_id).then((job) => {
                        if (job) mostrarJob(job);
                        else volverAlJobOculto();
                    });
                }
                throw new Error((d && d.error) || 'La acción falló.');
            })
            .catch((e) => {
                aviso('error', e.message || 'La acción falló.');
                volverAlJobOculto();
            });
    }

    // Al cargar la página: retomar la acción en curso (o mostrar una que quedó interrumpida).
    function retomarJob() {
        return fetch('/api/bot/jobs/activo', { cache: 'no-cache', credentials: 'same-origin' })
            .then((r) => (r.ok && !r.redirected ? r.json() : null))
            .then((d) => {
                if (d && d.job) {
                    mostrarJob(d.job);
                    return;
                }
                const guardado = jobGuardado();
                if (!guardado) return;
                return pedirJob(guardado).then((job) => {
                    if (job && job.estado !== 'ok') mostrarJob(job);
                    else guardarJob(null);
                });
            })
            .catch(() => { /* sin sesión o sin red: no hay nada que retomar */ });
    }

    confirmAccept.addEventListener('click', () => {
        if (!pending) return;
        if (pending.check && !confirmCheck.checked) return;
        const p = pending;
        pending = null;
        bootstrap.Modal.getOrCreateInstance(confirmEl).hide();
        if (typeof p.alAceptar === 'function') {
            // El plan lo arma el server después de cerrar esta confirmación (no se apilan modales).
            confirmEl.addEventListener('hidden.bs.modal', () => p.alAceptar(), { once: true });
            return;
        }
        ejecutar(p);
    });

    // Un plan del servidor (409 con requiere_confirmacion) va a la confirmación; al aceptar se manda con confirmar: true.
    function pedirPlan(url, body, armar, error) {
        setButtonsDisabled(true);
        fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
            body: JSON.stringify(body),
        })
            .then((r) => {
                if (r.redirected) throw new Error('Sesión expirada: volvé a iniciar sesión.');
                return r.json().then((d) => ({ status: r.status, d }));
            })
            .then(({ status, d }) => {
                actualizarBotones();
                if (status === 409 && d.requiere_confirmacion) {
                    pedirConfirmacion(armar(d));
                    return;
                }
                if (status === 202 && d.job) {
                    mostrarJob(d.job);
                    return;
                }
                if (status === 409 && d.job_id) {
                    aviso('error', d.error || 'Ya hay una acción en curso.');
                    return pedirJob(d.job_id).then((job) => { if (job) mostrarJob(job); });
                }
                throw new Error((d && d.error) || error);
            })
            .catch((e) => {
                aviso('error', e.message || error);
                cargarEstado();
            });
    }

    // El plan del perfil lo arma el servidor (forager, contenedor, modos del servicio, números).
    function pedirPlanPerfil() {
        const perfil = perfilSelect.value;
        pedirPlan('/api/bot/perfil', { perfil }, (d) => ({
            url: '/api/bot/perfil',
            body: { perfil, confirmar: true },
            titulo: `Aplicar perfil ${(d.plan && d.plan.nombre) || perfil}`,
            texto: d.error,
            check: true,
            checkLabel: 'Entiendo qué cambia, que passivbot se reinicia y que los números son de un backtest, no una promesa.',
        }), 'No se pudo preparar el perfil.');
    }

    // ---- Iniciar / Reanudar / Reiniciar con la estrategia guardada (POST /api/bot/start {reanudar: true}) ----
    const el = (tag, cls, texto) => {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (texto !== undefined && texto !== null) e.textContent = texto;
        return e;
    };
    const lista = (v) => (Array.isArray(v) ? (v.length ? v.join(', ') : 'todos (lista vacía)') : (v === null || v === undefined ? '—' : String(v)));
    const valor = (clave, v) => (clave.startsWith('pares') ? lista(v) : (v === null || v === undefined ? '—' : n(v)));

    // Resumen del plan: qué va a operar contra el último arranque, y los avisos (todo con textContent).
    function nodosPlan(plan) {
        const nodos = [];
        const v = plan.vigente || {};
        const antes = (plan.ultimo && plan.ultimo.resumen) || null;
        const cambios = {};
        (plan.cambios || []).forEach((c) => { cambios[c.clave] = c; });
        const sec = el('div', 'bb-modal-seccion');
        sec.appendChild(el('h3', '', 'Qué va a operar'));
        const tabla = el('table', 'bb-diff');
        const thead = el('thead');
        const trh = el('tr');
        const th0 = el('th', '', 'Qué'); th0.scope = 'col';
        const th1 = el('th', '', 'Último arranque'); th1.scope = 'col';
        if (plan.ultimo && plan.ultimo.ts) th1.appendChild(el('small', '', `${plan.ultimo.ts.replace('T', ' ').slice(0, 16)} UTC · ${plan.ultimo.usuario || '?'}`));
        const th2 = el('th', '', 'Ahora arranca con'); th2.scope = 'col';
        trh.append(th0, th1, th2);
        thead.appendChild(trh);
        const tbody = el('tbody');
        const filas = [['pares_long', 'Pares', 'approved_symbols_long'], ['n_longs', 'Pares a la vez', 'n_longs'],
            ['twe_long', 'TWE long', 'twe_long']];
        filas.forEach(([clave, titulo, sub]) => {
            const tr = el('tr');
            const th = el('th', '', titulo); th.scope = 'row';
            th.appendChild(el('small', '', sub));
            const tdA = el('td');
            const tdB = el('td');
            const cambio = cambios[clave];
            if (!antes) {
                tdA.textContent = 'sin registro';
                tdB.textContent = valor(clave, v[clave]);
            } else if (cambio) {
                tdA.appendChild(el('del', '', valor(clave, cambio.antes)));
                tdB.appendChild(el('ins', '', valor(clave, cambio.ahora)));
            } else {
                tdA.textContent = valor(clave, antes[clave]);
                tdB.className = 'bb-igual';
                tdB.textContent = `${valor(clave, v[clave])} (igual)`;
            }
            tr.append(th, tdA, tdB);
            tbody.appendChild(tr);
        });
        if (v.lw !== null && v.lw !== undefined) {
            const tr = el('tr');
            const th = el('th', '', 'Límite por par'); th.scope = 'row';
            th.appendChild(el('small', '', 'lw = TWE / pares a la vez'));
            tr.append(th, el('td', '', antes && antes.lw !== undefined && antes.lw !== null ? n(Number(antes.lw).toFixed(2)) : '—'),
                el('td', '', n(Number(v.lw).toFixed(2))));
            tbody.appendChild(tr);
        }
        const modos = plan.modos || {};
        if (modos.long_mode && modos.long_mode !== 'normal') {
            const tr = el('tr');
            const th = el('th', '', 'Modos'); th.scope = 'row';
            th.appendChild(el('small', '', 'long_mode / short_mode'));
            const td = el('td');
            td.appendChild(el('del', '', modos.long_mode));
            const td2 = el('td');
            td2.appendChild(el('ins', '', 'normal'));
            tr.append(th, td, td2);
            tbody.appendChild(tr);
        }
        tabla.append(thead, tbody);
        sec.appendChild(tabla);
        nodos.push(sec);
        const av = el('div', 'bb-modal-seccion');
        av.appendChild(el('h3', '', 'Antes de iniciar'));
        const ul = el('ul', 'bb-avisos');
        (plan.avisos || []).forEach((a) => {
            const li = el('li', `bb-alerta bb-alerta--${a.tipo === 'info' ? 'info' : 'aviso'}`);
            const i = el('i', a.tipo === 'info' ? 'fas fa-info-circle' : 'fas fa-exclamation-triangle');
            i.setAttribute('aria-hidden', 'true');
            const div = el('div');
            div.append(el('strong', '', a.titulo || ''), el('span', '', a.texto || ''));
            li.append(i, div);
            ul.appendChild(li);
        });
        av.appendChild(ul);
        nodos.push(av);
        return nodos;
    }

    function pedirInicio() {
        pedirPlan('/api/bot/start', { reanudar: true }, (d) => {
            const plan = d.plan || {};
            const nombre = { iniciar: 'Iniciar', reanudar: 'Reanudar', reiniciar: 'Reiniciar' }[plan.como] || 'Iniciar';
            return {
                url: '/api/bot/start',
                body: { reanudar: true, confirmar: true, plan_hash: d.plan_hash },
                titulo: `${nombre} passivbot`,
                texto: plan.cambios ? '' : d.error,
                nodos: plan.cambios ? nodosPlan(plan) : null,
                check: !!plan.requiere_casilla,
                checkLabel: 'Revisé lo que va a operar y lo que cambió desde el último arranque.',
                boton: nombre,
            };
        }, 'No se pudo preparar el inicio.');
    }

    function alClick(b) {
        if (b.disabled) return;
        const accion = b.getAttribute('data-bot-accion');
        if (jobEl.classList.contains('show')) {
            // Apagar con el modal de progreso abierto (espera a forager): primero se cierra ese modal, igual que con
            // su botón Apagar.
            if (!jobActivo || !jobCancelable) return;
            jobEl.addEventListener('hidden.bs.modal', () => pedirConfirmacion(accion), { once: true });
            ocultarJobEnCurso();
            return;
        }
        if (accion === 'twe') {
            const error = tweValida(tweInput.value === '' ? NaN : Number(tweInput.value));
            if (error) { aviso('error', `TWE manual: ${error}`); actualizarTwe(); return; }
        }
        if (accion === 'perfil') {
            pedirPlanPerfil();
            return;
        }
        if (accion === 'iniciar' || accion === 'reanudar') {
            pedirInicio();
            return;
        }
        if (!ACCIONES[accion]) return;
        pedirConfirmacion(accion);
    }
    document.addEventListener('click', (e) => {
        const b = e.target.closest('[data-bot-accion]');
        if (b) alClick(b);
    });

    window.BB = window.BB || {};
    window.BB.botAcciones = { cargarEstado, pedirConfirmacion, pedirPlan, aviso };

    setButtonsDisabled(true);
    retomarJob();
    // Perfiles, límites de la TWE y restricciones de los botones: se piden al cargar, con el tope corto del inspect.
    cargarEstado();
})();
