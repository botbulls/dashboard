/* formato.js: formato es-AR para presentación. Mismo contrato que futuresboard/formato.py.
   Solo para mostrar: los cálculos, data-value, data-order y los arrays de Chart.js siguen con el número crudo.
   window.BB.fmt.num(v, {dec, signo}) · usd(...) · pct(...) · tendencia(v) · token('--bb-...')
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

  window.BB = window.BB || {};
  window.BB.fmt = {
    num: hacer('', ''),
    usd: hacer('', '$'),
    pct: hacer(NBSP + '%', ''),
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
