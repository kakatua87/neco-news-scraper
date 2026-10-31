"""
Módulo para descargar imágenes de URLs externas y subirlas a Supabase Storage.
"""

import hashlib
import io
import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
from PIL import Image


import config

logger = logging.getLogger("neconews.image_handler")


# Las portadas de los medios muestran miniaturas; la foto original está en la misma carpeta o en el CDN sin la
# transformación. Estas reglas arman la URL de mayor resolución (se prueba primero y, si falla, se usa la original).
_RE_CDN_REDIMENSIONADO = re.compile(r"^https?://cdnartic\.ar/.*?/filters:[^/]*/(https?://.+)$", re.IGNORECASE)
_RE_SUFIJO_WP = re.compile(r"-\d{2,4}x\d{2,4}(\.(?:jpe?g|png|webp))$", re.IGNORECASE)
_RE_SUFIJO_OG = re.compile(r"-OG(\.(?:jpe?g|png|webp))$", re.IGNORECASE)


def urls_alta_resolucion(url: str) -> List[str]:
    """
    Variantes de mayor resolución de la misma foto, de la mejor a la peor, sin incluir la URL original:
    - cdnartic.ar (El Ecos): `.../768x0/filters:...quality(60)/https://origen/foto.webp` → la foto de origen (1600 px).
    - WordPress: `foto-750x375.jpg` → `foto.jpg` (solo bajo /wp-content/uploads/).
    - `foto-OG.jpg` (recorte 1200×630 para redes) → `foto.jpg`, la foto completa sin recortar.
    """
    candidatas: List[str] = []
    actual = url
    m = _RE_CDN_REDIMENSIONADO.match(actual)
    if m:
        actual = m.group(1)
        candidatas.append(actual)
    if "/wp-content/uploads/" in actual:
        sin_og = _RE_SUFIJO_OG.sub(r"\1", actual)
        sin_tam = _RE_SUFIJO_WP.sub(r"\1", sin_og)
        for c in (sin_tam, sin_og):
            if c != url and c not in candidatas:
                candidatas.append(c)
    return [c for c in candidatas if c != url]


def con_logo_del_medio(url: Optional[str]) -> bool:
    """True si la imagen viene de un dominio cuyas fotos llevan el logo del medio estampado."""
    try:
        dominio = urlparse(url or "").netloc.lower().removeprefix("www.")
    except Exception:
        return False
    return bool(dominio) and any(
        dominio == d or dominio.endswith("." + d) for d in config.IMAGEN_DOMINIOS_CON_LOGO
    )


def recortar_franja_inferior(datos: bytes) -> Optional[bytes]:
    """
    Quita la franja blanca inferior donde algunos medios (TSN Necochea) estampan su logo.
    Solo corta si, de abajo hacia arriba, hay una banda casi toda blanca que mide entre 3 % y 25 % del
    alto (se toleran hasta 3 filas de borde en el extremo). Devuelve el JPEG recortado o None si la
    imagen no tiene ese formato: el control de imagen revisa igual el resultado.
    """
    try:
        with Image.open(io.BytesIO(datos)) as img:
            img = img.convert("RGB")
            ancho, alto = img.size
            px = img.load()
            muestra = range(0, ancho, 2)

            def fila_blanca(y: int) -> bool:
                blancos = sum(1 for x in muestra if min(px[x, y]) >= 235)
                # El logo ocupa parte del ancho de la franja: 75 % de blanco alcanza para contarla.
                return blancos / len(muestra) >= 0.75

            y = alto - 1
            borde = 0
            while y >= 0 and borde < 3 and not fila_blanca(y):
                y -= 1
                borde += 1
            if y < 0 or not fila_blanca(y):
                return None
            fin_banda = y
            while y >= 0 and fila_blanca(y):
                y -= 1
            alto_banda = fin_banda - y
            if alto_banda < max(8, int(alto * 0.03)) or alto_banda > int(alto * 0.25):
                return None
            corte = max(1, y - 2)  # margen para la línea de borde entre la foto y la franja
            if corte < alto * 0.6:
                return None
            salida = io.BytesIO()
            img.crop((0, 0, ancho, corte)).save(salida, format="JPEG", quality=90, optimize=True)
            return salida.getvalue()
    except Exception:
        logger.exception("Error recortando la franja inferior")
        return None


