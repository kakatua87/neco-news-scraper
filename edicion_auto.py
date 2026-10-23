"""
Edición matutina automática (Fase 0).

Elige los grupos 'raw' más importantes de las últimas horas, los procesa con
`pipeline_ia` y los deja en 'pendiente' registrando el score en `edicion_log`.
Todavía NO publica: la auto-publicación depende de los gates de imagen y de
verificación (fases 1-3).
"""

import logging
import math
import os
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import uso_ia

logger = logging.getLogger("neconews.edicion")

VENTANA_HORAS = 14
SECCIONES_EXCLUIDAS_DEFAULT = ("Obituarios", "Farmacias")
PESOS_DEFAULT = {
    "fuentes": 3.0,       # por fuente distinta (log-escalado)
    "frescura": 2.0,      # máx. puntos por nota reciente
    "imagen": 0.5,        # tiene foto
    "secciones": {"Policiales": 1.5, "Política": 1.2, "Local": 1.5, "Deportes": 0.8},
    "max_por_seccion": 2,
}


def _parse_ts(valor: str) -> datetime:
    return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))


def _n_fuentes(notas: List[Dict]) -> int:
    return len({(n.get("fuente") or "").strip().lower() for n in notas} - {""}) or 1


_STOP = {
    "de", "la", "el", "los", "las", "un", "una", "unos", "unas", "en", "por", "con", "para", "del",
    "al", "y", "a", "o", "que", "se", "su", "sus", "lo", "es", "fue", "ser", "como", "mas", "sin",
    "sobre", "entre", "tras", "ante", "este", "esta", "estos", "estas", "ya", "no", "si", "le",
}
UMBRAL_DUPLICADO = 0.5
MIN_COMPARTIDOS = 3


