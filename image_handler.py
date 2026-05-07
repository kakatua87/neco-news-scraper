"""
Módulo para descargar imágenes de URLs externas y subirlas a Supabase Storage.
"""

import logging
from typing import Optional
import requests

import config

logger = logging.getLogger("neconews.image_handler")


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
            headers = {"User-Agent": "Mozilla/5.0 (compatible; NecoNews/1.0)"}
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

            path = f"noticias/{noticia_id}.{extension}"

            self.supabase.storage.from_("noticias-imagenes").upload(
                path=path,
                file=response.content,
                file_options={"content-type": content_type, "upsert": "true"}
            )

            public_url = f"{config.SUPABASE_URL}/storage/v1/object/public/noticias-imagenes/{path}"
            return public_url

        except Exception as e:
            logger.error("Error procesando imagen %s: %s", image_url, e)
            return None
