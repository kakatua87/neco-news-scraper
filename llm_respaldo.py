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

import uso_ia

logger = logging.getLogger("neconews.llm_respaldo")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODELO_TEXTO = os.getenv("GROQ_MODEL_TEXTO", "openai/gpt-oss-120b")
GROQ_MODELO_VISION = os.getenv("GROQ_MODEL_VISION", "qwen/qwen3.8-27b")
CLAUDE_MODELO = os.getenv("ANTHROPIC_VERIFICADOR_MODEL", "claude-haiku-5-5")
PRESUPUESTO_S = 90


def _restante_groq(cabeceras) -> dict:
    """Lo que Groq informa en cada respuesta: solicitudes restantes del día, tokens restantes del minuto y reinicios."""
    try:
        return {
            "solicitudes_dia": cabeceras.get("x-ratelimit-remaining-requests"),
            "limite_solicitudes_dia": cabeceras.get("x-ratelimit-limit-requests"),
            "tokens_minuto": cabeceras.get("x-ratelimit-remaining-tokens"),
            "limite_tokens_minuto": cabeceras.get("x-ratelimit-limit-tokens"),
            "reinicio_solicitudes": cabeceras.get("x-ratelimit-reset-requests"),
            "reinicio_tokens": cabeceras.get("x-ratelimit-reset-tokens"),
        }
    except Exception:
        return {}


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
    # Plan gratis: 8.000 tokens por minuto. El tope de salida cuenta en el cálculo, así que se acota.
    max_tokens = min(max_tokens, 1500)
    client = OpenAI(api_key=key, base_url=GROQ_BASE_URL, timeout=40, max_retries=0)
    for _ in range(2):
        try:
            bruto = client.chat.completions.with_raw_response.create(
                model=modelo, messages=mensajes, temperature=0, max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            r = bruto.parse()
            uso_ia.registrar("groq", key, modelo, getattr(r, "usage", None), True, 200, restante=_restante_groq(bruto.headers),
                             proposito_default="respaldo",
                             chars_in=sum(len(str(m.get("content", ""))) for m in mensajes),
                             chars_out=len(r.choices[0].message.content or ""))
            parsed = _extraer_json(r.choices[0].message.content or "")
            if parsed is not None and clave in parsed:
                logger.warning("Respaldo Groq (%s) respondió", modelo)
                return parsed
            return None
        except Exception as e:
            uso_ia.registrar("groq", key, modelo, ok=False, codigo=uso_ia.codigo_de_error(e), proposito_default="respaldo")
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
            cuerpo = r.json()
            uso_ia.registrar("claude", key, CLAUDE_MODELO, cuerpo.get("usage"), True, 200, proposito_default="respaldo")
            texto = "".join(b.get("text", "") for b in cuerpo.get("content", []) if b.get("type") == "text")
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


GROQ_MODELO_REDACCION = os.getenv("GROQ_MODEL_REDACCION", "openai/gpt-oss-120b")


def completar_texto(mensajes: List[Dict], max_tokens: int = 3500) -> Optional[str]:
    """
    Último recurso para la redacción cuando Gemini no responde: devuelve el texto (JSON) del primer respaldo
    que conteste, o None. Groq (gratis, límite 8.000 tokens/min: puede rechazar pedidos largos) y después Claude.
    Los prompts no se tocan: se manda el mismo system + user que se le mandaba a Gemini.
    """
    key = os.getenv("GROQ_API_KEY", "").strip()
    if key:
        try:
            client = OpenAI(api_key=key, base_url=GROQ_BASE_URL, timeout=60, max_retries=0)
            bruto = client.chat.completions.with_raw_response.create(
                model=GROQ_MODELO_REDACCION, messages=mensajes, temperature=0.65, max_tokens=max_tokens,
                response_format={"type": "json_object"}, extra_body={"reasoning_effort": "low"},
            )
            r = bruto.parse()
            uso_ia.registrar("groq", key, GROQ_MODELO_REDACCION, getattr(r, "usage", None), True, 200,
                             restante=_restante_groq(bruto.headers), proposito_default="redaccion",
                             chars_in=sum(len(str(m.get("content", ""))) for m in mensajes),
                             chars_out=len(r.choices[0].message.content or ""))
            texto = (r.choices[0].message.content or "").strip()
            if texto:
                logger.warning("Redacción con el respaldo Groq (%s)", GROQ_MODELO_REDACCION)
                return texto
        except Exception as e:
            uso_ia.registrar("groq", key, GROQ_MODELO_REDACCION, ok=False, codigo=uso_ia.codigo_de_error(e),
                             proposito_default="redaccion")
            logger.warning("Respaldo Groq de redacción falló: %s", str(e)[:200])

    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if key:
        try:
            system = "\n\n".join(m["content"] for m in mensajes if m["role"] == "system" and isinstance(m["content"], str))
            msgs = [{"role": m["role"], "content": _a_bloques_claude(m["content"])} for m in mensajes if m["role"] != "system"]
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": os.getenv("ANTHROPIC_REDACCION_MODEL", "claude-sonnet-5-5"), "max_tokens": 8192,
                      "temperature": 0.65, "system": system, "messages": msgs},
                timeout=120,
            )
            if r.status_code == 200:
                cuerpo = r.json()
                uso_ia.registrar("claude", key, os.getenv("ANTHROPIC_REDACCION_MODEL", "claude-sonnet-5-5"),
                                 cuerpo.get("usage"), True, 200, proposito_default="redaccion")
                texto = "".join(b.get("text", "") for b in cuerpo.get("content", []) if b.get("type") == "text").strip()
                if texto:
                    logger.warning("Redacción con el respaldo Claude")
                    return texto
            else:
                logger.warning("Respaldo Claude de redacción falló: %s %s", r.status_code, r.text[:150])
        except Exception as e:
            logger.warning("Respaldo Claude de redacción falló: %s", str(e)[:200])
    return None