def recortar_por_caja(datos: bytes, caja: dict) -> Optional[bytes]:
    """
    Quita un logo que está sobre un margen (inferior, superior, derecho o izquierdo, incluidas las esquinas)
    cortando la foto por el lado que menos superficie pierde. `caja` viene en milésimas (x0,y0,x1,y1).
    Devuelve None si el logo no está en un margen (centrado) o si el recorte dejaría menos del 65 % de la
    foto: en esos casos la nota va a revisión como antes.
    """
    try:
        with Image.open(io.BytesIO(datos)) as img:
            img = img.convert("RGB")
            ancho, alto = img.size
            x0, y0, x1, y1 = (caja[k] / 1000 for k in ("x0", "y0", "x1", "y1"))
            m = 0.015  # margen extra para no dejar el borde del logo
            # (caja de recorte en fracciones, fracción de superficie que queda)
            opciones = []
            if y0 >= 0.70:
                opciones.append(((0, 0, 1, max(0.0, y0 - m)), max(0.0, y0 - m)))
            if y1 <= 0.30:
                opciones.append(((0, min(1.0, y1 + m), 1, 1), 1 - min(1.0, y1 + m)))
            if x0 >= 0.70:
                opciones.append(((0, 0, max(0.0, x0 - m), 1), max(0.0, x0 - m)))
            if x1 <= 0.30:
                opciones.append(((min(1.0, x1 + m), 0, 1, 1), 1 - min(1.0, x1 + m)))
            if not opciones:
                return None
            fr, resto = max(opciones, key=lambda o: o[1])
            if resto < 0.65:
                return None
            salida = io.BytesIO()
            img.crop((int(fr[0] * ancho), int(fr[1] * alto), int(fr[2] * ancho), int(fr[3] * alto))).save(
                salida, format="JPEG", quality=90, optimize=True
            )
            return salida.getvalue()
    except Exception:
        logger.exception("Error recortando el logo por caja")
        return None


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
                img.save(salida, format="WEBP", quality=95)
            else:
                # "keep" (conserva el submuestreo de color del original) solo se permite sobre el JPEG sin convertir.
                rgb = img if img.mode == "RGB" else img.convert("RGB")
                rgb.save(salida, format="JPEG", quality=95, subsampling="keep" if rgb.format == "JPEG" else 0, optimize=True)
            return salida.getvalue()
    except Exception:
        logger.exception("Error re-codificando imagen")
        return None


class ImageHandler:
    def __init__(self, supabase_client) -> None:
        self.supabase = supabase_client.client

    def _preparar(self, image_url: str) -> Optional[Tuple[bytes, str, str]]:
        """
        Descarga la imagen (probando antes su versión de mayor resolución, si el medio la sirve) y le quita los
        metadatos. Devuelve (contenido, content_type, extensión) o None si no se pudo.
        """
        if not image_url or not image_url.startswith("http"):
            return None
        for candidata in urls_alta_resolucion(image_url):
            try:
                preparada = self._preparar_una(candidata)
            except Exception:
                preparada = None
            if preparada is not None:
                logger.info("Imagen en mayor resolución: %s (en vez de %s)", candidata, image_url)
                return preparada
        return self._preparar_una(image_url)

    def _preparar_una(self, image_url: str) -> Optional[Tuple[bytes, str, str]]:
        """Descarga una URL concreta y le quita los metadatos."""

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

    def subir_recorte_logo(self, image_url: str, noticia_id: str) -> Optional[str]:
        """
        Descarga una imagen (la de Storage de la nota), le recorta la franja inferior con el logo del
        medio y la sube como `noticias/<id>-recorte.jpg`. Devuelve la URL pública o None.
        """
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(image_url, timeout=10, headers=headers)
            if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                return None
            recortada = recortar_franja_inferior(r.content)
            if recortada is None:
                # Sin franja blanca: se ubica el logo con visión y se corta el margen donde esté (esquina o borde).
                import image_gate

                caja = image_gate.localizar_logo(r.content)
                recortada = recortar_por_caja(r.content, caja) if caja else None
            if recortada is None:
                return None
            return self._subir(f"noticias/{noticia_id}-recorte.jpg", recortada, "image/jpeg")
        except Exception as e:
            logger.error("Error subiendo el recorte de %s: %s", image_url, e)
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
