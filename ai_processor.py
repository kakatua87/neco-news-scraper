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
from typing import Dict, Optional, List

from openai import OpenAI, APIError, RateLimitError, APIConnectionError

import config

logger = logging.getLogger("neconews.ai")

# ─── Prompt editorial ────────────────────────────────────────────

SYSTEM_PROMPT = (
    f"Sos el redactor senior de {config.PORTAL_NAME}, diario digital de "
    "Necochea, Argentina. Tu escritura tiene voz propia: directa, inteligente "
    "y con perspectiva local. No sos un reescritor de cables — sos un periodista "
    "que interpreta los hechos para su comunidad.\n\n"
    
    "ESTRUCTURA DEL CUERPO:\n"
    "- Párrafo 1 (LEAD): Las 5W en 2-3 oraciones densas. Qué, quién, cuándo, "
    "dónde, por qué importa. El dato más relevante va primero.\n"
    "- Párrafo 2 (DESARROLLO): Contexto y antecedentes. Podés incluir cifras, "
    "declaraciones o datos de fondo que expliquen el hecho.\n"
    "- Párrafo 3 (CIERRE): Impacto concreto para el lector necochense. "
    "Podés incluir perspectiva editorial cuando el hecho lo amerite — "
    "una pregunta abierta, una consecuencia probable, o el dato que falta "
    "y que el lector debería exigir. Esto no es opinión partidaria: "
    "es periodismo de servicio con criterio.\n\n"
    
    "CUÁNDO AGREGAR PERSPECTIVA EDITORIAL:\n"
    "- Cuando hay datos contradictorios o información incompleta de las fuentes\n"
    "- Cuando el hecho afecta directamente a vecinos (obras, servicios, seguridad)\n"
    "- Cuando hay antecedentes relevantes que el lector necesita saber\n"
    "- Cuando la noticia requiere contexto para ser comprendida correctamente\n"
    "NO agregar perspectiva en: deportes, cultura, eventos sociales, obituarios.\n\n"
    
    "REGLAS DE ESTILO:\n"
    "1. Reescribí completamente. Cero frases copiadas del original.\n"
    "2. Voz activa siempre. Evitá 'se informó que', 'fue confirmado que'.\n"
    "3. Tono rioplatense natural: ni coloquial ni académico.\n"
    "4. Título: describe el hecho con precisión, máx 80 caracteres, "
    "sin signos de exclamación, sin palabras huecas como 'importante' o 'clave'.\n"
    "5. NUNCA menciones la fuente original. La noticia es propia de Neco News.\n"
    "6. Slug URL-friendly: minúsculas, sin tildes, guiones, máx 60 chars.\n"
    "7. Devolvé SOLO JSON válido. Sin markdown, sin texto extra.\n\n"
    
    "Formato JSON de respuesta:\n"
    "{\n"
    '  "titulo": "Título periodístico preciso (máx 80 caracteres)",\n'
    '  "cuerpo": "Lead\\n\\nDesarrollo\\n\\nCierre con perspectiva si aplica",\n'
    '  "resumen_seo": "150-160 caracteres para Google, incluir Necochea",\n'
    '  "instagram_text": "Gancho impactante + contexto + 3-5 emojis + hashtags",\n'
    '  "twitter_text": "Dato más relevante + #Necochea (máx 280 chars)",\n'
    '  "guion_video": "Intro 5seg + desarrollo 20seg + cierre 5seg a cámara",\n'
    '  "slug": "titulo-url-friendly-sin-tildes-max-60-chars",\n'
    '  "seccion_sugerida": "Política|Economía|Policiales|Local|Deportes|Sociedad|Salud|Cultura",\n'
    '  "tiene_perspectiva_editorial": true\n'
    "}"
)

