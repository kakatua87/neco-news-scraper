"""
Módulo para descargar imágenes de URLs externas y subirlas a Supabase Storage.
"""

import hashlib
import io
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests
from PIL import Image


import config

logger = logging.getLogger("neconews.image_handler")


def con_logo_del_medio(url: Optional[str]) -> bool:
    """True si la imagen viene de un dominio cuyas fotos llevan el logo del medio estampado."""
    try:
        dominio = urlparse(url or "").netloc.lower().removeprefix("www.")
    except Exception:
        return False
    return bool(dominio) and any(
        dominio == d or dominio.endswith("." + d) for d in config.IMAGEN_DOMINIOS_CON_LOGO
    )


def _sin_metadatos(datos: bytes, content_type: str) -> Optional[bytes]:
    """Re-codifica la imagen para quitar EXIF/IPTC/XMP (créditos, autor, software, GPS) que
    pueden delatar el origen. Los GIF se dejan tal cual para no perder la animación.
    Devuelve None si no se pudo procesar: es preferible no publicar la imagen a publicarla con
    sus metadatos originales."""
    if content_type == "image/gif":
        return datos
    try:
        with Image.open(io.BytesIO(datos)) as img:
            img.load()
            salida = io.BytesIO()
            if content_type == "image/png":
                img.save(salida, format="PNG", optimize=True)
            elif content_type == "image/webp":
                img.save(salida, format="WEBP", quality=90)
            else:
                img.convert("RGB").save(salida, format="JPEG", quality=90, optimize=True)
            return salida.getvalue()
    except Exception:
        logger.exception("Error re-codificando imagen")
        return None


class ImageHandler:
    def __init__(self, supabase_client) -> None:
        self.supabase = supabase_client.client

    def _preparar(self, image_url: str) -> Optional[Tuple[bytes, str, str]]:
        """
        Descarga la imagen y le quita los metadatos.
        Devuelve (contenido, content_type, extensión) o None si no se pudo.
        """
        if not image_url or not image_url.startswith("http"):
            return None

        # User-Agent genérico: el anterior ("NecoNews/1.0") identificaba al portal por nombre
        # en los logs del sitio de origen.
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        }
        response = requests.get(image_url, timeout=10, headers=headers)

        if response.status_code != 200:
            logger.warning("Fallo al descargar imagen. Status: %s | URL: %s", response.status_code, image_url)
            return None

        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            logger.warning("El content-type no es imagen: %s | URL: %s", content_type, image_url)
            return None

        if len(response.content) > 5_000_000:
            logger.warning("Imagen muy grande: %s bytes | URL: %s", len(response.content), image_url)
            return None

        if content_type == "image/jpeg":
            extension = "jpg"
        elif content_type == "image/png":
            extension = "png"
        elif content_type == "image/webp":
            extension = "webp"
        elif content_type == "image/gif":
            extension = "gif"
        else:
            extension = "jpg"

        contenido = _sin_metadatos(response.content, content_type)
        if contenido is None:
            logger.warning("No se pudo limpiar los metadatos; imagen descartada | URL: %s", image_url)
            return None
        return contenido, content_type, extension

    def _subir(self, path: str, contenido: bytes, content_type: str) -> str:
        self.supabase.storage.from_("noticias-imagenes").upload(
            path=path,
            file=contenido,
            file_options={"content-type": content_type, "upsert": "true"}
        )
        return f"{config.SUPABASE_URL}/storage/v1/object/public/noticias-imagenes/{path}"

    def upload_image(self, image_url: str, noticia_id: str) -> Optional[str]:
        """
        Descarga una imagen de una URL externa y la sube a Supabase Storage.
        Retorna la URL pública en Supabase o None si falla.
        """
        try:
            preparada = self._preparar(image_url)
            if preparada is None:
                return None
            contenido, content_type, extension = preparada
            return self._subir(f"noticias/{noticia_id}.{extension}", contenido, content_type)
        except Exception as e:
            logger.error("Error procesando imagen %s: %s", image_url, e)
            return None

    def upload_alternativa(self, image_url: str, noticia_id: str, indice: int) -> Optional[Tuple[str, str]]:
        """
        Sube una de las fotos de las fuentes de una nota para que el editor pueda elegirla desde
        Pendientes. Devuelve (url pública, sha1 del contenido limpio) o None; el hash permite
        descartar fotos idénticas que traen dos fuentes con distinta URL.
        """
        try:
            preparada = self._preparar(image_url)
            if preparada is None:
                return None
            contenido, content_type, extension = preparada
            url = self._subir(f"noticias/{noticia_id}-fuente{indice}.{extension}", contenido, content_type)
            return url, hashlib.sha1(contenido).hexdigest()
        except Exception as e:
            logger.error("Error subiendo imagen alternativa %s: %s", image_url, e)
            return None
