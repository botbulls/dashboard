/* credenciales.js: la página /bot/credenciales (cambiar la API key de Binance, spec bot-estrategia § 10.4).
   Sin dependencias (la CSP no deja cargar jQuery ni Bootstrap de un CDN). Lo que llega del servidor se pinta con
   textContent, nunca como HTML. La key y el secret solo viven en los campos hasta que responde la validación: después
   se borran, y nada se guarda en localStorage. */
(function () {
  'use strict';
  var body = document.body;
  if (!body || !body.hasAttribute('data-credenciales')) return;
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  var volver = body.getAttribute('data-volver') === '1';
  var URL_VALIDAR = body.getAttribute('data-url-validar');
  var URL_APLICAR = body.getAttribute('data-url-aplicar');
  var URL_VOLVER = body.getAttribute('data-url-volver');
  var URL_JOB = body.getAttribute('data-url-jobs');
  var estado = { pendiente: null, planHash: null, key: null };
  var SVG = 'http://www.w3.org/2000/svg';

  function $(sel) { return document.querySelector(sel); }
  function vaciar(nodo) { while (nodo && nodo.firstChild) nodo.removeChild(nodo.firstChild); }
  function ico(nombre, extra) {
    var i = document.createElement('i');
    i.className = 'bb-ico' + (extra ? ' ' + extra : '');
    i.setAttribute('aria-hidden', 'true');
    var s = document.createElementNS(SVG, 'svg');
    var u = document.createElementNS(SVG, 'use');
    u.setAttribute('href', '#' + nombre);
    s.appendChild(u);
    i.appendChild(s);
    return i;
  }
  function el(tag, clase, texto) {
    var n = document.createElement(tag);
    if (clase) n.className = clase;
    if (texto !== undefined && texto !== null) n.textContent = String(texto);
    return n;
  }
  function pedir(url, cuerpo) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Accept': 'application/json' },
      body: JSON.stringify(cuerpo)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return { status: r.status, data: d || {} }; });
    }, function () { return { status: 0, data: { error: 'No se pudo contactar al panel.' } }; });
  }

  // -- tema ----------------------------------------------------------------------------
  var bTema = $('#bbTemaSola');
  if (bTema) {
    bTema.addEventListener('click', function () {
      var orden = ['auto', 'light', 'dark'], d = document.documentElement;
      var t = orden[(orden.indexOf(d.getAttribute('data-theme')) + 1) % 3];
      d.setAttribute('data-theme', t);
      try { localStorage.setItem('futuresboard.theme', t); } catch (e) { /* sin localStorage */ }
    });
  }

  // -- pasos ---------------------------------------------------------------------------
  function mostrarPaso(n) {
    document.querySelectorAll('[data-paso]').forEach(function (s) { s.hidden = s.getAttribute('data-paso') !== String(n); });
    document.querySelectorAll('[data-indicador]').forEach(function (li) {
      var k = Number(li.getAttribute('data-indicador'));
      li.classList.toggle('is-actual', k === n);
      li.classList.toggle('is-hecho', k < n);
      if (k === n) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
      var sr = li.querySelector('[data-hecho]');
      if (sr) sr.hidden = !(k < n);
    });
    var h = document.querySelector('[data-paso="' + n + '"] h3');
    window.scrollTo(0, 0);
    if (h && h.focus) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); }
  }
  function error(slot, texto) {
    var p = $(slot);
    if (!p) return;
    p.hidden = !texto;
    var t = p.querySelector('[data-texto]');
    if (t) t.textContent = texto || '';
  }
  var ICONOS = { ok: 'i-ok', bloquea: 'i-error', info: 'i-info', aviso: 'i-aviso' };
  var PREFIJO = { ok: 'Bien: ', bloquea: 'Bloquea: ', info: '', aviso: 'Aviso: ' };
  function pintarChequeos(ul, chequeos) {
    vaciar(ul);
    (chequeos || []).forEach(function (c) {
      var li = el('li', 'bb-chequeo bb-chequeo--' + (c.estado || 'info'));
      li.appendChild(ico(ICONOS[c.estado] || 'i-info'));
      var span = el('span');
      if (PREFIJO[c.estado]) span.appendChild(el('span', 'bb-sr', PREFIJO[c.estado]));
      span.appendChild(document.createTextNode(c.texto || ''));
      li.appendChild(span);
      ul.appendChild(li);
    });
  }
  function keyEnmascarada(nodo, key) {
    vaciar(nodo);
    if (!key || key.length < 9) { nodo.textContent = key || '—'; return; }
    nodo.appendChild(el('span', 'bb-sr', 'API key que empieza con '));
    nodo.appendChild(document.createTextNode(key.slice(0, 4)));
    var e = el('span', null, '…'); e.setAttribute('aria-hidden', 'true'); nodo.appendChild(e);
    nodo.appendChild(el('span', 'bb-sr', ' y termina con '));
    nodo.appendChild(document.createTextNode(key.slice(-4)));
  }

  // -- paso 1: pegar y validar -----------------------------------------------------------
  var iKey = $('#bbApiKey'), iSec = $('#bbApiSecret'), bValidar = $('[data-cred-validar]');
  function limpiarCampos() { if (iKey) iKey.value = ''; if (iSec) iSec.value = ''; }
  function habilitarValidar() {
    if (!bValidar || volver) return;
    bValidar.disabled = !(iKey && iSec && iKey.value.trim() && iSec.value.trim());
  }
  [iKey, iSec].forEach(function (i) { if (i) i.addEventListener('input', habilitarValidar); });

  function resultadoBloqueado(d) {
    var caja = $('[data-cred-resultado]');
    if (!caja) return;
    caja.hidden = false;
    $('[data-cred-resultado-titulo]').textContent = 'Última prueba: ' + (d.key || 'key') + ' · no se puede usar';
    pintarChequeos($('[data-cred-chequeos]'), d.chequeos);
    var otra = $('[data-cred-otra-cuenta]');
    if (otra) otra.hidden = !d.otra_cuenta;
    var pie = $('[data-cred-pie]');
    if (pie) pie.textContent = 'Lo que pegaste se descartó: no quedó en el servidor ni en el historial (solo la key ' +
      'enmascarada y el motivo).';
  }

  function irAlPlan(d) {
    estado.pendiente = d.pendiente_id || estado.pendiente;
    estado.planHash = d.plan_hash;
    var plan = d.plan || {};
    keyEnmascarada($('[data-cred-key]'), estado.key);
    pintarChequeos($('[data-cred-chequeos2]'), estado.chequeos);
    var tb = $('[data-cred-diff]');
    vaciar(tb);
    (plan.cambios || []).forEach(function (c) {
      var tr = el('tr');
      var th = el('th', null, c.que); th.setAttribute('scope', 'row');
      if (c.nota) th.appendChild(el('small', null, c.nota));
      tr.appendChild(th);
      var antes = el('td');
      if (c.clase === 'key') { var del = el('del', null, c.antes); antes.appendChild(del); } else antes.textContent = c.antes;
      tr.appendChild(antes);
      var despues = el('td', c.clase === 'igual' ? 'bb-igual' : null);
      if (c.clase === 'key') despues.appendChild(el('ins', null, c.despues));
      else if (c.clase === 'cambia') {
        var partes = String(c.despues).split(' · ');
        despues.appendChild(el('ins', null, partes[0]));
        if (partes.length > 1) despues.appendChild(document.createTextNode(' · ' + partes.slice(1).join(' · ')));
      } else despues.textContent = c.despues;
      tr.appendChild(despues);
      tb.appendChild(tr);
    });
    var av = $('[data-cred-avisos]');
    vaciar(av);
    (plan.avisos || []).forEach(function (a) {
      var li = el('li', 'bb-alerta bb-alerta--' + (a.tipo === 'aviso' ? 'aviso' : 'info'));
      li.appendChild(ico(a.tipo === 'aviso' ? 'i-aviso' : (/reinicia/.test(a.titulo) ? 'i-reinicio' : 'i-sync')));
      var div = el('div');
      div.appendChild(el('strong', null, a.titulo));
      div.appendChild(el('span', null, a.texto));
      li.appendChild(div);
      av.appendChild(li);
    });
    var ol = $('[data-cred-plan-pasos]');
    vaciar(ol);
    (plan.pasos || []).forEach(function (p) { ol.appendChild(el('li', null, p)); });
    var cas = $('[data-cred-casilla]');
    if (cas && plan.casilla) cas.textContent = plan.casilla;
    $('#bbConfirmarCheck').checked = false;
    $('#bbReauth').value = '';
    habilitarAplicar();
    mostrarPaso(2);
  }

  if (bValidar) {
    bValidar.addEventListener('click', function () {
      var texto = bValidar.textContent;
      bValidar.disabled = true;
      bValidar.textContent = 'Validando…';
      error('[data-cred-error]', '');
      var promesa;
      if (volver) {
        promesa = pedir(URL_VOLVER, {});
      } else {
        promesa = pedir(URL_VALIDAR, { api_key: iKey.value.trim(), api_secret: iSec.value.trim() });
      }
      promesa.then(function (r) {
        limpiarCampos();
        bValidar.textContent = texto;
        habilitarValidar();
        if (volver) bValidar.disabled = false;
        var d = r.data;
        if (r.status === 200 && d.ok) {
          estado.key = d.key; estado.chequeos = d.chequeos; estado.pendiente = d.pendiente_id;
          return pedir(URL_APLICAR, { pendiente_id: d.pendiente_id }).then(function (r2) {
            if (r2.status === 409 && r2.data.requiere_confirmacion) irAlPlan(r2.data);
            else error('[data-cred-error]', r2.data.error || ('El panel respondió ' + r2.status + '.'));
          });
        }
        if (volver && r.status === 409 && d.requiere_confirmacion) {
          estado.key = d.key; estado.chequeos = d.chequeos;
          irAlPlan(d);
          return null;
        }
        if (r.status === 422) { resultadoBloqueado(d); return null; }
        error('[data-cred-error]', d.error || ('El panel respondió ' + r.status + '.'));
        if (d.chequeos) resultadoBloqueado(d);
        return null;
      });
    });
  }
  var bOtra = $('[data-cred-otra]');
  if (bOtra) bOtra.addEventListener('click', function () { estado.pendiente = null; estado.planHash = null; mostrarPaso(1); });

  // -- paso 2: revisar y confirmar con la contraseña -----------------------------------
  var chk = $('#bbConfirmarCheck'), pw = $('#bbReauth'), bAplicar = $('#bbConfirmarAplicar');
  function habilitarAplicar() { if (bAplicar) bAplicar.disabled = !(chk && chk.checked && pw && pw.value); }
  if (chk) chk.addEventListener('change', habilitarAplicar);
  if (pw) pw.addEventListener('input', habilitarAplicar);
  if (bAplicar) {
    bAplicar.addEventListener('click', function () {
      var clave = pw.value;
      pw.value = '';
      bAplicar.disabled = true;
      error('[data-cred-error2]', '');
      pedir(volver ? URL_VOLVER : URL_APLICAR, { pendiente_id: estado.pendiente, plan_hash: estado.planHash,
        password: clave, confirmar: chk.checked === true }).then(function (r) {
        clave = null;
        var d = r.data;
        if (r.status === 202 && d.job_id) { seguirJob(d.job_id); return; }
        if (r.status === 409 && d.requiere_confirmacion) {
          irAlPlan(d);
          error('[data-cred-error2]', d.error || 'El plan cambió: revisalo de nuevo.');
          return;
        }
        if (r.status === 409 && d.vencido) {
          error('[data-cred-error2]', (d.error || 'La validación venció.') + ' Volvé al paso 1.');
          return;
        }
        error('[data-cred-error2]', d.error || ('El panel respondió ' + r.status + '.'));
        habilitarAplicar();
      });
    });
  }

  // -- paso 3: el job -------------------------------------------------------------------
  var MARCA = { ok: 'i-ok', error: 'i-error', omitido: 'i-info', pendiente: 'i-info', en_curso: 'i-espera' };
  var ESTADO_TXT = { ok: 'listo', error: 'error', omitido: 'omitido', pendiente: 'pendiente', en_curso: 'en curso' };
  function pintarJob(job) {
    var ol = $('[data-cred-progreso]');
    vaciar(ol);
    (job.pasos || []).forEach(function (p) {
      var li = el('li');
      li.setAttribute('data-estado', p.estado);
      li.appendChild(ico(MARCA[p.estado] || 'i-info', p.estado === 'en_curso' ? 'bb-girando' : ''));
      var span = el('span');
      span.appendChild(el('strong', null, p.titulo));
      span.appendChild(el('span', 'bb-sr', ' — ' + (ESTADO_TXT[p.estado] || p.estado)));
      if (p.detalle) span.appendChild(el('small', null, p.detalle));
      li.appendChild(span);
      ol.appendChild(li);
    });
    if (job.estado === 'en_curso') return;
    var res = $('[data-cred-job-resultado]');
    vaciar(res);
    var r = job.resultado || {};
    var ok = job.estado === 'ok';
    $('[data-cred-job-titulo]').textContent = ok ? (r.warning ? 'Aplicado, con avisos' : 'Listo') : 'No se aplicó';
    var caja = el('p', 'bb-alerta bb-alerta--' + (ok ? (r.warning ? 'aviso' : 'info') : 'peligro'));
    caja.setAttribute('role', ok && !r.warning ? 'status' : 'alert');
    caja.appendChild(ico(ok ? (r.warning ? 'i-aviso' : 'i-ok') : 'i-error'));
    var espera = (job.pasos || []).filter(function (p) { return p.clave === 'consumidores'; })[0] || {};
    caja.appendChild(el('span', null, ok ? (r.warning || ('Key ' + (r.key || '') + ' en uso. ' + (espera.detalle || ''))) :
      (r.error || 'Error.')));
    res.appendChild(caja);
  }
  function seguirJob(id) {
    mostrarPaso(3);
    var url = URL_JOB.replace('__ID__', encodeURIComponent(id));
    function vuelta() {
      fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (job) {
          pintarJob(job);
          if (job.estado === 'en_curso') setTimeout(vuelta, 1000);
          else mostrarPasoFinal();
        }, function () { setTimeout(vuelta, 2000); });
    }
    vuelta();
  }
  function mostrarPasoFinal() {
    document.querySelectorAll('[data-indicador]').forEach(function (li) {
      li.classList.add('is-hecho'); li.classList.remove('is-actual'); li.removeAttribute('aria-current');
      var sr = li.querySelector('[data-hecho]'); if (sr) sr.hidden = false;
    });
  }

  habilitarValidar();
})();
