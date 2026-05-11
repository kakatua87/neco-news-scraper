import logging
import requests
import re
import json
from bs4 import BeautifulSoup
from typing import Optional, Dict, List
from datetime import datetime

import config
from ai_processor import AIProcessor
from supabase_client import SupabaseNewsClient

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

    def _group_obituarios_by_month(self, raw_text: str) -> Optional[List[Dict]]:
        """Usa IA para parsear cada aviso fúnebre, detectar su fecha y agruparlo por mes."""
        if not self.ai:
            logger.error("IA no disponible para agrupar obituarios.")
            return None
        
        system_prompt = (
            "Sos un asistente de procesamiento de datos para un diario digital. "
            "Tu tarea es analizar un texto largo con múltiples avisos fúnebres y agruparlos por MES y AÑO.\n\n"
            "INSTRUCCIONES:\n"
            "1. Cada aviso fúnebre comienza con el APELLIDO en mayúsculas seguido de la descripción.\n"
            "2. Detectá la fecha de fallecimiento de cada aviso (puede estar en formatos como 'Falleció el 08-05-2026', "
            "'Falleció el día 4 de mayo de 2026', 'Falleció el 01/05/2026', etc.)\n"
            "3. Agrupá los avisos por mes y año.\n"
            "4. SOLO incluí avisos del año 2026.\n"
            "5. Para cada grupo mensual, formateá los avisos en Markdown respetuoso.\n"
            "6. Devolvé ÚNICAMENTE un JSON array con esta estructura (sin texto extra):\n"
            '[\n'
            '  {\n'
            '    "mes": 5,\n'
            '    "mes_nombre": "Mayo",\n'
            '    "anio": 2026,\n'
            '    "cantidad": 7,\n'
            '    "contenido": "### APELLIDO, Nombre\\nFalleció el ...\\n\\n### APELLIDO2, Nombre2\\nFalleció el ..."\n'
            '  }\n'
            ']\n\n'
            "REGLAS:\n"
            "- NO inventes datos. Si no podés detectar la fecha, omití ese aviso.\n"
            "- Ordená los meses de mayor a menor (mayo primero, enero último).\n"
            "- En 'contenido', usá ### para cada nombre y un párrafo con los detalles.\n"
            "- NO incluyas avisos de 2025 u otros años.\n"
            "- Devolvé SOLO el JSON, sin bloques de código ni explicaciones."
        )
        
        text_to_send = raw_text[:4500]
        
        user_prompt = f"Texto crudo de necrológicas:\n{text_to_send}"
        
        try:
            response = self.ai.client.chat.completions.create(
                model=self.ai.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=4000,
            )
            result_text = (response.choices[0].message.content or "").strip()
            
            # Limpiar posibles bloques de código markdown
            if result_text.startswith("```"):
                result_text = re.sub(r'^```\w*\n?', '', result_text)
                result_text = re.sub(r'\n?```$', '', result_text)
            
            grouped = json.loads(result_text)
            
            if not isinstance(grouped, list):
                logger.error("La IA no devolvió un array válido.")
                return None
                
            logger.info("IA agrupó %d meses de obituarios.", len(grouped))
            return grouped
            
        except json.JSONDecodeError as e:
            logger.error("Error parseando JSON de IA: %s", e)
            logger.debug("Respuesta de IA: %s", result_text[:500] if result_text else "vacío")
            return None
        except Exception as e:
            logger.error("Error en IA para agrupar obituarios: %s", e)
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