def _tokens(titulo: str) -> set:
    """Tokens de contenido, sin tildes y truncados a 5 letras (despistó ~ despiste)."""
    t = unicodedata.normalize("NFKD", (titulo or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return {w[:5] for w in re.findall(r"[a-z0-9]+", t) if w not in _STOP and len(w) > 1}


def son_misma_noticia(titulo_a: str, titulo_b: str) -> bool:
    a, b = _tokens(titulo_a), _tokens(titulo_b)
    if not a or not b:
        return False
    comunes = len(a & b)
    return comunes >= MIN_COMPARTIDOS and comunes / min(len(a), len(b)) >= UMBRAL_DUPLICADO


def fusionar_duplicados(cands: List[Dict]) -> List[Dict]:
    """
    Junta grupos distintos que cuentan el mismo hecho (el agrupador del scraper
    no los une si cambia la sección o el enfoque del título). El grupo con más
    fuentes manda; las notas de los demás se suman para que la IA las use como
    fuentes adicionales y para que `n_fuentes` refleje la cobertura real.
    """
    orden = sorted(cands, key=lambda g: (-_n_fuentes(g["notas"]), g["grupo_id"]))
    resultado: List[Dict] = []
    for g in orden:
        titulo = g["notas"][0].get("titulo", "")
        destino = next((r for r in resultado if son_misma_noticia(r["notas"][0].get("titulo", ""), titulo)), None)
        if destino is None:
            g.setdefault("fusionados", [])
            resultado.append(g)
        else:
            destino["notas"] = destino["notas"] + g["notas"]
            destino["fusionados"].append(g["grupo_id"])
    return resultado


def inicio_dia_ar_utc() -> datetime:
    """Inicio del día argentino de hoy, en UTC."""
    ahora_ar = datetime.now(timezone.utc) - timedelta(hours=3)
    return ahora_ar.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=3)


def candidatos(client, horas: int = VENTANA_HORAS, desde: Optional[datetime] = None,
               grupo_ids: Optional[List[str]] = None) -> List[Dict]:
    """
    Agrupa notas raw por grupo_id. Por defecto las de las últimas `horas`; con `desde` las creadas desde
    esa fecha (p. ej. hoy); con `grupo_ids` solo esos grupos, sin importar la antigüedad.
    """
    consulta = (
        client.client.table("noticias")
        .select("id, grupo_id, titulo, fuente, seccion, seccion_prevista, prioridad, imagen_url, created_at")
        .eq("estado", "raw")
        .not_.is_("grupo_id", "null")
    )
    if grupo_ids:
        consulta = consulta.in_("grupo_id", list(grupo_ids))
    else:
        limite = desde or (datetime.now(timezone.utc) - timedelta(hours=horas))
        consulta = consulta.gte("created_at", limite.isoformat())
    resp = consulta.execute()
    grupos: Dict[str, List[Dict]] = defaultdict(list)
    for fila in resp.data or []:
        grupos[fila["grupo_id"]].append(fila)
    return [{"grupo_id": gid, "notas": notas} for gid, notas in grupos.items()]


def puntuar(grupo: Dict, pesos: Dict, ahora: Optional[datetime] = None) -> float:
    ahora = ahora or datetime.now(timezone.utc)
    notas = grupo["notas"]
    score = pesos["fuentes"] * math.log2(1 + _n_fuentes(notas))
    ultima = max(_parse_ts(n["created_at"]) for n in notas)
    horas = max((ahora - ultima).total_seconds() / 3600, 0)
    score += pesos["frescura"] * max(0.0, 1 - horas / VENTANA_HORAS)
    if any(n.get("imagen_url") for n in notas):
        score += pesos["imagen"]
    score += pesos["secciones"].get(notas[0].get("seccion"), 1.0)
    # Lo prioritario (local, policiales, política, economía, zona) pesa más que lo medio.
    if _prioridad_grupo(grupo) == 1:
        score += 1.0
    return round(score, 3)


def _prioridad_grupo(grupo: Dict) -> int:
    """Prioridad cargada por el scraper (1 alta, 2 media, 3 descartable); 2 si todavía no fue clasificada."""
    valores = [n.get("prioridad") for n in grupo["notas"] if n.get("prioridad")]
    return min(valores) if valores else 2


# Reparto de la edición: Locales y Policiales son lo principal; el resto de las secciones con
# material entra al menos una vez. Los topes evitan que una sola sección se coma la edición.
OBJETIVO_POR_SECCION = {"Local": 2, "Policiales": 2}
OBJETIVO_DEFAULT = 1
TOPE_POR_SECCION = {"Local": 3, "Policiales": 3}
TOPE_DEFAULT = 2
MAX_A_CLASIFICAR = 40
# Tras 3 notas de una sección que no pasan los controles se pasa a otra (el 19/10 seis Locales seguidas
# agotaron el tope de procesamiento sin probar el resto de las secciones).
MAX_FALLOS_POR_SECCION = 3
# Tope de duración de toda la edición: no se empiezan notas nuevas pasado este tiempo.
MAX_MINUTOS_EDICION = 70


_PATRON_ARCHIVO = re.compile(
    r"archivo hist[oó]rico|efem[eé]rides|^hace \d+ a[nñ]os"
    r"|^(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+\d{1,2}\s+de\s+\w+\s+(de\s+)?\d{4}\s*$",
    re.IGNORECASE,
)


def es_archivo_historico(titulo: str) -> bool:
    """
    Retrospectivas de hace décadas que publican algunos medios ("Sábado 19 de octubre 1996",
    "Archivo Histórico…"): no son noticias y consumían cupo de la edición.
    """
    return bool(_PATRON_ARCHIVO.search((titulo or "").strip()))


def ordenar(cands: List[Dict], cfg: Dict) -> List[Dict]:
    """Puntúa y ordena las candidatas (mejor primero) sin las secciones excluidas del scraper."""
    pesos = {**PESOS_DEFAULT, **(cfg.get("edicion_pesos") or {})}
    excluidas = set(cfg.get("edicion_secciones_excluidas") or SECCIONES_EXCLUIDAS_DEFAULT)
    for g in cands:
        g["seccion"] = g["notas"][0].get("seccion") or "Local"
        g["n_fuentes"] = _n_fuentes(g["notas"])
        g["score"] = puntuar(g, pesos)
    return sorted(
        (
            g for g in cands
            if g["seccion"] not in excluidas
            and _prioridad_grupo(g) != 3
            and not any(es_archivo_historico(n.get("titulo", "")) for n in g["notas"])
        ),
        key=lambda g: g["score"],
        reverse=True,
    )


def clasificar_pool(pool: List[Dict], cfg: Dict) -> List[Dict]:
    """
    Reemplaza la etiqueta del scraper por la sección real (por tema) y descarta las excluidas.
    La sección del scraper queda en `seccion_scraper`.
    """
    secciones = _secciones_de(pool, [g["seccion"] for g in pool])
    excluidas = set(cfg.get("edicion_secciones_excluidas") or SECCIONES_EXCLUIDAS_DEFAULT)
    for g, sec in zip(pool, secciones):
        g["seccion_scraper"] = g["seccion"]
        g["seccion"] = sec
    return [g for g in pool if g["seccion"] not in excluidas]


def _prevista_de(grupo: Dict) -> Optional[str]:
    """Sección que el scraper ya predijo para el grupo (se ignora 'Descartable', que no es una sección del portal)."""
    for n in grupo["notas"]:
        prev = n.get("seccion_prevista")
        if prev and prev != "Descartable":
            return prev
    return None


def _secciones_de(grupos: List[Dict], etiquetas: List[str]) -> List[str]:
    """Sección de cada grupo: la prevista si existe; el resto, en una sola llamada al clasificador."""
    import clasificador_secciones

    resultado: List[Optional[str]] = [_prevista_de(g) for g in grupos]
    faltan = [i for i, r in enumerate(resultado) if not r]
    if faltan:
        nuevas = clasificador_secciones.clasificar(
            [{"titulo": grupos[i]["notas"][0].get("titulo") or "", "etiqueta": etiquetas[i] or ""} for i in faltan]
        )
        for i, sec in zip(faltan, nuevas):
            resultado[i] = sec
    return [r or "Local" for r in resultado]


def siguiente_candidata(restantes: List[Dict], aprobadas_por_seccion: Dict[str, int]) -> Dict:
    """
    Elige la próxima nota a procesar según lo que falta cubrir:
      1) Locales y Policiales por debajo de su objetivo,
      2) cualquier otra sección todavía sin una nota aprobada,
      3) el resto por puntaje, respetando el tope por sección.
    Como se recalcula con lo ya aprobado, si una nota no pasa los gates se prueba otra de la
    misma sección antes de pasar a las demás.
    """
    def prioridad(g: Dict):
        sec = g["seccion"]
        tiene = aprobadas_por_seccion.get(sec, 0)
        objetivo = OBJETIVO_POR_SECCION.get(sec, OBJETIVO_DEFAULT)
        if tiene < objetivo:
            rango = 0 if sec in OBJETIVO_POR_SECCION else 1
        elif tiene < TOPE_POR_SECCION.get(sec, TOPE_DEFAULT):
            rango = 2
        else:
            rango = 3
        return (rango, -g["score"])

    return min(restantes, key=prioridad)


def _dominio(url: Optional[str]) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url or "").netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _con_logo_del_medio(url: Optional[str]) -> bool:
    """True si la imagen viene de un dominio cuyas fotos llevan su logo estampado."""
    import config

    d = _dominio(url)
    return bool(d) and any(d == x or d.endswith("." + x) for x in config.IMAGEN_DOMINIOS_CON_LOGO)


