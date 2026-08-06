import logging
import requests
import re
from bs4 import BeautifulSoup
from typing import Optional, Dict, List
from datetime import datetime

from supabase_client import SupabaseNewsClient

logger = logging.getLogger("neconews.services")

class ServicesScraper:
    """
    Scraper de servicios (farmacias de turno, obituarios). No usa IA:
    solo extrae texto de las fuentes y le da formato — los datos ya vienen
    completos de origen, no hace falta que nada los "redacte".
    """

    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        self.supabase = SupabaseNewsClient()

    # Línea que contiene solo el número de día (1-31) dentro de la tabla de turnos.
    _DIA_NUM_RE = re.compile(r'^\d{1,2}$')

    @classmethod
    def _formatear_farmacias(cls, raw_text: str) -> str:
        """
        Da formato de lectura al texto de turnos ya scrapeado, sin IA: separa
        el encabezado, marca "Turnos del mes de ..." como subtítulo y arma un
        bloque por día usando la numeración que ya trae la propia tabla.
        No intenta reconstruir a qué localidad corresponde cada farmacia
        (columnas ambiguas sin el HTML de la tabla) — mejor mostrar todos los
        datos del día juntos que asignar mal una farmacia a la localidad
        equivocada.
        """
        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

        turnos_idx = next(
            (i for i, l in enumerate(lines) if l.lower().startswith("turnos del mes")), None
        )
        if turnos_idx is None:
            # No se pudo ubicar la tabla: devolver el texto tal cual, limpio.
            return "\n\n".join(lines)

        bloques = ["\n".join(lines[:turnos_idx]), f"## {lines[turnos_idx]}"]

        i = turnos_idx + 1
        leyenda = []
        while i < len(lines) and not cls._DIA_NUM_RE.match(lines[i]):
            leyenda.append(lines[i])
            i += 1
        if leyenda:
            bloques.append(" · ".join(leyenda))

        dia_actual = None
        contenido_actual: List[str] = []
        for line in lines[i:]:
            if cls._DIA_NUM_RE.match(line):
                if dia_actual is not None:
                    bloques.append(f"### Día {dia_actual}\n\n" + " · ".join(contenido_actual))
                dia_actual = line
                contenido_actual = []
            else:
                contenido_actual.append(line)
        if dia_actual is not None:
            bloques.append(f"### Día {dia_actual}\n\n" + " · ".join(contenido_actual))

        return "\n\n".join(bloques)

    def fetch_farmacias(self) -> Optional[Dict]:
        """Scrapea farmacias de turno desde portalnecochea.com.ar (sin IA)."""
        url = "https://portalnecochea.com.ar/servicios/servicios-esenciales/farmacias-de-turno-en-necochea/"
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # El sitio está armado con Elementor; no usa .entry-content/main.
            # Tomamos el primer bloque .elementor que NO sea header/footer/popup.
            content_div = None
            for el in soup.select(".elementor"):
                classes = el.get("class") or []
                if any(c.startswith("elementor-location-") for c in classes):
                    continue
                content_div = el
                break
            if not content_div:
                logger.error("No se encontró el contenido de farmacias en la página.")
                return None

            text = content_div.get_text(separator="\n", strip=True)
            if not text or len(text) < 100:
                logger.error("Texto de farmacias demasiado corto o vacío.")
                return None

            formatted_text = self._formatear_farmacias(text)

            return {
                "titulo": f"Farmacias de Turno - {datetime.now().strftime('%d/%m/%Y')}",
                "cuerpo": formatted_text,
                "seccion": "Farmacias",
                "slug": "farmacias-de-turno",
                "url_original": url,
                "imagen_url": "https://images.unsplash.com/photo-1585435557343-3b092031a831?q=80&w=1200&auto=format&fit=crop"
            }
        except Exception as e:
            logger.error("Error scrapeando farmacias: %s", e)
            return None

    def fetch_obituarios(self) -> Optional[List[Dict]]:
        """
        Scrapea TODOS los avisos fúnebres desde la página principal de necrológicas de TSN.
        Los agrupa por mes/año usando IA para detectar las fechas.
        Retorna una lista de dicts, uno por cada mes de 2026.
        """
        url = "https://tsnnecochea.com.ar/servicios/necrologicas-157/"
        try:
            r = requests.get(url, headers=self.headers, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            
            content_div = soup.select_one(".entry-content") or soup.select_one(".article-body") or soup.select_one("article")
            if not content_div:
                logger.error("No se encontró el contenido de necrológicas.")
                return None
                
            # Obtener todo el texto crudo
            raw_text = content_div.get_text(separator="\n", strip=True)
            
            # Limpiar texto irrelevante (redes sociales, noticias relacionadas, etc.)
            cutoff_markers = ["Más avisos fúnebres", "Noticias relacionadas", "Seguinos en redes", "BRANDSAFETY"]
            for marker in cutoff_markers:
                idx = raw_text.find(marker)
                if idx > 0:
                    raw_text = raw_text[:idx]
            
            if not raw_text or len(raw_text) < 100:
                logger.error("Texto de necrológicas demasiado corto o vacío.")
                return None
            
            # Enviar a IA para agrupar por mes
            grouped = self._group_obituarios_by_month(raw_text)
            if not grouped:
                logger.error("No se pudo agrupar los obituarios por mes.")
                return None
                
            return grouped
            
        except Exception as e:
            logger.error("Error scrapeando obituarios: %s", e)
            return None

    # "Falleció"/"Fallecio" marca de forma confiable el arranque del cuerpo
    # de un aviso fúnebre. OJO: no usar también "Q.E.P.D." como disparador —
    # varios avisos ponen "(Q.E.P.D.)" pegado al final de la línea del
    # nombre, y eso generaría un trigger falso ahí mismo, partiendo el
    # aviso en dos.
    _AVISO_TRIGGER_RE = re.compile(r'fallec[ei][oó]?', re.IGNORECASE)

    _MESES_ES = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
        "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
        "noviembre": 11, "diciembre": 12,
    }
    _MESES_NOMBRE = {v: k.capitalize() for k, v in _MESES_ES.items() if k != "setiembre"}

    # Fecha numérica: D[/-]M[/-]YYYY o YY, con o sin espacios alrededor del
    # separador. Incluye guión medio/largo (– / —) que aparece en algunos avisos.
    _FECHA_NUMERICA_RE = re.compile(r'\b(\d{1,2})\s*[/\-–—]\s*(\d{1,2})\s*[/\-–—]\s*(\d{2,4})\b')
    # Fecha en texto: "D de MES(de)? YYYY"
    _FECHA_TEXTO_RE = re.compile(
        r'\b(\d{1,2})\s+de\s+([a-záéíóúñ]+)(?:\s+de)?\s+(\d{4})\b', re.IGNORECASE
    )

    @classmethod
    def _extraer_fecha(cls, texto: str) -> Optional[tuple]:
        """Busca la primera fecha válida (año, mes) dentro de un aviso fúnebre."""
        mejor: Optional[tuple] = None
        mejor_pos = len(texto) + 1

        m = cls._FECHA_TEXTO_RE.search(texto)
        if m:
            dia_str, mes_str, anio_str = m.groups()
            mes = cls._MESES_ES.get(mes_str.lower())
            if mes:
                mejor = (int(anio_str), mes)
                mejor_pos = m.start()

        for m in cls._FECHA_NUMERICA_RE.finditer(texto):
            if m.start() >= mejor_pos:
                continue
            dia_str, mes_str, anio_str = m.groups()
            try:
                dia, mes, anio = int(dia_str), int(mes_str), int(anio_str)
            except ValueError:
                continue
            if not (1 <= dia <= 31 and 1 <= mes <= 12):
                continue
            if anio < 100:
                anio += 2000
            mejor = (anio, mes)
            mejor_pos = m.start()
            break  # ya encontramos una fecha numérica más temprana que cualquier candidata previa

        return mejor

    def _group_obituarios_by_month(self, raw_text: str) -> Optional[List[Dict]]:
        """
        Agrupa los avisos fúnebres por mes/año sin usar IA: solo extrae el
        texto ya scrapeado y le da formato. Cada aviso trae sus propios datos
        reales (nombre, fecha) — no hace falta que nada los "redacte".
        """
        lines = [l.strip() for l in raw_text.split("\n")]
        lines = [l for l in lines if l]  # descartar líneas vacías

        trigger_idx = [i for i, l in enumerate(lines) if self._AVISO_TRIGGER_RE.search(l)]
        if not trigger_idx:
            logger.error("No se detectaron avisos fúnebres (patrón Q.E.P.D./Falleció) en el texto.")
            return None

        entries = []
        for n, idx in enumerate(trigger_idx):
            if idx == 0:
                continue  # no hay línea anterior para usar como nombre
            nombre = lines[idx - 1]
            body_end = (trigger_idx[n + 1] - 1) if n + 1 < len(trigger_idx) else len(lines)
            cuerpo_aviso = "\n".join(lines[idx:body_end]).strip()
            if not cuerpo_aviso:
                continue
            fecha = self._extraer_fecha(cuerpo_aviso)
            if not fecha:
                logger.debug("No se pudo detectar fecha para el aviso de '%s', se omite.", nombre)
                continue
            entries.append({"nombre": nombre, "cuerpo": cuerpo_aviso, "anio": fecha[0], "mes": fecha[1]})

        if not entries:
            logger.error("No se pudo extraer ninguna fecha válida de los avisos fúnebres.")
            return None

        grupos: Dict[tuple, List[Dict]] = {}
        for e in entries:
            grupos.setdefault((e["anio"], e["mes"]), []).append(e)

        resultado = []
        for (anio, mes), avisos in sorted(grupos.items(), reverse=True):
            contenido = "\n\n".join(f"### {a['nombre']}\n\n{a['cuerpo']}" for a in avisos)
            resultado.append({
                "mes": mes,
                "mes_nombre": self._MESES_NOMBRE.get(mes, str(mes)),
                "anio": anio,
                "cantidad": len(avisos),
                "contenido": contenido,
            })

        logger.info("Agrupados %d meses de obituarios (sin IA).", len(resultado))
        return resultado

    def update_services(self):
        """Ejecuta el scraper de servicios y actualiza la BD."""
        logger.info("Iniciando actualización de servicios diarios...")
        
        farmacias = self.fetch_farmacias()
        if farmacias:
            self._upsert_service(farmacias)
            logger.info("Farmacias actualizadas con éxito.")
            
        # Obituarios: scrapear, agrupar por mes y guardar cada mes
        obituarios_por_mes = self.fetch_obituarios()
        if obituarios_por_mes:
            for grupo in obituarios_por_mes:
                mes = grupo.get("mes", 0)
                mes_nombre = grupo.get("mes_nombre", "")
                anio = grupo.get("anio", 2026)
                cantidad = grupo.get("cantidad", 0)
                contenido = grupo.get("contenido", "")
                
                if not contenido or mes == 0:
                    continue
                    
                payload = {
                    "titulo": f"Avisos Fúnebres — {mes_nombre} {anio}",
                    "cuerpo": contenido,
                    "seccion": "Obituarios",
                    "slug": f"obituarios-{anio}-{mes:02d}",
                    "url_original": f"https://tsnnecochea.com.ar/servicios/necrologicas-157/?mes={anio}-{mes:02d}",
                    "imagen_url": ""
                }
                self._upsert_service(payload)
                logger.info("Obituarios %s %d: %d avisos guardados.", mes_nombre, anio, cantidad)
        else:
            logger.warning("No se pudieron obtener obituarios.")
            
    def _upsert_service(self, payload: Dict):
        """Busca si ya existe una nota de hoy para este servicio y la actualiza, sino la crea."""
        # Generar un payload compatible con noticias
        full_payload = {
            **payload,
            "resumen_seo": f"Información actualizada de {payload['seccion']} para el día de hoy.",
            "instagram_text": "",
            "twitter_text": "",
            "guion_video": "",
            "estado": "publicada" # Se publican automáticamente
        }
        
        try:
            # Buscar si existe por slug
            res = self.supabase.client.from_("noticias").select("id").eq("slug", payload["slug"]).execute()
            if res.data and len(res.data) > 0:
                # Update
                self.supabase.client.from_("noticias").update(full_payload).eq("id", res.data[0]["id"]).execute()
            else:
                # Insert
                self.supabase.client.from_("noticias").insert(full_payload).execute()
        except Exception as e:
            logger.error("Error guardando el servicio en BD: %s", e)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = ServicesScraper()
    scraper.update_services()
