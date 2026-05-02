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
        imagen_fuente = noticia.get("imagen_fuente", "")
        noticia_id = noticia.get("id")
        seccion = noticia.get("seccion", "General")

        if noticia_id is None:
            raise ValueError("La noticia debe contener 'id' para construir callbacks.")

        lines = [
            f"📰 NUEVA NOTICIA PENDIENTE",
            f"",
            f"📂 Sección sugerida: {seccion}",
            f"Título: {titulo}",
            f"",
            f"Primer párrafo:",
            f"{primer_parrafo}",
        ]

        if instagram_preview:
            lines.extend(["", f"📱 Instagram:", f"{instagram_preview}"])

        if fuente:
            lines.extend(["", f"📌 Fuente: {fuente}"])

        # Atribución de imagen
        if imagen_fuente and "Ilustrativa" in imagen_fuente:
            lines.extend(["", f"📷 Imagen: Wikimedia Commons (ilustrativa)"])
        elif imagen_fuente and imagen_fuente not in ("Fuente original", ""):
            lines.extend(["", f"📷 Imagen: {imagen_fuente}"])

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

    def callback_handler(self, callback_query: Dict) -> Dict:
        """Procesa callbacks de los botones inline."""
        if not callback_query:
            return {"ok": False, "error": "callback vacío"}

        callback_data = callback_query.get("data", "")
        callback_id = callback_query.get("id", "")
        message = callback_query.get("message", {})
        message_id = message.get("message_id")
        chat_id = message.get("chat", {}).get("id")

        # Confirmar el callback para detener el spinner en la app de Telegram
        if callback_id:
            with httpx.Client(timeout=10) as client:
                try:
                    client.post(f"{self.base_url}/answerCallbackQuery", data={"callback_query_id": callback_id})
                except Exception:
                    pass

        action, _, raw_id = callback_data.partition("_")
        if not raw_id:
            return {"ok": False, "error": "id inválido"}

        noticia_id = raw_id
        estado_final = ""
        mensaje_estado = ""
        
        if action == "pub":
            self.supabase_client.update_estado(noticia_id, "publicada")
            estado_final = "publicada"
            mensaje_estado = "✅ PUBLICADA"
        elif action == "des":
            self.supabase_client.update_estado(noticia_id, "descartada")
            estado_final = "descartada"
            mensaje_estado = "❌ DESCARTADA"
        else:
            return {"ok": False, "error": "acción no soportada"}

        # Editar el mensaje para quitar los botones y agregar el estado
        if message_id and chat_id:
            is_photo = "photo" in message
            original_text = message.get("caption") if is_photo else message.get("text")
            original_text = original_text or ""
            
            new_text = f"[{mensaje_estado}]\n\n{original_text}"
            
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": json.dumps({"inline_keyboard": []})  # Quitar botones
            }
            if is_photo:
                payload["caption"] = new_text[:1024]
                endpoint = "/editMessageCaption"
            else:
                payload["text"] = new_text[:4096]
                endpoint = "/editMessageText"
                
            with httpx.Client(timeout=20) as client:
                try:
                    client.post(f"{self.base_url}{endpoint}", data=payload)
                except Exception as e:
                    logger.warning("No se pudo editar mensaje de Telegram: %s", e)

        return {"ok": True, "id": noticia_id, "estado": estado_final}
