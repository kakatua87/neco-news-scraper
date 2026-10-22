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

# Secciones oficiales del portal (el menú del sitio): copia de SECCIONES_MENU en neco-news/src/lib/secciones.ts.
# Hay que mantenerlas iguales: la IA nunca puede publicar en una sección que el portal no tiene ni crear una nueva.
SECCIONES_PORTAL = (
    "Local", "Policiales", "Política", "Economía", "Deportes", "Zona", "Sociedad", "Opinión", "Cultura", "Tendencias",
)
# Secciones de servicio que carga el sistema aparte (no las elige la IA).
SECCIONES_SERVICIO = ("Obituarios", "Farmacias")
# Las que la IA puede elegir al clasificar por tema (Opinión es de autor: solo se carga a mano).
SECCIONES_VALIDAS = tuple(s for s in SECCIONES_PORTAL if s != "Opinión")

# Nombres anteriores o variantes que se llevan a una sección oficial.
_EQUIVALENCIAS = {
    "economia": "Economía", "finanzas": "Economía", "negocios": "Economía", "politica": "Política",
    "policial": "Policiales", "policiales y justicia": "Policiales",
    "deporte": "Deportes",
    "salud": "Sociedad", "educación": "Sociedad", "educacion": "Sociedad",
    "tecnología": "Tendencias", "tecnologia": "Tendencias", "tendencia": "Tendencias",
    "general": "Sociedad", "nacional": "Sociedad", "internacional": "Sociedad", "país": "Sociedad", "pais": "Sociedad",
    "clima": "Local", "región": "Zona", "region": "Zona", "regional": "Zona",
    "opinion": "Opinión",
}


def normalizar_seccion(valor: Optional[str], respaldo: str = "Sociedad") -> str:
    """
    Devuelve siempre una sección oficial del portal (o de servicio). Lo que no existe se lleva a su equivalente
    ("Salud" → "Sociedad", "Tecnología" → "Tendencias"...) y, si no hay ninguna, a `respaldo`: nunca se crea una sección nueva.
    """
    v = (valor or "").strip()
    if not v:
        return respaldo if respaldo in SECCIONES_PORTAL else "Sociedad"
    oficiales = {s.lower(): s for s in SECCIONES_PORTAL + SECCIONES_SERVICIO}
    if v.lower() in oficiales:
        return oficiales[v.lower()]
    equivalente = _EQUIVALENCIAS.get(v.lower())
    if equivalente:
        return equivalente
    logger.warning("Sección '%s' no existe en el portal: se usa '%s'", v, respaldo)
    return respaldo if respaldo in SECCIONES_PORTAL else "Sociedad"

_PROMPT = (
    "Sos el editor de un diario de Necochea. Clasificá cada nota en UNA sección.\n"
    "Secciones: " + ", ".join(SECCIONES_VALIDAS) + " (no existe ninguna otra).\n"
    "Orden de decisión:\n"
    "1. Policiales: delitos, accidentes, causas judiciales, rescates, operativos (sea local o no).\n"
    "2. Deportes: cualquier hecho deportivo (sea local o no).\n"
    "3. Local: todo hecho que ocurre en Necochea o Quequén, o que los afecta de forma concreta (gobierno "
    "municipal, puerto, servicios, eventos, aniversarios, clima local, vecinos, comercio local, cultura y "
    "educación locales), aunque tenga un tema de otra sección.\n"
    "4. Zona: hechos que ocurren en el resto del partido de Necochea (Juan N. Fernández, Claraz, Energía, "
    "Ramón Santos, Nicanor Olivera/La Dulce, Costa Bonita) o en localidades vecinas: Lobería, San Cayetano, "
    "Tres Arroyos, Balcarce, Mar del Plata y la región. Si el hecho es de Necochea/Quequén va a Local, no a Zona.\n"
    "5. Si NO es de Necochea/Quequén ni de la zona: Economía (dólar, inflación, precios, tarifas, salarios, "
    "empleo, impuestos, inversiones, empresas, comercio, agro, finanzas), Política (gobierno, elecciones, "
    "legislación, gestión pública) o Cultura según el tema principal.\n"
    "6. Tendencias: temas virales, redes sociales, tecnología, ciencia, estilo de vida, consumo, curiosidades.\n"
    "7. Sociedad: lo nacional o general (incluye salud y educación) que no entra claramente en las anteriores.\n"
    "Respondé SOLO JSON: {\"ok\": true, \"clasificacion\": [{\"i\": 0, \"seccion\": \"Local\"}, ...]} "
    "con una entrada por cada nota, usando exactamente los nombres de sección de la lista."
)

# Orden importa: las dos primeras (Policiales, Deportes) mandan sobre "Local" en el fallback.
_PALABRAS = (
    ("Policiales", r"polic|detenid|arrest|robo|robaron|choc|choq|herid|accidente|homicid|asesin|secuestr|"
                   r"incendi|balacera|vuelc|despist|prefectura|fiscal|condena|estafa|narco|droga|apresad|"
                   r"rescat|operativo|atrapad"),
    ("Deportes", r"fútbol|futbol|liga |gol |goles|torneo|campeonat|partido|boxeo|básquet|basquet|tenis|golf|"
                 r"selecci[oó]n|mundial|atleta|piloto|rally|clásico|clasico|cinturon"),
    ("Economía", r"dólar|dolar|inflaci|riesgo país|riesgo pais|salario|tarifa|empleo|banco|inversi|econom|"
                 r"precios|vtv|rigi|exportaci|importaci|petr[oó]leo|cosecha|soja|aguinaldo|impuest|deuda|"
                 r"mercado|bolsa|plazo fijo|jubilaci"),
    ("Política", r"diputad|senad|gobierno|intendente|concejo|concejal|elecciones|milei|kicillof|dnu|"
                 r"congreso|ministr|presidente|peronismo|oposici"),
    ("Cultura", r"festival|música|musica|banda|concierto|teatro|libro|muestra|cine|artista|homenaje|"
                r"tributo|martín fierro|martin fierro|show"),
    ("Tendencias", r"tecnolog|inteligencia artificial|celular|aplicaci|viral|tiktok|instagram|redes sociales|"
                   r"influencer|moda|gastronom"),
)


def _norm(t: str) -> str:
    return unicodedata.normalize("NFC", (t or "").lower())


def por_palabras(titulo: str, etiqueta_scraper: str = "") -> str:
    """Fallback sin modelo: Policiales y Deportes por palabras clave, Local por lugar, el resto por tema."""
    t = _norm(titulo)
    for seccion, patron in _PALABRAS[:2]:  # Policiales, Deportes
        if re.search(patron, t):
            return seccion
    if re.search(r"necochea|quequ[eé]n", t):
        return "Local"
    if re.search(r"lober[ií]a|san cayetano|tres arroyos|balcarce|mar del plata|energ[ií]a|claraz|"
                 r"juan n\. fern|ramón santos|ramon santos|la dulce|nicanor olivera|costa bonita", t):
        return "Zona"
    for seccion, patron in _PALABRAS[2:]:
        if re.search(patron, t):
            return seccion
    return normalizar_seccion(etiqueta_scraper)


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
