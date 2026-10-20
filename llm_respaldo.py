"""
Proveedores de respaldo para los controles (verificador de hechos y de imágenes) cuando Gemini no responde.

Orden: Groq (gratis, API compatible con OpenAI) y después Claude (de pago). Cada uno se activa solo si
su clave está en el entorno (GROQ_API_KEY, ANTHROPIC_API_KEY); sin clave se omite.
"""

import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

import requests
from openai import OpenAI

logger = logging.getLogger("neconews.llm_respaldo")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODELO_TEXTO = os.getenv("GROQ_MODEL_TEXTO", "openai/gpt-oss-120b")
GROQ_MODELO_VISION = os.getenv("GROQ_MODEL_VISION", "qwen/qwen3.8-27b")
CLAUDE_MODELO = os.getenv("ANTHROPIC_VERIFICADOR_MODEL", "claude-haiku-5-5")
PRESUPUESTO_S = 90


def _extraer_json(texto: str) -> Optional[Dict]:
    m = re.search(r"\{.*\}", texto or "", re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _con_groq(mensajes: List[Dict], clave: str, max_tokens: int, vision: bool) -> Optional[Dict]:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    modelo = GROQ_MODELO_VISION if vision else GROQ_MODELO_TEXTO
    client = OpenAI(api_key=key, base_url=GROQ_BASE_URL, timeout=40, max_retries=0)
    for _ in range(2):
        try:
            r = client.chat.completions.create(
                model=modelo, messages=mensajes, temperature=0, max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            parsed = _extraer_json(r.choices[0].message.content or "")
            if parsed is not None and clave in parsed:
                logger.warning("Respaldo Groq (%s) respondió", modelo)
                return parsed
            return None
        except Exception as e:
            logger.warning("Respaldo Groq falló (%s): %s", modelo, str(e)[:150])
            if "429" in str(e):
                return None  # sin cupo: pasar a Claude
            if any(c in str(e) for c in ("500", "502", "503")):
                time.sleep(3)
                continue
            return None
    return None


def _a_bloques_claude(contenido) -> list:
    """Convierte el contenido estilo OpenAI (texto o texto + data URL de imagen) a bloques de Anthropic."""
    if isinstance(contenido, str):
        return [{"type": "text", "text": contenido}]
    bloques = []
    for parte in contenido:
        if parte.get("type") == "text":
            bloques.append({"type": "text", "text": parte.get("text", "")})
        elif parte.get("type") == "image_url":
            url = (parte.get("image_url") or {}).get("url", "")
            m = re.match(r"data:(image/[\w.+-]+);base64,(.+)", url, re.DOTALL)
            if m:
                bloques.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
    return bloques


def _con_claude(mensajes: List[Dict], clave: str, max_tokens: int) -> Optional[Dict]:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    msgs = [{"role": m["role"], "content": _a_bloques_claude(m["content"])} for m in mensajes]
    for _ in range(2):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": CLAUDE_MODELO, "max_tokens": max_tokens, "temperature": 0, "messages": msgs},
                timeout=60,
            )
            if r.status_code != 200:
                logger.warning("Respaldo Claude falló: %s %s", r.status_code, r.text[:150])
                if r.status_code in (429, 500, 502, 503, 529):
                    time.sleep(3)
                    continue
                return None
            texto = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            parsed = _extraer_json(texto)
            if parsed is not None and clave in parsed:
                logger.warning("Respaldo Claude (%s) respondió", CLAUDE_MODELO)
                return parsed
            return None
        except Exception as e:
            logger.warning("Respaldo Claude falló: %s", str(e)[:150])
    return None


def completar_json(mensajes: List[Dict], clave: str, max_tokens: int = 4096, vision: bool = False) -> Optional[Dict]:
    """Prueba Groq y luego Claude. Devuelve el JSON (que debe incluir `clave`) o None si ninguno respondió."""
    return _con_groq(mensajes, clave, max_tokens, vision) or _con_claude(mensajes, clave, max_tokens)
