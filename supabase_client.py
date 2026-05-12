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
            "url_original": datos.get("url_original"),
            "imagen_url": datos.get("imagen_url"),
            "instagram_text": datos.get("instagram_text"),
            "twitter_text": datos.get("twitter_text"),
            "guion_video": datos.get("guion_video"),
            "slug": datos.get("slug", "").strip(),
            "es_portada": False,
        }
        response = self.client.table("noticias").insert(payload).execute()
        rows = response.data or []
        if not rows:
            raise RuntimeError("Supabase no devolvió filas insertadas.")
        return rows[0]

    def get_noticia_by_id(self, noticia_id: str) -> Dict:
        """Obtiene una noticia por su ID."""
        try:
            response = self.client.table("noticias")\
                .select("id, titulo, estado, seccion")\
                .eq("id", noticia_id)\
                .single()\
                .execute()
            return response.data or {}
        except Exception:
            logger.exception("Error obteniendo noticia id=%s", noticia_id)
            return {}

    def update_imagen(self, noticia_id: str, imagen_url: str) -> None:
        """Actualiza la imagen_url de una noticia ya insertada."""
        try:
            self.client.table("noticias")\
                .update({"imagen_url": imagen_url})\
                .eq("id", noticia_id)\
                .execute()
            logger.info("Imagen actualizada: id=%s | url=%s", noticia_id, imagen_url[:60])
        except Exception:
            logger.exception("Error actualizando imagen para id=%s", noticia_id)

    def update_estado(self, noticia_id: int, estado: str) -> None:
        """Actualiza el estado de una noticia (publicada, descartada, etc.)."""
        update_data = {"estado": estado}
        if estado == "publicada":
            update_data["fecha_publicacion"] = datetime.now(timezone.utc).isoformat()
        self.client.table("noticias").update(update_data).eq("id", noticia_id).execute()

    def update_seccion(self, noticia_id: str, nueva_seccion: str) -> None:
        """Actualiza la sección de una noticia desde Telegram."""
        self.client.table("noticias").update({"seccion": nueva_seccion}).eq("id", noticia_id).execute()
        logger.info("Sección actualizada: id=%s → %s", noticia_id, nueva_seccion)

    def update_portada(self, noticia_id: str) -> None:
        """
        Marca una noticia como portada del día.
        Primero quita la portada de todas las del día de hoy,
        luego marca la indicada.
        """
        try:
            hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Quitar portada del día actual
            self.client.table("noticias") \
                .update({"es_portada": False}) \
                .gte("fecha_publicacion", f"{hoy}T00:00:00Z") \
                .lte("fecha_publicacion", f"{hoy}T23:59:59Z") \
                .execute()
            # Marcar la nueva portada
            self.client.table("noticias").update({"es_portada": True}).eq("id", noticia_id).execute()
            logger.info("Portada del día actualizada: id=%s", noticia_id)
        except Exception:
            logger.exception("Error actualizando portada para id=%s", noticia_id)

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

    # ─── Métodos para pipeline raw-first ─────────────────────────────

    def insert_noticia_raw(self, datos: Dict) -> Dict:
        """Inserta una noticia cruda SIN reescritura de IA, con estado='raw'."""
        payload = {
            "titulo": datos.get("titulo_original", "").strip(),
            "cuerpo": datos.get("cuerpo", "").strip(),
            "resumen_seo": "",
            "seccion": datos.get("seccion", "Local"),
            "estado": "raw",
            "fuente": datos.get("fuente", ""),
            "url_original": datos.get("url_original"),
            "imagen_url": datos.get("imagen_url"),
            "instagram_text": "",
            "twitter_text": "",
            "guion_video": "",
            "slug": datos.get("slug", "").strip(),
            "es_portada": False,
            "grupo_id": datos.get("grupo_id"),
            "titulo_original": datos.get("titulo_original", ""),
        }
        try:
            response = self.client.table("noticias").insert(payload).execute()
            rows = response.data or []
            if not rows:
                raise RuntimeError("Supabase no devolvió filas insertadas (raw).")
            logger.info(
                "Noticia raw insertada: slug=%s | grupo_id=%s",
                payload["slug"], payload["grupo_id"]
            )
            return rows[0]
        except Exception:
            logger.exception("Error insertando noticia raw: url=%s", datos.get("url_original"))
            raise

    def get_notas_by_grupo(self, grupo_id: str) -> List[Dict]:
        """Obtiene todas las notas raw de un grupo dado su grupo_id."""
        try:
            response = (
                self.client.table("noticias")
                .select("*")
                .eq("grupo_id", grupo_id)
                .eq("estado", "raw")
                .execute()
            )
            return response.data or []
        except Exception:
            logger.exception("Error obteniendo notas del grupo_id=%s", grupo_id)
            return []

    def get_notas_by_ids(self, ids: List[str]) -> List[Dict]:
        """Obtiene notas por lista de IDs."""
        if not ids:
            return []
        try:
            response = (
                self.client.table("noticias")
                .select("id, titulo, cuerpo, fuente, imagen_url, seccion, url_original")
                .in_("id", ids)
                .execute()
            )
            return response.data or []
        except Exception:
            logger.exception("Error obteniendo notas por ids: %s", ids)
            return []

    def delete_notas_raw_del_grupo(self, grupo_id: str, excepto_id: str) -> None:
        """Marca como 'descartada' todas las notas raw de un grupo excepto la publicada."""
        try:
            self.client.table("noticias") \
                .update({"estado": "descartada"}) \
                .eq("grupo_id", grupo_id) \
                .neq("id", excepto_id) \
                .eq("estado", "raw") \
                .execute()
            logger.info(
                "Notas raw descartadas del grupo_id=%s (excepto id=%s)",
                grupo_id, excepto_id
            )
        except Exception:
            logger.exception(
                "Error descartando notas raw del grupo_id=%s", grupo_id
            )

    def update_noticia_con_ia(self, noticia_id: str, datos_ia: Dict) -> None:
        """Actualiza una noticia raw con el resultado de la IA y la pasa a 'pendiente'."""
        try:
            # Obtener la sección actual como fallback
            noticia_actual = self.get_noticia_by_id(noticia_id)
            seccion_fallback = noticia_actual.get("seccion", "Local")

            update_data = {
                "titulo": datos_ia.get("titulo", "").strip(),
                "cuerpo": datos_ia.get("cuerpo", "").strip(),
                "resumen_seo": datos_ia.get("resumen_seo", ""),
                "instagram_text": datos_ia.get("instagram_text", ""),
                "twitter_text": datos_ia.get("twitter_text", ""),
                "guion_video": datos_ia.get("guion_video", ""),
                "slug": datos_ia.get("slug", "").strip(),
                "seccion": datos_ia.get("seccion_sugerida") or seccion_fallback,
                "estado": "pendiente",
            }
            self.client.table("noticias") \
                .update(update_data) \
                .eq("id", noticia_id) \
                .execute()
            logger.info(
                "Noticia actualizada con IA: id=%s | seccion=%s",
                noticia_id, update_data["seccion"]
            )
        except Exception:
            logger.exception("Error actualizando noticia con IA: id=%s", noticia_id)
            raise


# ─── SQL para ejecutar en Supabase (una sola vez) ────────────────────────────
# ALTER TABLE noticias ADD COLUMN IF NOT EXISTS grupo_id uuid;
# ALTER TABLE noticias ADD COLUMN IF NOT EXISTS titulo_original text;
# ALTER TABLE noticias ADD COLUMN IF NOT EXISTS fuente text;
# CREATE INDEX IF NOT EXISTS idx_noticias_grupo_id ON noticias(grupo_id);
