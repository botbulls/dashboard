/* bot_pagina.js: historial de acciones de Bot (GET /api/bot/acciones): filtros y "Ver más" sin recargar.
   La primera página llega armada del server (sin JS se ve igual). Todo se pinta con textContent: lo que viene del log
   no se interpreta como HTML. El endpoint nunca devuelve la IP del log. */
(function () {
    'use strict';
    const raiz = document.getElementById('bbHistorial');
    if (!raiz || !window.fetch) return;
    const url = raiz.getAttribute('data-acciones-url');
    const tbody = raiz.querySelector('[data-historial-filas]');
    const cuenta = raiz.querySelector('[data-historial-cuenta]');
    const mas = raiz.querySelector('[data-historial-mas]');
    const botones = raiz.querySelectorAll('[data-filtro]');
    let filtro = 'todas';
    let mostradas = tbody.querySelectorAll('tr[data-ts], tr').length;

    function celda(cls, texto) {
        const td = document.createElement('td');
        if (cls) td.className = cls;
        if (texto !== undefined) td.textContent = texto;
        return td;
    }

    function fila(a) {
        const tr = document.createElement('tr');
        const ts = a.ts || '';
        tr.appendChild(celda('bb-cuando', `${ts.slice(8, 10)}/${ts.slice(5, 7)} ${ts.slice(11, 19)}`));
        tr.appendChild(celda('bb-col-quien', a.usuario));
        const que = celda('bb-col-que');
        const t = document.createElement('span');
        t.className = 'bb-que';
        t.textContent = a.titulo;
        que.appendChild(t);
        if (a.detalle) {
            const d = document.createElement('span');
            d.className = 'bb-detalle-fila';
            d.textContent = a.detalle;
            que.appendChild(d);
        }
        tr.appendChild(que);
        const res = celda('bb-col-resultado');
        const tipo = (a.estado === 'aplicado' || a.estado === 'validada') ? 'ok' : (a.estado === 'plan_mostrado' || a.estado === 'sin_confirmar' ? 'pregunta' : 'rechazado');
        const span = document.createElement('span');
        span.className = `bb-resultado bb-resultado--${tipo}`;
        const i = document.createElement('i');
        i.className = tipo === 'ok' ? 'fas fa-check-circle' : (tipo === 'pregunta' ? 'far fa-question-circle' : 'fas fa-times-circle');
        i.setAttribute('aria-hidden', 'true');
        span.append(i, ` ${a.estado_texto}`);
        res.appendChild(span);
        tr.appendChild(res);
        return tr;
    }

    function pedir(desde, agregar) {
        const q = new URLSearchParams({ desde: String(desde), limite: '12', filtro });
        return fetch(`${url}?${q}`, { cache: 'no-cache', credentials: 'same-origin' })
            .then((r) => {
                if (r.redirected || !r.ok) throw new Error('No se pudo leer el historial.');
                return r.json();
            })
            .then((d) => {
                const filas = d.acciones.map(fila);
                if (agregar) {
                    tbody.append(...filas);
                    mostradas += filas.length;
                } else {
                    if (!filas.length) {
                        const tr = document.createElement('tr');
                        const td = celda('', 'Sin acciones con este filtro.');
                        td.colSpan = 4;
                        tr.appendChild(td);
                        filas.push(tr);
                    }
                    tbody.replaceChildren(...filas);
                    mostradas = d.acciones.length;
                }
                cuenta.textContent = `${mostradas} de ${d.total}, las más nuevas`;
                mas.classList.toggle('d-none', d.siguiente === null);
                mas.setAttribute('data-desde', d.siguiente === null ? '' : String(d.siguiente));
            })
            .catch((e) => { cuenta.textContent = e.message; });
    }

    botones.forEach((b) => {
        b.addEventListener('click', () => {
            filtro = b.getAttribute('data-filtro');
            botones.forEach((x) => x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
            pedir(0, false);
        });
    });
    if (mas) {
        mas.addEventListener('click', () => {
            const desde = parseInt(mas.getAttribute('data-desde'), 10);
            if (!isNaN(desde)) pedir(desde, true);
        });
    }
})();
