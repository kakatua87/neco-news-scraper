"""
Fecha de publicación de una nota scrapeada (día argentino), para quedarse solo con las del día.

Cada medio la expone distinto: JSON-LD / meta `article:published_time` (la mayoría), la fecha visible junto al título
(Necochea Digital: su JSON-LD trae la hora en que se renderizó la página, no la de publicación), un <time> (Municipal),
`.jeg_meta_date` (Diario NQ) o el propio permalink (`/2026/06/01/…`). Se juntan las señales que haya y, si discrepan,
manda la más vieja: es preferible descartar una nota dudosa que publicar una vieja que contradiga a las actuales.
"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

ART = timezone(timedelta(hours=-3))

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_MES_RE = "|".join(_MESES)


def hoy_ar() -> date:
    return datetime.now(timezone.utc).astimezone(ART).date()


def fecha_desde_url(url: str) -> Optional[date]:
    """Permalinks tipo https://diarionecochea.com/2026/06/01/titulo/."""
    m = re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/", url or "")
    return _armar(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _armar(anio: int, mes: int, dia: int) -> Optional[date]:
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def _parse_iso(valor: str) -> Optional[date]:
    """'2026-10-24T00:12:16-03:00', '…-0300', '…Z' o sin zona (se toma como hora argentina) → día argentino."""
    v = (valor or "").strip()
    if not re.match(r"20\d{2}-\d{2}-\d{2}", v):
        return None
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return _armar(int(v[:4]), int(v[5:7]), int(v[8:10]))
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(ART).date()


_FORMATOS_TEXTO = (
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b"), lambda m: _armar(int(m[3]), int(m[2]), int(m[1]))),
    (re.compile(rf"\b(\d{{1,2}})\s*(?:de\s+)?({_MES_RE})\s*,?\s*(?:de\s+)?(20\d{{2}})\b", re.I),
     lambda m: _armar(int(m[3]), _MESES[m[2].lower()], int(m[1]))),
    (re.compile(rf"\b({_MES_RE})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b", re.I),
     lambda m: _armar(int(m[3]), _MESES[m[1].lower()], int(m[2]))),
)


def _parse_texto(texto: str) -> Optional[date]:
    for patron, a_fecha in _FORMATOS_TEXTO:
        m = patron.search(texto or "")
        if m:
            f = a_fecha(m)
            if f:
                return f
    return None


def _de_metadatos(soup: BeautifulSoup, html: str = "") -> Optional[date]:
    for attrs in ({"property": "article:published_time"}, {"property": "og:article:published_time"},
                  {"itemprop": "datePublished"}, {"name": "date"}, {"name": "pubdate"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            f = _parse_iso(tag["content"])
            if f:
                return f

    def buscar(nodo) -> Optional[str]:
        if isinstance(nodo, dict):
            if isinstance(nodo.get("datePublished"), str):
                return nodo["datePublished"]
            for v in nodo.values():
                r = buscar(v)
                if r:
                    return r
        elif isinstance(nodo, list):
            for v in nodo:
                r = buscar(v)
                if r:
                    return r
        return None

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            valor = buscar(json.loads(script.string or script.get_text() or ""))
        except (ValueError, TypeError):
            continue
        f = _parse_iso(valor or "")
        if f:
            return f
    # JSON-LD mal formado (2262): se lo busca como texto.
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html or "")
    return _parse_iso(m.group(1)) if m else None


def _visible(soup: BeautifulSoup) -> List[date]:
    """Fechas que el propio sitio muestra para la nota: junto al título y en los bloques de fecha habituales."""
    halladas: List[date] = []
    h1 = soup.find("h1")
    if h1:
        # Texto que sigue al título (autor, fecha) — ventana corta para no tomar fechas de widgets o del cuerpo.
        resto = ""
        for nodo in h1.next_elements:
            if nodo is h1:
                continue
            if isinstance(nodo, str):
                resto += " " + nodo.strip()
            if len(resto) > 220:
                break
        f = _parse_texto(resto[:220])
        if f:
            halladas.append(f)
    for sel in (".jeg_meta_date", ".entry-date", ".post-date", ".fecha", "time"):
        for el in soup.select(sel)[:1]:
            f = _parse_iso(el.get("datetime", "")) if el.name == "time" and el.get("datetime") else None
            f = f or _parse_texto(el.get_text(" ", strip=True))
            if f:
                halladas.append(f)
    return halladas


def _solo_hora_tras_titulo(soup: BeautifulSoup) -> bool:
    """Necochea Digital muestra solo la hora («08:33») junto al título de las notas de hoy y la fecha completa en las demás."""
    h1 = soup.find("h1")
    if not h1:
        return False
    for texto in h1.find_all_next(string=True):
        if h1 in texto.parents or not texto.strip():
            continue
        return bool(re.match(r"\s*([01]?\d|2[0-3]):[0-5]\d", texto))
    return False


# Medios cuyo JSON-LD/meta no sirve (informa la hora de renderizado, no la de publicación).
_SIN_METADATOS = ("necocheadigital.com",)


def extraer_fecha(html: str, url: str = "") -> Optional[date]:
    """Día argentino de publicación, o None si la página no lo dice."""
    soup = BeautifulSoup(html or "", "html.parser")
    candidatas: List[date] = []
    if not any(d in (url or "") for d in _SIN_METADATOS):
        f = _de_metadatos(soup, html)
        if f:
            candidatas.append(f)
    candidatas += _visible(soup)
    if not candidatas and "necocheadigital.com" in (url or "") and _solo_hora_tras_titulo(soup):
        candidatas.append(hoy_ar())
    f = fecha_desde_url(url)
    if f:
        candidatas.append(f)
    # Fechas futuras son ruido (p. ej. «sesión del 30/10/2026» en el texto): se ignoran.
    candidatas = [c for c in candidatas if c <= hoy_ar() + timedelta(days=1)]
    return min(candidatas) if candidatas else None
