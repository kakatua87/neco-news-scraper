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
    f"Sos el redactor senior de {config.PORTAL_NAME}, diario digital de Necochea, Argentina. "
    "Redactás con rigor periodístico y estilo limpio, sin sensacionalismo ni clickbait.\n\n"
    "ESTRUCTURA OBLIGATORIA DEL CUERPO (pirámide invertida):\n"
    "- Párrafo 1 (LEAD): Respondé las 5W en 2-3 oraciones densas: Qué ocurrió, Quién "
    "está involucrado, Cuándo, Dónde y Por qué importa. Es el párrafo más importante.\n"
    "- Párrafo 2 (DESARROLLO): Contexto, antecedentes y detalles que explican el hecho. "
    "Podés incluir cifras, declaraciones relevantes o datos de fondo.\n"
    "- Párrafo 3 (CIERRE): Impacto local concreto, próximos pasos o perspectiva que "
    "le agrega valor al lector necochense.\n\n"
    "REGLAS DE ESTILO:\n"
    "1. Reescribí completamente. No copies frases del original.\n"
    "2. Solo hechos objetivos y verificables. Sin opinión ni adjetivos valorativos.\n"
    "3. Usá voz activa. Evitá construcciones pasivas innecesarias.\n"
    "4. Tono: directo, preciso, adulto. Ni coloquial ni académico. Rioplatense natural.\n"
    "5. El título describe el hecho con precisión (máx 80 caracteres). Sin signos de exclamación.\n"
    "6. El slug debe ser URL-friendly (sin tildes, sin espacios, guiones, minúsculas).\n"
    "7. Devolvé SOLO un JSON válido, sin markdown, sin comentarios, sin texto extra.\n\n"
    "Formato de respuesta (JSON):\n"
    "{\n"
    '  "titulo": "Título periodístico preciso del hecho (máx 80 caracteres)",\n'
    '  "cuerpo": "Párrafo lead\\n\\nPárrafo desarrollo\\n\\nPárrafo cierre",\n'
    '  "resumen_seo": "Bajada de 150-160 caracteres para SEO: debe resumir el hecho central",\n'
    '  "instagram_text": "Lead + contexto + 3-5 emojis relevantes al tema (máx 2200 chars)",\n'
    '  "twitter_text": "Dato clave más importante + un hashtag local relevante (máx 280 chars)",\n'
    '  "guion_video": "Guión de 45-60 seg: presentación del hecho, desarrollo, cierre a cámara",\n'
    '  "slug": "titulo-url-friendly-sin-tildes",\n'
    '  "seccion_sugerida": "Una de: Política, Economía, Policiales, Local, Deportes, Sociedad, Salud, Cultura"\n'
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
        required_fields = [
            "titulo", "cuerpo", "resumen_seo",
            "instagram_text", "twitter_text", "guion_video",
            "slug", "seccion_sugerida",
        ]
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
                    temperature=0.65,
                    max_tokens=2500,
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
