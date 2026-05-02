import logging
import requests
from bs4 import BeautifulSoup
from typing import Optional, Dict

import config
from ai_processor import AIProcessor
from supabase_client import SupabaseNewsClient
from datetime import datetime

logger = logging.getLogger("neconews.services")

class ServicesScraper:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        self.ai = None
        try:
            self.ai = AIProcessor()
        except Exception as e:
            logger.warning("IA no disponible para servicios: %s", e)
            
        self.supabase = SupabaseNewsClient()

    def fetch_farmacias(self) -> Optional[Dict]:
        """Scrapea farmacias de turno desde portalnecochea.com.ar"""
        url = "https://portalnecochea.com.ar/servicios/servicios-esenciales/farmacias-de-turno-en-necochea/"
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            
            # El contenido principal suele estar en el entry-content o main
            content_div = soup.select_one(".entry-content") or soup.select_one("main")
            if not content_div:
                logger.error("No se encontró el contenido de farmacias en la página.")
                return None
            
            text = content_div.get_text(separator="\n", strip=True)
            
            # Limpiar y formatear con IA
            formatted_text = self._format_with_ai("Farmacias de turno en Necochea y Quequén", text, "Extraé y presentá la lista de farmacias de turno del día actual de forma clara usando Markdown (listas con viñetas, destacando en negrita el nombre de la farmacia y luego la dirección). No inventes datos.")
            
            if not formatted_text:
                formatted_text = text[:2000] # Fallback
                
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

    def fetch_obituarios(self) -> Optional[Dict]:
        """Scrapea avisos fúnebres desde tsnnecochea.com.ar"""
        index_url = "https://tsnnecochea.com.ar/seccion/servicios/"
        try:
            r = requests.get(index_url, headers=self.headers, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            
            # Buscar el primer artículo que diga Avisos fúnebres
            article_link = None
            for a in soup.find_all("a", href=True):
                if "Avisos fúnebres" in a.text or "avisos-funebres" in a["href"]:
                    article_link = a["href"]
                    break
                    
            if not article_link:
                logger.error("No se encontró enlace a avisos fúnebres.")
                return None
                
            # Ir al artículo
            r2 = requests.get(article_link, headers=self.headers, timeout=15)
            r2.raise_for_status()
            soup2 = BeautifulSoup(r2.text, "html.parser")
            
            content_div = soup2.select_one(".entry-content") or soup2.select_one("article")
            if not content_div:
                logger.error("No se encontró el contenido del aviso fúnebre.")
                return None
                
            text = content_div.get_text(separator="\n", strip=True)
            
            formatted_text = self._format_with_ai("Avisos Fúnebres", text, "Extraé y presentá los avisos fúnebres de forma sumamente respetuosa usando Markdown (usa `### Nombre del fallecido` y debajo los detalles como edad, familiares y servicio de sepelio). No inventes datos ni agregues opiniones.")
            
            if not formatted_text:
                formatted_text = text[:2000]
                
            return {
                "titulo": f"Avisos Fúnebres - {datetime.now().strftime('%d/%m/%Y')}",
                "cuerpo": formatted_text,
                "seccion": "Obituarios",
                "slug": "avisos-funebres",
                "url_original": article_link,
                "imagen_url": "https://images.unsplash.com/photo-1497926131494-01306eeb41a1?q=80&w=1200&auto=format&fit=crop"
            }
        except Exception as e:
            logger.error("Error scrapeando obituarios: %s", e)
            return None

    def _format_with_ai(self, title: str, text: str, instructions: str) -> Optional[str]:
        if not self.ai:
            return None
            
        system_prompt = (
            "Sos un asistente de procesamiento de datos para Neco News. "
            "Tu única tarea es tomar un texto crudo scrapeado de la web y extraer la información útil "
            "formateándola en un documento Markdown limpio y estético.\n\n"
            f"INSTRUCCIONES ESPECÍFICAS: {instructions}\n\n"
            "REGLAS:\n"
            "1. NO devuelvas un JSON, devuelve directamente el contenido Markdown.\n"
            "2. NO inventes ningún dato. Si el texto no tiene información útil, responde con un mensaje breve indicando que no hay datos disponibles.\n"
            "3. NO agregues introducciones como 'Aquí tienes la lista'. Empieza directamente con el contenido."
        )
        
        user_prompt = f"Título de referencia: {title}\nTexto crudo:\n{text[:4000]}"
        
        try:
            response = self.ai.client.chat.completions.create(
                model=self.ai.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=1500,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error("Error en AI formatting: %s", e)
            return None

    def update_services(self):
        """Ejecuta el scraper de servicios y actualiza la BD."""
        logger.info("Iniciando actualización de servicios diarios...")
        
        farmacias = self.fetch_farmacias()
        if farmacias:
            self._upsert_service(farmacias)
            logger.info("Farmacias actualizadas con éxito.")
            
        obituarios = self.fetch_obituarios()
        if obituarios:
            self._upsert_service(obituarios)
            logger.info("Obituarios actualizados con éxito.")
            
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
        
        # Eliminar las notas viejas de esa sección para mantener solo la actual (o buscar y actualizar la existente)
        # Lo más fácil: buscar si hay una con ese slug exacto y actualizar.
        try:
            # Buscar si existe
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
