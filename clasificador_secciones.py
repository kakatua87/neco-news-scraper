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
                   r"\brobar\b|\brobó\b|ladr[oó]n|aprehend|allanamient|detuvier|apu[ñn]al|balead|sustra[ií]d|hurto|"
                   r"rescat|operativo|atrapad"),
    ("Deportes", r"fútbol|futbol|liga |gol |goles|torneo|campeonat|partido|boxeo|básquet|basquet|tenis|golf|"
                 r"selecci[oó]n|mundial|atleta|piloto|rally|clásico|clasico|cinturon|copa |boca|river plate|racing|"
                 r"f[oó]rmula 1|colapinto|rugby|v[oó]ley|ciclismo|billar|automovilismo|venci[oó]|golea|empat[oó]"),
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
    return por_palabras_estricto(titulo) or normalizar_seccion(etiqueta_scraper)


def por_palabras_estricto(titulo: str) -> Optional[str]:
    """Como `por_palabras`, pero None si ninguna palabra clave ni lugar coincide (no adivina «Local»)."""
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
    return None


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


# ─── Predicción al ingresar: sección prevista, prioridad y descartables ──────────────────────────────

PRIORIDAD_ALTA, PRIORIDAD_MEDIA, PRIORIDAD_DESCARTABLE = 1, 2, 3
# Lo que el editor quiere ver primero: lo de acá y lo de la zona, policiales, política y economía.
PRIORIDAD_POR_SECCION = {
    "Local": PRIORIDAD_ALTA, "Policiales": PRIORIDAD_ALTA, "Política": PRIORIDAD_ALTA,
    "Economía": PRIORIDAD_ALTA, "Zona": PRIORIDAD_ALTA,
    "Deportes": PRIORIDAD_MEDIA, "Sociedad": PRIORIDAD_MEDIA, "Cultura": PRIORIDAD_MEDIA,
    "Tendencias": PRIORIDAD_MEDIA, "Opinión": PRIORIDAD_MEDIA,
}
SECCION_DESCARTABLE = "Descartable"
MOTIVOS_DESCARTE = {"espectaculos": "Espectáculos", "archivo": "Archivo histórico", "comercial": "Nota comercial"}

_LUGARES_PROPIOS = re.compile(
    r"necochea|quequ[eé]n|lober[ií]a|san cayetano|tres arroyos|balcarce|mar del plata|energ[ií]a|claraz|"
    r"juan n\. fern|ram[oó]n santos|la dulce|nicanor olivera|costa bonita"
)
# Farándula y espectáculos "de afuera": solo cuenta si la nota NO nombra Necochea ni la zona (un recital o una obra
# locales son Cultura).
_FARANDULA = re.compile(
    r"far[aá]ndula|vedette|\bactriz\b|\bactor\b|cantante|famos[oa]s?\b|celebrity|celebridad|\breality\b|gran hermano|"
    r"masterchef|bailando por|netflix|streaming|telenovela|alfombra roja|chimento|exnovi[oa]|romance|infidelidad|"
    r"separaci[oó]n de|premios (grammy|oscar|emmy|martin fierro|martín fierro|latin)|miss universo|rumores de|"
    r"\brating\b|mirtha|tinelli|pampita|wanda nara|susana gim[eé]nez|justin bieber|taylor swift|\bbts\b|"
    r"lollapalooza|shakira|madonna|bad bunny|ricardo arjona|alejandro sanz|luck ra|tini stoessel|\bkhea\b|\bu2\b"
)
_ETIQUETA_ESPECTACULOS = re.compile(r"espect|far[aá]ndula|celebr|famosos|televis|entreten")
_URL_ESPECTACULOS = re.compile(r"/(espectaculos|far[aá]ndula|famosos|celebrities|entretenimiento|television)/")
_COMERCIAL = re.compile(
    r"publinota|contenido patrocinado|espacio publicitario|promoci[oó]n imperdible|\d+ ?% de descuento|"
    r"aprovech[aá] (la|esta) (oferta|promo)|\bofertas? (de|por) (apertura|lanzamiento)|inauguramos|te espera en"
)

_PROMPT_DETALLADO = (
    _PROMPT.split("Respondé SOLO JSON")[0]
    + "Además hay dos categorías que NO son del diario y tienen prioridad sobre las demás cuando corresponden:\n"
    "- Espectáculos: farándula, famosos y celebridades, TV, series y streaming, cine y música pop, chimentos, "
    "conciertos o premios internacionales, muertes y romances de artistas. Una obra de teatro, un recital o un evento "
    "cultural de Necochea o la zona NO es Espectáculos (va a Cultura o Local).\n"
    "- Publicidad: notas comerciales que promocionan un comercio, marca o servicio.\n"
    "Respondé SOLO JSON: {\"ok\": true, \"clasificacion\": [{\"i\": 0, \"seccion\": \"Local\"}, ...]} con una entrada "
    "por cada nota, usando exactamente los nombres de la lista de secciones, o \"Espectáculos\" o \"Publicidad\"."
)