def _rescatar_foto_con_logo(client, noticia_id: str, url_actual: Optional[str]) -> Optional[str]:
    """
    Última salida cuando la única foto es de un medio que estampa su logo (TSN): recorta la franja
    inferior, sube el recorte y lo pasa por el control de imagen con doble revisión. Si queda limpia se
    usa; si no, devuelve None y la nota va a revisión como antes.
    """
    import image_gate
    from image_handler import ImageHandler

    if not url_actual:
        return None
    nueva = ImageHandler(client).subir_recorte_logo(url_actual, noticia_id)
    if not nueva:
        return None
    elegida = image_gate.elegir_imagen_limpia([nueva], doble_revision=True)
    if elegida["url"] is None:
        return None
    client.update_imagen(noticia_id, nueva)
    return "imagen: recortada (se quitó la franja con el logo del medio)"


def verificar_imagen_nota(client, noticia_id: str, g: Dict) -> Tuple[bool, str]:
    """
    Image gate: devuelve (apta, motivo para `auto_motivo`).
    - Las fotos de dominios que estampan su logo (TSN Necochea) no se usan si otra fuente del
      grupo trae foto; si es la única, la nota queda para revisión.
    - Si el grupo incluye alguna de esas fuentes, la imagen elegida pasa por doble revisión.
    - Una foto con marca/logo/texto se reemplaza por la de otra fuente que esté limpia.
    """
    import image_gate
    from image_handler import ImageHandler

    fila = client.client.table("noticias").select("imagen_url").eq("id", noticia_id).single().execute().data or {}
    actual = fila.get("imagen_url")
    originales = [n.get("imagen_url") for n in g["notas"] if n.get("imagen_url")]
    lider = next((n for n in g["notas"] if n.get("id") == noticia_id), None)
    original_lider = (lider or {}).get("imagen_url")

    actual_con_logo = _con_logo_del_medio(original_lider)
    alternativas = [u for u in originales if not _con_logo_del_medio(u)]
    candidatas = ([] if actual_con_logo else [actual]) + alternativas
    doble = any(_con_logo_del_medio(u) for u in originales)

    if not [c for c in candidatas if c]:
        if actual_con_logo or any(_con_logo_del_medio(u) for u in originales):
            dom = _dominio(original_lider or (originales[0] if originales else ""))
            # Recorte del logo del medio (franja o margen/esquina): activo por defecto, es lo acordado con el
            # editor. IMAGEN_RECORTAR_LOGO=false lo apaga.
            rescate = None
            if os.getenv("IMAGEN_RECORTAR_LOGO", "true").strip().lower() not in ("0", "false", "no"):
                try:
                    rescate = _rescatar_foto_con_logo(client, noticia_id, actual)
                except Exception:
                    logger.exception("Recorte del logo falló para %s", noticia_id)
            if rescate:
                return True, rescate
            return False, f"imagen: la única foto disponible es de {dom} y lleva su logo (el recorte no alcanzó: elegí otra foto o cargá una propia)"
        return False, "imagen: sin foto disponible"

    elegida = image_gate.elegir_imagen_limpia(candidatas, doble_revision=doble)

    if elegida["url"] is None:
        analizadas = [d for d in elegida["detalle"] if d["resultado"]]
        if not analizadas:
            return False, "imagen: no se pudo verificar"
        r = analizadas[0]["resultado"]
        return False, "imagen: con marca/logo (%s)" % (r.get("nombre_medio") or r.get("motivo") or "sin detalle")
    if elegida["url"] == actual:
        return True, "imagen: limpia"
    nueva = ImageHandler(client).upload_image(elegida["url"], noticia_id)
    if not nueva:
        return False, "imagen: alternativa limpia pero no se pudo subir"
    client.update_imagen(noticia_id, nueva)
    return True, "imagen: reemplazada por alternativa limpia"


