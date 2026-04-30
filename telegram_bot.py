"""
Bot de Telegram para Neco News.
Envía previews de noticias con botones inline para publicar/descartar.
"""

import json
import logging
from typing import Dict

import httpx

import config
from supabase_client import SupabaseNewsClient

logger = logging.getLogger("neconews.telegram")


def _escape_text(text: str) -> str:
    """Escapa caracteres problemáticos para evitar errores de parse en Telegram."""
    # NO usamos parse_mode para evitar problemas con caracteres especiales
    return text


class TelegramBotClient:
    def __init__(self, supabase_client: SupabaseNewsClient) -> None:
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        if not self.token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID son obligatorios.")
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.supabase_client = supabase_client

    def send_preview(self, noticia: Dict) -> None:
        """Envía una preview de la noticia con botones de acción."""
        titulo = noticia.get("titulo", "(sin titulo)")
        cuerpo = noticia.get("cuerpo", "")
        primer_parrafo = cuerpo.split("\n\n")[0][:600]
        instagram_preview = (noticia.get("instagram_text") or "")[:250]
        fuente = noticia.get("fuente", "")
        noticia_id = noticia.get("id")

        if noticia_id is None:
            raise ValueError("La noticia debe contener 'id' para construir callbacks.")

        # Construir mensaje sin parse_mode para evitar errores
        lines = [
            f"📰 NUEVA NOTICIA PENDIENTE",
            f"",
            f"Título: {titulo}",
            f"",
            f"Primer párrafo:",
            f"{primer_parrafo}",
        ]

        if instagram_preview:
            lines.extend(["", f"📱 Instagram:", f"{instagram_preview}"])

        if fuente:
            lines.extend(["", f"📌 Fuente: {fuente}"])

        lines.extend(["", f"🔗 Editar: {config.ADMIN_URL}"])

        text = "\n".join(lines)

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Publicar", "callback_data": f"pub_{noticia_id}"},
                    {"text": "❌ Descartar", "callback_data": f"des_{noticia_id}"},
                ],
                [
                    {"text": "✏️ Editar en panel", "url": config.ADMIN_URL},
                ],
            ]
        }

        # Si hay imagen, enviar foto con caption
        imagen_url = noticia.get("imagen_url")
        if imagen_url:
            self._send_photo(imagen_url, text, keyboard)
        else:
            self._send_message(text, keyboard)

        logger.info("Preview enviada a Telegram para noticia id=%s", noticia_id)

    def _send_message(self, text: str, keyboard: Dict) -> None:
        """Envía un mensaje de texto con botones inline."""
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4096],  # Telegram limita a 4096 chars
            "reply_markup": json.dumps(keyboard),
        }
        with httpx.Client(timeout=20) as client:
            response = client.post(f"{self.base_url}/sendMessage", data=payload)
            if response.status_code >= 400:
                logger.error(
                    "Telegram sendMessage falló (status=%s). body=%s",
                    response.status_code,
                    (response.text or "")[:2000],
                )
            response.raise_for_status()

    def _send_photo(self, photo_url: str, caption: str, keyboard: Dict) -> None:
        """Envía una foto con caption y botones inline."""
        payload = {
            "chat_id": self.chat_id,
            "photo": photo_url,
            "caption": caption[:1024],  # Caption máximo 1024 chars
            "reply_markup": json.dumps(keyboard),
        }
        with httpx.Client(timeout=20) as client:
            response = client.post(f"{self.base_url}/sendPhoto", data=payload)
            if response.status_code >= 400:
                # Fallback: si la foto falla, enviar solo texto
                logger.warning(
                    "Telegram sendPhoto falló (status=%s), haciendo fallback a texto. body=%s",
                    response.status_code,
                    (response.text or "")[:1000],
                )
                self._send_message(caption, keyboard)
                return
            response.raise_for_status()

    def callback_handler(self, callback_data: str) -> Dict:
        """Procesa callbacks de los botones inline."""
        if not callback_data:
            return {"ok": False, "error": "callback vacío"}

        action, _, raw_id = callback_data.partition("_")
        if not raw_id:
            return {"ok": False, "error": "id inválido"}

        noticia_id = raw_id
        if action == "pub":
            self.supabase_client.update_estado(noticia_id, "publicada")
            return {"ok": True, "id": noticia_id, "estado": "publicada"}
        if action == "des":
            self.supabase_client.update_estado(noticia_id, "descartada")
            return {"ok": True, "id": noticia_id, "estado": "descartada"}

        return {"ok": False, "error": "acción no soportada"}
