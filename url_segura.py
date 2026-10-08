"""
Chequeo de URLs que el scraper va a abrir con un navegador.

Las fuentes de noticias agregadas a mano desde /admin (scraper_config.fuentes_custom) se abren con
Playwright desde el servidor: una URL que apunte a localhost, a la red interna o a la metadata de la
nube (169.254.169.254) haría que el scraper consulte recursos privados. Solo se aceptan URLs http(s)
cuyo host resuelva ÚNICAMENTE a direcciones públicas.

Limitación conocida: es un chequeo previo a la conexión. No cubre redirecciones a destinos internos
ni DNS rebinding (el host resolviendo distinto entre el chequeo y la conexión real).
Función pura salvo por la resolución DNS, que se puede inyectar para probar.
"""

import ipaddress
import socket
from typing import Callable, List, Optional
from urllib.parse import urlparse


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
