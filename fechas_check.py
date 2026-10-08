"""
Corrección determinista de "este <día> <número>" en notas ya redactadas.

"Este miércoles 14 de octubre" solo es correcto si el miércoles 14 cae en la misma semana
(lunes a domingo) que la fecha de hoy. Si cae en la semana siguiente se dice "el próximo
miércoles 14 de octubre", y si es más adelante, simplemente "el miércoles 14 de octubre".
Los modelos livianos a veces ignoran esta regla del prompt, así que se aplica por código.
Funciones puras (sin red) para poder probarlas solas.
"""

import re
from datetime import date, timedelta
from typing import Optional

_DIAS = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}
_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

_RE_ESTE = re.compile(
    r"\b(?P<este>[Ee]ste)\s+(?P<dia>" + "|".join(_DIAS) + r")\s+(?P<num>\d{1,2})"
    r"(?P<mes>\s+de\s+(?:" + "|".join(_MESES) + r"))?",
    re.IGNORECASE,
)


def _fecha_mencionada(hoy: date, dia_semana: int, numero: int, mes: Optional[int]) -> Optional[date]:
    """La fecha a la que se refiere el texto: la más cercana a hoy cuyo día de la semana coincide."""
    candidatas = []
    meses = [mes] if mes else [hoy.month, hoy.month % 12 + 1]
    for m in meses:
        for anio in (hoy.year, hoy.year + 1):
            try:
                f = date(anio, m, numero)
            except ValueError:
                continue
            if f.weekday() == dia_semana:
                candidatas.append(f)
    if not candidatas:
        return None  # el día de la semana no coincide con el número: no se toca
    return min(candidatas, key=lambda f: abs((f - hoy).days))


def corregir_este(texto: str, hoy: date) -> str:
    """Reemplaza "este X N" por "el próximo X N" / "el X N" cuando la fecha no es de esta semana."""
    if not texto:
        return texto
    lunes_de_hoy = hoy - timedelta(days=hoy.weekday())

    def reemplazo(m: "re.Match[str]") -> str:
        dia = m.group("dia")
        mes_txt = (m.group("mes") or "").strip().lower().replace("de ", "", 1).strip()
        fecha = _fecha_mencionada(hoy, _DIAS[dia.lower()], int(m.group("num")), _MESES.get(mes_txt))
        if fecha is None:
            return m.group(0)
        semanas = (fecha - lunes_de_hoy).days // 7
        if semanas == 0:
            return m.group(0)  # misma semana: "este" es correcto
        resto = m.group(0)[len(m.group("este")):]  # " miércoles 14 de octubre"
        if semanas == 1:
            return ("El próximo" if m.group("este")[0].isupper() else "el próximo") + resto
        if semanas < 0:
            return ("El pasado" if m.group("este")[0].isupper() else "el pasado") + resto
        return ("El" if m.group("este")[0].isupper() else "el") + resto

    return _RE_ESTE.sub(reemplazo, texto)
