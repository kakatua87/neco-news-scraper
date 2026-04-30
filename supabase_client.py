"""
Cliente Supabase para Neco News.
Maneja inserción, consulta y actualización de noticias.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Set

from supabase import Client, create_client

import config

logger = logging.getLogger("neconews.supabase")


class SupabaseNewsClient:
    def __init__(self) -> None:
        url = config.SUPABASE_URL
        key = config.SUPABASE_KEY
        if not url or not key:
            raise ValueError("SUPABASE_URL y SUPABASE_KEY son obligatorios.")
        self.client: Client = create_client(url, key)

    def get_urls_existentes(self) -> Set[str]:
        """Obtiene todas las URLs ya procesadas para evitar duplicados."""
        try:
            response = (
                self.client.table("noticias")
                .select("url_original")
                .not_.is_("url_original", "null")
                .execute()
            )
            data: List[Dict] = response.data or []
            return {str(row["url_original"]).strip() for row in data if row.get("url_original")}
        except Exception:
            logger.exception("Error al obtener URLs existentes en Supabase.")
            return set()

    def insert_noticia(self, datos: Dict) -> Dict:
        """Inserta una noticia nueva con estado 'pendiente'."""
        payload = {
            "titulo": datos.get("titulo", "").strip(),
            "cuerpo": datos.get("cuerpo", "").strip(),
            "resumen_seo": datos.get("resumen_seo"),
            "seccion": datos.get("seccion", "General"),
            "estado": "pendiente",
            "fuente": datos.get("fuente", "Neco News"),
            "url_original": datos.get("url_original"),
            "imagen_url": datos.get("imagen_url"),
            "instagram_text": datos.get("instagram_text"),
            "twitter_text": datos.get("twitter_text"),
            "guion_video": datos.get("guion_video"),
            "slug": datos.get("slug", "").strip(),
        }
        response = self.client.table("noticias").insert(payload).execute()
        rows = response.data or []
        if not rows:
            raise RuntimeError("Supabase no devolvió filas insertadas.")
        return rows[0]

    def update_estado(self, noticia_id: int, estado: str) -> None:
        """Actualiza el estado de una noticia (publicada, descartada, etc.)."""
        update_data = {"estado": estado}
        if estado == "publicada":
            update_data["fecha_publicacion"] = datetime.now(timezone.utc).isoformat()
        self.client.table("noticias").update(update_data).eq("id", noticia_id).execute()

    def get_stats(self) -> Dict:
        """Obtiene estadísticas para el dashboard."""
        try:
            publicadas = self.client.table("noticias").select("id", count="exact").eq("estado", "publicada").execute()
            pendientes = self.client.table("noticias").select("id", count="exact").eq("estado", "pendiente").execute()
            descartadas = self.client.table("noticias").select("id", count="exact").eq("estado", "descartada").execute()
            return {
                "publicadas": publicadas.count or 0,
                "pendientes": pendientes.count or 0,
                "descartadas": descartadas.count or 0,
            }
        except Exception:
            logger.exception("Error obteniendo stats de Supabase.")
            return {"publicadas": 0, "pendientes": 0, "descartadas": 0}
