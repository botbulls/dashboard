"""Filtros Jinja de formato es-AR para mostrar números (solo presentación).

Reciben el valor crudo que imprime el template (texto de ``format_dp`` como ``"-1.24"`` o ``"5416.80"``, un
``Decimal``, un ``float`` calculado en el template, ``None`` o ``"-"``) y devuelven el texto a mostrar:

- separador de miles ``.`` (también con 4 cifras) y decimal ``,``;
- negativo con U+2212 (``−``); el signo copia al del crudo, también en cero (``"-0.00"`` -> ``−0,00``): no se
  esconde una pérdida redondeada;
- sin ``dec`` se respetan los decimales del crudo (no se redondea nada); con ``dec``, un ``float`` se formatea igual
  que ``"%.{dec}f"`` y un texto o ``Decimal`` se cuantiza con ROUND_HALF_EVEN, como ``format_dp``;
- lo que no es un número limpio (``None``, ``"-"``, ``""``, ``"5,416.80"``, ``"1e-05"``) vuelve tal cual: nunca se
  adivina.

El crudo va aparte, sin tocar, en ``data-value`` / ``data-order`` y en los arrays de Chart.js. Nunca se re-parsea un
texto ya formateado: ``"5.416,81"`` leído como número daría 5,41681.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN
from decimal import Decimal
from decimal import InvalidOperation

MENOS = "−"
NBSP = " "
_LIMPIO = re.compile(r"^-?\d+(\.\d+)?$")

MESES = {
    "january": "Enero", "february": "Febrero", "march": "Marzo", "april": "Abril", "may": "Mayo",
    "june": "Junio", "july": "Julio", "august": "Agosto", "september": "Septiembre", "october": "Octubre",
    "november": "Noviembre", "december": "Diciembre",
}


def _texto(valor, dec):
    """Texto decimal canónico (``"-1234.50"``) o None si el valor no es un número limpio."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, float):
        if valor != valor or valor in (float("inf"), float("-inf")):
            return None
        if dec is not None:
            return f"{valor:.{dec}f}"
        s = repr(valor)
        return s if _LIMPIO.match(s) else None
    if isinstance(valor, int):
        s = str(valor)
    elif isinstance(valor, Decimal):
        if not valor.is_finite():
            return None
        s = format(valor, "f")
    else:
        s = str(valor).strip()
    if not _LIMPIO.match(s):
        return None
    if dec is not None:
        try:
            q = Decimal(s).quantize(Decimal(1).scaleb(-dec), rounding=ROUND_HALF_EVEN)
        except InvalidOperation:
            return None
        t = format(q, "f")
        # quantize puede perder el signo de un cero negativo: se conserva el del crudo
        if s.startswith("-") and not t.startswith("-"):
            t = "-" + t
        return t
    return s


def _partes(valor, dec):
    t = _texto(valor, dec)
    if t is None:
        return None
    negativo = t.startswith("-")
    entero, _, fraccion = t.lstrip("-").partition(".")
    entero = f"{int(entero):,}".replace(",", ".")
    return negativo, entero + ("," + fraccion if fraccion else "")


def _signo(negativo, numero, signo):
    if negativo:
        return MENOS
    if signo and any(c in "123456789" for c in numero):
        return "+"
    return ""


def num(valor, dec=None, signo=False):
    """``1852.77`` -> ``1.852,77``; ``-500.00`` -> ``−500,00``; ``0.0022`` -> ``0,0022``."""
    p = _partes(valor, dec)
    if p is None:
        return valor
    negativo, numero = p
    return _signo(negativo, numero, signo) + numero


def usd(valor, dec=None, signo=False):
    """``5416.80`` -> ``$5.416,80``; ``-0.69`` -> ``−$0,69``; con ``signo=True``, ``5.63`` -> ``+$5,63``."""
    p = _partes(valor, dec)
    if p is None:
        return valor
    negativo, numero = p
    return _signo(negativo, numero, signo) + "$" + numero


def pct(valor, dec=None, signo=False):
    """``4.98`` -> ``4,98 %`` (espacio duro); ``-2.27`` -> ``−2,27 %``."""
    p = _partes(valor, dec)
    if p is None:
        return valor
    negativo, numero = p
    return _signo(negativo, numero, signo) + numero + NBSP + "%"


def tendencia(valor):
    """Clase de color de una variación: ``gana`` (> 0), ``pierde`` (< 0, también ``-0.00``), ``cero`` o ``""``.

    Solo decide el color del texto; el signo escrito lo pone ``usd``/``pct``/``num`` con ``signo=True``.
    """
    t = _texto(valor, None)
    if t is None:
        return ""
    if t.startswith("-"):
        return "pierde"
    return "gana" if any(c in "123456789" for c in t) else "cero"


def mes_es(valor):
    """Nombre del mes en inglés (``strftime("%B")`` en el server) -> en español; lo demás vuelve tal cual."""
    if not isinstance(valor, str):
        return valor
    return MESES.get(valor.strip().lower(), valor)


FILTROS = {"num": num, "usd": usd, "pct": pct, "tendencia": tendencia, "mes_es": mes_es}


def registrar(app) -> None:
    """Registra los filtros en la app de Flask (presentación: no cambian rutas ni datos)."""
    for nombre, filtro in FILTROS.items():
        app.add_template_filter(filtro, nombre)
