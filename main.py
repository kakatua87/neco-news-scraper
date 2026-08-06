"""
Neco News — Pipeline principal de scraping y publicación.

Flujo en dos fases:
  Fase 1 — pipeline_scraping():  Scrape → Dedup → Supabase (estado=raw) → Telegram notifica
  Fase 2 — pipeline_ia():        Activada manualmente desde Telegram o /procesar-grupo
                                  Lee notas raw → IA sintetiza → Supabase (estado=pendiente)

Scheduler: pipeline_scraping() cada N minutos (configurable)
API: FastAPI con /health, /telegram/callback, /procesar-grupo, /run, /run-services
"""

import argparse
import logging
import math
import re
import sys
import time
import uuid
from collections import Counter
from typing import Dict, List, Optional

import requests

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
import uvicorn

import config
from ai_processor import AIProcessor
from scraper import NewsScraper
from services_scraper import ServicesScraper
from supabase_client import SupabaseNewsClient

# ─── Logging ─────────────────────────────────────────────────────
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
logger = logging.getLogger("neconews.pipeline")

# ─── FastAPI ─────────────────────────────────────────────────────
app = FastAPI(title="Neco News Scraper", version="3.0.0")
scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")


# ─── Utilidades ──────────────────────────────────────────────────

def slug_from_url(url: str, fallback_title: str) -> str:
    """Genera un slug estable a partir de la URL."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        last = (parsed.path or "").rstrip("/").split("/")[-1]
    except Exception:
        last = ""

    base = last or fallback_title or "nota"
    base = base.lower()
    base = re.sub(r"[^a-z0-9]+", "-", base)
    base = re.sub(r"-+", "-", base).strip("-")
    return base or "nota"


def notify_nuevo_grupo(cantidad_notas: int, seccion: str) -> None:
    """Avisa al portal (push OneSignal) que hay un grupo nuevo para revisar en /admin."""
    try:
        requests.post(
            f"{config.PORTAL_URL}/api/notificaciones/nuevo-grupo",
            json={
                "titulo": "📰 Nuevo grupo de noticias",
                "cuerpo_corto": f"{cantidad_notas} fuente(s) nueva(s) en {seccion} para revisar.",
            },
            timeout=10,
        )
    except Exception:
        logger.warning("No se pudo notificar nuevo grupo al portal.")


# ─── Deduplicación semántica por título ──────────────────────────

_STOPWORDS = frozenset({
    "el", "la", "los", "las", "de", "del", "en", "un", "una", "y", "a", "que",
    "se", "con", "por", "es", "su", "al", "lo", "le", "esta", "este", "son",
    "ha", "fue", "para", "como", "más", "no", "ya", "sin", "ante", "sobre",
    "pero", "sus", "muy", "ser", "hasta", "hay", "entre",
    # Interrogativos/pronombres con tilde (headlines tipo "Cómo anotarse",
    # "Cuándo se juega", "Qué días estará"): no distinguen tema, y sin esto
    # "cómo" no matcheaba contra "como" (sin tilde) y colaba como palabra
    # de contenido.
    "cómo", "cuándo", "qué", "cuál", "cuáles", "quién", "quiénes", "dónde",
    "también", "así", "aún", "según", "desde", "todo", "toda", "todos",
    "todas", "otro", "otra", "otros", "otras",
})


def _normalize_title(title: str) -> set:
    """Convierte un título en un conjunto de tokens normalizados."""
    clean = re.sub(r"[^a-záéíóúüñ\s]", "", title.lower())
    return {t for t in clean.split() if t not in _STOPWORDS and len(t) > 2}


def _group_by_similarity(
    notes: List[Dict],
    threshold: float = 0.40,
    gray_zone_threshold: float = 0.25,
) -> List[List[Dict]]:
    """
    Agrupa noticias sobre el mismo hecho usando similaridad contextual.

    Distintos portales casi nunca redactan un título igual para el mismo hecho
    (ver relevamiento de 2026-08-05: de 28 pares reales de duplicados entre
    fuentes, un Jaccard simple con umbral 0.65 solo detectaba 2). Por eso
    usamos un "overlap coefficient" ponderado por rareza de palabra (idf-like,
    calculado sobre el propio lote de notas scrapeadas): las palabras que
    aparecen en muchos títulos del día (p. ej. "necochea", "gobierno",
    "secuestran") pesan poco, y las palabras distintivas de un hecho puntual
    (nombres propios, "practicaje", "baliza", "Auditórium") pesan mucho.

    Criterios para agrupar (basta con UNO):
    1. Similaridad ponderada >= threshold (fuerte por sí sola)
    2. Similaridad ponderada >= gray_zone_threshold Y comparten >= 2 entidades
       concretas (nombres propios, números, lugares) — cubre títulos muy
       reescritos que igual comparten los datos puntuales del hecho.
    Además, la sección temática debe ser compatible en ambos casos.
    """
    # Lugares/gentilicios que aparecen en casi cualquier título de un portal
    # hiperlocal (Necochea/Quequén/región): por sí solos no indican que dos
    # notas hablen del mismo hecho, así que no deben aportar al score de
    # similitud (sí se conservan como señal débil en extract_entities).
    LOW_SIGNAL_WORDS = {
        "necochea", "quequén", "quequen", "mar", "plata", "argentina",
        "buenos", "aires", "provincia", "nacional", "país", "region",
        "región", "san",
    }

    groups: List[List[Dict]] = []
    used = set()
    tokens = [_normalize_title(n.get("titulo", "")) for n in notes]
    sig_tokens = [t - LOW_SIGNAL_WORDS for t in tokens]

    n_notes = len(notes)
    doc_freq: Counter = Counter()
    for tok_set in sig_tokens:
        doc_freq.update(tok_set)

    def idf(tok: str) -> float:
        return math.log((n_notes + 1) / (doc_freq.get(tok, 0) + 1)) + 1.0

    MIN_TOKENS = 4  # títulos muy cortos (kickers tipo "PRONÓSTICO") no son comparables

    def weighted_overlap(a: set, b: set) -> float:
        if len(a) < MIN_TOKENS or len(b) < MIN_TOKENS:
            return 0.0
        inter = a & b
        w_inter = sum(idf(t) for t in inter)
        w_min = min(sum(idf(t) for t in a), sum(idf(t) for t in b))
        return w_inter / w_min if w_min else 0.0

    SECTION_GROUPS = [
        {"Deportes"},
        {"Policiales"},
        {"Política", "Local"},
        {"Economía"},
        {"Sociedad", "Salud", "Cultura"},
    ]

    def sections_compatible(s1: str, s2: str) -> bool:
        s1, s2 = s1.strip().lower(), s2.strip().lower()
        if s1 == s2:
            return True
        for group in SECTION_GROUPS:
            g_lower = {s.lower() for s in group}
            if s1 in g_lower and s2 in g_lower:
                return True
        return False

    def extract_entities(title: str) -> set:
        numbers = set(re.findall(r'\b\d+(?:[.,]\d+)?\b', title))
        words = title.split()
        capitalized = {w.lower() for w in words[1:] if w and w[0].isupper()
                       and len(w) > 3 and w.lower() not in _STOPWORDS}
        LUGARES = {"necochea", "quequén", "quequen", "lobería", "loberia",
                   "san cayetano", "miramar", "tres arroyos", "claromecó",
                   "ruta 88", "ruta 11", "ruta 3"}
        text_lower = title.lower()
        lugares_found = {l for l in LUGARES if l in text_lower}
        return numbers | capitalized | lugares_found

    entities = [extract_entities(n.get("titulo", "")) for n in notes]

    for i, note in enumerate(notes):
        if i in used:
            continue
        group = [note]
        used.add(i)
        section_i = note.get("seccion", "Local")

        for j in range(i + 1, len(notes)):
            if j in used:
                continue
            section_j = notes[j].get("seccion", "Local")
            if not sections_compatible(section_i, section_j):
                continue

            sim = weighted_overlap(sig_tokens[i], sig_tokens[j])
            shared_entities = entities[i] & entities[j]

            strong_match = sim >= threshold
            gray_zone_match = sim >= gray_zone_threshold and len(shared_entities) >= 2
            if not (strong_match or gray_zone_match):
                continue

            logger.info(
                "Agrupadas (sim=%.2f, entidades=%s):\n  [A] %s\n  [B] %s",
                sim, shared_entities,
                note.get("titulo", "")[:70], notes[j].get("titulo", "")[:70],
            )
            group.append(notes[j])
            used.add(j)

        groups.append(group)

    return groups


# ─── FASE 1: Pipeline de scraping (sin IA) ───────────────────────

def pipeline_scraping() -> None:
    """
    Fase 1: Scrape → Dedup → Supabase (estado=raw) → notifica el portal (push).
    No invoca la IA en ningún momento. La revisión/selección de fuentes, imagen
    y sección, y el disparo de la Fase 2, se hacen desde /admin (Bandeja de Entrada).
    """
    logger.info("═══ pipeline_scraping v3.0 — inicio ═══")
    try:
        supabase_client = SupabaseNewsClient()
        existing_urls = supabase_client.get_urls_existentes()
        scraper = NewsScraper(existing_urls=existing_urls)
    except Exception:
        logger.exception("Error inicializando dependencias de pipeline_scraping.")
        return

    # ── Config del scraper (controlada desde /admin) ─────────────
    cfg = supabase_client.get_scraper_config()
    if not cfg.get("activo", True):
        logger.info("Scraper desactivado desde el panel admin. Se salta esta corrida.")
        return

    fecha_inicio = cfg.get("fecha_inicio")
    if fecha_inicio:
        try:
            from datetime import datetime, timezone
            dt_inicio = datetime.fromisoformat(str(fecha_inicio).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < dt_inicio:
                logger.info("Scraper programado para %s. Todavía no llegó la fecha.", fecha_inicio)
                return
        except Exception:
            logger.warning("No se pudo interpretar fecha_inicio=%s, se ignora.", fecha_inicio)

    source_map = {
        "nden": scraper.scrape_nden,
        "diarionecochea": scraper.scrape_diario_necochea,
        "diario4v": scraper.scrape_diario4v,
        "tsn": scraper.scrape_tsn,
        "diarionq": scraper.scrape_diarionq,
        "elecos": scraper.scrape_elecos,
    }
    fuentes_activas = cfg.get("fuentes_activas") or list(source_map.keys())

    # ── Scraping de las fuentes activas ──────────────────────────
    raw_notes: List[Dict] = []
    for key in fuentes_activas:
        source_fn = source_map.get(key)
        if not source_fn:
            continue
        try:
            raw_notes.extend(source_fn())
        except Exception:
            logger.exception("Error en fuente=%s", key)

    logger.info("Notas candidatas scrapeadas: %s", len(raw_notes))

    # ── Deduplicación ────────────────────────────────────────────
    groups = _group_by_similarity(raw_notes)
    logger.info("Grupos tras dedup: %s (de %s notas)", len(groups), len(raw_notes))

    # ── Procesar cada grupo ──────────────────────────────────────
    saved = 0
    for group in groups[:config.MAX_NOTES_PER_RUN]:
        leader = group[0]
        urls = [n["url"] for n in group if n.get("url")]

        # Saltar si ya procesamos todas las URLs del grupo
        if all(u in existing_urls for u in urls):
            continue

        # UUID compartido para todas las notas del grupo
        grupo_id = str(uuid.uuid4())
        notas_insertadas: List[Dict] = []

        for note in group:
            note_url = note.get("url")
            if not note_url or note_url in existing_urls:
                continue
            try:
                article_data = scraper.get_article_content(note_url)
                texto = article_data.get("text", "")
                if len(texto) < 60:
                    logger.debug("Contenido insuficiente para %s, omitiendo.", note_url)
                    continue

                imagen = (
                    article_data.get("og_image")
                    or article_data.get("content_image")
                    or note.get("imagen_url")
                )
                titulo_original = note.get("titulo", "")

                datos_raw = {
                    "titulo_original": titulo_original,
                    "cuerpo": texto,
                    "seccion": note.get("seccion", "Local"),
                    "fuente": note.get("fuente", ""),
                    "url_original": note_url,
                    "imagen_url": imagen,
                    "slug": slug_from_url(note_url, titulo_original),
                    "grupo_id": grupo_id,
                }
                insertada = supabase_client.insert_noticia_raw(datos_raw)
                notas_insertadas.append(insertada)
                existing_urls.add(note_url)

            except Exception:
                logger.exception("Error guardando nota raw url=%s", note_url)

        if not notas_insertadas:
            logger.info("Grupo sin notas guardables (contenido insuficiente), omitiendo.")
            continue

        saved += 1
        logger.info(
            "Grupo guardado: grupo_id=%s | notas=%s | líder='%s'",
            grupo_id, len(notas_insertadas), leader.get("titulo", "")[:60],
        )

        # ── Notificar al portal (push) ────────────────────────────
        notify_nuevo_grupo(len(notas_insertadas), leader.get("seccion", "Local"))

        time.sleep(1)  # Pausa mínima entre grupos

    logger.info("═══ pipeline_scraping finalizado. Grupos nuevos: %s ═══", saved)


# ─── FASE 2: Pipeline de IA (bajo demanda) ───────────────────────

def pipeline_ia(
    grupo_id: str,
    fuentes_ids: List[str],
    imagen_url: Optional[str],
    seccion: str,
) -> Dict:
    """
    Fase 2: Lee notas raw del grupo → IA → actualiza líder → limpia secundarias.
    Retorna {"ok": True, "noticia_id": id} o {"ok": False, "error": msg}.
    """
    logger.info("═══ pipeline_ia — grupo_id=%s | fuentes=%s ═══", grupo_id, fuentes_ids)
    try:
        supabase_client = SupabaseNewsClient()
    except Exception:
        logger.exception("Error inicializando dependencias de pipeline_ia.")
        return {"ok": False, "error": "error de inicialización"}

    # Inicializar IA
    try:
        ai = AIProcessor()
    except Exception:
        logger.exception("IA no disponible.")
        return {"ok": False, "error": "IA no disponible"}

    # Obtener notas raw seleccionadas
    notas = supabase_client.get_notas_by_ids(fuentes_ids)
    if not notas:
        logger.warning("No se encontraron notas para los ids=%s", fuentes_ids)
        return {"ok": False, "error": "notas no encontradas"}

    # Nota líder = primera de la lista
    lider = notas[0]
    titulo = lider.get("titulo", "")
    all_texts = [n["cuerpo"] for n in notas if n.get("cuerpo") and len(n["cuerpo"]) >= 60]
    fuentes = [n["fuente"] for n in notas if n.get("fuente")]
    fuente_unica = ", ".join(sorted(set(fuentes))) if fuentes else None

    # Guardamos fuente+URL de cada nota seleccionada: las secundarias se
    # marcan como 'descartada' más abajo y perderían su URL si no la
    # conserváramos acá.
    fuentes_urls = [
        {"fuente": n.get("fuente", ""), "url": n.get("url_original")}
        for n in notas
        if n.get("url_original")
    ]

    if not all_texts:
        return {"ok": False, "error": "sin contenido suficiente para procesar"}

    # Elegir mejor imagen: primero la enviada, luego la del líder
    best_image = imagen_url or lider.get("imagen_url")
    if not best_image:
        try:
            scraper = NewsScraper(existing_urls=set())
            best_image = scraper.get_wikimedia_image(titulo)
        except Exception:
            pass

    # Reescritura con IA
    try:
        if len(all_texts) > 1:
            rewritten = ai.process_multi_source(titulo, all_texts, seccion, fuentes=fuentes)
        else:
            rewritten = ai.process_article(titulo, all_texts[0], seccion, fuente=fuente_unica)
    except Exception as e:
        logger.exception("IA falló en pipeline_ia para grupo_id=%s", grupo_id)
        return {"ok": False, "error": f"IA falló: {str(e)[:200]}"}

    # Actualizar la nota líder en Supabase (estado=pendiente)
    rewritten["imagen_url"] = best_image
    rewritten["fuentes_urls"] = fuentes_urls
    if fuente_unica:
        rewritten["fuente"] = fuente_unica
    noticia_id = lider["id"]
    supabase_client.update_noticia_con_ia(noticia_id, rewritten)

    # Subir imagen a Supabase Storage
    if best_image:
        try:
            from image_handler import ImageHandler
            handler = ImageHandler(supabase_client)
            nueva_url = handler.upload_image(best_image, noticia_id)
            if nueva_url:
                supabase_client.update_imagen(noticia_id, nueva_url)
                rewritten["imagen_url"] = nueva_url
                logger.info("Imagen subida a Storage: %s", nueva_url[:80])
        except Exception:
            logger.warning("No se pudo subir imagen para id=%s. Usando URL original.", noticia_id)

    # Descartar notas raw secundarias del grupo
    supabase_client.delete_notas_raw_del_grupo(grupo_id, excepto_id=noticia_id)

    logger.info("═══ pipeline_ia finalizado. noticia_id=%s ═══", noticia_id)
    return {"ok": True, "noticia_id": noticia_id}


# ─── Endpoints ───────────────────────────────────────────────────

@app.get("/health")
def health() -> Dict[str, str]:
    """Health check para UptimeRobot / Render."""
    return {"status": "ok", "service": "neco-news-scraper", "version": "3.0.0"}


@app.get("/stats")
def stats() -> Dict:
    """Estadísticas rápidas para el dashboard."""
    try:
        client = SupabaseNewsClient()
        return client.get_stats()
    except Exception:
        logger.exception("Error obteniendo stats.")
        return {"error": "no disponible"}


@app.get("/debug-env")
async def debug_env() -> Dict:
    """Muestra las variables de entorno relevantes (enmascaradas)."""
    def mask(val: str) -> str:
        if not val:
            return "(VACÍO)"
        if len(val) <= 6:
            return val[:2] + "***"
        return val[:4] + "..." + val[-4:]

    return {
        "SCRAPER_URL": config.SCRAPER_URL or "(VACÍO)",
        "PORTAL_URL": config.PORTAL_URL or "(VACÍO)",
        "SUPABASE_URL": mask(config.SUPABASE_URL or ""),
        "AI_PROVIDER": config.AI_PROVIDER or "(VACÍO)",
    }


@app.post("/procesar-grupo")
async def procesar_grupo(request: Request) -> Dict:
    """
    Activa la Fase 2 (IA) para un grupo de notas raw.
    Body: { grupo_id, fuentes_ids: [id1, id2, ...], imagen_url?, seccion? }
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "json inválido"}

    grupo_id = body.get("grupo_id", "").strip()
    fuentes_ids: List[str] = body.get("fuentes_ids", [])
    imagen_url: Optional[str] = body.get("imagen_url")
    seccion: str = body.get("seccion", "Local")

    if not grupo_id:
        return {"ok": False, "error": "grupo_id es obligatorio"}
    if not fuentes_ids:
        return {"ok": False, "error": "fuentes_ids no puede estar vacío"}

    try:
        result = pipeline_ia(grupo_id, fuentes_ids, imagen_url, seccion)
        return result
    except Exception as e:
        logger.exception("Error en /procesar-grupo grupo_id=%s", grupo_id)
        return {"ok": False, "error": str(e)}