def verificar_hechos_nota(client, noticia_id: str, g: Dict, textos_fuente: List[str]) -> Tuple[bool, str]:
    """Fact gate: contrasta la nota redactada con los textos de sus fuentes."""
    import fact_gate

    fila = (
        client.client.table("noticias").select("titulo, cuerpo, seccion")
        .eq("id", noticia_id).single().execute().data or {}
    )
    ev = fact_gate.evaluar_nota(
        fila.get("titulo", ""), fila.get("cuerpo", ""), textos_fuente,
        seccion=fila.get("seccion") or g["seccion"], n_fuentes=g["n_fuentes"],
    )
    # Los avisos (p. ej. cifras que la redacción derivó y no están literales en la fuente) no bloquean,
    # pero quedan escritos para que el editor los vea en Pendientes.
    aviso = "".join(f" | aviso: {a}" for a in ev.get("avisos", []))
    if ev["automatica"]:
        return True, "hechos: verificados" + aviso
    return False, "hechos: " + "; ".join(ev["motivos"]) + aviso


def publicar_en_portal(noticia_id: str) -> Tuple[bool, str]:
    """
    Publica vía /api/publicar del portal (mismo camino que el botón del admin: estado,
    fecha, push OneSignal, kit de Instagram y recálculo de la portada por trigger SQL).
    """
    import config
    import portal_client

    if not config.INTERNAL_API_SECRET:
        return False, "INTERNAL_API_SECRET no configurado"
    try:
        # sinPush: la edición manda un único push resumen al final (ver enviar_push_resumen).
        r, url = portal_client.post_portal("/api/publicar", {"id": noticia_id, "sinPush": True})
        if r.status_code == 200 and r.json().get("ok"):
            return True, "ok"
        return False, f"portal {r.status_code} ({url}): {r.text[:150]}"
    except Exception as e:
        return False, f"portal inaccesible: {str(e)[:150]}"


def enviar_push_resumen(ids: List[str]) -> Tuple[bool, str]:
    """Un solo push a los suscriptores con la nota principal (la primera) y la cantidad publicada."""
    import config
    import portal_client

    if not ids or not config.INTERNAL_API_SECRET:
        return False, "sin notas o sin secreto"
    try:
        r, url = portal_client.post_portal("/api/push/resumen", {"ids": ids})
        if r.status_code == 200 and r.json().get("ok"):
            return True, "ok"
        return False, f"portal {r.status_code} ({url}): {r.text[:150]}"
    except Exception as e:
        return False, f"portal inaccesible: {str(e)[:150]}"


def ya_corrio_hoy(client) -> bool:
    """
    True si la edición de hoy (día argentino) ya procesó o publicó notas. Permite disparar la
    edición varias veces por la mañana como respaldo (los crons de GitHub Actions se demoran
    horas a veces) sin que se repita. Una corrida que solo dio errores no cuenta.
    """
    ahora_ar = datetime.now(timezone.utc) - timedelta(hours=3)
    inicio_utc = ahora_ar.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=3)
    resp = (
        client.client.table("edicion_log").select("id", count="exact")
        .in_("resultado", ["procesada", "publicada"]).neq("modo", "manual")
        .gte("ejecutado_at", inicio_utc.isoformat())
        .limit(1).execute()
    )
    return (resp.count or 0) > 0


