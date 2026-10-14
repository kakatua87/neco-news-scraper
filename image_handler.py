"""
Módulo para descargar imágenes de URLs externas y subirlas a Supabase Storage.
"""

import io
import logging
from typing import Optional

import requests
from PIL import Image


import config

logger = logging.getLogger("neconews.image_handler")


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

    def upload_image(self, image_url: str, noticia_id: str) -> Optional[str]:
        """
        Descarga una imagen de una URL externa y la sube a Supabase Storage.
        Retorna la URL pública en Supabase o None si falla.
        """
        if not image_url or not image_url.startswith("http"):
            return None

        try:
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

            path = f"noticias/{noticia_id}.{extension}"

            self.supabase.storage.from_("noticias-imagenes").upload(
                path=path,
                file=contenido,
                file_options={"content-type": content_type, "upsert": "true"}
            )

            public_url = f"{config.SUPABASE_URL}/storage/v1/object/public/noticias-imagenes/{path}"
            return public_url

        except Exception as e:
            logger.error("Error procesando imagen %s: %s", image_url, e)
            return None