@app.post("/run")
async def manual_run() -> Dict:
    """Ejecuta pipeline_scraping() manualmente (para debug)."""
    try:
        pipeline_scraping()
        return {"ok": True, "message": "pipeline_scraping ejecutado"}
    except Exception as e:
        logger.exception("Error en ejecución manual de pipeline_scraping.")
        return {"ok": False, "error": str(e)}


@app.post("/run-services")
async def manual_run_services() -> Dict:
    """Ejecuta el pipeline de servicios manualmente."""
    try:
        ServicesScraper().update_services()
        return {"ok": True, "message": "Pipeline de servicios ejecutado"}
    except Exception as e:
        logger.exception("Error en ejecución manual de servicios.")
        return {"ok": False, "error": str(e)}


@app.post("/limpieza")
async def manual_limpieza() -> Dict:
    """Descartar notas viejas pendientes sin grupo y limpiar base de datos."""
    try:
        supabase = SupabaseNewsClient()
        n_sin_grupo = supabase.descartar_pendientes_sin_grupo()
        n_expiradas = supabase.expirar_noticias_antiguas(dias=15)
        return {
            "ok": True,
            "message": "Limpieza completada",
            "descartadas_sin_grupo": n_sin_grupo,
            "expiradas_por_antiguedad": n_expiradas
        }
    except Exception as e:
        logger.exception("Error en ejecución manual de limpieza.")
        return {"ok": False, "error": str(e)}