def publicadas_hoy(client) -> int:
    """Notas que la edición ya publicó hoy (día argentino): evita pasarse si se reejecuta."""
    ahora_ar = datetime.now(timezone.utc) - timedelta(hours=3)
    inicio_utc = ahora_ar.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=3)
    resp = (
        client.client.table("edicion_log").select("id", count="exact")
        .eq("resultado", "publicada").neq("modo", "manual").gte("ejecutado_at", inicio_utc.isoformat()).execute()
    )
    return resp.count or 0


def _log(client, modo: str, g: Dict, resultado: str, motivo: str = "", noticia_id: Optional[str] = None) -> None:
    try:
        client.client.table("edicion_log").insert({
            "modo": modo,
            "grupo_id": g["grupo_id"],
            "noticia_id": noticia_id,
            "titulo": (g["notas"][0].get("titulo") or "")[:200],
            "seccion": g["seccion"],
            "score": g["score"],
            "n_fuentes": g["n_fuentes"],
            "resultado": resultado,
            "motivo": motivo[:500],
        }).execute()
    except Exception:
        logger.exception("No se pudo escribir edicion_log")


CARRUSEL_POR_LOTE = 3


def elegir_para_carrusel(candidatas: List[Dict], cantidad: int = CARRUSEL_POR_LOTE) -> List[str]:
    """
    Las `cantidad` notas más relevantes de un lote: mayor score primero, con foto, y de secciones distintas mientras
    se pueda (una segunda nota de la misma sección solo si no alcanzan secciones para completar). Cada candidata es
    {"id", "score", "seccion", "imagen_url"}. Sin foto el carrusel muestra una imagen genérica, así que van últimas.
    """
    orden = sorted(candidatas, key=lambda c: (bool(c.get("imagen_url")), c.get("score") or 0), reverse=True)
    elegidas: List[Dict] = []
    for c in orden:
        if len(elegidas) < cantidad and c["seccion"] not in {e["seccion"] for e in elegidas}:
            elegidas.append(c)
    for c in orden:
        if len(elegidas) < cantidad and c not in elegidas:
            elegidas.append(c)
    return [c["id"] for c in elegidas]


def sumar_al_carrusel(client, publicadas: Dict[str, Dict]) -> List[str]:
    """
    De un lote de notas publicadas, suma al carrusel de la portada las 3 más relevantes (se acumulan con las de los
    lotes anteriores del día; la función SQL `agregar_a_carrusel` se encarga del orden y del tope). Devuelve los ids.
    """
    if not publicadas:
        return []
    filas = (
        client.client.table("noticias").select("id, imagen_url, estado").in_("id", list(publicadas)).execute().data or []
    )
    candidatas = [
        {"id": f["id"], "imagen_url": f.get("imagen_url"), **publicadas[f["id"]]}
        for f in filas if f.get("estado") == "publicada" and f["id"] in publicadas
    ]
    ids = elegir_para_carrusel(candidatas)
    if ids:
        client.client.rpc("agregar_a_carrusel", {"ids": ids}).execute()
        logger.info("Carrusel de portada: se sumaron %s nota(s) del lote", len(ids))
    return ids


def notificar_admin(client, tipo: str, titulo: str, detalle: Optional[Dict] = None) -> None:
    """Deja un aviso en la campana del admin (tabla `admin_notificaciones`). Nunca rompe la edición."""
    try:
        client.client.table("admin_notificaciones").insert(
            {"tipo": tipo, "titulo": titulo[:200], "detalle": detalle}
        ).execute()
    except Exception:
        logger.exception("No se pudo guardar el aviso para el admin")


def notificar_edicion(client, modo: str, resumen: Dict) -> None:
    """Aviso de que terminó de procesar y publicar con IA: la matutina, la manual (botón) o la de prueba."""
    nombre = {"manual": "Edición manual", "auto": "Edición matutina", "sombra": "Edición de prueba"}.get(modo, "Edición")
    pub = int(resumen.get("publicadas") or 0)
    rev = int(resumen.get("para_revision") or 0)
    err = int(resumen.get("errores") or 0)
    partes = [f"{pub} publicada{'s' if pub != 1 else ''}", f"{rev} a Pendientes"]
    if err:
        partes.append(f"{err} con error de la IA")
    if resumen.get("cortada_por_tiempo"):
        partes.append("cortada por tiempo")
    if resumen.get("cortada_por_cuota"):
        partes.append("cortada: sin cuota de IA")
    items = [
        {"titulo": (i.get("titulo") or "")[:120], "seccion": i.get("seccion"), "resultado": i.get("resultado"),
         "motivo": (i.get("motivo") or "")[:160]}
        for i in (resumen.get("items") or [])
    ]
    detalle = {
        "modo": modo, "objetivo": resumen.get("objetivo"), "procesadas": resumen.get("procesadas"),
        "publicadas": pub, "para_revision": rev, "errores": err,
        "cortada_por_tiempo": bool(resumen.get("cortada_por_tiempo")),
        "cortada_por_cuota": bool(resumen.get("cortada_por_cuota")),
        "por_seccion": resumen.get("por_seccion") or {}, "items": items,
    }
    notificar_admin(client, "edicion" if err == 0 and not resumen.get("cortada_por_cuota") else "edicion_aviso",
                    f"{nombre} terminada: {', '.join(partes)}", detalle)


