"""
Motor IA multi-proveedor para reescritura de noticias.

Usa la librería `openai` de Python que es compatible con Groq, OpenAI y otros
proveedores que exponen una API compatible con el formato OpenAI.

Proveedores soportados:
  - groq   (gratis, Llama 3.3 70B)   ← default
  - openai (GPT-4o-mini, requiere pago)
  - anthropic (Claude, futuro)
"""

import json
import logging
import re
import time
from typing import Dict, Optional

from openai import OpenAI, APIError, RateLimitError, APIConnectionError

import config

logger = logging.getLogger("neconews.ai")

# ─── Prompt editorial ────────────────────────────────────────────

SYSTEM_PROMPT = (
    f"Sos el redactor jefe de {config.PORTAL_NAME}, portal de noticias de Necochea, Argentina. "
    "Tu trabajo es reescribir noticias de forma completamente original. "
    "REGLAS:\n"
    "1. Reescribí completamente sin copiar frases del original\n"
    "2. Mantené solo hechos objetivos y verificables\n"
    "3. Usá tono rioplatense claro y profesional\n"
    "4. El cuerpo debe tener exactamente 3 párrafos bien desarrollados\n"
    "5. El slug debe ser URL-friendly (sin tildes, sin espacios, separado por guiones)\n"
    "6. Devolvé SOLO un JSON válido, sin markdown, sin comentarios, sin texto extra\n\n"
    "Formato de respuesta (JSON):\n"
    "{\n"
    '  "titulo": "Título reescrito",\n'
    '  "cuerpo": "Párrafo 1\\n\\nPárrafo 2\\n\\nPárrafo 3",\n'
    '  "resumen_seo": "Resumen de 160 caracteres para meta description",\n'
    '  "instagram_text": "Texto para Instagram con emojis (máx 2200 chars)",\n'
    '  "twitter_text": "Texto para Twitter/X (máx 280 chars)",\n'
    '  "guion_video": "Guión corto para video de 60 segundos",\n'
    '  "slug": "titulo-reescrito-url-friendly"\n'
    "}"
)


class AIProcessor:
    """
    Procesador de noticias multi-proveedor.
    Compatible con cualquier API que siga el formato OpenAI.
    """

    def __init__(self) -> None:
        if not config.AI_API_KEY:
            raise ValueError("AI_API_KEY es obligatoria. Configurala en .env")

        self.provider = config.AI_PROVIDER
        self.model = config.AI_MODEL
        self.client = OpenAI(
            api_key=config.AI_API_KEY,
            base_url=config.AI_BASE_URL,
        )

        key_preview = f"{config.AI_API_KEY[:8]}...{config.AI_API_KEY[-4:]}" if len(config.AI_API_KEY) >= 12 else "***"
        logger.info(
            "AI inicializado | provider=%s | model=%s | key=%s",
            self.provider, self.model, key_preview,
        )

    def process_article(self, titulo: str, cuerpo: str, seccion: str) -> Dict:
        """
        Reescribe una noticia usando IA.
        Retorna dict con: titulo, cuerpo, resumen_seo, instagram_text, twitter_text, guion_video, slug.
        Lanza excepción si falla tras todos los reintentos.
        """
        user_prompt = (
            f"Sección: {seccion}\n"
            f"Título original: {titulo}\n"
            f"Cuerpo original:\n{cuerpo}\n"
        )

        text = self._call_with_retry(user_prompt)
        parsed = self._safe_json_parse(text)

        # Validar campos requeridos
        required_fields = ["titulo", "cuerpo", "resumen_seo", "instagram_text", "twitter_text", "guion_video", "slug"]
        missing = [f for f in required_fields if f not in parsed]
        if missing:
            raise ValueError(f"IA no devolvió campos requeridos: {', '.join(missing)}")

        logger.info("Artículo procesado OK | provider=%s | slug=%s", self.provider, parsed.get("slug"))
        return parsed

    def _call_with_retry(self, user_prompt: str, max_retries: int = 3) -> str:
        """Llama a la API con backoff exponencial."""
        last_err: Optional[Exception] = None
        delays = [2, 4, 8]  # Backoff: 2s, 4s, 8s

        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.7,
                    max_tokens=2000,
                )

                text = (response.choices[0].message.content or "").strip()
                if not text:
                    logger.warning("IA devolvió texto vacío (intento %s/%s)", attempt + 1, max_retries)
                    last_err = RuntimeError("IA devolvió respuesta vacía")
                    continue

                # Log de uso
                usage = response.usage
                if usage:
                    logger.info(
                        "Tokens | prompt=%s | completion=%s | total=%s",
                        usage.prompt_tokens, usage.completion_tokens, usage.total_tokens,
                    )

                return text

            except RateLimitError as e:
                last_err = e
                delay = delays[min(attempt, len(delays) - 1)]
                logger.warning(
                    "Rate limit (429) en intento %s/%s. Esperando %ss... | error=%s",
                    attempt + 1, max_retries, delay, str(e)[:200],
                )
                time.sleep(delay)
                continue

            except APIConnectionError as e:
                last_err = e
                delay = delays[min(attempt, len(delays) - 1)]
                logger.warning(
                    "Error de conexión en intento %s/%s. Esperando %ss... | error=%s",
                    attempt + 1, max_retries, delay, str(e)[:200],
                )
                time.sleep(delay)
                continue

            except APIError as e:
                last_err = e
                logger.error("Error API en intento %s/%s: %s", attempt + 1, max_retries, str(e)[:300])
                if e.status_code and e.status_code >= 500:
                    time.sleep(delays[min(attempt, len(delays) - 1)])
                    continue
                raise

        if last_err is not None:
            raise last_err
        raise RuntimeError("IA no generó texto tras todos los reintentos.")

    def _safe_json_parse(self, raw_text: str) -> Dict:
        """Intenta parsear JSON, limpiando si es necesario."""
        # Intento directo
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            pass

        # Limpiar bloques markdown ```json ... ```
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Extraer primer bloque JSON {...}
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"No se pudo parsear JSON de la respuesta IA. Texto (truncado): {raw_text[:500]}")

    def smoke_test(self) -> bool:
        """Test mínimo para verificar conectividad con el proveedor."""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": 'Respondé exactamente "OK" y nada más.'}],
                max_tokens=10,
            )
            out = (response.choices[0].message.content or "").strip()
            logger.info("Smoke test OK | provider=%s | model=%s | respuesta=%r", self.provider, self.model, out)
            return True
        except Exception as e:
            logger.error("Smoke test FALLÓ | provider=%s | model=%s | error=%s", self.provider, self.model, e)
            raise
