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
    "- LEAD (siempre primero, sin subtítulo): las 5W en 2-3 oraciones densas. "
    "Qué, quién, cuándo, dónde, por qué importa. El dato más relevante va primero.\n"
    "- DESARROLLO: contexto, antecedentes, cifras, declaraciones. NO comprimas de "
    "más — el original suele traer más datos de los que entran en un resumen "
    "mínimo (nombres, cifras exactas, declaraciones textuales, antecedentes, "
    "cronología del hecho); retenelos todos los que sean relevantes en vez de "
    "quedarte con lo justo y necesario. El objetivo no es un resumen, es una "
    "nota completa. Si la nota tiene tela para cortar (2 o más temas distintos "
    "dentro del desarrollo, o fuentes múltiples con datos de distinto tipo), "
    "dividilo en 2 a 4 bloques temáticos, cada uno con un subtítulo corto y "
    "concreto (nunca genérico tipo 'Contexto' o 'Más detalles' — describí de "
    "qué habla ese bloque puntual, como haría un diario real). Marcá cada "
    "subtítulo con '## ' al inicio de su propia línea, seguido de un párrafo "
    "en blanco y después el/los párrafo/s de ese bloque. Para una nota corta o "
    "simple (resultado deportivo, agenda, aviso breve) alcanza con el lead y "
    "uno o dos párrafos sueltos, sin forzar subtítulos ni estirar contenido "
    "que el original no tiene.\n"
    "- CIERRE (último bloque, con o sin subtítulo según corresponda): impacto "
    "concreto para el lector necochense. Podés incluir perspectiva editorial "
    "cuando el hecho lo amerite — una pregunta abierta, una consecuencia "
    "probable, o el dato que falta y que el lector debería exigir. Esto no es "
    "opinión partidaria: es periodismo de servicio con criterio.\n\n"

    "CUÁNDO AGREGAR PERSPECTIVA EDITORIAL:\n"
    "- Cuando hay datos contradictorios o información incompleta de las fuentes\n"
    "- Cuando el hecho afecta directamente a vecinos (obras, servicios, seguridad)\n"
    "- Cuando hay antecedentes relevantes que el lector necesita saber\n"
    "- Cuando la noticia requiere contexto para ser comprendida correctamente\n"
    "NO agregar perspectiva en: deportes, cultura, eventos sociales, obituarios.\n\n"

    "COHERENCIA Y RIGOR (no negociable):\n"
    "- Todo dato, cifra, cita o nombre del cuerpo tiene que estar presente en el "
    "original. Si un dato no aparece ahí, no lo pongas — ni lo redondees, ni lo "
    "estimes, ni lo completes 'a ojo'.\n"
    "- No inventes vínculos causales entre hechos que la fuente no conecta "
    "explícitamente ('por eso', 'como consecuencia', 'esto llevó a') salvo que "
    "el original lo afirme.\n"
    "- El cuerpo entero (con o sin subtítulos) tiene que leerse como una sola "
    "idea que avanza, no como datos sueltos pegados uno atrás del otro: cada "
    "bloque retoma algo del anterior y lo desarrolla (una cifra, una "
    "consecuencia, un actor involucrado), en vez de arrancar un tema nuevo de "
    "la nada. Los subtítulos organizan la lectura, no son una excusa para "
    "cortar el hilo narrativo.\n"
    "- Si falta un dato para que la historia cierre (quién, cuándo, cuánto), "
    "decilo explícitamente ('todavía no se confirmó...', 'no trascendió...') "
    "en lugar de omitirlo en silencio o inventarlo para que 'cierre mejor'.\n"
    "- Todo subtítulo ('## ') va SIEMPRE seguido de al menos un párrafo de "
    "desarrollo debajo. Nunca termines el cuerpo en un subtítulo sin texto, y "
    "nunca metas una oración larga adentro de un subtítulo — el subtítulo es "
    "un título corto (4-8 palabras), no una frase del cuerpo.\n\n"

    "REGLAS DE ESTILO:\n"
    "1. Reescribí completamente. Cero frases copiadas del original.\n"
    "2. Voz activa siempre. Evitá 'se informó que', 'fue confirmado que'.\n"
    "3. Tono rioplatense natural: ni coloquial ni académico.\n"
    "4. Título: describe el hecho con precisión, máx 80 caracteres, "
    "sin signos de exclamación, sin palabras huecas como 'importante' o 'clave'.\n"
    "5. Si te indico la fuente original, podés incluir UNA mención breve y natural "
    "en el cuerpo (ej. 'según informó {fuente}', 'de acuerdo a lo publicado por "
    "{fuente}'), como máximo una vez. Si no te doy fuente, no la inventes.\n"
    "6. Priorizá datos concretos (cifras, nombres, lugares, fechas) por sobre "
    "frases genéricas de relleno ('un hecho relevante', 'la situación preocupa'). "
    "Variá la construcción de las oraciones — evitá que dos párrafos seguidos "
    "arranquen con la misma estructura sintáctica.\n"
    "7. Slug URL-friendly: minúsculas, sin tildes, guiones, máx 60 chars.\n"
    "8. Devolvé SOLO JSON válido. Sin markdown, sin texto extra (la ÚNICA excepción "
    "es el campo 'cuerpo', que sí puede contener líneas '## Subtítulo' como se "
    "explicó arriba — eso no es markdown libre, es el único marcador permitido).\n\n"

    "Formato JSON de respuesta:\n"
    "{\n"
    '  "titulo": "Título periodístico preciso (máx 80 caracteres)",\n'
    '  "cuerpo": "Lead\\n\\n## Subtítulo del primer bloque\\n\\nDesarrollo...\\n\\n## Subtítulo del cierre\\n\\nCierre con perspectiva si aplica",\n'
    '  "resumen_seo": "150-160 caracteres para Google, incluir Necochea",\n'
    '  "instagram_text": "Gancho impactante + contexto + 3-5 emojis + hashtags",\n'
    '  "instagram_titulo": "Hook corto y atrapante para Instagram (máx 60 caracteres, sin hashtags)",\n'
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

    "COHERENCIA ENTRE FUENTES (no negociable):\n"
    "- Antes de combinar dos datos de versiones distintas, confirmá que "
    "hablan del mismo hecho, persona o cifra. Si dos versiones no "
    "necesariamente se refieren a lo mismo, no las fusiones — dejá el dato "
    "más confiable (el que se repite) y descartá el ambiguo.\n"
    "- No completes huecos de una versión con suposiciones tomadas de otra "
    "si esa otra versión no lo dice explícitamente sobre el mismo punto.\n"
    "- El resultado tiene que leerse como UNA sola nota con hilo narrativo "
    "propio — lead que plantea el hecho, desarrollo que lo explica con los "
    "datos cruzados, cierre que conecta con el impacto local — y no como "
    "una lista de datos de cada fuente pegados en orden.\n"
    "- Si las versiones se contradicen en un dato central y no podés "
    "resolverlo con el resto del texto, señalalo con una frase explícita en "
    "vez de elegir una versión al azar o mezclarlas en un promedio inventado.\n"
    "- Todo subtítulo ('## ') va siempre seguido de al menos un párrafo de "
    "desarrollo debajo; nunca termines el cuerpo en un subtítulo sin texto, y "
    "el subtítulo tiene que ser corto (4-8 palabras), no una oración del cuerpo.\n\n"

    "ESTRUCTURA DEL CUERPO:\n"
    "- Lead (siempre primero, sin subtítulo): el hecho central verificado en "
    "todas las fuentes.\n"
    "- Desarrollo: los datos cruzados más relevantes y el contexto. No lo "
    "reduzcas a lo mínimo indispensable — combinar varias fuentes te da más "
    "material del habitual (cifras, nombres, declaraciones, antecedentes de "
    "cada versión), aprovechalo en vez de comprimir. Al combinar varias "
    "fuentes suele haber más de un ángulo (el hecho en sí, reacciones, "
    "antecedentes, cifras) — cuando eso pasa, dividí el desarrollo en 2 a 4 "
    "bloques con subtítulo propio y concreto (no genérico), marcado con "
    "'## ' al inicio de línea seguido de línea en blanco y el párrafo. Si las "
    "fuentes solo aportan un ángulo único y acotado, un par de párrafos sin "
    "subtítulos alcanza — no los fuerces.\n"
    "- Cierre: impacto local y perspectiva si el hecho lo amerita.\n\n"

    "REGLAS:\n"
    "1. Cero frases copiadas de ninguna fuente.\n"
    "2. Si te indico las fuentes, podés atribuir de forma genérica ('medios locales "
    "informaron', 'distintos medios de Necochea publicaron') o citar puntualmente un "
    "medio si aporta un dato exclusivo suyo — máximo una mención en todo el cuerpo. "
    "Si no te doy fuentes, no las inventes.\n"
    "3. Voz activa, tono rioplatense, rigor periodístico.\n"
    "4. Priorizá datos concretos por sobre relleno genérico. Variá la sintaxis "
    "entre párrafos — no repitas la misma estructura de oración dos veces seguidas.\n"
    "5. Solo JSON válido como respuesta (excepto las líneas '## Subtítulo' dentro "
    "de 'cuerpo', que son el único marcador permitido).\n\n"

    "Formato JSON:\n"
    "{\n"
    '  "titulo": "Título periodístico preciso (máx 80 caracteres)",\n'
    '  "cuerpo": "Lead\\n\\n## Subtítulo del desarrollo\\n\\nDesarrollo...\\n\\nCierre",\n'
    '  "resumen_seo": "150-160 caracteres para Google, incluir Necochea",\n'
    '  "instagram_text": "Gancho + contexto + emojis + hashtags",\n'
    '  "instagram_titulo": "Hook corto y atrapante para Instagram (máx 60 caracteres, sin hashtags)",\n'
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

    def __init__(self, provider: Optional[str] = None) -> None:
        """
        Si se pasa `provider`, se usa la config de ESE proveedor (API key,
        modelo, base_url) en vez del AI_PROVIDER por defecto — siempre que
        tenga API key configurada. Si no se pasa nada, comportamiento
        idéntico al de siempre (proveedor default de config.py).
        """
        if provider:
            cfg = config.get_provider_config(provider)
            if not cfg:
                raise ValueError(f"Proveedor de IA '{provider}' no está configurado (falta su API key).")
            self.provider = cfg["provider"]
            self.model = cfg["model"]
            api_key = cfg["api_key"]
            base_url = cfg["base_url"]
        else:
            if not config.AI_API_KEY:
                raise ValueError("AI_API_KEY es obligatoria. Configurala en .env")
            self.provider = config.AI_PROVIDER
            self.model = config.AI_MODEL
            api_key = config.AI_API_KEY
            base_url = config.AI_BASE_URL

        self.client = OpenAI(api_key=api_key, base_url=base_url)

        key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) >= 12 else "***"
        logger.info(
            "AI inicializado | provider=%s | model=%s | key=%s",
            self.provider, self.model, key_preview,
        )

    def process_article(self, titulo: str, cuerpo: str, seccion: str, fuente: Optional[str] = None) -> Dict:
        """
        Reescribe una noticia usando IA.
        Retorna dict con: titulo, cuerpo, resumen_seo, instagram_text, instagram_titulo,
        twitter_text, guion_video, slug.
        Lanza excepción si falla tras todos los reintentos.
        """
        cuerpo = cuerpo[:4500] + "..." if len(cuerpo) > 4500 else cuerpo

        user_prompt = (
            f"Sección: {seccion}\n"
            f"Título original: {titulo}\n"
            + (f"Fuente original (para atribución opcional, no la copies textualmente): {fuente}\n" if fuente else "")
            + f"Cuerpo original:\n{cuerpo}\n"
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
            "instagram_text", "instagram_titulo", "twitter_text", "guion_video",
            "slug", "seccion_sugerida",
        ]
        missing = [f for f in required_fields if f not in parsed]
        if missing:
            raise ValueError(f"IA no devolvió campos requeridos: {', '.join(missing)}")

        logger.info("Artículo procesado OK | provider=%s | slug=%s", self.provider, parsed.get("slug"))
        return parsed

    def process_multi_source(self, titulo: str, textos: List[str], seccion: str, fuentes: Optional[List[str]] = None) -> Dict:
        """
        Sintetiza múltiples versiones del mismo hecho en una sola noticia original.
        Recibe una lista de textos de diferentes fuentes sobre el mismo evento.
        """
        textos = [t[:2200] for t in textos]

        sources_block = "\n\n--- VERSIÓN SIGUIENTE ---\n\n".join(
            f"[Versión {i+1}]:\n{t}" for i, t in enumerate(textos)
        )
        fuentes_unicas = sorted({f for f in (fuentes or []) if f})
        user_prompt = (
            f"Sección: {seccion}\n"
            f"Título referencial: {titulo}\n"
            f"Cantidad de fuentes: {len(textos)}\n"
            + (f"Medios de origen (para atribución opcional, no los copies textualmente): {', '.join(fuentes_unicas)}\n" if fuentes_unicas else "")
            + f"\nA continuación las {len(textos)} versiones del mismo hecho:\n\n"
            f"{sources_block}\n"
        )

        text = self._call_with_retry(user_prompt, system_prompt=MULTI_SOURCE_PROMPT)
        parsed = self._safe_json_parse(text)

        required_fields = [
            "titulo", "cuerpo", "resumen_seo",
            "instagram_text", "instagram_titulo", "twitter_text", "guion_video",
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
                    max_tokens=3200,
                    # Fuerza salida JSON válida a nivel de decoding — sin esto,
                    # el modelo a veces mete saltos de línea sin escapar
                    # dentro de un string (p. ej. alrededor de los "## "
                    # subtítulos) y rompe el parseo.
                    response_format={"type": "json_object"},
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
