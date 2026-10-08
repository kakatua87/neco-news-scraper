"""
Controles automáticos sobre una nota ya redactada por la IA, contra los textos
fuente con los que se la generó. Son funciones puras (sin red ni proveedor) para
poder probarlas solas.

Dos problemas que el prompt pide evitar pero los modelos livianos a veces no cumplen:
  1. Copia: tramos largos idénticos a la fuente FUERA de comillas. La nota tiene que
     leerse como redacción propia del medio, no como el texto de otro con retoques.
  2. Citas alteradas: lo que va ENTRE COMILLAS tiene que estar tal cual en la fuente
     (una palabra cambiada, p. ej. "personas" por "fieles", es una cita falsa).
"""

import re
import unicodedata
from typing import List, Sequence, Tuple

# Cantidad de palabras seguidas iguales a la fuente (fuera de comillas) a partir de la
# cual se considera copia. Con 9 no saltan los nombres propios ni las frases hechas.
PALABRAS_COPIA = 9
# Un tramo copiado solo cuenta si tiene al menos tantas palabras "de contenido" en
# minúscula. Así no disparan falsos positivos los datos que no se pueden decir de otra
# forma: nombres con cargo ("la ministra de Deportes, Juventud y Empleo, Annick ..."),
# fechas ("5 de octubre de 2026") ni conectores.
MIN_PALABRAS_MINUSCULA = 5

_NO_CUENTAN = {
    # artículos, preposiciones y conectores
    "el", "la", "los", "las", "un", "una", "unos", "unas", "lo", "al", "del", "de", "en", "con", "por", "para",
    "a", "y", "e", "o", "u", "que", "se", "su", "sus", "es", "son", "fue", "ser", "como", "mas", "pero", "sin",
    "sobre", "entre", "hasta", "desde", "ante", "tras", "este", "esta", "estos", "estas",
    # meses y días (parte de una fecha)
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
    "noviembre", "diciembre", "lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo",
}
# Las citas más cortas que esto (una palabra suelta entre comillas, un título) no se verifican.
MIN_PALABRAS_CITA = 5

_RE_PALABRA = re.compile(r"\w+", re.UNICODE)
_RE_CITA = re.compile(r"“([^”]+)”|\"([^\"]+)\"|«([^»]+)»")


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def _tokens(texto: str) -> List[str]:
    """Palabras normalizadas (minúsculas, sin tildes) para comparar sin depender de puntuación."""
    return [_sin_tildes(m.group(0).lower()) for m in _RE_PALABRA.finditer(texto)]


def _dividir_citas(cuerpo: str) -> Tuple[List[str], List[str]]:
    """Devuelve (tramos fuera de comillas, citas). Los tramos se comparan por separado
    para que una coincidencia no cruce una cita."""
    fuera: List[str] = []
    citas: List[str] = []
    pos = 0
    for m in _RE_CITA.finditer(cuerpo):
        fuera.append(cuerpo[pos:m.start()])
        citas.append(next(g for g in m.groups() if g))
        pos = m.end()
    fuera.append(cuerpo[pos:])
    return fuera, citas


def frases_copiadas(cuerpo: str, fuentes: Sequence[str], n: int = PALABRAS_COPIA) -> List[str]:
    """Tramos de `n` o más palabras seguidas que coinciden con alguna fuente fuera de comillas."""
    ngramas = set()
    for fuente in fuentes:
        tk = _tokens(fuente)
        for i in range(len(tk) - n + 1):
            ngramas.add(tuple(tk[i:i + n]))
    if not ngramas:
        return []

    encontrados: List[str] = []
    for tramo in _dividir_citas(cuerpo)[0]:
        palabras = list(_RE_PALABRA.finditer(tramo))
        tk = [_sin_tildes(m.group(0).lower()) for m in palabras]
        cubierto = [False] * len(tk)
        for i in range(len(tk) - n + 1):
            if tuple(tk[i:i + n]) in ngramas:
                for j in range(i, i + n):
                    cubierto[j] = True

        i = 0
        while i < len(tk):
            if not cubierto[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(tk) and cubierto[j + 1]:
                j += 1
            originales = [m.group(0) for m in palabras[i:j + 1]]
            minusculas = sum(
                1 for w in originales
                if w[:1].islower() and w.isalpha() and _sin_tildes(w.lower()) not in _NO_CUENTAN
            )
            if minusculas >= MIN_PALABRAS_MINUSCULA:
                encontrados.append(tramo[palabras[i].start():palabras[j].end()])
            i = j + 1
    return encontrados


def citas_alteradas(cuerpo: str, fuentes: Sequence[str], minimo: int = MIN_PALABRAS_CITA) -> List[str]:
    """Citas entre comillas (de `minimo` palabras o más) que NO figuran tal cual en ninguna fuente."""
    textos_fuente = [" ".join(_tokens(f)) for f in fuentes]
    alteradas: List[str] = []
    for cita in _dividir_citas(cuerpo)[1]:
        tk = _tokens(cita)
        if len(tk) < minimo:
            continue
        normalizada = " ".join(tk)
        if not any(normalizada in texto for texto in textos_fuente):
            alteradas.append(cita)
    return alteradas


def revisar(cuerpo: str, fuentes: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(frases copiadas, citas alteradas)."""
    return frases_copiadas(cuerpo, fuentes), citas_alteradas(cuerpo, fuentes)


def armar_correccion(copiadas: Sequence[str], alteradas: Sequence[str]) -> str:
    """Texto que se agrega al pedido para pedirle al modelo que corrija su borrador."""
    partes = ["\n\nREVISIÓN DE TU BORRADOR ANTERIOR. Hay que corregirlo:\n"]
    if copiadas:
        partes.append(
            "- Estos tramos están copiados casi tal cual de la fuente. La nota tiene que ser "
            "redacción propia: reescribilos con otra estructura y otras palabras (si es una "
            "declaración, citá solo la frase más fuerte entre comillas y contá el resto):\n"
        )
        partes.extend(f'    · "{c[:220]}"\n' for c in list(copiadas)[:4])
    if alteradas:
        partes.append(
            "- Estas citas entre comillas NO coinciden palabra por palabra con la fuente. Una cita "
            "no se puede alterar: copiala exacta o contala sin comillas:\n"
        )
        partes.extend(f'    · "{c[:220]}"\n' for c in list(alteradas)[:4])
    partes.append("Devolvé el JSON completo de nuevo, con la nota corregida.\n")
    return "".join(partes)
