"""
Bot de Telegram para Neco News.
Envía previews de noticias con botones inline para publicar/descartar.
"""

import json
import logging
from typing import Dict

import requests

import config
from supabase_client import SupabaseNewsClient

logger = logging.getLogger("neconews.telegram")


class TelegramBotClient:
    def __init__(self, supabase_client: SupabaseNewsClient) -> None:
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        if not self.token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID son obligatorios.")
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.supabase_client = supabase_client

    def send_preview(self, noticia_data: Dict) -> None:
        """Envía una preview de la noticia con botones de acción usando requests."""
        titulo = noticia_data.get("titulo", "(sin titulo)")
        cuerpo = noticia_data.get("cuerpo", "")
        primer_parrafo = cuerpo.split("\n\n")[0][:600]
        noticia_id = noticia_data.get("id")

        if noticia_id is None:
            raise ValueError("La noticia debe contener 'id' para construir callbacks.")

        text = (
            f"📰 NUEVA NOTICIA PENDIENTE\n\n"
            f"Título: {titulo}\n\n"
            f"Primer párrafo:\n{primer_parrafo}\n\n"
            f"🔗 Editar: {config.ADMIN_URL}"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✓ Publicar", "callback_data": f"pub_{noticia_id}"},
                    {"text": "✕ Descartar", "callback_data": f"des_{noticia_id}"},
                ]
            ]
        }

        payload = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "reply_markup": json.dumps(keyboard),
        }

        try:
            response = requests.post(
                f"{self.base_url}/sendMessage", 
                json=payload, 
                timeout=20
            )
            if response.status_code >= 400:
                logger.error(
                    "Telegram sendMessage falló (status=%s). body=%s", 
                    response.status_code, 
                    response.text[:2000]
                )
            response.raise_for_status()
            logger.info("Preview enviada a Telegram para noticia id=%s", noticia_id)
        except Exception as e:
            logger.error("Error al enviar preview a Telegram: %s", e)

    def callback_handler(self, data: str) -> Dict:
        """Recibe el string del callback_data, extrae el id y llama a update_estado."""
        if not data:
            return {"ok": False, "error": "callback vacío"}

        parts = data.split("_", 1)
        if len(parts) != 2:
            logger.error("callback_data con formato inválido: %s", data)
            return {"ok": False, "error": "formato inválido"}

        action, noticia_id = parts
        
        if action == "pub":
            self.supabase_client.update_estado(noticia_id, "publicada")
            logger.info("Estado actualizado a 'publicada' para id=%s", noticia_id)
            return {"ok": True, "action": "publicada"}
        elif action == "des":
            self.supabase_client.update_estado(noticia_id, "descartada")
            logger.info("Estado actualizado a 'descartada' para id=%s", noticia_id)
            return {"ok": True, "action": "descartada"}
        else:
            logger.warning("Acción no soportada en callback_data: %s", data)
            return {"ok": False, "error": "acción no soportada"}

