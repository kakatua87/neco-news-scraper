"""
Image gate de la edición automática: detecta marcas de agua / logos del medio de
origen en una foto con un modelo de visión (Gemini por el endpoint compatible
con OpenAI) y decide si se puede usar.

Una respuesta desconocida (sin key, error de red, JSON roto) NUNCA cuenta como
"imagen limpia": la nota queda para revisión humana.
"""

import base64
import io
import json
import logging
import time
from typing import Dict, List, Optional

import requests
from openai import OpenAI
from PIL import Image

import config
from url_segura import url_es_publica

logger = logging.getLogger("neconews.image_gate")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_MAX_BYTES = 5_000_000
_LADO_MAX = 1024

_PROMPT = (
    "Sos un editor gráfico de un diario. Mirá la imagen y respondé SOLO un JSON con estas claves:\n"
    '{"marca_agua": bool, "logo_medio": bool, "texto_superpuesto": bool, '
    '"nombre_medio": string, "calidad": "buena"|"media"|"baja", "motivo": string}\n'
    "- marca_agua: hay una marca de agua visible (semitransparente o superpuesta) o crédito de agencia/fotógrafo "
    "estampado sobre la foto.\n"
    "- logo_medio: se ve el logo o nombre de un medio de comunicación (esquina, banner, pie).\n"
    "- texto_superpuesto: hay titulares, zócalos o texto gráfico que no es parte de la escena "
    "(carteles de la calle NO cuentan).\n"
    "- nombre_medio: nombre del medio si se lee, sino \"\".\n"
    "- calidad: baja si está borrosa, muy pixelada o es un recorte ilegible.\n"
    "- motivo: una frase corta."
)


def _bytes_a_jpeg_b64(datos: bytes) -> Optional[str]:
    try:
        with Image.open(io.BytesIO(datos)) as img:
            img = img.convert("RGB")
            img.thumbnail((_LADO_MAX, _LADO_MAX))
            salida = io.BytesIO()
            img.save(salida, format="JPEG", quality=85)
            return base64.b64encode(salida.getvalue()).decode()
    except Exception:
        logger.exception("No se pudo preparar la imagen para visión")
        return None


def _descargar(url: str) -> Optional[bytes]:
    """Descarga siguiendo hasta 3 redirecciones, validando cada salto contra SSRF."""
    try:
        for _ in range(4):
            if not url_es_publica(url):
                logger.warning("URL no pública rechazada: %s", url[:100])
                return None
            r = requests.get(url, timeout=10, headers={"User-Agent": _UA}, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = requests.compat.urljoin(url, r.headers["Location"])
                continue
            if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                return None
            if len(r.content) > _MAX_BYTES:
                return None
            return r.content
        return None
    except Exception:
        logger.warning("No se pudo descargar la imagen %s", url[:100])
        return None


def _modelos() -> List[str]:
    base = config.AI_MODEL if config.AI_PROVIDER == "gemini" else config._PROVIDER_DEFAULTS["gemini"]["model"]
    return [base] + [m for m in config.AI_FALLBACK_MODELS if m != base]


def interpretar(respuesta: Dict) -> Dict:
    """Aplica la regla de decisión a la respuesta del modelo."""
    marca = bool(respuesta.get("marca_agua"))
    logo = bool(respuesta.get("logo_medio"))
    texto = bool(respuesta.get("texto_superpuesto"))
    calidad = str(respuesta.get("calidad", "")).lower()
    limpia = not (marca or logo or texto) and calidad != "baja"
    return {
        "limpia": limpia,
        "marca_agua": marca,
        "logo_medio": logo,
        "texto_superpuesto": texto,
        "nombre_medio": str(respuesta.get("nombre_medio", ""))[:80],
        "calidad": calidad,
        "motivo": str(respuesta.get("motivo", ""))[:200],
    }


def diagnosticar_imagen(url: str) -> Dict:
    """Como `analizar_imagen`, pero devuelve {"resultado": ..., "error": motivo} para depurar."""
    if not url or not url.startswith("http"):
        return {"resultado": None, "error": "url invalida"}
    keys = config.gemini_keys_ordenadas()
    if not keys:
        return {"resultado": None, "error": "sin GEMINI_API_KEY configurada (AI_PROVIDER=%s)" % config.AI_PROVIDER}
    datos = _descargar(url)
    if not datos:
        return {"resultado": None, "error": "no se pudo descargar (bloqueo, no es imagen, >5MB o URL no publica)"}
    b64 = _bytes_a_jpeg_b64(datos)
    if not b64:
        return {"resultado": None, "error": "no se pudo decodificar la imagen"}

    mensajes = [{
        "role": "user",
        "content": [
            {"type": "text", "text": _PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ],
    }]
    base_url = config._PROVIDER_DEFAULTS["gemini"]["base_url"]
    ultimo_error = "sin respuesta"
    for key in keys:
        key_agotada = False
        client = OpenAI(api_key=key, base_url=base_url, timeout=25, max_retries=0)
        for modelo in _modelos():
            for intento in range(2):  # 503 "high demand" suele ser transitorio
                try:
                    resp = client.chat.completions.create(
                        model=modelo, messages=mensajes, temperature=0, max_tokens=300,
                        response_format={"type": "json_object"},
                    )
                    parsed = json.loads(resp.choices[0].message.content or "{}")
                    if isinstance(parsed, dict) and "marca_agua" in parsed:
                        return {"resultado": interpretar(parsed), "error": None}
                    ultimo_error = "respuesta sin campo marca_agua"
                    break
                except Exception as e:
                    ultimo_error = "%s: %s" % (modelo, str(e)[:200])
                    logger.warning("Visión falló (modelo=%s, intento=%s): %s", modelo, intento + 1, str(e)[:150])
                    if "429" in str(e):
                        config.gemini_key_agotada(key)
                        key_agotada = True
                        break
                    if "503" in str(e):
                        time.sleep(4)
                        continue
                    break
            if key_agotada:
                break
    return {"resultado": None, "error": ultimo_error}


def analizar_imagen(url: str) -> Optional[Dict]:
    """Devuelve la decisión (ver `interpretar`) o None si no se pudo analizar."""
    return diagnosticar_imagen(url)["resultado"]


def elegir_imagen_limpia(candidatas: List[str]) -> Dict:
    """
    Prueba las candidatas en orden y devuelve la primera limpia.
    {"url": str|None, "indice": int|None, "detalle": [..por candidata..]}
    """
    vistas = set()
    detalle = []
    for i, url in enumerate(candidatas):
        if not url or url in vistas:
            continue
        vistas.add(url)
        res = analizar_imagen(url)
        detalle.append({"url": url, "resultado": res})
        if res and res["limpia"]:
            return {"url": url, "indice": i, "detalle": detalle}
    return {"url": None, "indice": None, "detalle": detalle}
