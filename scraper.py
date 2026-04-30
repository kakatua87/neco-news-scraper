"""
Scraper de noticias para portales de Necochea.

Fuentes soportadas:
  - nden.com.ar
  - diarionecochea.com

Usa Playwright headless + BeautifulSoup para extracción robusta.
Selectores específicos por dominio para máxima calidad de contenido.
"""

import logging
import re
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logger = logging.getLogger("neconews.scraper")

# ─── Selectores específicos por dominio ─────────────────────────────────────
# Cada entrada: (selector_contenido, selector_fallback)
DOMAIN_CONTENT_SELECTORS: Dict[str, List[str]] = {
    "nden.com.ar": [
        "div.td-post-content",
        "div.entry-content",
        "div.post-content",
        "article.post .td-post-content",
        "div.tdb-block-inner",
        "article",
    ],
    "diarionecochea.com": [
        "div.nota-cuerpo",
        "div.article-body",
        "div.content-article",
        "div.single-content",
        "div.entry-content",
        "div.post-content",
        "article",
    ],
}


class NewsScraper:
    def __init__(self, existing_urls: Optional[Set[str]] = None) -> None:
        self.existing_urls = existing_urls or set()

    def scrape_nden(self) -> List[Dict]:
        """Scrapea la homepage de NDEN."""
        base_url = "https://nden.com.ar"
        logger.info("Scrapeando homepage NDEN...")
        return self._scrape_homepage(
            base_url=base_url,
            card_selector="article, .td_module_flex, .td-animation-stack, .post",
            title_selector="h3.entry-title a, h3.td-module-title a, h2, h3",
            link_selector="h3.entry-title a, h3.td-module-title a, a",
            image_selector="img.entry-thumb, img.td-image-wrap, img",
            section_selector=".td-post-category, .entry-category, .category",
            fuente="NDEN",
        )

    def scrape_diario_necochea(self) -> List[Dict]:
        """Scrapea la homepage de Diario Necochea."""
        base_url = "https://diarionecochea.com"
        logger.info("Scrapeando homepage Diario Necochea...")
        return self._scrape_homepage(
            base_url=base_url,
            card_selector="article, .post, .entry, .item, .nota",
            title_selector="h1, h2, h3",
            link_selector="a",
            image_selector="img",
            section_selector=".category, .seccion, .tag, .post-category",
            fuente="Diario Necochea",
        )

    def get_article_content(self, url: str) -> str:
        """Extrae el cuerpo completo de una nota individual con selectores por dominio."""
        logger.info("Extrayendo cuerpo completo: %s", url)
        domain = self._get_domain(url)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                html = page.content()
                return self._extract_article_text(html, domain=domain)
            finally:
                browser.close()

    def _scrape_homepage(
        self,
        base_url: str,
        card_selector: str,
        title_selector: str,
        link_selector: str,
        image_selector: str,
        section_selector: str,
        fuente: str,
    ) -> List[Dict]:
        items: List[Dict] = []
        seen: Set[str] = set()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                html = page.content()
                soup = BeautifulSoup(html, "html.parser")
                cards = soup.select(card_selector)

                for card in cards:
                    # Extraer título y link con múltiples selectores
                    titulo, url = self._extract_title_and_url(
                        card, title_selector, link_selector, base_url
                    )
                    if not titulo or not url:
                        continue
                    if len(titulo) < 8:
                        continue
                    if self._is_non_article_url(url):
                        continue
                    if url in self.existing_urls or url in seen:
                        continue

                    # Imagen
                    imagen_url = self._extract_image(card, image_selector, base_url)

                    # Sección
                    section_el = card.select_one(section_selector)
                    seccion = section_el.get_text(" ", strip=True) if section_el else "General"
                    seccion = self._normalize_section(seccion)

                    items.append({
                        "titulo": titulo,
                        "url": url,
                        "imagen_url": imagen_url,
                        "seccion": seccion or "General",
                        "fuente": fuente,
                    })
                    seen.add(url)

            except Exception:
                logger.exception("Error scrapeando %s", base_url)
            finally:
                browser.close()

        logger.info("Encontradas %s notas nuevas en %s", len(items), base_url)
        return items

    @staticmethod
    def _extract_title_and_url(
        card: BeautifulSoup, title_selector: str, link_selector: str, base_url: str
    ) -> Tuple[str, str]:
        """Extrae título y URL de una card de noticia."""
        # Intentar con selectors específicos primero
        for sel in title_selector.split(", "):
            el = card.select_one(sel.strip())
            if el:
                titulo = el.get_text(" ", strip=True)
                # Si el elemento tiene href, usarlo directamente
                href = el.get("href", "")
                if href:
                    url = urljoin(base_url, href)
                    if url.startswith("http"):
                        return titulo, url
                # Buscar el link más cercano
                parent = el.parent
                while parent and parent.name != "article":
                    link = parent.find("a", href=True)
                    if link:
                        href = link.get("href", "").strip()
                        if href:
                            url = urljoin(base_url, href)
                            if url.startswith("http"):
                                return titulo, url
                    parent = parent.parent
                break

        # Fallback: link_selector genérico
        link_el = card.select_one(link_selector)
        title_el = card.select_one("h1, h2, h3, h4")
        if link_el and title_el:
            href = (link_el.get("href") or "").strip()
            titulo = title_el.get_text(" ", strip=True)
            if href:
                url = urljoin(base_url, href)
                if url.startswith("http"):
                    return titulo, url

        return "", ""

    @staticmethod
    def _extract_image(card: BeautifulSoup, image_selector: str, base_url: str) -> Optional[str]:
        """Extrae URL de imagen de una card."""
        for sel in image_selector.split(", "):
            el = card.select_one(sel.strip())
            if el:
                raw = (
                    el.get("src")
                    or el.get("data-src")
                    or el.get("data-lazy-src")
                    or el.get("data-original")
                )
                if raw and not raw.startswith("data:"):
                    return urljoin(base_url, raw)
        return None

    @classmethod
    def _extract_article_text(cls, html: str, domain: str = "") -> str:
        """Extrae el texto del artículo con selectores específicos por dominio."""
        soup = BeautifulSoup(html, "html.parser")

        # Limpiar elementos no deseados
        for tag in ["script", "style", "noscript", "header", "footer",
                    "aside", "nav", ".sharedaddy", ".jp-relatedposts",
                    ".tags-links", ".post-navigation", ".wp-block-buttons"]:
            for node in soup.select(tag):
                node.decompose()

        # Selectores específicos por dominio
        domain_selectors = DOMAIN_CONTENT_SELECTORS.get(domain, [])

        article_node = None
        used_selector = "generic"

        # Intentar selectores específicos del dominio primero
        for sel in domain_selectors:
            node = soup.select_one(sel)
            if node:
                article_node = node
                used_selector = sel
                break

        # Fallback genérico
        if not article_node:
            for sel in ["article", ".single-content", ".entry-content",
                        ".post-content", ".content-body"]:
                node = soup.select_one(sel)
                if node:
                    article_node = node
                    used_selector = sel
                    break

        if not article_node:
            article_node = soup.body

        logger.debug("Selector usado para extracción: %s | dominio: %s", used_selector, domain)

        if not article_node:
            return cls._extract_meta_fallback(soup)

        # Extraer párrafos
        paragraphs = [p.get_text(" ", strip=True) for p in article_node.select("p")]
        paragraphs = [p for p in paragraphs if len(p) > 30]

        # Si hay pocos párrafos, incluir listas
        if len(paragraphs) < 2:
            li_texts = [li.get_text(" ", strip=True) for li in article_node.select("li")]
            paragraphs += [li for li in li_texts if len(li) > 30]

        # Si sigue siendo poco, usar texto completo del nodo
        if len(paragraphs) < 2:
            raw_text = article_node.get_text("\n", strip=True)
            chunks = [c.strip() for c in re.split(r"\n+", raw_text) if len(c.strip()) > 40]
            paragraphs = chunks[:15]

        text = "\n\n".join(paragraphs).strip()

        # Si el texto sigue siendo muy corto, usar meta fallback
        if len(text) < 80:
            fallback = cls._extract_meta_fallback(soup)
            if len(fallback) > len(text):
                logger.debug("Usando meta fallback (texto extraído muy corto: %s chars)", len(text))
                return fallback

        logger.debug("Texto extraído: %s chars | selector: %s", len(text), used_selector)
        return text

    @staticmethod
    def _get_domain(url: str) -> str:
        """Extrae el dominio limpio de una URL."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            return domain.removeprefix("www.")
        except Exception:
            return ""

    @staticmethod
    def _normalize_section(raw: str) -> str:
        """Normaliza el nombre de la sección."""
        mapping = {
            "policiales": "Policiales", "policial": "Policiales",
            "economia": "Economía", "economía": "Economía", "economic": "Economía",
            "política": "Política", "politica": "Política", "politics": "Política",
            "deportes": "Deportes", "deporte": "Deportes", "sports": "Deportes",
            "sociedad": "Sociedad", "social": "Sociedad",
            "local": "Local", "ciudad": "Local",
            "salud": "Salud", "health": "Salud",
            "educacion": "Educación", "educación": "Educación",
            "cultura": "Cultura", "culture": "Cultura",
            "tecnologia": "Tecnología", "tecnología": "Tecnología",
        }
        normalized = raw.strip().lower()
        for key, value in mapping.items():
            if key in normalized:
                return value
        return raw.strip().title() if raw.strip() else "General"

    @staticmethod
    def _is_non_article_url(url: str) -> bool:
        lowered = url.lower()
        blocked_keywords = [
            "/video/", "/category/", "/tag/", "/author/",
            "/wp-content/", "/feed/", "/page/", "#",
            "/publicidad/", "/aviso/", "/contacto/", "/quienes-somos/",
        ]
        return any(token in lowered for token in blocked_keywords)

    @staticmethod
    def _extract_meta_fallback(soup: BeautifulSoup) -> str:
        candidates: List[str] = []
        for selector in [
            'meta[property="og:description"]',
            'meta[name="description"]',
            'meta[name="twitter:description"]',
        ]:
            node = soup.select_one(selector)
            if not node:
                continue
            content = (node.get("content") or "").strip()
            if len(content) > 30:
                candidates.append(content)

        headings = [h.get_text(" ", strip=True) for h in soup.select("h1, h2")]
        headings = [h for h in headings if len(h) > 10]
        if headings:
            candidates.append(". ".join(headings[:4]))

        return "\n\n".join(candidates).strip()
