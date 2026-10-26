/* estrategia.js: editor de la estrategia (diseno/bot-estrategia/spec.md § 2.4 y § 3.3).
   Un borrador en el navegador (TWE, pares a la vez y pares) que no se guarda hasta "Revisar y aplicar". Los datos salen
   de GET /api/bot/estrategia (vigente, chequeos por par contra Binance, lugares ocupados, avanzado); el plan, los avisos
   y la regla de exposición los decide el servidor (POST /api/bot/estrategia → 409 con el plan → confirmación con el
   modal del panel → job). Acá solo se muestran y se recalculan las cuentas del borrador para verlas al tocar.
   Todo lo que viene del servidor o de Binance se pinta con textContent. Herramienta, no asesor. */
(function () {
    'use strict';
    const raiz = document.querySelector('[data-estrategia-editor]');
    if (!raiz || !window.fetch) return;
    const q = (k) => raiz.querySelector(`[data-estrategia="${k}"]`);
    const url = raiz.getAttribute('data-url');
    const urlCandidatos = raiz.getAttribute('data-url-candidatos');
    const fmt = window.BB && window.BB.fmt;
    const num = (v, dec) => (v === null || v === undefined || Number.isNaN(v) ? '—'
        : (fmt ? fmt.num(v, dec === undefined ? undefined : { dec }) : String(v)));
    const usdt = (v) => (v === null || v === undefined ? '—' : `${num(v, 2)} USDT`);
    const corto = (s) => (typeof s === 'string' && s.endsWith('USDT') ? s.slice(0, -4) : String(s));
    const MANT = 0.01;  // mantenimiento aproximado de "caída que liquida" (estrategia.MANTENIMIENTO_APROX)
    const ICONOS = { ok: 'fas fa-check-circle', info: 'fas fa-info-circle', aviso: 'fas fa-exclamation-triangle',
        bloquea: 'fas fa-times-circle', sin_datos: 'fas fa-question-circle' };
    const SR = { ok: 'Bien: ', info: '', aviso: 'Aviso: ', bloquea: 'Bloquea: ', sin_datos: 'Sin verificar: ' };
    let srv = null;
    let borrador = null;
    let porPar = {};

    function el(tag, cls, texto) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (texto !== undefined && texto !== null) e.textContent = texto;
        return e;
    }
    function csrf() {
        const m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.getAttribute('content') : '';
    }

    // ---------------------------------------------------------------- cuentas del borrador
    function lugares(pares) {
        const e = srv.exposicion || { con_posicion: [], con_ordenes: [], ignorar: [] };
        const ign = new Set(e.ignorar || []);
        const P = (e.con_posicion || []).filter((s) => !ign.has(s));
        const O = (e.con_ordenes || []).filter((s) => pares.includes(s) && !ign.has(s));
        const ocupados = Array.from(new Set(P.concat(O))).sort();
        const salen = P.filter((s) => !pares.includes(s));
        return { P, O, ocupados, salen, min: Math.max(1, ocupados.length), max: pares.length + salen.length };
    }
    function ajustarN() {
        const l = lugares(borrador.pares);
        borrador.n = Math.min(Math.max(borrador.n, l.min), Math.max(l.max, l.min));
    }
    function derivados(twe, n) {
        const bal = srv.balance && srv.balance.para_dimensionar;
        const lev = Number(srv.leverage) || null;
        const lw = twe / n;
        const margen = lev ? Math.min(100, twe / lev * 100) : null;
        return {
            lw,
            maxPar: bal !== null && bal !== undefined ? bal * lw : null,
            maxTotal: bal !== null && bal !== undefined ? bal * twe : null,
            caida: Math.max(0, (1 / twe - MANT) * 100),
            sinMargen: margen === null ? null : -Math.max(0, 100 - margen),
            margen,
        };
    }
    function cambios() {
        const v = srv.vigente;
        const out = [];
        borrador.pares.filter((p) => !v.pares_long.includes(p)).forEach((p) => out.push(`+${p}`));
        v.pares_long.filter((p) => !borrador.pares.includes(p)).forEach((p) => out.push(`−${p}`));
        if (borrador.n !== v.n_longs) out.push(`pares a la vez ${v.n_longs} → ${borrador.n}`);
        if (Number(borrador.twe) !== Number(v.twe_long)) out.push(`TWE ${num(Number(v.twe_long))} → ${num(borrador.twe)}`);
        return out;
    }

    // ---------------------------------------------------------------- pintar
    function chequeoLi(c, texto) {
        const li = el('li', `bb-chequeo bb-chequeo--${c.estado}`);
        const i = el('i', ICONOS[c.estado] || ICONOS.info);
        i.setAttribute('aria-hidden', 'true');
        const s = el('span');
        if (SR[c.estado]) s.appendChild(el('span', 'bb-sr', SR[c.estado]));
        s.appendChild(document.createTextNode(texto || c.texto));
        li.append(i, s);
        return li;
    }
    // 1.ª compra con la lw del borrador (misma cuenta que estrategia.primera_compra).
    function primeraCompra(c, lw) {
        const d = c.datos || {};
        if (!d.balance || !d.qty_pct || d.min === null || d.min === undefined) return c;
        const compra = d.balance * lw * d.qty_pct;
        let texto = `1.ª compra ${num(compra, 1)} USDT`;
        let estado = 'ok';
        if (compra < d.min) {
            estado = 'aviso';
            const real = d.en_demo && d.min_real !== null && d.min_real !== undefined ? ` / ${num(d.min_real, 2)} real` : '';
            texto += `, menor que el mínimo (${num(d.min, 2)}${d.en_demo ? ' Demo' : ''}${real}): compra el mínimo`;
        } else if (d.en_demo && d.min_real !== null && d.min_real !== undefined && compra < d.min_real) {
            estado = 'aviso';
            texto += `: en real compraría el mínimo, ${num(d.min_real, 2)}`;
        } else {
            texto += ` (mín. ${num(d.min, 2)})`;
        }
        return Object.assign({}, c, { estado, texto });
    }
    const VISIBLES = ['medido', 'config_medida', 'primera_compra', 'minimo'];
    function chequeosDe(r, nuevo, lw) {
        const ul = el('ul', 'bb-chequeos');
        ul.setAttribute('aria-label', `Chequeos de ${r.symbol}`);
        (r.chequeos || []).forEach((c) => {
            const mostrar = VISIBLES.includes(c.clave) || c.estado !== 'ok' || (nuevo && ['config', 'edad'].includes(c.clave));
            if (!mostrar) return;
            ul.appendChild(chequeoLi(c.clave === 'primera_compra' ? primeraCompra(c, lw) : c));
        });
        return ul;
    }
    function ahoraDe(sym, nuevo, libre) {
        const div = el('div', 'bb-par__ahora');
        const a = ((porPar[sym] || {}).ahora) || null;
        if (a && a.posicion > 0) {
            div.appendChild(el('span', '', `${num(a.posicion)} ${corto(sym)}${a.pct_balance !== null && a.pct_balance !== undefined ? ` · ${num(a.pct_balance, 0)} %` : ''}`));
        } else {
            div.appendChild(el('span', '', 'Sin posición'));
        }
        let detalle = 'sin órdenes abiertas';
        if (a && (a.compras || a.ventas)) {
            const partes = [];
            if (a.compras) partes.push(`${a.compras} ${a.compras === 1 ? 'orden de compra abierta' : 'órdenes de compra abiertas'}`);
            if (a.ventas) partes.push(`${a.ventas} ${a.ventas === 1 ? 'venta abierta' : 'ventas abiertas'}`);
            detalle = partes.join(' · ');
        }
        if (nuevo) detalle = libre ? 'entra cuando arranque forager (lugar libre)' : 'espera a que se libere un lugar';
        div.appendChild(el('small', '', detalle));
        return div;
    }
    function filaPar(sym, modo, lw, libre) {
        const r = porPar[sym] || { symbol: sym, chequeos: [] };
        const li = el('li', `bb-par${modo === 'nuevo' ? ' bb-par--nuevo' : ''}${modo === 'sale' ? ' bb-par--sale' : ''}`);
        const nombre = el('div', 'bb-par__nombre');
        const cab = el('span');
        cab.appendChild(el('span', 'bb-par__sym', sym));
        if (modo === 'nuevo') { cab.appendChild(document.createTextNode(' ')); cab.appendChild(el('span', 'bb-etiqueta bb-etiqueta--info', 'nuevo')); }
        if (modo === 'sale') { cab.appendChild(document.createTextNode(' ')); cab.appendChild(el('span', 'bb-etiqueta bb-etiqueta--neutra', 'sale')); }
        const cfg = (r.chequeos || []).find((c) => c.clave === 'config');
        nombre.append(cab, el('small', '', cfg && cfg.estado === 'ok' ? 'config clock propia' : (cfg ? 'config sin verificar' : '')));
        li.appendChild(nombre);
        li.appendChild(ahoraDe(sym, modo === 'nuevo', libre));
        if (modo === 'sale') {
            const ul = el('ul', 'bb-chequeos');
            const conPos = lugares(borrador.pares).salen.includes(sym);
            ul.appendChild(chequeoLi({ estado: conPos ? 'aviso' : 'info' }, conPos
                ? 'Sale con posición: sigue en parada gradual con la misma lw hasta cerrar y ocupa un lugar'
                : 'Sale en el próximo reinicio'));
            li.appendChild(ul);
        } else {
            li.appendChild(chequeosDe(r, modo === 'nuevo', lw));
        }
        const acc = el('div', 'bb-par__accion');
        const b = el('button', 'btn btn-sm btn-outline-secondary', modo === 'sale' ? 'Deshacer' : 'Quitar');
        b.type = 'button';
        b.setAttribute('aria-label', `${modo === 'sale' ? 'Volver a agregar' : 'Quitar'} ${sym}`);
        b.addEventListener('click', () => (modo === 'sale' ? volverAAgregar(sym) : quitar(sym)));
        if (modo !== 'sale' && borrador.pares.length <= 1) {
            b.disabled = true;
            b.title = 'Tiene que quedar al menos un par';
        }
        acc.appendChild(b);
        li.appendChild(acc);
        return li;
    }
    function pintarPares() {
        const ul = q('pares');
        const d = derivados(borrador.twe, borrador.n);
        const l = lugares(borrador.pares);
        let libres = Math.max(0, borrador.n - l.ocupados.length);
        const filas = [];
        const enc = el('li', 'bb-pares__enc');
        enc.setAttribute('aria-hidden', 'true');
        ['Par', 'Ahora', 'Chequeos', ''].forEach((t) => enc.appendChild(el('span', '', t)));
        filas.push(enc);
        borrador.pares.forEach((s) => {
            const nuevo = !srv.vigente.pares_long.includes(s);
            filas.push(filaPar(s, nuevo ? 'nuevo' : '', d.lw, nuevo && libres > 0));
            if (nuevo && libres > 0) libres -= 1;
        });
        srv.vigente.pares_long.filter((s) => !borrador.pares.includes(s)).forEach((s) => filas.push(filaPar(s, 'sale', d.lw)));
        const pie = el('li', 'bb-pares__pie');
        const izq = el('span');
        const cajas = el('span', 'bb-lugares');
        cajas.setAttribute('aria-hidden', 'true');
        for (let i = 0; i < Math.max(borrador.n, l.ocupados.length); i += 1) cajas.appendChild(el('span', i < l.ocupados.length ? 'is-ocupado' : ''));
        izq.appendChild(cajas);
        const ord = l.O.filter((s) => !l.P.includes(s));
        izq.appendChild(document.createTextNode(`${l.ocupados.length} de ${borrador.n} lugares ocupados${ord.length ? ` (${ord.map(corto).join(', ')} por su orden abierta)` : ''}`));
        pie.appendChild(izq);
        const der = el('span', '', '');
        der.style.display = 'flex';
        der.style.gap = '8px';
        der.style.alignItems = 'center';
        der.style.flexWrap = 'wrap';
        const motivo = !srv.manifiesto.usable ? srv.manifiesto.motivo
            : (borrador.pares.length >= srv.max_pares ? `Hasta ${srv.max_pares} pares.` : '');
        if (motivo) der.appendChild(el('span', 'bb-motivo', motivo));
        const agregar = el('button', 'btn btn-outline-secondary');
        agregar.type = 'button';
        const mas = el('i', 'fas fa-plus');
        mas.setAttribute('aria-hidden', 'true');
        agregar.append(mas, document.createTextNode(' Agregar par'));
        agregar.disabled = !!motivo;
        agregar.addEventListener('click', abrirAgregar);
        der.appendChild(agregar);
        pie.appendChild(der);
        filas.push(pie);
        ul.replaceChildren(...filas);
    }
    function tile(titulo, valor, chico, antes) {
        const div = el('div', `bb-derivado${antes !== undefined && antes !== null && antes !== valor ? ' bb-derivado--cambia' : ''}`);
        div.appendChild(el('dt', '', titulo));
        const dd = el('dd');
        if (antes !== undefined && antes !== null && antes !== valor) dd.appendChild(el('span', 'bb-antes', antes));
        dd.appendChild(document.createTextNode(valor));
        dd.appendChild(el('small', '', chico));
        div.appendChild(dd);
        return div;
    }
    function pintarExposicion() {
        const v = srv.vigente;
        const d = derivados(borrador.twe, borrador.n);
        const a = derivados(Number(v.twe_long), v.n_longs);
        const l = lugares(borrador.pares);
        document.getElementById('bbTwe').value = num(borrador.twe);
        document.getElementById('bbNLongs').value = String(borrador.n);
        const ayudaN = document.getElementById('bbNLongsAyuda');
        if (l.min === l.max) {
            ayudaN.textContent = `Queda fijo en ${l.min}: no puede bajar de los lugares ocupados (${l.ocupados.length}: posiciones y órdenes abiertas) ni pasar de ${l.max} (${borrador.pares.length} pares${l.salen.length ? ` + ${l.salen.map(corto).join(', ')} mientras tenga posición` : ''}). Agregando pares se puede rotar.`;
        } else {
            ayudaN.textContent = `De ${l.min} (lugares ocupados) a ${l.max} (${borrador.pares.length} pares${l.salen.length ? ` + ${l.salen.length} que salen con posición` : ''}). Con menos lugares que pares, forager rota cada ${srv.ciclo_min} min.`;
        }
        const bal = srv.balance && srv.balance.para_dimensionar;
        const en = srv.derivados || {};
        const dl = q('derivados');
        dl.replaceChildren(
            tile('Límite por par (lw)', num(d.lw, 2), `TWE ${num(borrador.twe)} ÷ ${borrador.n} a la vez`, num(a.lw, 2)),
            tile('Máximo por par', usdt(d.maxPar), 'balance para dimensionar × lw', usdt(a.maxPar)),
            tile('Máximo total', usdt(d.maxTotal), 'balance × TWE', usdt(a.maxTotal)),
            tile('Caída que liquida, todo al máximo', `≈ ${num(d.caida, 0)} %`, 'desde el precio promedio, pareja en todos los pares y sin rescate', `≈ ${num(a.caida, 0)} %`),
            tile('Sin margen para promediar', d.sinMargen === null ? '—' : `uPnL ≈ ${num(d.sinMargen, 0)} %`, d.margen === null ? '' : `del balance, todo al máximo: el margen inicial ya usa ${num(d.margen, 0)} % (TWE ${num(borrador.twe)} ÷ apalancamiento ${num(Number(srv.leverage))})`),
            tile('En uso ahora', usdt(en.en_uso), `${en.en_uso_pct_balance !== null && en.en_uso_pct_balance !== undefined ? num(en.en_uso_pct_balance, 0) : '—'} % del balance · ${d.maxTotal ? num((en.en_uso || 0) / d.maxTotal * 100, 1) : '—'} % del máximo`),
        );
        q('exposicion-bajada').textContent = `La TWE es cuánto puede llegar a exponerse la cuenta en total, en veces el balance (${bal !== null && bal !== undefined ? usdt(bal) : 'sin balance'}). Se reparte en partes iguales entre los pares que opera a la vez.`;
        const prestamo = srv.balance && srv.balance.prestamo;
        q('exposicion-nota').textContent = `Balance para dimensionar: el de la billetera menos el préstamo del rescate (${prestamo ? `hoy ${usdt(prestamo)}` : 'hoy no hay préstamo'}). Las dos cifras de riesgo suponen todo al máximo. Short apagado (n_shorts 0): los perfiles se midieron solo con longs.`;
    }
    function pintarAvanzado() {
        const filas = (srv.avanzado || []).map((a) => {
            const tr = el('tr');
            const th = el('th', '', a.titulo);
            th.scope = 'row';
            const cand = el('i', 'fas fa-lock bb-candado');
            cand.setAttribute('aria-hidden', 'true');
            th.appendChild(cand);
            const code = el('code', 'bb-diff-codigo d-block', a.clave);
            th.appendChild(code);
            tr.append(th, el('td', '', a.valor), el('td', '', a.efecto));
            return tr;
        });
        q('avanzado').replaceChildren(...filas);
    }
    function pintarBarra() {
        const lista = cambios();
        const barra = q('cambios');
        barra.classList.toggle('d-none', !lista.length);
        q('cambios-titulo').textContent = `${lista.length} ${lista.length === 1 ? 'cambio sin aplicar' : 'cambios sin aplicar'}`;
        q('cambios-lista').textContent = lista.join(' · ');
        if (!lista.length) limpiarBloqueos();
    }
    function pintar() {
        pintarExposicion();
        pintarPares();
        pintarBarra();
    }

    // ---------------------------------------------------------------- borrador
    function quitar(sym) {
        borrador.pares = borrador.pares.filter((p) => p !== sym);
        ajustarN();
        pintar();
    }
    function volverAAgregar(sym) {
        const orden = srv.vigente.pares_long;
        borrador.pares.push(sym);
        borrador.pares.sort((a, b) => {
            const ia = orden.indexOf(a); const ib = orden.indexOf(b);
            return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
        });
        ajustarN();
        pintar();
    }
    function descartar() {
        borrador = { twe: Number(srv.vigente.twe_long), n: srv.vigente.n_longs, pares: srv.vigente.pares_long.slice() };
        pintar();
    }
    function parsearTwe(texto) {
        const v = Number(String(texto).trim().replace(',', '.'));
        if (!Number.isFinite(v)) return null;
        return v;
    }
    function validaTwe(v) {
        const t = srv.twe;
        if (v === null) return 'Ingresá un número.';
        if (v < Number(t.min) || v > Number(t.max)) return `Tiene que estar entre ${num(Number(t.min))} y ${num(Number(t.max))}.`;
        if (Math.abs(v / Number(t.paso) - Math.round(v / Number(t.paso))) > 1e-9) return `Va en pasos de ${num(Number(t.paso))}.`;
        return null;
    }
    raiz.addEventListener('click', (e) => {
        const b = e.target.closest('[data-paso]');
        if (b && borrador) {
            const dir = Number(b.getAttribute('data-dir'));
            if (b.getAttribute('data-paso') === 'twe') {
                const v = Math.round((borrador.twe + dir * Number(srv.twe.paso)) * 100) / 100;
                if (!validaTwe(v)) borrador.twe = v;
            } else {
                const l = lugares(borrador.pares);
                borrador.n = Math.min(l.max, Math.max(l.min, borrador.n + dir));
            }
            pintar();
            return;
        }
        const a = e.target.closest('[data-estrategia-accion]');
        if (!a || !borrador) return;
        if (a.getAttribute('data-estrategia-accion') === 'descartar') descartar();
        if (a.getAttribute('data-estrategia-accion') === 'revisar') revisar();
    });
    document.getElementById('bbTwe').addEventListener('change', (e) => {
        const v = parsearTwe(e.target.value);
        const error = validaTwe(v);
        document.getElementById('bbTweAyuda').textContent = error
            ? `${error} De 0,5 a 8 en pasos de 0,25.`
            : 'De 0,5 a 8 en pasos de 0,25. Arriba de 4 pide confirmación: en el backtest aparecen liquidaciones desde TWE 6.';
        if (!error) borrador.twe = v;
        pintar();
    });
    document.getElementById('bbNLongs').addEventListener('change', (e) => {
        const v = Number(e.target.value);
        const l = lugares(borrador.pares);
        if (Number.isInteger(v)) borrador.n = Math.min(l.max, Math.max(l.min, v));
        pintar();
    });

    // ---------------------------------------------------------------- revisar y aplicar (plan del servidor)
    function limpiarBloqueos() {
        const ul = q('bloqueos');
        ul.replaceChildren();
        ul.classList.add('d-none');
    }
    function mostrarBloqueos(lista) {
        const ul = q('bloqueos');
        ul.replaceChildren(...lista.map((t) => {
            const li = el('li', 'bb-alerta bb-alerta--peligro');
            const i = el('i', 'fas fa-times-circle');
            i.setAttribute('aria-hidden', 'true');
            li.append(i, el('span', '', t));
            return li;
        }));
        ul.classList.toggle('d-none', !lista.length);
    }
    function celdaPares(antes, despues, lado) {
        const td = el('td');
        const lista = lado === 'antes' ? antes : despues;
        (lista || []).forEach((p, i) => {
            if (i) td.appendChild(document.createTextNode(', '));
            const marcado = lado === 'antes' ? !(despues || []).includes(p) : !(antes || []).includes(p);
            td.appendChild(marcado ? el(lado === 'antes' ? 'del' : 'ins', '', corto(p)) : document.createTextNode(corto(p)));
        });
        if (!(lista || []).length) td.textContent = '—';
        return td;
    }
    function nodosPlan(plan) {
        const nodos = [];
        const sec = el('div', 'bb-modal-seccion');
        sec.appendChild(el('h3', '', 'Qué cambia'));
        const tabla = el('table', 'bb-diff');
        const thead = el('thead');
        const trh = el('tr');
        ['Qué', 'Ahora', 'Después'].forEach((t) => { const th = el('th', '', t); th.scope = 'col'; trh.appendChild(th); });
        thead.appendChild(trh);
        const tbody = el('tbody');
        (plan.diff || []).forEach((f) => {
            if (!f.cambia && !['n_longs', 'lw', 'twe_long'].includes(f.clave)) return;
            const tr = el('tr');
            const th = el('th', '', f.titulo);
            th.scope = 'row';
            if (f.sub) th.appendChild(el('small', '', f.sub));
            tr.appendChild(th);
            if (f.clave === 'pares_long' || f.clave === 'pares_short') {
                tr.append(celdaPares(f.antes, f.despues, 'antes'), celdaPares(f.antes, f.despues, 'despues'));
            } else {
                const tdA = el('td');
                if (f.cambia && f.clave !== 'twe_efectiva') tdA.appendChild(el('del', '', f.texto_antes)); else tdA.textContent = f.texto_antes;
                const tdB = el('td');
                if (f.cambia) tdB.appendChild(el('ins', '', f.texto_despues)); else tdB.appendChild(el('span', 'bb-igual', `${f.texto_despues} (igual)`));
                if (f.nota) tdB.appendChild(document.createTextNode(` · ${f.nota}`));
                tr.append(tdA, tdB);
            }
            tbody.appendChild(tr);
        });
        tabla.append(thead, tbody);
        sec.appendChild(tabla);
        nodos.push(sec);
        const av = el('div', 'bb-modal-seccion');
        av.appendChild(el('h3', '', 'Antes de aplicar'));
        const ul = el('ul', 'bb-avisos');
        (plan.avisos || []).forEach((a) => {
            const li = el('li', `bb-alerta bb-alerta--${a.tipo === 'info' ? 'info' : 'aviso'}`);
            const i = el('i', a.tipo === 'info' ? 'fas fa-info-circle' : 'fas fa-exclamation-triangle');
            i.setAttribute('aria-hidden', 'true');
            const div = el('div');
            div.append(el('strong', '', a.titulo || ''), el('span', '', ` ${a.texto || ''}`));
            li.append(i, div);
            ul.appendChild(li);
        });
        av.appendChild(ul);
        nodos.push(av);
        return nodos;
    }
    function pedirPlanEstrategia(cuerpo, titulo) {
        limpiarBloqueos();
        const acciones = window.BB && window.BB.botAcciones;
        const revisarBtn = raiz.querySelector('[data-estrategia-accion="revisar"]');
        if (revisarBtn) revisarBtn.disabled = true;
        return fetch(url, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
            body: JSON.stringify(cuerpo),
        })
            .then((r) => {
                if (r.redirected) throw new Error('Sesión expirada: volvé a iniciar sesión.');
                return r.json().then((d) => ({ status: r.status, d }));
            })
            .then(({ status, d }) => {
                if (status === 409 && d.requiere_confirmacion && d.plan && acciones) {
                    const plan = d.plan;
                    acciones.pedirConfirmacion({
                        url,
                        body: Object.assign({}, cuerpo, { confirmar: true, plan_hash: d.plan_hash }),
                        titulo: titulo || plan.titulo,
                        texto: '',
                        nodos: nodosPlan(plan),
                        check: !!plan.requiere_casilla,
                        checkLabel: plan.casilla_texto || 'Revisé el cambio.',
                        boton: 'Aplicar',
                        ancho: 'lg',
                    });
                    return;
                }
                if (d && d.bloqueos) { mostrarBloqueos(d.bloqueos); return; }
                if (status === 409 && d.job_id && acciones) { acciones.aviso('error', d.error); return; }
                throw new Error((d && d.error) || 'No se pudo preparar el cambio.');
            })
            .catch((e) => mostrarBloqueos([e.message || 'No se pudo preparar el cambio.']))
            .finally(() => { if (revisarBtn) revisarBtn.disabled = false; });
    }
    function revisar() {
        const lista = cambios();
        const titulo = lista.length === 1 && lista[0].startsWith('−') ? `Revisar y aplicar: quitar ${lista[0].slice(1)}` : 'Revisar y aplicar la estrategia';
        pedirPlanEstrategia({ twe_long: borrador.twe, n_longs: borrador.n, pares: borrador.pares.slice(), base_hash: srv.base_hash }, titulo);
    }

    // ---------------------------------------------------------------- agregar un par
    const modalEl = document.getElementById('bbAgregarParModal');
    const buscar = document.getElementById('bbBuscarPar');
    const qa = (k) => modalEl.querySelector(`[data-agregar="${k}"]`);
    let elegido = null;
    let modo = 'todos';
    let pedido = 0;
    let espera = null;
    function abrirAgregar() {
        elegido = null;
        modo = 'todos';
        buscar.value = '';
        qa('resultados').replaceChildren();
        qa('estado').textContent = '';
        qa('modo').classList.add('d-none');
        qa('confirmar').disabled = true;
        window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
    modalEl.addEventListener('shown.bs.modal', () => buscar.focus());
    function opcionModo(valor, titulo, texto, marcado) {
        const label = el('label', 'bb-opcion');
        const input = el('input');
        input.type = 'radio';
        input.name = 'bbModoPar';
        input.value = valor;
        input.checked = marcado;
        input.addEventListener('change', () => { modo = valor; });
        label.append(input, el('span', 'bb-opcion__nombre', titulo), el('p', 'bb-opcion__texto', texto));
        return label;
    }
    function pintarModos() {
        if (!elegido) { qa('modo').classList.add('d-none'); return; }
        const pares = borrador.pares.concat([elegido.symbol]);
        const l = lugares(pares);
        const todos = l.max;
        const d = derivados(borrador.twe, todos);
        const libres = Math.max(0, todos - l.ocupados.length);
        const nodos = [opcionModo('todos', `${todos} a la vez`, `lw ${num(d.lw, 2)} por par (${num(borrador.twe)} ÷ ${todos}): cada par puede llegar a ${usdt(d.maxPar)}. ${elegido.symbol} ${libres ? 'entra apenas arranque forager: queda 1 lugar libre' : 'espera a que se libere un lugar'}.`, modo === 'todos')];
        if (borrador.n >= l.min && borrador.n < pares.length) {
            const dr = derivados(borrador.twe, borrador.n);
            nodos.push(opcionModo('rotando', `${borrador.n} a la vez, rotando`, `lw ${num(dr.lw, 2)} (igual). Cada ${srv.ciclo_min} min forager elige ${borrador.n} de los ${pares.length} por volumen, unilateralidad y ruido; el que queda afuera sigue en parada gradual hasta cerrar y no abre nada nuevo.`, modo === 'rotando'));
        } else if (modo === 'rotando') {
            modo = 'todos';
        }
        qa('modos').replaceChildren(...nodos);
        qa('modo').classList.remove('d-none');
    }
    function resultadoLi(r) {
        const enBorrador = borrador.pares.includes(r.symbol);
        const li = el('li', `bb-resultado-par${r.bloquea ? ' is-bloqueado' : ''}${elegido && elegido.symbol === r.symbol ? ' is-elegido' : ''}`);
        const nombre = el('div', 'bb-resultado-par__nombre');
        nombre.appendChild(el('span', '', r.symbol));
        let etiqueta = ['bb-etiqueta--ok', 'se puede agregar'];
        if (enBorrador || r.ya_esta) etiqueta = ['bb-etiqueta--neutra', 'ya está'];
        else if (r.estado === 'sin_datos') etiqueta = ['bb-etiqueta--aviso', 'sin verificar'];
        else if (r.bloquea) etiqueta = ['bb-etiqueta--peligro', 'no se puede'];
        nombre.appendChild(el('span', `bb-etiqueta ${etiqueta[0]}`, etiqueta[1]));
        const b = el('button', `btn btn-sm ${elegido && elegido.symbol === r.symbol ? 'btn-dark' : 'btn-outline-secondary'}`, elegido && elegido.symbol === r.symbol ? 'Elegido' : 'Elegir');
        b.type = 'button';
        b.setAttribute('aria-pressed', elegido && elegido.symbol === r.symbol ? 'true' : 'false');
        b.setAttribute('aria-label', `Elegir ${r.symbol}`);
        b.disabled = r.bloquea || enBorrador || r.ya_esta;
        b.addEventListener('click', () => {
            elegido = r;
            qa('confirmar').disabled = false;
            pintarResultados(ultimos);
            pintarModos();
        });
        const ul = el('ul', 'bb-chequeos');
        ul.setAttribute('aria-label', `Chequeos de ${r.symbol}`);
        (r.chequeos || []).forEach((c) => {
            if (c.clave === 'config_medida') return;
            ul.appendChild(chequeoLi(c.clave === 'primera_compra' ? primeraCompra(c, derivados(borrador.twe, lugares(borrador.pares.concat([r.symbol])).max).lw) : c));
        });
        li.append(nombre, b, ul);
        return li;
    }
    let ultimos = [];
    function pintarResultados(lista) {
        ultimos = lista;
        qa('resultados').replaceChildren(...lista.map(resultadoLi));
    }
    function buscarAhora() {
        const texto = buscar.value.trim().toUpperCase();
        if (!/^[A-Z0-9]{1,20}$/.test(texto)) {
            qa('estado').textContent = texto ? 'Usá letras y números (ej.: SOL).' : '';
            pintarResultados([]);
            return;
        }
        const id = ++pedido;
        qa('estado').textContent = 'Buscando y chequeando contra Binance…';
        const lw = derivados(borrador.twe, borrador.n).lw;
        fetch(`${urlCandidatos}?q=${encodeURIComponent(texto)}&lw=${encodeURIComponent(lw.toFixed(4))}`,
            { credentials: 'same-origin', cache: 'no-cache' })
            .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
            .then(({ ok, d }) => {
                if (id !== pedido) return;
                if (!ok || d.error) {
                    qa('estado').textContent = d.error || 'No se pudo buscar.';
                    pintarResultados([]);
                    return;
                }
                const n = d.resultados.length;
                qa('estado').textContent = n ? `${n} ${n === 1 ? 'resultado' : 'resultados'} en Binance ${d.endpoint}.` : 'Sin resultados.';
                pintarResultados(d.resultados);
            })
            .catch(() => { if (id === pedido) qa('estado').textContent = 'No se pudo buscar: revisá la conexión.'; });
    }
    buscar.addEventListener('input', () => {
        clearTimeout(espera);
        elegido = null;
        qa('confirmar').disabled = true;
        qa('modo').classList.add('d-none');
        espera = setTimeout(buscarAhora, 400);
    });
    buscar.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); clearTimeout(espera); buscarAhora(); } });
    qa('confirmar').addEventListener('click', () => {
        if (!elegido) return;
        porPar[elegido.symbol] = elegido;
        borrador.pares.push(elegido.symbol);
        const l = lugares(borrador.pares);
        borrador.n = modo === 'todos' ? l.max : Math.min(l.max, Math.max(l.min, borrador.n));
        window.bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        pintar();
    });

    // ---------------------------------------------------------------- carga
    function error(msg) {
        q('cargando').classList.add('d-none');
        q('error').classList.remove('d-none');
        q('error-texto').textContent = msg;
    }
    fetch(url, { credentials: 'same-origin', cache: 'no-cache' })
        .then((r) => {
            if (r.redirected) throw new Error('Sesión expirada: volvé a iniciar sesión.');
            return r.json().then((d) => ({ ok: r.ok, d }));
        })
        .then(({ ok, d }) => {
            if (!ok || !d.ok) throw new Error(d.error || 'No se pudo leer la estrategia.');
            srv = d;
            porPar = {};
            (d.pares || []).forEach((p) => { porPar[p.symbol] = p; });
            borrador = { twe: Number(d.vigente.twe_long), n: d.vigente.n_longs, pares: d.vigente.pares_long.slice() };
            q('cargando').classList.add('d-none');
            raiz.querySelectorAll('[data-estrategia="seccion"]').forEach((s) => s.classList.remove('d-none'));
            pintarAvanzado();
            pintar();
            raiz.dispatchEvent(new CustomEvent('bb-estrategia-cargada', { detail: d, bubbles: true }));
        })
        .catch((e) => error(e.message || 'No se pudo leer la estrategia.'));

    window.BB = window.BB || {};
    window.BB.estrategia = { pedirPlan: pedirPlanEstrategia, nodosPlan, datos: () => srv };
})();