def descartable_por_reglas(titulo: str, url: str = "", etiqueta_original: str = "") -> Optional[str]:
    """Motivo ('archivo', 'espectaculos', 'comercial') si la nota se descarta sin consultar al modelo; si no, None."""
    from edicion_auto import es_archivo_historico  # import tardío: edicion_auto también usa este módulo

    if es_archivo_historico(titulo):
        return "archivo"
    if _URL_ESPECTACULOS.search((url or "").lower()) or _ETIQUETA_ESPECTACULOS.search((etiqueta_original or "").lower()):
        return "espectaculos"
    t = _norm(titulo)
    if _COMERCIAL.search(t):
        return "comercial"
    if _FARANDULA.search(t) and not _LUGARES_PROPIOS.search(t):
        return "espectaculos"
    return None


def _resultado(seccion: str, motivo: Optional[str] = None) -> Dict:
    if motivo:
        return {"seccion_prevista": SECCION_DESCARTABLE, "prioridad": PRIORIDAD_DESCARTABLE, "motivo_descarte": motivo}
    return {"seccion_prevista": seccion, "prioridad": PRIORIDAD_POR_SECCION.get(seccion, PRIORIDAD_MEDIA),
            "motivo_descarte": None}


TAMANO_LOTE = 40
MAX_LOTES = 3


def clasificar_detallado(items: List[Dict]) -> List[Dict]:
    """
    Predicción previa de cada grupo al ingresar a la Bandeja.
    `items`: [{"titulo", "etiqueta" (la del scraper, normalizada), "url", "etiqueta_original"}].
    Devuelve, por ítem y en orden, {"seccion_prevista", "prioridad" (1 alta, 2 media, 3 descartable), "motivo_descarte"}.
    Primero reglas sin costo (archivo histórico, etiqueta/URL de espectáculos, farándula, nota comercial); lo que queda va al
    modelo en UNA sola llamada (liviano primero). Si el modelo falla o no responde un ítem, se usan palabras clave.
    """
    if not items:
        return []
    salida: List[Optional[Dict]] = [None] * len(items)
    pendientes: List[int] = []
    for i, it in enumerate(items):
        motivo = descartable_por_reglas(it.get("titulo", ""), it.get("url", ""), it.get("etiqueta_original", ""))
        if motivo:
            salida[i] = _resultado("", motivo)
        else:
            pendientes.append(i)

    # Lotes chicos: con cientos de títulos en una sola respuesta el modelo trunca el JSON o se pasa del límite de
    # tokens por minuto. Lo que no entra en MAX_LOTES (o el modelo no responde) usa palabras clave y, si ni eso
    # alcanza, queda «sin clasificar» hasta la próxima corrida (no se adivina «Local»).
    for desde in range(0, min(len(pendientes), TAMANO_LOTE * MAX_LOTES), TAMANO_LOTE):
        lote = pendientes[desde:desde + TAMANO_LOTE]
        try:
            import fact_gate
            import uso_ia

            lista = "\n".join(
                f"{n}. {items[i]['titulo'][:160]} (el scraper la marcó: {items[i].get('etiqueta') or '?'})"
                for n, i in enumerate(lote)
            )
            with uso_ia.proposito("clasificador"):
                parsed = fact_gate._llm_json(
                    [{"role": "user", "content": f"{_PROMPT_DETALLADO}\n\nNOTAS:\n{lista}"}],
                    clave="clasificacion", max_tokens=4096, liviano=True,
                )
            for fila in (parsed or {}).get("clasificacion", []) or []:
                try:
                    n, sec = int(fila["i"]), str(fila["seccion"]).strip()
                except Exception:
                    continue
                if not 0 <= n < len(lote):
                    continue
                i = lote[n]
                if sec.lower() in ("espectáculos", "espectaculos"):
                    salida[i] = _resultado("", "espectaculos")
                elif sec.lower() == "publicidad":
                    salida[i] = _resultado("", "comercial")
                elif sec in SECCIONES_VALIDAS:
                    salida[i] = _resultado(sec)
        except Exception:
            logger.exception("Predicción con modelo falló para un lote")

    sin_modelo = [i for i in pendientes if salida[i] is None]
    if sin_modelo:
        logger.warning("%s de %s notas sin predicción del modelo: palabras clave", len(sin_modelo), len(items))
    for i in sin_modelo:
        sec = por_palabras_estricto(items[i]["titulo"])
        if sec:
            salida[i] = _resultado(sec)
        else:
            # Sin sección prevista (la Bandeja dice «Sin clasificar») y prioridad media: ni oculta ni destacada.
            # La siguiente corrida la vuelve a intentar.
            salida[i] = {"seccion_prevista": None, "prioridad": PRIORIDAD_MEDIA, "motivo_descarte": None}
    return [s for s in salida if s is not None]