TOPE_MANUAL_DEFAULT = 4


def _preparar_manual(client, cfg: Dict, manual: Dict) -> Tuple[List[Dict], int, bool]:
    """
    Candidatas de una corrida manual (botón de la Bandeja). Devuelve (grupos, objetivo, en_orden).
    - Con `items` ([{grupo_id, fuentes_ids?, seccion?}]) se procesan esos grupos, en ese orden, con la sección y las
      fuentes que eligió el editor; no se aplican el reparto por secciones ni los filtros de archivo/excluidas.
    - Con `solo_hoy` se rankea y reparte como la edición matutina, pero solo con las notas de hoy (día argentino).
    """
    pesos = {**PESOS_DEFAULT, **(cfg.get("edicion_pesos") or {})}
    items = [i for i in (manual.get("items") or []) if i.get("grupo_id")]
    if items:
        por_grupo = {g["grupo_id"]: g for g in candidatos(client, grupo_ids=[i["grupo_id"] for i in items])}
        grupos: List[Dict] = []
        for it in items:
            g = por_grupo.get(it["grupo_id"])
            if not g:
                continue
            elegidas = {str(x) for x in (it.get("fuentes_ids") or [])}
            if elegidas:
                g["notas"] = [n for n in g["notas"] if str(n["id"]) in elegidas] or g["notas"]
            g["seccion_scraper"] = g["notas"][0].get("seccion")
            g["seccion"] = it.get("seccion") or ""
            g["n_fuentes"] = _n_fuentes(g["notas"])
            g["score"] = puntuar(g, pesos)
            grupos.append(g)
        # La sección la decide la IA (ya no se elige a mano): una sola llamada para todo el lote.
        sin_seccion = [g for g in grupos if not g["seccion"]]
        if sin_seccion:
            secciones = _secciones_de(sin_seccion, [g["seccion_scraper"] or "" for g in sin_seccion])
            for g, sec in zip(sin_seccion, secciones):
                g["seccion"] = sec
        return grupos, len(grupos), True
    if manual.get("solo_hoy"):
        ranking = ordenar(fusionar_duplicados(candidatos(client, desde=inicio_dia_ar_utc())), cfg)
        grupos = clasificar_pool(ranking[:MAX_A_CLASIFICAR], cfg)
        tope = int(manual.get("tope") or TOPE_MANUAL_DEFAULT)
        return grupos, max(1, min(tope, 20)), False
    return [], 0, False