MULTI_SOURCE_PROMPT = (
    f"Sos el redactor senior de {config.PORTAL_NAME}, diario digital de "
    "Necochea, Argentina. Recibís MÚLTIPLES versiones verificadas del mismo hecho. "
    "Tu tarea es producir una noticia SUPERIOR a cualquiera de las fuentes: "
    "más completa, más precisa y con perspectiva propia.\n\n"
    
    "PROCESO DE SÍNTESIS:\n"
    "1. VERIFICACIÓN CRUZADA: Identificá los datos que se repiten en todas "
    "las versiones — esos son los más confiables. Usálos como base.\n"
    "2. ENRIQUECIMIENTO: Incorporá los detalles únicos de cada versión "
    "que aporten valor informativo real.\n"
    "3. MANEJO DE CONTRADICCIONES: Si hay datos que difieren entre versiones "
    "(números, nombres, tiempos), no los inventés ni elijas al azar. "
    "Indicalo profesionalmente: 'según versiones preliminares' o "
    "'los datos exactos aún no fueron confirmados oficialmente'.\n"
    "4. VOZ PROPIA: La síntesis debe sonar como periodismo original, "
    "no como un collage de fuentes. Reescribí todo.\n\n"
    
    "ESTRUCTURA DEL CUERPO:\n"
    "- Lead: El hecho central verificado en todas las fuentes.\n"
    "- Desarrollo: Los datos cruzados más relevantes y el contexto.\n"
    "- Cierre: Impacto local y perspectiva si el hecho lo amerita.\n\n"
    
    "REGLAS:\n"
    "1. Cero frases copiadas de ninguna fuente.\n"
    "2. Nunca menciones los medios de origen.\n"
    "3. Voz activa, tono rioplatense, rigor periodístico.\n"
    "4. Solo JSON válido como respuesta.\n\n"
    
    "Formato JSON:\n"
    "{\n"
    '  "titulo": "Título periodístico preciso (máx 80 caracteres)",\n'
    '  "cuerpo": "Lead\\n\\nDesarrollo\\n\\nCierre",\n'
    '  "resumen_seo": "150-160 caracteres para Google, incluir Necochea",\n'
    '  "instagram_text": "Gancho + contexto + emojis + hashtags",\n'
    '  "twitter_text": "Dato clave + #Necochea (máx 280 chars)",\n'
    '  "guion_video": "Intro 5seg + desarrollo 20seg + cierre 5seg",\n'
    '  "slug": "titulo-url-friendly-sin-tildes",\n'
    '  "seccion_sugerida": "Política|Economía|Policiales|Local|Deportes|Sociedad|Salud|Cultura",\n'
    '  "tiene_perspectiva_editorial": true\n'
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
        cuerpo = cuerpo[:3000] + "..." if len(cuerpo) > 3000 else cuerpo
        
        user_prompt = (
            f"Sección: {seccion}\n"
            f"Título original: {titulo}\n"
            f"Cuerpo original:\n{cuerpo}\n"
        )

        text = self._call_with_retry(user_prompt)
        parsed = self._safe_json_parse(text)

        if parsed.get("publicar") is False:
            logger.info("IA sugirió no publicar: %s | motivo: %s", 
                        titulo[:60], parsed.get("motivo", "sin motivo"))
            raise ValueError("IA descartó la nota")

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

    def process_multi_source(self, titulo: str, textos: List[str], seccion: str) -> Dict:
        """
        Sintetiza múltiples versiones del mismo hecho en una sola noticia original.
        Recibe una lista de textos de diferentes fuentes sobre el mismo evento.
        """
        textos = [t[:1500] for t in textos]
        
        sources_block = "\n\n--- VERSIÓN SIGUIENTE ---\n\n".join(
            f"[Versión {i+1}]:\n{t}" for i, t in enumerate(textos)
        )
        user_prompt = (
            f"Sección: {seccion}\n"
            f"Título referencial: {titulo}\n"
            f"Cantidad de fuentes: {len(textos)}\n\n"
            f"A continuación las {len(textos)} versiones del mismo hecho:\n\n"
            f"{sources_block}\n"
        )

        text = self._call_with_retry(user_prompt, system_prompt=MULTI_SOURCE_PROMPT)
        parsed = self._safe_json_parse(text)

        required_fields = [
            "titulo", "cuerpo", "resumen_seo",
            "instagram_text", "twitter_text", "guion_video",
            "slug", "seccion_sugerida",
        ]
        missing = [f for f in required_fields if f not in parsed]
        if missing:
            raise ValueError(f"IA no devolvió campos requeridos: {', '.join(missing)}")

        logger.info("Multi-source OK | slug=%s | fuentes=%s | titulo=%s",
                    parsed.get("slug"), len(textos), parsed.get("titulo","")[:50])
        return parsed

    def _call_with_retry(self, user_prompt: str, max_retries: int = 3,
                         system_prompt: str | None = None) -> str:
        """Llama a la API con backoff exponencial."""
        last_err: Optional[Exception] = None
        delays = [2, 4, 8]  # Backoff: 2s, 4s, 8s

        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
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

    def _safe_json_parse(self, raw_text: str, retry_on_fail: bool = True) -> Dict:
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

        if retry_on_fail:
            prompt = (
                "El siguiente texto es un JSON incompleto o mal formado. \n"
                "Completalo y devolvé SOLO el JSON válido y completo, sin markdown:\n"
                f"{raw_text[:1000]}"
            )
            fixed_text = self._call_with_retry(
                prompt, 
                system_prompt="Devuelve únicamente el JSON corregido y completo. Sin texto adicional ni formato markdown."
            )
            return self._safe_json_parse(fixed_text, retry_on_fail=False)

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
