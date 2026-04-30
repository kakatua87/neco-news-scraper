"""
Configuración centralizada del scraper Neco News.
Todas las variables de entorno se validan aquí al iniciar.
"""

import logging
import os
import sys

from dotenv import load_dotenv

# Cargar .env desde el directorio del script
_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_dir, ".env"), override=False)

logger = logging.getLogger("neconews.config")

# ─── Base de datos ───────────────────────────────────────────────
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "").strip()

# ─── IA Multi-proveedor ─────────────────────────────────────────
AI_PROVIDER: str = os.getenv("AI_PROVIDER", "groq").strip().lower()
AI_API_KEY: str = os.getenv("AI_API_KEY", "").strip()
AI_MODEL: str = os.getenv("AI_MODEL", "").strip()
AI_BASE_URL: str = os.getenv("AI_BASE_URL", "").strip()

# Defaults por proveedor
_PROVIDER_DEFAULTS = {
    "groq": {
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "openai": {
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
    "anthropic": {
        "model": "claude-sonnet-4-5",
        "base_url": "https://api.anthropic.com/v1",
    },
}

if AI_PROVIDER in _PROVIDER_DEFAULTS:
    defaults = _PROVIDER_DEFAULTS[AI_PROVIDER]
    if not AI_MODEL:
        AI_MODEL = defaults["model"]
    if not AI_BASE_URL:
        AI_BASE_URL = defaults["base_url"]
else:
    logger.warning("AI_PROVIDER=%s no reconocido. Asegurate de setear AI_MODEL y AI_BASE_URL.", AI_PROVIDER)

# ─── Telegram ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ─── Pipeline ────────────────────────────────────────────────────
PORT: int = int(os.getenv("PORT", "8000"))
MAX_NOTES_PER_RUN: int = int(os.getenv("MAX_NOTES_PER_RUN", "12"))
AI_DELAY_SECONDS: float = float(os.getenv("AI_DELAY_SECONDS", "2.0"))
SCHEDULER_INTERVAL_MINUTES: int = int(os.getenv("SCHEDULER_INTERVAL_MINUTES", "15"))

# ─── Portal URL (para links en Telegram) ─────────────────────────
PORTAL_URL: str = os.getenv("PORTAL_URL", "https://neco-news.vercel.app").strip()
ADMIN_URL: str = f"{PORTAL_URL}/admin"

# ─── Branding ─────────────────────────────────────────────────────
PORTAL_NAME: str = "Neco News"


def validate() -> bool:
    """Valida que las variables críticas estén definidas. Retorna False si falta alguna."""
    errors = []
    if not SUPABASE_URL:
        errors.append("SUPABASE_URL")
    if not SUPABASE_KEY:
        errors.append("SUPABASE_KEY")
    if not AI_API_KEY:
        errors.append("AI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID")

    if errors:
        logger.error(
            "Variables de entorno faltantes: %s. Configuralas en .env y reiniciá.",
            ", ".join(errors),
        )
        return False

    logger.info("Config OK | provider=%s | model=%s | portal=%s", AI_PROVIDER, AI_MODEL, PORTAL_NAME)
    return True
