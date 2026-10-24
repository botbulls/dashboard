/* en_vivo.js: los números de Inicio, Posiciones y Moneda se actualizan sin recargar la página.
   Pide cada 15 s el JSON de /api/vivo/... (data-vivo-url de #bbVivo, ver futuresboard/vivo.py) y lo aplica en el lugar:
   - valores[k] -> [data-vivo="k"]: texto ya formateado por el server (es-AR, formato.py), data-value con el crudo y la
     clase is-gana/is-pierde/is-cero si el elemento tiene data-vivo-tono. Un <input> recibe el crudo en .value.
     [data-vivo-title="k"] recibe el texto en title.
   - bloques[k] -> [data-vivo-bloque="k"]: HTML armado por el server con el mismo macro de Jinja que la página. Se
     cambia solo lo que cambió (hijos con data-vivo-clave / data-tarjeta), se conservan los <details> abiertos, no se
     toca lo que tiene el foco y un aviso con role alert/status que sigue diciendo lo mismo no se vuelve a anunciar.
   - tablas[id] -> filas de la DataTable #id por data-fila, sin perder orden, búsqueda ni scroll.
   - graficos -> window.BB.vivoGraficos (Inicio), con chart.update('none').
   - bot -> evento fb-bot-status (franja y menú), igual que el panel.
   No pide nada al cargar (la página llega con los datos), usa setTimeout encadenado, se pausa con la pestaña oculta y
   refresca apenas vuelve, espera más entre reintentos si falla y, si la sesión venció (redirect al login o a
   Cloudflare Access), avisa y deja de pedir. No actualiza con un modal abierto ni con el foco en un campo del panel del
   bot. No hace nada si la página se abrió desde file:// (las capturas usan el HTML guardado). */
