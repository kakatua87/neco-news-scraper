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
        # gemini-2.0-flash fue dado de baja por Google (confirmado con 404
        # real en agosto 2026) — este es el reemplazo vigente al día de hoy.
        "model": "gemini-2.5-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "label": "Google Gemini 2.5 Flash",
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

# Gemini soporta hasta 3 API keys gratis rotando entre sí: si una se queda
# sin cuota (429 / RESOURCE_EXHAUSTED), AIProcessor prueba con la siguiente
# antes de rendirse. GEMINI_API_KEY ya la toma PROVIDER_API_KEYS de arriba;
# acá juntamos las 3 en orden para el mecanismo de fallback.
GEMINI_API_KEYS: list = [
    k for k in (
        PROVIDER_API_KEYS.get("gemini", ""),
        os.getenv("GEMINI_API_KEY_2", "").strip(),
        os.getenv("GEMINI_API_KEY_3", "").strip(),
    )
    if k
]


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
SCHEDULER_INTERVAL_MINUTES: int = int(os.getenv("SCHEDULER_INTERVAL_MINUTES", "30"))

# ─── Portal URL (para links en Telegram) ─────────────────────────
PORTAL_URL: str = os.getenv("PORTAL_URL", "https://neco-news.vercel.app").strip()
ADMIN_URL: str = f"{PORTAL_URL}/admin"

# URL del propio servidor de scraping (Render). El bot la usa para llamar
# a /procesar-grupo. Por defecto apunta a sí mismo (localhost en dev).
SCRAPER_URL: str = os.getenv("SCRAPER_URL", "http://localhost:8000").strip()

# Secreto compartido entre este servicio y el portal Next.js (neco-news),
# usado en llamadas server-to-server sin sesión de usuario: protege los
# endpoints de control del scraper (/run, /procesar-grupo, etc.) y el envío
# de notificaciones push desde este servicio hacia el portal. Debe tener el
# mismo valor en ambos deploys (Render y Vercel).
INTERNAL_API_SECRET: str = os.getenv("INTERNAL_API_SECRET", "").strip()

# ─── Proxy rotativo (Webshare u otro compatible) ──────────────────
# PROXY_LIST: "host:puerto,host:puerto,..." (SIN esquema — el código antepone
# http://). Una IP se elige al azar por request. Mismo usuario/clave para
# todas (plan free de Webshare). Si falla un proxy (banda agotada, timeout,
# auth) el scraper cae a conexión directa en vez de romper el pipeline —
# ver scraper.py:_abrir_pagina.
PROXY_ENABLED: bool = os.getenv("PROXY_ENABLED", "false").strip().lower() == "true"
PROXY_LIST: list = [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]
PROXY_USERNAME: str = os.getenv("PROXY_USERNAME", "").strip()
PROXY_PASSWORD: str = os.getenv("PROXY_PASSWORD", "").strip()

# Dominios que SÍ salen por proxy (el resto va directo, aunque PROXY_ENABLED
# esté en true). El plan free de Webshare tiene 1 GB/mes: reservamos esa banda
# para las fuentes que realmente la necesitan (las que están detrás de un WAF
# y bloquean el scraping desde IP de datacenter). Set vacío = proxy para todo.
PROXY_ONLY_DOMAINS: set = {
    d.strip().lower().removeprefix("www.")
    for d in os.getenv("PROXY_ONLY_DOMAINS", "diarionecochea.com,diario4v.com").split(",")
    if d.strip()
}

# ─── Branding ─────────────────────────────────────────────────────
PORTAL_NAME: str = "Neco Now"


def validate(require_ai: bool = True) -> bool:
    """Valida que las variables críticas estén definidas. Retorna False si falta alguna.

    require_ai=False para corridas que no tocan la IA (--scrape / --services): así
    el job de scraping en GitHub Actions no necesita cargar la API key del proveedor.
    """
    errors = []
    if not SUPABASE_URL:
        errors.append("SUPABASE_URL")
    if not SUPABASE_KEY:
        errors.append("SUPABASE_KEY")
    if require_ai and not PROVIDER_API_KEYS.get(AI_PROVIDER):
        errors.append(f"API key para el proveedor activo ({AI_PROVIDER}): seteá AI_API_KEY o {AI_PROVIDER.upper()}_API_KEY")
    if AI_PROVIDER == "gemini" and len(GEMINI_API_KEYS) > 1:
        logger.info("Gemini con %s API keys configuradas (rotan si una se queda sin cuota).", len(GEMINI_API_KEYS))
    if not INTERNAL_API_SECRET:
        logger.warning(
            "INTERNAL_API_SECRET no configurado: los endpoints de control "
            "(/run, /procesar-grupo, etc.) quedarán bloqueados para todos "
            "hasta que lo definas (debe coincidir con el del portal Next.js)."
        )
    if PROXY_ENABLED and not (PROXY_LIST and PROXY_USERNAME and PROXY_PASSWORD):
        logger.warning(
            "PROXY_ENABLED=true pero falta PROXY_LIST/PROXY_USERNAME/PROXY_PASSWORD. "
            "El scraper va a usar conexión directa (sin proxy) hasta que los definas."
        )
    elif PROXY_ENABLED:
        logger.info("Proxy rotativo activo: %s IPs configuradas.", len(PROXY_LIST))

    if errors:
        logger.error(
            "Variables de entorno faltantes: %s. Configuralas en .env y reiniciá.",
            ", ".join(errors),
        )
        return False

    logger.info("Config OK | provider=%s | model=%s | portal=%s", AI_PROVIDER, AI_MODEL, PORTAL_NAME)
    return True
