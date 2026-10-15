"""
Fact gate de la edición automática: decide si una nota redactada por la IA
puede salir sin revisión humana, contrastándola con los textos de las fuentes.

Dos capas:
1. Determinista (gratis): cifras y nombres propios de la nota que NO aparecen en
   ninguna fuente. Atrapa números inventados y nombres alucinados.
2. LLM verificador: cifras/nombres/contradicciones que la capa 1 no ve
   (atribuciones cambiadas, causas inventadas, hechos mezclados).

Cualquier duda (error del modelo, sin fuentes) => NO es automática.
"""

import json
import logging
import re
import time
import unicodedata
from typing import Dict, List, Optional

from openai import OpenAI

import config

logger = logging.getLogger("neconews.fact_gate")

# Palabras capitalizadas que no necesitan estar en la fuente.
_NOMBRES_OK = {
    "necochea", "quequen", "neco", "argentina", "buenos", "aires", "enero", "febrero", "marzo", "abril",
    "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre", "lunes",
    "martes", "miercoles", "jueves", "viernes", "sabado", "domingo", "ruta", "calle", "avenida",
    "municipalidad", "municipio", "policia", "segun", "ademas", "tambien", "ante", "sin", "tras",
}
# La cantidad de fuentes NO bloquea la automatización: una nota de fuente única se publica igual
# si el verificador la respalda (decisión editorial).
PALABRAS_SENSIBLES = (
    "menor de edad", "menores", "adolescente", "niña", "niño", "abuso", "violación", "violacion",
    "suicidio", "femicidio", "homicidio", "imputado", "detenido", "sospechoso", "acusado",
)
MAX_FUENTE_CHARS = 3500


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _numeros(texto: str) -> set:
    """Cifras normalizadas sin separadores ('1.200' -> '1200'); ignora dígitos sueltos."""
    out = set()
    for m in re.findall(r"\d+(?:[.,]\d+)*", texto or ""):
        limpio = re.sub(r"[.,]", "", m)
        if len(limpio) >= 2:
            out.add(limpio)
    return out


def _nombres(texto: str) -> set:
    """Palabras capitalizadas que no abren oración."""
    out = set()
    for oracion in re.split(r"(?<=[.!?:\n])\s+|\n+", texto or ""):
        palabras = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", oracion)
        for p in palabras[1:]:
            if p[0].isupper() and len(p) > 3:
                n = _norm(p)
                if n not in _NOMBRES_OK:
                    out.add(n)
    return out


def chequeo_determinista(titulo: str, cuerpo: str, fuentes: List[str]) -> Dict:
    nota = f"{titulo}\n{cuerpo}"
    base = "\n".join(fuentes)
    base_norm = _norm(base)
    nums_fuente = _numeros(base)
    cifras = sorted(n for n in _numeros(nota) if n not in nums_fuente)
    nombres = sorted(n for n in _nombres(nota) if n not in base_norm)
    return {"cifras_no_respaldadas": cifras, "nombres_no_respaldados": nombres}


def _hay_sensible(*textos: str) -> Optional[str]:
    t = _norm(" ".join(textos))
    for p in PALABRAS_SENSIBLES:
        if re.search(r"\b" + re.escape(_norm(p)) + r"\b", t):
            return p
    return None


_PROMPT = (
    "Sos un verificador de hechos de un diario. Recibís una NOTA redactada y los TEXTOS DE LAS FUENTES de "
    "las que salió. Tu trabajo: detectar información de la nota que las fuentes NO respaldan.\n"
    "Buscá: cifras, fechas, lugares, nombres, cargos, causas, atribuciones de dichos y hechos que no estén "
    "en las fuentes, y contradicciones entre la nota y las fuentes (o entre fuentes). Reformular está bien; "
    "agregar o cambiar datos no.\n"
    'Respondé SOLO JSON: {"ok": bool, "hallazgos": [{"tipo": "cifra|nombre|atribucion|hecho|contradiccion", '
    '"texto": "fragmento de la nota", "detalle": "por qué no está respaldado"}]}\n'
    "ok=true solo si hallazgos está vacío. No inventes hallazgos."
)


def _llm_json(mensajes: List[Dict]) -> Optional[Dict]:
    keys = config.GEMINI_API_KEYS
    if not keys:
        return None
    base = config.AI_MODEL if config.AI_PROVIDER == "gemini" else config._PROVIDER_DEFAULTS["gemini"]["model"]
    modelos = [base] + [m for m in config.AI_FALLBACK_MODELS if m != base]
    base_url = config._PROVIDER_DEFAULTS["gemini"]["base_url"]
    for key in keys[:2]:
        client = OpenAI(api_key=key, base_url=base_url, timeout=40, max_retries=0)
        for modelo in modelos:
            for _ in range(2):
                try:
                    r = client.chat.completions.create(
                        model=modelo, messages=mensajes, temperature=0, max_tokens=900,
                        response_format={"type": "json_object"},
                    )
                    parsed = json.loads(r.choices[0].message.content or "{}")
                    if isinstance(parsed, dict) and "ok" in parsed:
                        return parsed
                    break
                except Exception as e:
                    logger.warning("Verificador falló (%s): %s", modelo, str(e)[:150])
                    if "503" in str(e) or "429" in str(e):
                        time.sleep(4)
                        continue
                    break
    return None


def verificar_con_llm(titulo: str, cuerpo: str, fuentes: List[str]) -> Optional[Dict]:
    bloques = "\n\n".join(
        f"--- FUENTE {i + 1} ---\n{f[:MAX_FUENTE_CHARS]}" for i, f in enumerate(fuentes)
    )
    contenido = f"{_PROMPT}\n\n=== NOTA ===\nTítulo: {titulo}\n\n{cuerpo}\n\n=== FUENTES ===\n{bloques}"
    return _llm_json([{"role": "user", "content": contenido}])


def evaluar_nota(titulo: str, cuerpo: str, fuentes: List[str], seccion: str = "", n_fuentes: int = 1) -> Dict:
    """
    Devuelve {"automatica": bool, "motivos": [str], "determinista": {...}, "llm": {...}|None}.
    `automatica` es True solo si TODAS las salvaguardas pasan.
    """
    motivos: List[str] = []
    fuentes = [f for f in fuentes if f and f.strip()]
    if not fuentes:
        return {"automatica": False, "motivos": ["sin texto de fuente para contrastar"],
                "determinista": None, "llm": None}

    sens = _hay_sensible(titulo, cuerpo, *fuentes)
    if sens:
        motivos.append(f"tema sensible ('{sens}'): revisión humana")

    det = chequeo_determinista(titulo, cuerpo, fuentes)
    if det["cifras_no_respaldadas"]:
        motivos.append("cifras sin respaldo: " + ", ".join(det["cifras_no_respaldadas"][:5]))
    if len(det["nombres_no_respaldados"]) >= 2:
        motivos.append("nombres sin respaldo: " + ", ".join(det["nombres_no_respaldados"][:5]))

    llm = verificar_con_llm(titulo, cuerpo, fuentes)
    if llm is None:
        motivos.append("verificador no disponible")
    elif not llm.get("ok") or llm.get("hallazgos"):
        h = llm.get("hallazgos") or []
        resumen = "; ".join(f"{x.get('tipo', '?')}: {str(x.get('texto', ''))[:60]}" for x in h[:3])
        motivos.append("verificador: " + (resumen or "no respaldada"))

    return {"automatica": not motivos, "motivos": motivos, "determinista": det, "llm": llm}
