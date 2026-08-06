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

# Defaults por proveedor. "label" y "gratis" son solo para mostrar en el
# selector del panel admin (GET /ai-providers).
_PROVIDER_DEFAULTS = {
    "groq": {
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
        "label": "Groq (Llama 3.3 70B)",
        "gratis": True,
    },
    "gemini": {
        "model": "gemini-2.0-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "label": "Google Gemini 2.0 Flash",
        "gratis": True,
    },
    "openrouter": {
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "base_url": "https://openrouter.ai/api/v1",
        "label": "OpenRouter (Llama 3.3 70B free)",
        "gratis": True,
    },
    "openai": {
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "label": "OpenAI (GPT-4o mini)",
        "gratis": False,
    },
    "anthropic": {
        "model": "claude-sonnet-4-5",
        "base_url": "https://api.anthropic.com/v1",
        "label": "Anthropic (Claude Sonnet)",
        "gratis": False,
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

# API key por proveedor: cada uno puede tener la suya propia
# (GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, ...). Si no está seteada
# pero ese proveedor es el AI_PROVIDER activo, cae a AI_API_KEY — así el
# setup actual (un solo AI_API_KEY para el proveedor por defecto) sigue
# funcionando sin tocar nada.
def _api_key_for(provider: str) -> str:
    specific = os.getenv(f"{provider.upper()}_API_KEY", "").strip()
    if specific:
        return specific
    return AI_API_KEY if provider == AI_PROVIDER else ""

PROVIDER_API_KEYS: dict = {p: _api_key_for(p) for p in _PROVIDER_DEFAULTS}


def get_provider_config(provider: str) -> "dict | None":
    """Config completa de un proveedor si tiene API key configurada, sino None."""
    defaults = _PROVIDER_DEFAULTS.get(provider)
    api_key = PROVIDER_API_KEYS.get(provider, "")
    if not defaults or not api_key:
        return None
    return {
        "provider": provider,
        "model": AI_MODEL if provider == AI_PROVIDER else defaults["model"],
        "base_url": AI_BASE_URL if provider == AI_PROVIDER else defaults["base_url"],
        "api_key": api_key,
        "label": defaults["label"],
    }


def list_available_providers() -> list:
    """Proveedores con API key configurada, para el selector del admin."""
    result = []
    for p in _PROVIDER_DEFAULTS:
        cfg = get_provider_config(p)
        if cfg:
            result.append({
                "provider": cfg["provider"],
                "model": cfg["model"],
                "label": cfg["label"],
                "gratis": _PROVIDER_DEFAULTS[p]["gratis"],
                "default": p == AI_PROVIDER,
            })
    return result

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

# URL del propio servidor de scraping (Render). El bot la usa para llamar
# a /procesar-grupo. Por defecto apunta a sí mismo (localhost en dev).
SCRAPER_URL: str = os.getenv("SCRAPER_URL", "http://localhost:8000").strip()

# ─── Branding ─────────────────────────────────────────────────────
PORTAL_NAME: str = "Neco Now"


def validate() -> bool:
    """Valida que las variables críticas estén definidas. Retorna False si falta alguna."""
    errors = []
    if not SUPABASE_URL:
        errors.append("SUPABASE_URL")
    if not SUPABASE_KEY:
        errors.append("SUPABASE_KEY")
    if not AI_API_KEY:
        errors.append("AI_API_KEY")

    if errors:
        logger.error(
            "Variables de entorno faltantes: %s. Configuralas en .env y reiniciá.",
            ", ".join(errors),
        )
        return False

    logger.info("Config OK | provider=%s | model=%s | portal=%s", AI_PROVIDER, AI_MODEL, PORTAL_NAME)
    return True
