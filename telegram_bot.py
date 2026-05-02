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
                # Fila 1: Acción principal
                [
                    {"text": "✅ Publicar", "callback_data": f"pub_{noticia_id}"},
                    {"text": "❌ Descartar", "callback_data": f"des_{noticia_id}"},
                ],
                # Fila 2: Sección (primera mitad)
                [
                    {"text": "💼 Política", "callback_data": f"sec_{noticia_id}_Política"},
                    {"text": "💹 Economía", "callback_data": f"sec_{noticia_id}_Economía"},
                    {"text": "🚨 Policiales", "callback_data": f"sec_{noticia_id}_Policiales"},
                    {"text": "📍 Local", "callback_data": f"sec_{noticia_id}_Local"},
                ],
                # Fila 3: Sección (segunda mitad)
                [
                    {"text": "⚽ Deportes", "callback_data": f"sec_{noticia_id}_Deportes"},
                    {"text": "👥 Sociedad", "callback_data": f"sec_{noticia_id}_Sociedad"},
                    {"text": "🎨 Cultura", "callback_data": f"sec_{noticia_id}_Cultura"},
                    {"text": "⚕️ Salud", "callback_data": f"sec_{noticia_id}_Salud"},
                ],
                # Fila 4: Portada y edición
                [
                    {"text": "⭐ Hacer portada del día", "callback_data": f"portada_{noticia_id}"},
                    {"text": "✏️ Panel", "url": config.ADMIN_URL},
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

        # Parsear acción: formatos son pub_ID, des_ID, sec_ID_SECCION, portada_ID
        parts = callback_data.split("_", 2)
        action = parts[0] if parts else ""

        mensaje_estado = ""
        estado_final = ""
        response_data: Dict = {"ok": True}

        if action == "pub":
            noticia_id = parts[1] if len(parts) > 1 else ""
            self.supabase_client.update_estado(noticia_id, "publicada")
            estado_final = "publicada"
            mensaje_estado = "✅ PUBLICADA"
            response_data = {"ok": True, "id": noticia_id, "estado": estado_final}

        elif action == "des":
            noticia_id = parts[1] if len(parts) > 1 else ""
            self.supabase_client.update_estado(noticia_id, "descartada")
            estado_final = "descartada"
            mensaje_estado = "❌ DESCARTADA"
            response_data = {"ok": True, "id": noticia_id, "estado": estado_final}

        elif action == "sec":
            # formato: sec_ID_NombreSeccion
            noticia_id = parts[1] if len(parts) > 1 else ""
            nueva_seccion = parts[2] if len(parts) > 2 else "General"
            self.supabase_client.update_seccion(noticia_id, nueva_seccion)
            mensaje_estado = f"📂 SECCIÓN: {nueva_seccion.upper()}"
            response_data = {"ok": True, "id": noticia_id, "seccion": nueva_seccion}

        elif action == "portada":
            noticia_id = parts[1] if len(parts) > 1 else ""
            self.supabase_client.update_portada(noticia_id)
            mensaje_estado = "⭐ PORTADA DEL DÍA"
            response_data = {"ok": True, "id": noticia_id, "es_portada": True}

        else:
            return {"ok": False, "error": "acción no soportada"}

        # Editar el mensaje para reflejar el cambio
        if message_id and chat_id and mensaje_estado:
            is_photo = "photo" in message
            original_text = message.get("caption") if is_photo else message.get("text")
            original_text = original_text or ""
            new_text = f"[{mensaje_estado}]\n\n{original_text}"

            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                # Solo quitar botones si es acción definitiva (pub/des)
                "reply_markup": json.dumps({"inline_keyboard": []}) if action in ("pub", "des") else json.dumps({"inline_keyboard": []}),
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

        return response_data
