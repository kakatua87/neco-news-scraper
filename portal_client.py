"""
Llamadas del scraper al portal (Vercel) con el secreto interno.

`requests` descarta la cabecera Authorization cuando una redirección cambia de host. El portal
redirige con 308 `www.necobeat.com.ar` y `neco-news-seven.vercel.app` al dominio canónico, así que
con un PORTAL_URL apuntando a cualquiera de esos dos, la petición llegaba sin credencial y el portal
respondía 401 (así fallaron las 3 publicaciones automáticas del 18/10). Acá las redirecciones se
siguen a mano, reenviando la credencial SOLO si el destino es necobeat.com.ar o un subdominio.
"""

import logging
from typing import Dict, Tuple
from urllib.parse import urljoin, urlparse

import requests

import config

logger = logging.getLogger("neconews.portal")

MAX_SALTOS = 3
_REDIRECCIONES = (301, 302, 303, 307, 308)


def _host_permitido(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "necobeat.com.ar" or host.endswith(".necobeat.com.ar")


def post_portal(path: str, json_body: Dict, timeout: int = 30) -> Tuple[requests.Response, str]:
    """
    POST a `{PORTAL_URL}{path}` con `Authorization: Bearer <INTERNAL_API_SECRET>`.
    Devuelve (respuesta final, URL final). Lanza excepción si la red falla, si hay demasiadas
    redirecciones o si una redirección apunta a un host que no es del portal.
    """
    url = f"{config.PORTAL_URL.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
    for _ in range(MAX_SALTOS + 1):
        r = requests.post(url, json=json_body, headers=headers, timeout=timeout, allow_redirects=False)
        destino = r.headers.get("Location")
        if r.status_code in _REDIRECCIONES and destino:
            destino = urljoin(url, destino)
            if not _host_permitido(destino):
                raise RuntimeError(
                    f"el portal redirigió a un host no permitido ({urlparse(destino).hostname}); no se envía la credencial"
                )
            logger.warning("El portal redirigió %s → %s (revisar PORTAL_URL)", url, destino)
            url = destino
            continue
        return r, url
    raise RuntimeError("demasiadas redirecciones del portal")
