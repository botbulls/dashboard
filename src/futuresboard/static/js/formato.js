/* formato.js: formato es-AR para presentación. Mismo contrato que futuresboard/formato.py.
   Solo para mostrar: los cálculos, data-value, data-order y los arrays de Chart.js siguen con el número crudo.
   window.BB.fmt.num(v, {dec, signo}) · usd(...) · pct(...) · tendencia(v) · token('--bb-...')
   usdEje (callback de ticks de Chart.js) · dias(lista) / fecha(d) (etiquetas y tooltips de fechas)
   Lo que no es un número limpio vuelve tal cual (como texto). */
(function () {
  'use strict';
  var MENOS = '−', NBSP = ' ', LIMPIO = /^-?\d+(\.\d+)?$/;

  function texto(v, dec) {
    if (v === null || v === undefined || typeof v === 'boolean') return null;
    var s;
    if (typeof v === 'number') {
      if (!isFinite(v)) return null;
      s = (dec === undefined || dec === null) ? String(v) : v.toFixed(dec);
      if (Object.is(v, -0) || (v < 0 && s.charAt(0) !== '-')) s = '-' + s.replace('-', '');
    } else {
      s = String(v).trim();
      if (/^\+\d/.test(s)) s = s.slice(1);  // "+0.00": positivo explícito (variaciones del rescate)
      if (!LIMPIO.test(s)) return null;
      if (dec !== undefined && dec !== null) {
        var neg = s.charAt(0) === '-';
        s = Number(s).toFixed(dec);
        if (neg && s.charAt(0) !== '-') s = '-' + s;
      }
    }
    return LIMPIO.test(s) ? s : null;
  }

  function partes(v, dec) {
    var t = texto(v, dec);
    if (t === null) return null;
    var negativo = t.charAt(0) === '-';
    var p = t.replace('-', '').split('.');
    return { negativo: negativo, numero: p[0].replace(/\B(?=(\d{3})+(?!\d))/g, '.') + (p[1] ? ',' + p[1] : '') };
  }

  function signo(p, conSigno) {
    if (p.negativo) return MENOS;
    return conSigno && /[1-9]/.test(p.numero) ? '+' : '';
  }

  function hacer(sufijo, prefijo) {
    return function (v, o) {
      o = o || {};
      var p = partes(v, o.dec);
      if (p === null) return v === null || v === undefined ? '' : String(v);
      return signo(p, o.signo) + (prefijo || '') + p.numero + (sufijo || '');
    };
  }

  function tendencia(v) {
    var t = texto(v);
    if (t === null) return '';
    if (t.charAt(0) === '-') return 'pierde';
    return /[1-9]/.test(t) ? 'gana' : 'cero';
  }

  /* Eje de montos de Chart.js 3 (callback(value, index, ticks)). Los decimales salen de los ticks del eje: los
     mínimos que distinguen a todos (0 si son enteros; si no, 2 o más). Así ticks distintos no se leen iguales
     ($0, $0, $0) ni una pérdida chica se lee como −$1. El 0 de la grilla con ruido de coma flotante (-1e-17) es 0. */
  function decimalesEje(vs) {
    for (var d = 0; d <= 4; d++) {
      var f = Math.pow(10, d);
      var ok = vs.every(function (v) { return Math.abs(Math.round(v * f) - v * f) <= 1e-6 * Math.max(1, Math.abs(v * f)); });
      if (ok) return d === 0 ? 0 : Math.max(d, 2);
    }
    return 4;
  }

  function usdEje(v, i, ticks) {
    var vs = (ticks || []).map(function (t) { return t && t.value; })
      .filter(function (x) { return typeof x === 'number' && isFinite(x); });
    if (typeof v !== 'number' || !isFinite(v)) return v;
    if (!vs.length) vs = [v];
    var paso = 0;
    for (var k = 1; k < vs.length; k++) {
      var p = Math.abs(vs[k] - vs[k - 1]);
      if (p > 0 && (!paso || p < paso)) paso = p;
    }
    if (paso && Math.abs(v) < paso * 1e-6) v = 0;
    return window.BB.fmt.usd(v, { dec: decimalesEje(vs.map(function (x) { return paso && Math.abs(x) < paso * 1e-6 ? 0 : x; })) });
  }

  /* Fechas 'AAAA-MM-DD' de los gráficos: dd/mm si todas son del mismo año, dd/mm/aa si el rango cruza años
     (si no, en "Todo" se repiten los dd/mm). fecha(): dd/mm/aaaa para el título del tooltip. */
  var FECHA = /^(\d{4})-(\d{2})-(\d{2})/;
  function dias(lista) {
    lista = lista || [];
    var anios = {};
    lista.forEach(function (d) { var m = FECHA.exec(typeof d === 'string' ? d : ''); if (m) anios[m[1]] = 1; });
    var varios = Object.keys(anios).length > 1;
    return lista.map(function (d) {
      var m = FECHA.exec(typeof d === 'string' ? d : '');
      return m ? m[3] + '/' + m[2] + (varios ? '/' + m[1].slice(2) : '') : d;
    });
  }
  function fecha(d) {
    var m = FECHA.exec(typeof d === 'string' ? d : '');
    return m ? m[3] + '/' + m[2] + '/' + m[1] : (d === null || d === undefined ? '' : String(d));
  }

  window.BB = window.BB || {};
  window.BB.fmt = {
    num: hacer('', ''),
    usd: hacer('', '$'),
    pct: hacer(NBSP + '%', ''),
    usdEje: usdEje,
    dias: dias,
    fecha: fecha,
    tendencia: tendencia,
    /* Colores de gráficos leídos de los tokens (se recalculan al cambiar el tema) */
    token: function (nombre) { return getComputedStyle(document.documentElement).getPropertyValue(nombre).trim(); },
  };
  /* Textos de DataTables en español (solo la interfaz: el orden usa los crudos de data-order). */
  window.BB.dataTablesEs = {
    search: 'Buscar:', lengthMenu: 'Mostrar _MENU_', info: 'Mostrando _START_ a _END_ de _TOTAL_',
    infoEmpty: 'Sin filas', infoFiltered: '(de _MAX_)', zeroRecords: 'No hay coincidencias', emptyTable: 'Sin datos',
    paginate: { first: 'Primera', last: 'Última', next: 'Siguiente', previous: 'Anterior' },
    aria: { sortAscending: ': ordenar de menor a mayor', sortDescending: ': ordenar de mayor a menor' },
  };
})();