(function () {
  'use strict';
  var raiz = document.getElementById('bbVivo');
  if (!raiz || !raiz.getAttribute('data-vivo-url') || location.protocol === 'file:' || !window.fetch ||
      !window.Promise || !('content' in document.createElement('template'))) return;

  var URL_VIVO = raiz.getAttribute('data-vivo-url');
  var INTERVALO = 15000, ESPERA_MAX = 120000, TIEMPO_MAX = 10000, REINTENTO_BLOQUEADO = 3000;
  var reloj = raiz.querySelector('[data-vivo-reloj]');
  var datosEl = raiz.querySelector('[data-vivo-datos]');
  var aviso = document.querySelector('[data-vivo-aviso]');
  var reducido = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: true };

  var timer = null, relojTimer = null, enCurso = null, fallos = 0, parado = false;
  var actualizadoEn = Date.now();  // la página llega con los datos recién leídos
  var edadDatos = entero(raiz.getAttribute('data-vivo-edad')), edadDatosEn = Date.now();
  var intervaloScraper = entero(raiz.getAttribute('data-vivo-intervalo'));
  var ultimoGrafico = null, ultimaTabla = {};

  function entero(v) {
    var n = parseInt(v, 10);
    return isNaN(n) ? null : n;
  }

  function hace(s) {
    s = Math.max(0, Math.floor(s));
    if (s < 90) return s + ' s';
    if (s < 90 * 60) return Math.floor(s / 60) + ' min';
    if (s < 48 * 3600) return Math.floor(s / 3600) + ' h';
    return Math.floor(s / 86400) + ' d';
  }

  // ------------------------------------------------------------------ indicador "actualizado hace X s"
  function pintarReloj() {
    if (parado) return;
    var ahora = Date.now();
    var s = Math.floor((ahora - actualizadoEn) / 1000);
    var texto = s < 2 ? 'Actualizado recién' : 'Actualizado hace ' + hace(s);
    if (reloj.textContent !== texto) reloj.textContent = texto;
    if (edadDatos === null) {
      if (datosEl.textContent !== '· sin latido del scraper') datosEl.textContent = '· sin latido del scraper';
      return;
    }
    var edad = edadDatos + Math.floor((ahora - edadDatosEn) / 1000);
    var t = '· datos de hace ' + hace(edad);
    if (datosEl.textContent !== t) datosEl.textContent = t;
    // Datos viejos: el scraper no está actualizando la base (más de 3 vueltas, o 3 min sin dato del intervalo).
    raiz.classList.toggle('is-viejo', edad > Math.max(3 * (intervaloScraper || 60), 180));
  }

  function arrancarReloj() {
    if (relojTimer === null) relojTimer = setInterval(pintarReloj, 1000);
  }

  function pararReloj() {
    if (relojTimer !== null) clearInterval(relojTimer);
    relojTimer = null;
  }

  function mostrarAviso(texto, conBoton) {
    if (!aviso) return;
    // Solo se escribe cuando cambia: el role="status" lo anuncia una vez, no en cada reintento.
    if (aviso.getAttribute('data-texto') === texto) return;
    aviso.setAttribute('data-texto', texto);
    aviso.textContent = texto;
    if (conBoton) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-sm btn-outline-secondary ms-2';
      b.textContent = 'Volver a entrar';
      b.addEventListener('click', function () { location.reload(); });
      aviso.appendChild(document.createTextNode(' '));
      aviso.appendChild(b);
    }
  }

  function estado(tipo) {
    raiz.classList.toggle('is-error', tipo === 'error' || tipo === 'sin-conexion');
    if (tipo === 'ok') mostrarAviso('', false);
    else if (tipo === 'sin-conexion') mostrarAviso('Sin conexión con el servidor, reintentando…', false);
    else if (tipo === 'error') mostrarAviso('No se pudo actualizar, reintentando…', false);
  }

  function sesionVencida() {
    parado = true;
    clearTimeout(timer);
    timer = null;
    pararReloj();
    raiz.classList.remove('is-error');
    raiz.classList.add('is-sesion');
    reloj.textContent = 'Sin actualizar';
    mostrarAviso('La sesión venció o se cortó el acceso: los números dejaron de actualizarse.', true);
  }

  // ------------------------------------------------------------------ cuándo no se toca la página
  function bloqueado() {
    if (document.querySelector('.modal.show')) return true;  // confirmaciones, progreso del bot, velas, detalle
    var a = document.activeElement, panel = document.getElementById('menu_admin');
    return !!(panel && a && panel.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName));
  }

  // ------------------------------------------------------------------ valores sueltos
  function resaltar(el) {
    if (reducido.matches) return;
    el.classList.remove('bb-vivo-cambio');
    void el.offsetWidth;  // reinicia la animación si ya estaba
    el.classList.add('bb-vivo-cambio');
    setTimeout(function () { el.classList.remove('bb-vivo-cambio'); }, 1700);
  }

  function aplicarValores(valores) {
    var els = document.querySelectorAll('[data-vivo]');
    Array.prototype.forEach.call(els, function (el) {
      var v = valores[el.getAttribute('data-vivo')];
      if (!v) return;
      if (el.tagName === 'INPUT') {
        if (document.activeElement !== el && el.value !== v.crudo) el.value = v.crudo;
        return;
      }
      var cambio = false;
      if (el.hasAttribute('data-value') && el.getAttribute('data-value') !== v.crudo) {
        el.setAttribute('data-value', v.crudo);
        cambio = true;
      }
      if (el.textContent !== v.texto) el.textContent = v.texto;
      if (el.hasAttribute('data-vivo-tono')) {
        var c = 'is-' + (v.tono || 'cero');
        if (!el.classList.contains(c)) {
          el.classList.remove('is-gana', 'is-pierde', 'is-cero');
          el.classList.add(c);
        }
      }
      if (cambio) resaltar(el);
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-vivo-title]'), function (el) {
      var v = valores[el.getAttribute('data-vivo-title')];
      if (v && el.getAttribute('title') !== v.texto) el.setAttribute('title', v.texto);
    });
  }

  // ------------------------------------------------------------------ bloques (HTML del server)
  function parsear(html) {
    var t = document.createElement('template');
    t.innerHTML = html.trim();
    // Los nodos de un <template> son de un documento inerte (sin documentElement): jQuery/DataTables fallan con
    // ellos. Se importan al documento de la página antes de usarlos.
    return document.importNode(t.content, true);
  }

  function firma(el) {
    return el.outerHTML.replace(/ open=""/g, '');
  }

  function clave(el) {
    return el.getAttribute('data-vivo-clave') || el.getAttribute('data-tarjeta');
  }

  function hijosConClave(el) {
    var hs = Array.prototype.slice.call(el.children);
    return hs.length && hs.every(clave) ? hs : null;
  }

  function esAviso(el) {
    return el.matches('[role="alert"], [role="status"], [data-vivo-rol]');
  }

  function sinNumeros(el) {
    return el.textContent.replace(/[\d.,]+/g, '#').replace(/\s+/g, ' ').trim();
  }

  function mismaEtiqueta(a, b) {
    return a.tagName === b.tagName && clave(a) === clave(b);
  }

  function copiarAtributos(de, a) {
    Array.prototype.slice.call(a.attributes).forEach(function (at) {
      if (at.name !== 'open' && !de.hasAttribute(at.name)) a.removeAttribute(at.name);
    });
    Array.prototype.forEach.call(de.attributes, function (at) {
      if (at.name !== 'open' && a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
    });
  }

  function conservarDetalles(viejo, nuevo) {
    var dv = Array.prototype.slice.call(viejo.querySelectorAll('details'));
    var dn = Array.prototype.slice.call(nuevo.querySelectorAll('details'));
    if (viejo.tagName === 'DETAILS') dv.unshift(viejo);
    if (nuevo.tagName === 'DETAILS') dn.unshift(nuevo);
    var abiertos = {};
    dv.forEach(function (d) {
      var s = d.querySelector('summary');
      abiertos[s ? s.textContent.trim() : ''] = d.open;
    });
    dn.forEach(function (d, i) {
      var s = d.querySelector('summary');
      var k = s ? s.textContent.trim() : '';
      if (Object.prototype.hasOwnProperty.call(abiertos, k)) d.open = abiertos[k];
      else if (dv.length === dn.length) d.open = dv[i].open;
    });
  }

  function parchear(viejo, nuevo) {
    if (firma(viejo) === firma(nuevo)) return;
    // Un aviso (role alert/status) que sigue diciendo lo mismo y solo cambia un número ("hace 12 min") ya se anunció
    // cuando apareció: se actualiza sin role (queda en data-vivo-rol) para que el lector de pantalla no lo repita.
    if (esAviso(viejo) && esAviso(nuevo) && sinNumeros(viejo) === sinNumeros(nuevo)) {
      var rol = nuevo.getAttribute('role');
      if (rol) {
        nuevo.removeAttribute('role');
        nuevo.setAttribute('data-vivo-rol', rol);
      }
      if (firma(viejo) !== firma(nuevo) && !viejo.contains(document.activeElement)) {
        viejo.parentNode.replaceChild(nuevo, viejo);
      }
      return;
    }
    var hv = hijosConClave(viejo), hn = hijosConClave(nuevo);
    if (hv && hn && hv.length === hn.length && mismaEtiqueta(viejo, nuevo) &&
        hv.every(function (h, i) { return clave(h) === clave(hn[i]); })) {
      copiarAtributos(nuevo, viejo);
      hv.forEach(function (h, i) { parchear(h, hn[i]); });
      return;
    }
    if (viejo.contains(document.activeElement)) return;  // no se le saca el foco a nadie: se cambia en la próxima vuelta
    conservarDetalles(viejo, nuevo);
    viejo.parentNode.replaceChild(nuevo, viejo);
  }

  function aplicarBloques(bloques) {
    Object.keys(bloques).forEach(function (k) {
      var viejo = document.querySelector('[data-vivo-bloque="' + k + '"]');
      if (!viejo) return;
      var nuevo = parsear(bloques[k]).firstElementChild;
      if (!nuevo || nuevo.getAttribute('data-vivo-bloque') !== k) return;
      parchear(viejo, nuevo);
    });
  }

  // ------------------------------------------------------------------ tablas (DataTables)
  function claves(filas) {
    return filas.map(function (f) { return f.getAttribute('data-fila'); });
  }

  function mismoConjunto(a, b) {
    if (a.length !== b.length || a.some(function (k) { return !k; })) return false;
    var x = a.slice().sort(), y = b.slice().sort();
    for (var i = 0; i < x.length; i++) {
      if (x[i] !== y[i] || (i && x[i] === x[i - 1])) return false;
    }
    return true;
  }

  // Las clases que pone DataTables (franjas odd/even, columna ordenada sorting_N) no vienen del server: se conservan.
  function copiarConClasesDeTabla(de, a) {
    var propias = Array.prototype.filter.call(a.classList, function (c) { return /^(odd|even|sorting_)/.test(c); });
    copiarAtributos(de, a);
    propias.forEach(function (c) { a.classList.add(c); });
  }

  function copiarFila(de, a) {
    copiarConClasesDeTabla(de, a);
    var cn = de.children, cv = a.children;
    for (var i = 0; i < cn.length && i < cv.length; i++) {
      copiarConClasesDeTabla(cn[i], cv[i]);
      if (cv[i].innerHTML !== cn[i].innerHTML) cv[i].innerHTML = cn[i].innerHTML;
    }
  }

  function aplicarTablas(tablas) {
    var $ = window.jQuery;
    Object.keys(tablas).forEach(function (id) {
      var html = tablas[id];
      if (ultimaTabla[id] === html) return;
      var tabla = document.getElementById(id);
      if (!tabla || !$ || !$.fn.dataTable || !$.fn.dataTable.isDataTable(tabla)) return;
      var dt = $(tabla).DataTable();
      var nuevas = Array.prototype.slice.call(parsear('<table><tbody>' + html + '</tbody></table>')
        .querySelectorAll('tbody > tr'));
      var actuales = dt.rows().nodes().toArray();
      var caja = tabla.closest('.bb-tabla-scroll') || tabla.parentNode;
      var x = caja.scrollLeft;
      if (actuales.length && mismoConjunto(claves(actuales), claves(nuevas))) {
        // Mismas filas: se cambian las celdas en el lugar (los nodos quedan) y DataTables relee texto y data-order.
        var porClave = {};
        actuales.forEach(function (f) { porClave[f.getAttribute('data-fila')] = f; });
        nuevas.forEach(function (f) { copiarFila(f, porClave[f.getAttribute('data-fila')]); });
        dt.rows().invalidate('dom').draw(false);
      } else {
        dt.clear();
        dt.rows.add(nuevas);
        dt.draw(false);
      }
      caja.scrollLeft = x;
      ultimaTabla[id] = html;
    });
  }

  // ------------------------------------------------------------------ todo junto
  function seguro(fn) {
    try {
      fn();
    } catch (e) {
      if (window.console) console.error('en_vivo:', e);
    }
  }

  function aplicar(d) {
    seguro(function () { aplicarValores(d.valores || {}); });
    seguro(function () { aplicarBloques(d.bloques || {}); });
    seguro(function () { aplicarTablas(d.tablas || {}); });
    if (d.graficos && window.BB && typeof window.BB.vivoGraficos === 'function') {
      seguro(function () {
        var g = JSON.stringify(d.graficos);
        if (g !== ultimoGrafico) {
          window.BB.vivoGraficos(d.graficos);
          ultimoGrafico = g;
        }
      });
    }
    if (d.bot) {
      seguro(function () {
        var b = d.bot;
        if (b.datos && (b.http === 200 || b.datos.enabled === false)) {
          window.dispatchEvent(new CustomEvent('fb-bot-status', { detail: b.datos }));
        } else {
          window.dispatchEvent(new CustomEvent('fb-bot-status-error', { detail: b }));
        }
      });
    }
    if (d.datos) {
      edadDatos = typeof d.datos.edad_segundos === 'number' ? d.datos.edad_segundos : null;
      edadDatosEn = Date.now();
      intervaloScraper = typeof d.datos.intervalo_segundos === 'number' ? d.datos.intervalo_segundos : intervaloScraper;
    }
  }

  function programar(ms) {
    clearTimeout(timer);
    timer = setTimeout(tick, ms);
  }

  function falla(tipo) {
    fallos += 1;
    estado(tipo);
    var espera = Math.min(INTERVALO * Math.pow(2, fallos), ESPERA_MAX);
    programar(espera + Math.floor(Math.random() * 1000));
  }

  function tick() {
    timer = null;
    if (parado || document.hidden) return;
    if (bloqueado()) {
      programar(REINTENTO_BLOQUEADO);
      return;
    }
    var ctrl = window.AbortController ? new AbortController() : null;
    var corte = setTimeout(function () { if (ctrl) ctrl.abort(); }, TIEMPO_MAX);
    enCurso = ctrl;
    fetch(URL_VIVO, {
      credentials: 'same-origin', cache: 'no-store', redirect: 'manual',
      headers: { Accept: 'application/json' }, signal: ctrl ? ctrl.signal : undefined,
    })
      .then(function (r) {
        // Sesión vencida (redirect al login de la app o a Cloudflare Access): con redirect 'manual' llega como
        // opaqueredirect; una página de login con 200 llega sin JSON. Nunca se pinta eso.
        if (r.type === 'opaqueredirect' || r.status === 401 || r.status === 403 || (r.status >= 300 && r.status < 400)) {
          throw { sesion: true };
        }
        var tipo = r.headers.get('Content-Type') || '';
        if (tipo.indexOf('application/json') === -1) throw r.ok ? { sesion: true } : { http: r.status };
        return r.json().then(function (d) {
          if (!r.ok || !d || d.ok !== true) throw { http: r.status };
          return d;
        });
      })
      .then(function (d) {
        clearTimeout(corte);
        enCurso = null;
        if (parado) return;
        if (bloqueado()) {  // se abrió un modal mientras llegaba: se descarta y se vuelve a pedir en un rato
          programar(REINTENTO_BLOQUEADO);
          return;
        }
        aplicar(d);
        fallos = 0;
        actualizadoEn = Date.now();
        estado('ok');
        pintarReloj();
        programar(INTERVALO);
      })
      .catch(function (e) {
        clearTimeout(corte);
        enCurso = null;
        if (e && e.sesion) {
          sesionVencida();
          return;
        }
        if (document.hidden) return;  // se cortó al ocultar la pestaña: al volver se pide de nuevo
        falla(e && e.http ? 'error' : 'sin-conexion');
      });
  }

  document.addEventListener('visibilitychange', function () {
    if (parado) return;
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
      if (enCurso) enCurso.abort();
      pararReloj();
    } else {
      pintarReloj();
      arrancarReloj();
      programar(0);  // al volver a la pestaña, enseguida
    }
  });
  // Vuelta atrás desde la caché del navegador (bfcache): la página quedó congelada, se refresca enseguida.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted && !parado && !document.hidden) programar(0);
  });

  window.BB = window.BB || {};
  window.BB.vivo = { activo: true, url: URL_VIVO, actualizar: function () { if (!parado) programar(0); } };
  window.dispatchEvent(new CustomEvent('fb-vivo-activo'));
  pintarReloj();
  if (!document.hidden) {
    arrancarReloj();
    programar(INTERVALO);
  }
})();
