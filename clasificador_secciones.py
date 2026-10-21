"""
Clasificación previa de las candidatas de la edición por sección del portal.

El scraper etiqueta casi todo como "Local" (117 de 146 notas), así que no sirve para repartir la
edición entre secciones. Con una sola llamada al modelo se clasifica el lote de títulos: Policiales
y Deportes por tema, Local para todo lo que ocurre en Necochea y la región, y el resto por tema.
Si el modelo falla se cae a palabras clave y, en última instancia, a la etiqueta del scraper.
"""

import logging
import re
import unicodedata
from typing import Dict, List, Optional

logger = logging.getLogger("neconews.clasificador")

SECCIONES_VALIDAS = (
    "Política", "Economía", "Policiales", "Local", "Deportes",
    "Sociedad", "Salud", "Cultura", "Tecnología", "Educación",
)

_PROMPT = (
    "Sos el editor de un diario de Necochea. Clasificá cada nota en UNA sección.\n"
    "Secciones: " + ", ".join(SECCIONES_VALIDAS) + ".\n"
    "Orden de decisión:\n"
    "1. Policiales: delitos, accidentes, causas judiciales, rescates, operativos (sea local o no).\n"
    "2. Deportes: cualquier hecho deportivo (sea local o no).\n"
    "3. Local: todo hecho que ocurre en Necochea, Quequén o la región, o que los afecta de forma "
    "concreta (gobierno municipal, puerto, servicios, eventos, aniversarios, clima local, vecinos, "
    "comercio local, cultura y educación locales), aunque tenga un tema de otra sección.\n"
    "4. Si NO es local: Política (gobierno, elecciones, legislación), Economía (precios, empleo, "
    "finanzas, tarifas), Educación, Salud, Cultura, Tecnología según el tema principal.\n"
    "5. Sociedad: lo nacional o general que no entra claramente en las anteriores.\n"
    "Respondé SOLO JSON: {\"ok\": true, \"clasificacion\": [{\"i\": 0, \"seccion\": \"Local\"}, ...]} "
    "con una entrada por cada nota, usando exactamente los nombres de sección de la lista."
)

# Orden importa: las dos primeras (Policiales, Deportes) mandan sobre "Local" en el fallback.
_PALABRAS = (
    ("Policiales", r"polic|detenid|arrest|robo|robaron|choc|herid|accidente|homicid|asesin|secuestr|"
                   r"incendi|balacera|vuelc|despist|prefectura|fiscal|condena|estafa|narco|droga|apresad|"
                   r"rescat|operativo|atrapad"),
    ("Deportes", r"fútbol|futbol|liga |gol |goles|torneo|campeonat|partido|boxeo|básquet|basquet|tenis|golf|"
                 r"selecci[oó]n|mundial|atleta|piloto|rally|clásico|clasico|cinturon"),
    ("Educación", r"escuela|docente|universit|unicen|clases|alumnos|estudiantes|paro docente"),
    ("Política", r"diputad|senad|gobierno|intendente|concejo|concejal|elecciones|milei|kicillof|dnu|"
                 r"congreso|ministr|presidente|peronismo|oposici"),
    ("Economía", r"dólar|dolar|inflaci|riesgo país|riesgo pais|salario|tarifa|empleo|banco|inversi|"
                 r"econom|precios|vtv"),
    ("Salud", r"hospital|salud|vacun|médic|medic|sanatorio|dengue|mamograf|cáncer|cancer"),
    ("Cultura", r"festival|música|musica|banda|concierto|teatro|libro|muestra|cine|artista|homenaje|"
                r"tributo|martín fierro|martin fierro|show"),
    ("Tecnología", r"tecnolog|inteligencia artificial|celular|aplicaci"),
)


def _norm(t: str) -> str:
    return unicodedata.normalize("NFC", (t or "").lower())


def por_palabras(titulo: str, etiqueta_scraper: str = "") -> str:
    """Fallback sin modelo: Policiales y Deportes por palabras clave, Local por lugar, el resto por tema."""
    t = _norm(titulo)
    for seccion, patron in _PALABRAS[:2]:  # Policiales, Deportes
        if re.search(patron, t):
            return seccion
    if re.search(r"necochea|quequ[eé]n|costa atl|lober[ií]a|san cayetano", t):
        return "Local"
    for seccion, patron in _PALABRAS[2:]:
        if re.search(patron, t):
            return seccion
    return etiqueta_scraper if etiqueta_scraper in SECCIONES_VALIDAS else "Sociedad"


def clasificar(items: List[Dict]) -> List[str]:
    """
    `items`: [{"titulo": str, "etiqueta": str}]. Devuelve una sección por ítem, en el mismo orden.
    Un modelo caído o una respuesta incompleta no frena la edición: esos ítems usan palabras clave.
    """
    if not items:
        return []
    resultado: List[Optional[str]] = [None] * len(items)
    try:
        import fact_gate  # reutiliza la rotación de keys y los reintentos de Gemini

        lista = "\n".join(
            f"{i}. {it['titulo'][:160]} (el scraper la marcó: {it.get('etiqueta') or '?'})"
            for i, it in enumerate(items)
        )
        import uso_ia

        with uso_ia.proposito("clasificador"):
            parsed = fact_gate._llm_json(
                [{"role": "user", "content": f"{_PROMPT}\n\nNOTAS:\n{lista}"}], clave="clasificacion", max_tokens=4096
            )
        for fila in (parsed or {}).get("clasificacion", []) or []:
            try:
                i, sec = int(fila["i"]), str(fila["seccion"]).strip()
            except Exception:
                continue
            if 0 <= i < len(items) and sec in SECCIONES_VALIDAS:
                resultado[i] = sec
    except Exception:
        logger.exception("Clasificación con modelo falló; se usan palabras clave")

    faltan = sum(1 for r in resultado if r is None)
    if faltan:
        logger.warning("%s de %s notas sin clasificar por el modelo: palabras clave", faltan, len(items))
    return [r or por_palabras(it["titulo"], it.get("etiqueta", "")) for r, it in zip(resultado, items)]
