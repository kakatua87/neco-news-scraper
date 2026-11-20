"""
Chequeo de URLs que el scraper va a abrir con un navegador.

Las fuentes de noticias agregadas a mano desde /admin (scraper_config.fuentes_custom) se abren con
Playwright desde el servidor: una URL que apunte a localhost, a la red interna o a la metadata de la
nube (169.254.169.254) haría que el scraper consulte recursos privados. Solo se aceptan URLs http(s)
cuyo host resuelva ÚNICAMENTE a direcciones públicas.

`descargar_publica` aplica el chequeo a cada salto de una redirección y limita el tamaño de lo descargado.

Limitación conocida: es un chequeo previo a la conexión. No cubre DNS rebinding (el host resolviendo distinto
entre el chequeo y la conexión real).
Función pura salvo por la resolución DNS, que se puede inyectar para probar.
"""

import ipaddress
import socket
from typing import Callable, List, Optional
from urllib.parse import urljoin, urlparse


def _es_global(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True solo para direcciones públicas: no privadas, loopback, link-local, CGNAT, reservadas ni multicast."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _es_global(ip.ipv4_mapped)
    return ip.is_global and not ip.is_multicast


def _resolver(host: str, puerto: int) -> List[str]:
    return [info[4][0].split("%")[0] for info in socket.getaddrinfo(host, puerto, proto=socket.IPPROTO_TCP)]


def url_es_publica(url: str, resolver: Optional[Callable[[str, int], List[str]]] = None) -> bool:
    """True si `url` es http(s), no trae usuario/clave y su host resuelve solo a IPs públicas."""
    try:
        partes = urlparse(url)
        host = partes.hostname
        puerto = partes.port or (443 if partes.scheme == "https" else 80)
    except ValueError:
        return False
    if partes.scheme not in ("http", "https") or not host or partes.username or partes.password:
        return False

    try:
        direcciones = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            direcciones = [ipaddress.ip_address(a) for a in (resolver or _resolver)(host, puerto)]
        except (OSError, ValueError):
            return False  # no resuelve: no se abre

    return bool(direcciones) and all(_es_global(d) for d in direcciones)


# Tope de lo que se baja de una URL ajena: una foto de noticia pesa unos cientos de KB. Sin tope, una URL maliciosa
# (o un sitio roto) que devuelva varios GB agotaría la memoria del servicio (512 MB en el plan gratis de Render).
MAX_BYTES_DESCARGA = 15 * 1024 * 1024
_REDIRECCIONES = (301, 302, 303, 307, 308)


def descargar_publica(
    url: str,
    *,
    timeout: int = 10,
    headers: Optional[dict] = None,
    max_redirecciones: int = 3,
    max_bytes: int = MAX_BYTES_DESCARGA,
    resolver: Optional[Callable[[str, int], List[str]]] = None,
    get: Optional[Callable] = None,
):
    """
    Descarga `url` solo si es pública (ver url_es_publica), siguiendo hasta `max_redirecciones` redirecciones
    chequeando cada destino, y cortando si supera `max_bytes`. Devuelve la respuesta de `requests` con el
    contenido ya leído (`.status_code`, `.headers`, `.content`), o None si la URL no es pública, redirige a un
    destino no público, hay demasiadas redirecciones, es demasiado grande o la conexión falla.
    """
    import requests  # import local: el resto del módulo no necesita red

    pedir = get or requests.get
    actual = url
    try:
        for _ in range(max_redirecciones + 1):
            if not url_es_publica(actual, resolver):
                return None
            r = pedir(actual, timeout=timeout, headers=headers, allow_redirects=False, stream=True)
            if r.status_code in _REDIRECCIONES:
                destino = r.headers.get("Location")
                r.close()
                if not destino:
                    return None
                actual = urljoin(actual, destino)
                continue
            declarado = r.headers.get("Content-Length", "")
            if declarado.isdigit() and int(declarado) > max_bytes:
                r.close()
                return None
            total = 0
            partes = []
            for trozo in r.iter_content(65536):
                total += len(trozo)
                if total > max_bytes:
                    r.close()
                    return None
                partes.append(trozo)
            r._content = b"".join(partes)
            r._content_consumed = True
            return r
    except requests.RequestException:
        return None
    return None  # demasiadas redirecciones
