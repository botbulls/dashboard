/* Tema de la página de la API key antes de pintar (la CSP no deja el <script> en línea de base.html). Mismo
   localStorage que el resto del panel. */
(function () {
  var KEY = 'futuresboard.theme', TEMAS = ['auto', 'light', 'dark'], d = document.documentElement;
  var t = d.getAttribute('data-default-theme') || 'auto';
  try { t = localStorage.getItem(KEY) || t; } catch (e) { /* sin localStorage: el del usuario */ }
  d.setAttribute('data-theme', TEMAS.indexOf(t) >= 0 ? t : 'auto');
})();
