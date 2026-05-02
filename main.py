"""
Neco News — Pipeline principal de scraping y publicación.

Flujo: Scrape → Deduplicación → IA sintetiza → Supabase → Telegram notifica
Scheduler: cada N minutos (configurable)
API: FastAPI con /health y /telegram/callback
"""

import argparse
import logging
import re
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

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
app = FastAPI(title="Neco News Scraper", version="2.1.0")
scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")


def slug_from_url(url: str, fallback_title: str) -> str:
    """Genera un slug estable a partir de la URL."""
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
    Agrupa noticias por similaridad de título usando Jaccard.
    Retorna lista de grupos (cada grupo = lista de noticias sobre el mismo evento).
    """
    groups: List[List[Dict]] = []
    used = set()

    # Pre-calcular tokens de cada título
    tokens = [_normalize_title(n.get("titulo", "")) for n in notes]

    for i, note in enumerate(notes):
        if i in used:
            continue
        group = [note]
        used.add(i)

        for j in range(i + 1, len(notes)):
            if j in used:
                continue
            sim = _jaccard(tokens[i], tokens[j])
            if sim >= threshold:
                group.append(notes[j])
                used.add(j)

        groups.append(group)

    return groups


def pipeline() -> None:
    """Ejecuta el ciclo completo de scraping → dedup → IA → Supabase → Telegram."""
    logger.info("═══ Iniciando pipeline Neco News v2.2 ═══")
    try:
        supabase_client = SupabaseNewsClient()
        existing_urls = supabase_client.get_urls_existentes()
        scraper = NewsScraper(existing_urls=existing_urls)
        telegram = TelegramBotClient(supabase_client=supabase_client)
    except Exception:
        logger.exception("Error inicializando dependencias de pipeline.")
        return

    # Intentar inicializar IA (puede fallar si no hay API key)
    ai: AIProcessor | None = None
    try:
        ai = AIProcessor()
    except Exception:
        logger.warning("IA no disponible. Las notas se guardarán sin reescritura.")

    # ─── Scraping ────────────────────────────────────────────────
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
            logger.exception("Error en source_fn=%s", source_fn.__name__)

    logger.info("Total de notas candidatas: %s", len(raw_notes))

    # ─── Deduplicación: agrupar noticias similares ───────────────
    groups = _group_by_similarity(raw_notes)
    logger.info("Grupos tras deduplicación: %s (de %s notas)", len(groups), len(raw_notes))

    # ─── Procesamiento por grupo ─────────────────────────────────
    processed = 0
    ai_failed = False

    for group in groups[:config.MAX_NOTES_PER_RUN]:
        # Elegir la nota "líder" (primera del grupo)
        leader = group[0]
        urls = [n["url"] for n in group if n.get("url")]
        title = leader.get("titulo", "")
        section = leader.get("seccion", "General")
        image = leader.get("imagen_url")

        # Si todas las URLs ya están procesadas, skip
        if all(u in existing_urls for u in urls):
            continue
        primary_url = next((u for u in urls if u not in existing_urls), urls[0])

        try:
            # Extraer contenido de cada fuente del grupo
            all_texts: List[str] = []
            best_image: Optional[str] = None

            for note in group:
                note_url = note.get("url")
                if not note_url:
                    continue
                try:
                    article_data = scraper.get_article_content(note_url)
                    text = article_data.get("text", "")
                    if len(text) >= 60:
                        all_texts.append(text)
                    # Buscar la mejor imagen entre todas las fuentes
                    if not best_image:
                        best_image = (
                            article_data.get("og_image")
                            or article_data.get("content_image")
                            or note.get("imagen_url")
                        )
                except Exception:
                    logger.debug("No se pudo extraer contenido de %s", note_url)

            if not all_texts:
                logger.info("Grupo descartado: sin contenido suficiente | título=%s", title)
                continue

            # Fallback de imagen: Wikimedia
            if not best_image:
                best_image = scraper.get_wikimedia_image(title)

            # Límite de cross-sourcing: tomar solo las 2 fuentes más extensas si hay muchas
            if len(all_texts) > 2:
                all_texts.sort(key=len, reverse=True)
                all_texts = all_texts[:2]

            combined_content = "\n\n---\n\n".join(all_texts)
            num_sources = len(all_texts)

            if num_sources > 1:
                logger.info(
                    "Cross-sourcing: %s fuentes agrupadas para '%s'",
                    num_sources, title[:60],
                )

            # Intentar reescribir con IA (Bypass para servicios)
            bypass_ai = section in ["Obituarios", "Farmacias", "Clima"]
            payload: Dict
            if ai and not ai_failed and not bypass_ai:
                try:
                    if num_sources > 1:
                        rewritten = ai.process_multi_source(title, all_texts, section)
                    else:
                        rewritten = ai.process_article(title, combined_content, section)
                    seccion_final = rewritten.get("seccion_sugerida") or section
                    payload = {
                        **rewritten,
                        "seccion": seccion_final,
                        "url_original": primary_url,
                        "imagen_url": best_image,
                    }
                except Exception as ai_err:
                    logger.warning("IA falló. Guardando cruda. Error: %s", str(ai_err)[:300])
                    err_str = str(ai_err).lower()
                    if "429" in err_str or "rate" in err_str or "limit" in err_str:
                        ai_failed = True
                        logger.warning("Rate limit detectado. IA desactivada para el resto.")
                    payload = _raw_payload(title, combined_content, section, primary_url, best_image)
            else:
                payload = _raw_payload(title, combined_content, section, primary_url, best_image)

            # Insertar en Supabase
            inserted = supabase_client.insert_noticia(payload)
            # Marcar todas las URLs del grupo como procesadas
            for u in urls:
                existing_urls.add(u)
            processed += 1
            logger.info(
                "Nota procesada: id=%s | slug=%s | fuentes=%s",
                inserted.get("id"), inserted.get("slug"), num_sources,
            )

            # Notificar por Telegram
            try:
                telegram.send_preview(inserted)
            except Exception:
                logger.exception("No se pudo enviar preview a Telegram para id=%s", inserted.get("id"))

            # Delay entre notas (respetar rate limits)
            time.sleep(config.AI_DELAY_SECONDS)

        except Exception:
            logger.exception("Fallo procesando grupo url=%s.", primary_url)

    logger.info("═══ Pipeline finalizado. Notas procesadas: %s ═══", processed)


def _raw_payload(title: str, content: str, section: str,
                  url: str, image: str | None) -> Dict:
    """Construye payload sin reescritura IA."""
    return {
        "titulo": title,
        "cuerpo": content,
        "resumen_seo": "",
        "seccion": section,
        "url_original": url,
        "imagen_url": image,
        "instagram_text": "",
        "twitter_text": "",
        "guion_video": "",
        "slug": slug_from_url(url, title),
    }


# ─── Endpoints ───────────────────────────────────────────────────

@app.get("/health")
def health() -> Dict[str, str]:
    """Health check para UptimeRobot / Render."""
    return {"status": "ok", "service": "neco-news-scraper"}


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
        supabase_client = SupabaseNewsClient()
        telegram = TelegramBotClient(supabase_client=supabase_client)
        result = telegram.callback_handler(callback_query)
        logger.info("Callback telegram procesado: %s", result)
        return result
    except Exception:
        logger.exception("Error procesando callback de Telegram.")
        return {"ok": False}


@app.post("/run")
async def manual_run() -> Dict:
    """Ejecuta el pipeline de noticias manualmente (para debug)."""
    try:
        pipeline()
        return {"ok": True, "message": "Pipeline de noticias ejecutado"}
    except Exception as e:
        logger.exception("Error en ejecución manual de noticias.")
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


# ─── Scheduler ───────────────────────────────────────────────────

@app.on_event("startup")
def on_startup() -> None:
    scheduler.add_job(
        pipeline,
        trigger="interval",
        minutes=config.SCHEDULER_INTERVAL_MINUTES,
        id="news_pipeline",
        replace_existing=True,
    )
    
    # Scraper de servicios diario a las 7:00 AM
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
    
    scheduler.start()
    logger.info(
        "Scheduler iniciado. Pipeline de noticias cada %s minutos. Pipeline de servicios diario a las 07:00.",
        config.SCHEDULER_INTERVAL_MINUTES,
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler detenido.")


# ─── CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Neco News Scraper")
    parser.add_argument("--test", action="store_true", help="Ejecuta pipeline una sola vez y termina.")
    parser.add_argument("--smoke", action="store_true", help="Test de conectividad con IA y termina.")
    args = parser.parse_args()

    # Validar configuración
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

    if args.test:
        pipeline()
        sys.exit(0)

    # Servidor con scheduler
    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT, reload=False)