def ejecutar_edicion(client, procesar: Callable[..., Dict], dry_run: bool = False,
                     manual: Optional[Dict] = None) -> Dict:
    """
    `procesar` es `pipeline_ia` (inyectado para evitar import circular).
    dry_run=True solo rankea, clasifica y loguea el reparto previsto, sin redactar con la IA.

    Se procesan notas hasta tener `edicion_max` aprobadas (pasan image gate y fact gate), con un
    máximo de 2x procesadas. Con `edicion_auto_publicar` las aprobadas se publican; sin él (modo
    sombra) solo se cuentan. Las que no pasan quedan en 'pendiente' con el motivo en `auto_motivo`.
    El orden lo decide `siguiente_candidata`, para repartir entre secciones.
    """
    cfg = client.get_scraper_config() or {}
    en_orden = False
    if manual is not None:
        # Corrida manual (botón de la Bandeja): el click es la autorización, así que publica lo aprobado sin
        # depender de las banderas de la edición matutina y sin descontar su cupo diario.
        auto, modo = True, "manual"
        restantes, objetivo, en_orden = _preparar_manual(client, cfg, manual)
        if not restantes:
            return {"ok": True, "omitida": "no hay grupos para procesar", "modo": modo, "objetivo": 0,
                    "candidatas": 0, "procesadas": 0, "aprobadas": 0, "publicadas": 0, "para_revision": 0,
                    "errores": 0, "por_seccion": {}, "items": []}
    else:
        if not cfg.get("edicion_auto_activa") and not dry_run:
            return {"ok": True, "omitida": "edicion_auto_activa=false"}

        auto = bool(cfg.get("edicion_auto_publicar")) and not dry_run
        modo = "auto" if auto else "sombra"
        objetivo = int(cfg.get("edicion_max") or 8)
        if auto:
            objetivo -= publicadas_hoy(client)
            if objetivo <= 0:
                return {"ok": True, "omitida": "ya se publicó el cupo diario"}

        ranking = ordenar(fusionar_duplicados(candidatos(client)), cfg)
        restantes = clasificar_pool(ranking[:MAX_A_CLASIFICAR], cfg)
    resumen = {"ok": True, "modo": modo, "objetivo": objetivo, "candidatas": len(restantes),
               "procesadas": 0, "aprobadas": 0, "publicadas": 0, "para_revision": 0, "errores": 0,
               "por_seccion": {}, "items": []}

    aprobadas_por_seccion: Dict[str, int] = defaultdict(int)
    fallos_por_seccion: Dict[str, int] = defaultdict(int)
    publicadas_ids: List[str] = []
    publicadas_info: Dict[str, Dict] = {}  # id de la nota → score y sección, para elegir las del carrusel
    errores_cuota = 0
    etiqueta_pub = "publicada por boton (Bandeja)" if manual is not None else "publicada automaticamente"
    inicio = time.monotonic()
    # Los errores de la IA (503, timeouts de Gemini) no consumen el tope de notas procesadas: se acotan aparte.
    # Hoy (21/10) tres errores se llevaron 3 de las 8 plazas y la edición cerró con 3 de 4 publicadas.
    while (restantes and resumen["aprobadas"] < objetivo
           and resumen["procesadas"] - resumen["errores"] < objetivo * 2
           and resumen["errores"] < 6):
        if time.monotonic() - inicio > MAX_MINUTOS_EDICION * 60:
            logger.warning("Edición cortada por tiempo (%s min)", MAX_MINUTOS_EDICION)
            resumen["cortada_por_tiempo"] = True
            break
        if not en_orden:
            restantes = [g for g in restantes if fallos_por_seccion[g["seccion"]] < MAX_FALLOS_POR_SECCION]
        if not restantes:
            break
        # Seleccionadas a mano: se respeta el orden pedido (sin reparto por secciones).
        g = restantes[0] if en_orden else siguiente_candidata(restantes, aprobadas_por_seccion)
        restantes.remove(g)
        uso_ia.abrir(g["grupo_id"])  # las llamadas de IA de este grupo (redacción, controles) se atribuyen a su nota
        item = {"grupo_id": g["grupo_id"], "titulo": g["notas"][0].get("titulo"), "score": g["score"],
                "seccion": g["seccion"], "seccion_scraper": g.get("seccion_scraper"), "n_fuentes": g["n_fuentes"]}
        if dry_run:
            # Simula que todas pasan los gates para mostrar el reparto previsto.
            aprobadas_por_seccion[g["seccion"]] += 1
            resumen["aprobadas"] += 1
            resumen["procesadas"] += 1
            _log(client, modo, g, "omitida", "dry_run")
            resumen["items"].append({**item, "resultado": "dry_run"})
            continue
        ids = [n["id"] for n in g["notas"]]
        # Los textos de las fuentes hay que leerlos ANTES: pipeline_ia pisa el cuerpo de la líder.
        try:
            textos_fuente = [n.get("cuerpo") or "" for n in client.get_notas_by_ids(ids)]
        except Exception:
            logger.exception("No se pudieron leer las fuentes de %s", g["grupo_id"])
            textos_fuente = []
        r = {"ok": False, "error": "sin intentar"}
        for _intento in range(2):  # la IA a veces devuelve el JSON incompleto: un reintento
            try:
                # Se pasa la sección ya clasificada por tema (igual que cuando la elige el editor).
                r = procesar(g["grupo_id"], ids, None, g["seccion"])
            except Exception as e:  # una nota no debe frenar la edición
                logger.exception("Error procesando grupo %s", g["grupo_id"])
                r = {"ok": False, "error": str(e)[:200]}
            if r.get("ok") or "campos requeridos" not in str(r.get("error", "")):
                break
        if not r.get("ok"):
            resumen["errores"] += 1
            resumen["procesadas"] += 1
            sin_cuota = "429" in str(r.get("error", "")) or "quota" in str(r.get("error", "")).lower()
            errores_cuota = errores_cuota + 1 if sin_cuota else 0
            _log(client, modo, g, "error", r.get("error", ""))
            resumen["items"].append({**item, "resultado": "error", "motivo": r.get("error", "")})
            if errores_cuota >= 2:
                logger.error("Cuota de IA agotada: se corta la edición para no gastar más llamadas")
                resumen["cortada_por_cuota"] = True
                break
            continue

        resumen["procesadas"] += 1
        noticia_id = r["noticia_id"]
        uso_ia.fijar_nota(noticia_id)
        # pipeline_ia pisó el cuerpo de la líder: se guarda el texto original de las fuentes para poder
        # contrastar la nota después (rutina de revisión de pendientes). Columna oculta al público.
        try:
            texto = "\n\n".join(
                f"--- FUENTE {i + 1} ---\n{t[:3500]}" for i, t in enumerate(textos_fuente) if t and t.strip()
            )
            if texto:
                client.client.table("noticias").update({"texto_fuentes": texto[:12000]}).eq("id", noticia_id).execute()
        except Exception:
            logger.exception("No se pudo guardar el texto de las fuentes de %s", noticia_id)
        # Las notas de grupos fusionados quedan raw si no se las descarta.
        sobrantes = [i for i in ids if i != noticia_id]
        if sobrantes:
            client.client.table("noticias").update({"estado": "descartada"}) \
                .in_("id", sobrantes).eq("estado", "raw").execute()
        try:
            img_ok, motivo_img = verificar_imagen_nota(client, noticia_id, g)
        except Exception:
            logger.exception("Image gate falló para %s", noticia_id)
            img_ok, motivo_img = False, "imagen: no se pudo verificar"
        try:
            hechos_ok, motivo_hechos = verificar_hechos_nota(client, noticia_id, g, textos_fuente)
        except Exception:
            logger.exception("Fact gate falló para %s", noticia_id)
            hechos_ok, motivo_hechos = False, "hechos: no se pudo verificar"
        motivo = f"{motivo_hechos} | {motivo_img}"
        aprobada = img_ok and hechos_ok

        if aprobada:
            if auto:
                ok_pub, detalle = publicar_en_portal(noticia_id)
                if ok_pub:
                    publicadas_ids.append(noticia_id)
                    publicadas_info[noticia_id] = {"score": g["score"], "seccion": g["seccion"]}
                    resumen["publicadas"] += 1
                    resumen["aprobadas"] += 1
                    aprobadas_por_seccion[g["seccion"]] += 1
                    client.client.table("noticias").update(
                        {"score": g["score"], "auto_motivo": f"{etiqueta_pub} | {motivo}"[:500]}
                    ).eq("id", noticia_id).execute()
                    _log(client, modo, g, "publicada", motivo=motivo, noticia_id=noticia_id)
                    resumen["items"].append({**item, "resultado": "publicada"})
                    continue
                motivo = f"{motivo} | publicacion fallida: {detalle}"
            else:
                # Modo sombra: se cuenta como si se hubiera publicado, para ver el reparto real.
                resumen["aprobadas"] += 1
                aprobadas_por_seccion[g["seccion"]] += 1
                motivo = f"{motivo} | APROBADA (modo sombra: se habria publicado)"

        # Llegar acá en modo auto con la nota aprobada significa que la publicación falló; cuenta como fallo.
        if not (aprobada and not auto):
            fallos_por_seccion[g["seccion"]] += 1
        resumen["para_revision"] += 0 if (aprobada and not auto) else 1
        client.client.table("noticias").update(
            {"score": g["score"], "auto_motivo": f"pendiente de revision | {motivo}"[:500]}
        ).eq("id", noticia_id).execute()
        _log(client, modo, g, "procesada", motivo=motivo, noticia_id=noticia_id)
        resumen["items"].append({**item, "resultado": "aprobada_sombra" if aprobada and not auto else "revision",
                                 "motivo": motivo})
    uso_ia.abrir(None)
    resumen["por_seccion"] = dict(aprobadas_por_seccion)
    resumen["fallos_por_seccion"] = dict(fallos_por_seccion)
    # Un solo push con lo publicado (también si el bucle se cortó por cuota). Si falla no afecta lo ya publicado.
    if publicadas_ids:
        try:
            ok_push, detalle_push = enviar_push_resumen(publicadas_ids)
        except Exception as e:
            ok_push, detalle_push = False, str(e)[:150]
        resumen["push_resumen"] = "ok" if ok_push else f"error: {detalle_push}"
        if not ok_push:
            logger.warning("Push resumen no enviado: %s", detalle_push)
    if publicadas_ids and not dry_run:
        try:
            resumen["carrusel"] = sumar_al_carrusel(client, publicadas_info)
        except Exception:
            logger.exception("No se pudo sumar el lote al carrusel de la portada")
    if not dry_run:
        notificar_edicion(client, modo, resumen)
    return resumen
