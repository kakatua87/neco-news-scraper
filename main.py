"""
Neco News — Pipeline principal de scraping y publicación.

Flujo: Scrape → IA reescribe → Supabase → Telegram notifica
Scheduler: cada N minutos (configurable)
API: FastAPI con /health y /telegram/callback
"""

import argparse
import logging
import re
import sys
import time
from typing import Dict, List
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
import uvicorn

import config
from ai_processor import AIProcessor
from scraper import NewsScraper
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
app = FastAPI(title="Neco News Scraper", version="2.0.0")
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


def pipeline() -> None:
    """Ejecuta el ciclo completo de scraping → IA → Supabase → Telegram."""
    logger.info("═══ Iniciando pipeline Neco News ═══")
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
    for source_fn in (scraper.scrape_nden, scraper.scrape_diario_necochea):
        try:
            raw_notes.extend(source_fn())
        except Exception:
            logger.exception("Error en source_fn=%s", source_fn.__name__)

    logger.info("Total de notas candidatas: %s", len(raw_notes))

    # ─── Procesamiento ───────────────────────────────────────────
    processed = 0
    ai_failed = False

    for note in raw_notes[:config.MAX_NOTES_PER_RUN]:
        url = note.get("url")
        title = note.get("titulo", "")
        section = note.get("seccion", "General")
        fuente = note.get("fuente", "Neco News")
        image = note.get("imagen_url")

        if not url or url in existing_urls:
            continue

        try:
            # Extraer contenido completo + métadatos de imagen
            article_data = scraper.get_article_content(url)
            content = article_data["text"]
            og_image = article_data.get("og_image")
            content_image = article_data.get("content_image")

            if len(content) < 60:
                logger.info("Nota descartada por cuerpo corto: %s", url)
                continue

            # Prioridad de imagen: OG > content_image > card_image > Wikimedia
            best_image: str | None = og_image or content_image or image
            imagen_fuente = "Fuente original"
            if not best_image:
                best_image = scraper.get_wikimedia_image(title)
                if best_image:
                    imagen_fuente = "Wikimedia Commons / Ilustrativa"
            elif og_image:
                imagen_fuente = fuente

            # Intentar reescribir con IA
            payload: Dict
            if ai and not ai_failed:
                try:
                    rewritten = ai.process_article(title, content, section)
                    # La IA puede sugerir una sección más precisa
                    seccion_final = rewritten.get("seccion_sugerida") or section
                    payload = {
                        **rewritten,
                        "seccion": seccion_final,
                        "fuente": fuente,
                        "url_original": url,
                        "imagen_url": best_image,
                        "imagen_fuente": imagen_fuente,
                    }
                except Exception as ai_err:
                    logger.warning("IA falló para esta nota. Guardando cruda. Error: %s", str(ai_err)[:300])
                    err_str = str(ai_err).lower()
                    if "429" in err_str or "rate" in err_str or "limit" in err_str:
                        ai_failed = True
                        logger.warning("Rate limit detectado. IA desactivada para el resto de la corrida.")
                    payload = _raw_payload(title, content, section, fuente, url, best_image, imagen_fuente)
            else:
                payload = _raw_payload(title, content, section, fuente, url, best_image, imagen_fuente)

            # Insertar en Supabase
            inserted = supabase_client.insert_noticia(payload)
            existing_urls.add(url)
            processed += 1
            logger.info("Nota procesada: id=%s | slug=%s | imagen_fuente=%s",
                        inserted.get("id"), inserted.get("slug"), imagen_fuente)

            # Notificar por Telegram
            try:
                telegram.send_preview(inserted)
            except Exception:
                logger.exception("No se pudo enviar preview a Telegram para noticia id=%s", inserted.get("id"))

            # Delay entre notas (respetar rate limits)
            time.sleep(config.AI_DELAY_SECONDS)

        except Exception:
            logger.exception("Fallo procesando nota url=%s. Se continúa con la siguiente.", url)

    logger.info("═══ Pipeline finalizado. Notas procesadas: %s ═══", processed)


def _raw_payload(title: str, content: str, section: str, fuente: str,
                  url: str, image: str | None, imagen_fuente: str = "Fuente original") -> Dict:
    """Construye payload sin reescritura IA."""
    return {
        "titulo": title,
        "cuerpo": content,
        "resumen_seo": "",
        "seccion": section,
        "fuente": fuente,
        "url_original": url,
        "imagen_url": image,
        "imagen_fuente": imagen_fuente,
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
    """Ejecuta el pipeline manualmente (para debug)."""
    try:
        pipeline()
        return {"ok": True, "message": "Pipeline ejecutado"}
    except Exception as e:
        logger.exception("Error en ejecución manual.")
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
    scheduler.start()
    logger.info(
        "Scheduler iniciado. Pipeline cada %s minutos.",
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