# ─── Scheduler ───────────────────────────────────────────────────

@app.on_event("startup")
def on_startup() -> None:
    # Fase 1: scraping sin IA, cada N minutos
    scheduler.add_job(
        pipeline_scraping,
        trigger="interval",
        minutes=config.SCHEDULER_INTERVAL_MINUTES,
        id="scraping_pipeline",
        replace_existing=True,
    )

    # Servicios diarios a las 7:00 AM
    def update_services_job():
        try:
            ServicesScraper().update_services()
        except Exception as e:
            logger.error("Error en update_services_job: %s", e)

    scheduler.add_job(
        update_services_job,
        trigger="cron",
        hour=7,
        minute=0,
        id="services_pipeline",
        replace_existing=True,
    )

    # Limpieza diaria a las 3:00 AM (expiar notas >15 dias y pendientes huerfanas)
    def limpieza_diaria_job():
        try:
            logger.info("Iniciando tarea programada de limpieza...")
            supabase = SupabaseNewsClient()
            supabase.descartar_pendientes_sin_grupo()
            supabase.expirar_noticias_antiguas(dias=15)
        except Exception as e:
            logger.error("Error en limpieza_diaria_job: %s", e)

    scheduler.add_job(
        limpieza_diaria_job,
        trigger="cron",
        hour=3,
        minute=0,
        id="limpieza_pipeline",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler iniciado | scraping cada %s min | servicios diario 07:00",
        config.SCHEDULER_INTERVAL_MINUTES,
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler detenido.")


# ─── CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Neco News Scraper v3")
    parser.add_argument("--scrape", action="store_true", help="Ejecuta pipeline_scraping() y termina.")
    parser.add_argument("--smoke", action="store_true", help="Test de conectividad con IA y termina.")
    args = parser.parse_args()

    if not config.validate():
        sys.exit(1)

    if args.smoke:
        try:
            AIProcessor().smoke_test()
            print("[OK] Smoke test passed")
        except Exception:
            logger.exception("Smoke test falló.")
            sys.exit(1)
        sys.exit(0)

    if args.scrape:
        pipeline_scraping()
        sys.exit(0)

    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT, reload=False)
