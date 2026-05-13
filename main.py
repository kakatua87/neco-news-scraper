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
import re
import sys
import time
import uuid
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
from telegram_bot import TelegramBotClient

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


# ─── Deduplicación semántica por título ──────────────────────────

_STOPWORDS = frozenset({
    "el", "la", "los", "las", "de", "del", "en", "un", "una", "y", "a", "que",
    "se", "con", "por", "es", "su", "al", "lo", "le", "esta", "este", "son",
    "ha", "fue", "para", "como", "más", "no", "ya", "sin", "ante", "sobre",
    "pero", "sus", "muy", "ser", "hasta", "hay", "entre",
})


def _normalize_title(title: str) -> set:
    """Convierte un título en un conjunto de tokens normalizados."""
    clean = re.sub(r"[^a-záéíóúüñ\s]", "", title.lower())
    return {t for t in clean.split() if t not in _STOPWORDS and len(t) > 2}


def _jaccard(a: set, b: set) -> float:
    """Similaridad de Jaccard entre dos conjuntos de tokens."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _group_by_similarity(notes: List[Dict], threshold: float = 0.65) -> List[List[Dict]]:
    """
    Agrupa noticias sobre el mismo hecho usando similaridad contextual.

    Criterios para agrupar (TODOS deben cumplirse):
    1. Similaridad Jaccard de tokens >= threshold
    2. Comparten al menos UNA entidad concreta (nombre propio, número, lugar)
    3. La sección temática es compatible
    """
    groups: List[List[Dict]] = []
    used = set()
    tokens = [_normalize_title(n.get("titulo", "")) for n in notes]

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
        capitalized = {w for w in words[1:] if w and w[0].isupper()
                       and len(w) > 2 and w.lower() not in _STOPWORDS}
        LUGARES = {"necochea", "quequén", "quequen", "lobería", "loberia",
                   "san cayetano", "miramar", "tres arroyos", "claromecó",
                   "ruta 88", "ruta 11", "ruta 3"}
        text_lower = title.lower()
        lugares_found = {l for l in LUGARES if l in text_lower}
        return numbers | capitalized | lugares_found

    for i, note in enumerate(notes):
        if i in used:
            continue
        group = [note]
        used.add(i)
        entities_i = extract_entities(note.get("titulo", ""))
        section_i = note.get("seccion", "Local")

        for j in range(i + 1, len(notes)):
            if j in used:
                continue
            sim = _jaccard(tokens[i], tokens[j])
            if sim < threshold:
                continue
            section_j = notes[j].get("seccion", "Local")
            if not sections_compatible(section_i, section_j):
                continue
            entities_j = extract_entities(notes[j].get("titulo", ""))
            shared_entities = entities_i & entities_j
            if not shared_entities:
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
    Fase 1: Scrape → Dedup → Supabase (estado=raw) → Telegram notifica grupo.
    No invoca la IA en ningún momento.
    """
    logger.info("═══ pipeline_scraping v3.0 — inicio ═══")
    try:
        supabase_client = SupabaseNewsClient()
        existing_urls = supabase_client.get_urls_existentes()
        scraper = NewsScraper(existing_urls=existing_urls)
        telegram = TelegramBotClient(supabase_client=supabase_client)
    except Exception:
        logger.exception("Error inicializando dependencias de pipeline_scraping.")
        return

    # ── Scraping de todas las fuentes ────────────────────────────
    raw_notes: List[Dict] = []
    for source_fn in (
        scraper.scrape_nden,
        scraper.scrape_diario_necochea,
        scraper.scrape_diario4v,
        scraper.scrape_tsn,
        scraper.scrape_diarionq,
        scraper.scrape_elecos,
    ):
        try:
            raw_notes.extend(source_fn())
        except Exception:
            logger.exception("Error en fuente=%s", source_fn.__name__)

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

        # ── Notificar a Telegram ──────────────────────────────────
        try:
            telegram.send_grupo_preview({
                "grupo_id": grupo_id,
                "notas": notas_insertadas,
                "seccion_sugerida": leader.get("seccion", "Local"),
            })
        except Exception:
            logger.exception("Error enviando grupo preview a Telegram grupo_id=%s", grupo_id)

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
        telegram = TelegramBotClient(supabase_client=supabase_client)
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
            rewritten = ai.process_multi_source(titulo, all_texts, seccion)
        else:
            rewritten = ai.process_article(titulo, all_texts[0], seccion)
    except Exception as e:
        logger.exception("IA falló en pipeline_ia para grupo_id=%s", grupo_id)
        return {"ok": False, "error": f"IA falló: {str(e)[:200]}"}

    # Actualizar la nota líder en Supabase (estado=pendiente)
    rewritten["imagen_url"] = best_image
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

    # Notificar a Telegram para revisión final (send_preview con botones publicar/descartar)
    noticia_para_telegram = {
        "id": noticia_id,
        "titulo": rewritten.get("titulo", titulo),
        "cuerpo": rewritten.get("cuerpo", ""),
        "seccion": rewritten.get("seccion_sugerida") or seccion,
        "imagen_url": rewritten.get("imagen_url"),
    }
    try:
        telegram.send_preview(noticia_para_telegram)
    except Exception:
        logger.exception("Error enviando preview final a Telegram id=%s", noticia_id)

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


@app.post("/telegram/callback")
async def telegram_callback(request: Request) -> Dict:
    """Webhook de Telegram para procesar botones inline."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "json inválido"}

    callback_query = body.get("callback_query")
    if not callback_query:
        return {"ok": True}  # Ignorar mensajes normales u otros eventos

    try:
        data = callback_query.get("data", "")
        cq_id = callback_query.get("id")

        supabase_client = SupabaseNewsClient()
        telegram = TelegramBotClient(supabase_client=supabase_client)
        result = telegram.callback_handler(data, callback_query=callback_query)
        logger.info("Callback telegram procesado: %s", result)

        # Responder a Telegram (answer para cerrar el loading)
        if cq_id:
            token = config.TELEGRAM_BOT_TOKEN
            action = result.get("action", "")
            text_map = {
                "publicada": "✓ Publicada",
                "descartada": "✕ Descartada",
                "cambio_seccion": "📂 Sección actualizada",
                "menu_seccion": "",
                "volver": "",
            }
            answer_text = text_map.get(action, "")
            payload: Dict = {"callback_query_id": cq_id}
            if answer_text:
                payload["text"] = answer_text
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                    json=payload,
                    timeout=10,
                )
            except Exception as e:
                logger.error("Error enviando answerCallbackQuery: %s", e)

        return result
    except Exception:
        logger.exception("Error procesando callback de Telegram.")
        return {"ok": False}


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
