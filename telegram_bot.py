"""
Bot de Telegram para Neco News.
Envía previews de noticias con botones inline para publicar/descartar.
"""

import json
import logging
from typing import Dict, List

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
        seccion = noticia_data.get("seccion", "General")
        primer_parrafo = cuerpo.split("\n\n")[0][:400]
        noticia_id = noticia_data.get("id")

        if noticia_id is None:
            raise ValueError("La noticia debe contener 'id' para construir callbacks.")

        text = (
            f"📰 NUEVA NOTICIA PENDIENTE\n\n"
            f"Título: {titulo}\n\n"
            f"Primer párrafo:\n{primer_parrafo}\n\n"
            f"📂 Sección: {seccion}\n\n"
            f"🔗 Editar: {config.ADMIN_URL}"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✓ Publicar", "callback_data": f"pub_{noticia_id}"},
                    {"text": "✕ Descartar", "callback_data": f"des_{noticia_id}"},
                ],
                [
                    {"text": "🔄 Cambiar sección", "callback_data": f"sec_menu_{noticia_id}"}
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

    def send_grupo_preview(self, notas: List[Dict], grupo_id: str) -> None:
        """
        Envía una preview de un grupo de notas raw scrapeadas (sin IA).
        Muestra todas las fuentes disponibles y ofrece botones para procesar cada fuente
        o procesar el grupo completo con IA (cross-sourcing).
        """
        if not notas:
            return

        lider = notas[0]
        titulo = lider.get("titulo", "(sin título)")
        seccion = lider.get("seccion", "Local")
        num_fuentes = len(notas)

        # Construir líneas de fuentes
        fuentes_lines = []
        for i, nota in enumerate(notas, 1):
            fuente_nombre = nota.get("fuente", f"Fuente {i}")
            fuentes_lines.append(f"  {i}. {fuente_nombre}")

        fuentes_text = "\n".join(fuentes_lines) if fuentes_lines else "  (fuente desconocida)"

        text = (
            f"🔍 NUEVO GRUPO — {num_fuentes} {'fuente' if num_fuentes == 1 else 'fuentes'}\n\n"
            f"📰 {titulo}\n\n"
            f"📂 Sección: {seccion}\n\n"
            f"📡 Fuentes:\n{fuentes_text}\n\n"
            f"🆔 grupo_id: {grupo_id[:8]}…"
        )

        # IDs de las notas como lista para los botones
        ids = [n["id"] for n in notas if n.get("id")]

        # Fila de botones por fuente individual
        keyboard = []
        for i, nota in enumerate(notas):
            nota_id = nota.get("id")
            if not nota_id:
                continue
            fuente_label = nota.get("fuente", f"Fuente {i+1}")[:15]
            keyboard.append([{
                "text": f"⚡ Procesar: {fuente_label}",
                "callback_data": f"procesar_solo_{nota_id}_{grupo_id[:8]}"
            }])

        # Si hay más de 1 fuente, botón para cross-sourcing completo
        if len(ids) > 1:
            ids_str = ",".join(ids)
            keyboard.append([{
                "text": f"🔀 Cross-source ({num_fuentes} fuentes)",
                "callback_data": f"procesar_grupo_{grupo_id}"
            }])

        # Botón para descartar todo el grupo
        keyboard.append([{
            "text": "🗑 Descartar grupo",
            "callback_data": f"descartar_grupo_{grupo_id}"
        }])

        payload = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "reply_markup": json.dumps({"inline_keyboard": keyboard}),
        }

        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json=payload,
                timeout=20,
            )
            if response.status_code >= 400:
                logger.error(
                    "Telegram sendMessage (grupo) falló (status=%s). body=%s",
                    response.status_code, response.text[:2000]
                )
            response.raise_for_status()
            logger.info("Grupo preview enviado a Telegram: grupo_id=%s", grupo_id)
        except Exception as e:
            logger.error("Error al enviar grupo preview a Telegram: %s", e)

    def _edit_message(self, chat_id, message_id, text, keyboard=None):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
        }
        if keyboard is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        else:
            payload["reply_markup"] = json.dumps({"inline_keyboard": []})
        try:
            requests.post(
                f"{self.base_url}/editMessageText",
                json=payload,
                timeout=10
            )
        except Exception as e:
            logger.error("Error al editar mensaje en Telegram: %s", e)

    def callback_handler(self, data: str, callback_query: dict = None) -> Dict:
        """Maneja las acciones de los botones inline de Telegram."""
        if not data:
            return {"ok": False, "error": "callback vacío"}

        chat_id = None
        message_id = None
        titulo = ""
        
        if callback_query:
            message = callback_query.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")
            text_msg = message.get("text", "")
            # Intentar extraer el título del mensaje
            import re
            match = re.search(r"Título: (.*?)\n\n", text_msg)
            if match:
                titulo = match.group(1).strip()
            else:
                titulo = "(Noticia)"

        parts = data.split("_")
        
        # Parse action and ID
        if data.startswith("sec_menu_"):
            action = "sec_menu"
            noticia_id = data.replace("sec_menu_", "")
        elif data.startswith("sec_volver_"):
            action = "sec_volver"
            noticia_id = data.replace("sec_volver_", "")
        elif data.startswith("sec_"):
            action = "sec"
            # formato: sec_{id}_{seccion}
            # el uuid tiene guiones, así que tenemos que extraer la sección que está al final
            # o dividir con cuidado. El id normalmente tiene 36 caracteres.
            # Mejor quitamos "sec_" y buscamos el primer guion bajo que separe ID de sección.
            resto = data[4:]
            partes_resto = resto.split("_", 1)
            if len(partes_resto) == 2:
                noticia_id = partes_resto[0]
                seccion = partes_resto[1]
            else:
                return {"ok": False, "error": "formato sec inválido"}
        else:
            action = parts[0]
            noticia_id = "_".join(parts[1:])

        if not noticia_id:
            return {"ok": False, "error": "falta id"}

        if action == "pub":
            self.supabase_client.update_estado(noticia_id, "publicada")
            # Verificar
            noticia = self.supabase_client.get_noticia_by_id(noticia_id)
            if noticia.get("estado") != "publicada":
                logger.error("Verificación falló: noticia id=%s no está publicada.", noticia_id)
                return {"ok": False, "error": "no se pudo verificar publicación"}
            
            logger.info("Estado actualizado a 'publicada' para id=%s", noticia_id)
            if chat_id and message_id:
                texto = f"✅ PUBLICADA\n\n{titulo}\n\nPublicada en el portal."
                self._edit_message(chat_id, message_id, texto)
            return {"ok": True, "action": "publicada"}
            
        elif action == "des":
            self.supabase_client.update_estado(noticia_id, "descartada")
            logger.info("Estado actualizado a 'descartada' para id=%s", noticia_id)
            if chat_id and message_id:
                texto = f"🗑 DESCARTADA\n\n{titulo}"
                self._edit_message(chat_id, message_id, texto)
            return {"ok": True, "action": "descartada"}
            
        elif action == "sec_menu":
            if chat_id and message_id:
                secciones = ["Política", "Economía", "Policiales", "Local", 
                           "Deportes", "Sociedad", "Salud", "Cultura"]
                keyboard = []
                # 2 por fila
                for i in range(0, len(secciones), 2):
                    fila = []
                    fila.append({"text": secciones[i], "callback_data": f"sec_{noticia_id}_{secciones[i]}"})
                    if i + 1 < len(secciones):
                        fila.append({"text": secciones[i+1], "callback_data": f"sec_{noticia_id}_{secciones[i+1]}"})
                    keyboard.append(fila)
                # Volver
                keyboard.append([{"text": "← Volver", "callback_data": f"sec_volver_{noticia_id}"}])
                
                texto_actual = callback_query.get("message", {}).get("text", "")
                self._edit_message(chat_id, message_id, texto_actual, keyboard)
            return {"ok": True, "action": "menu_seccion"}
            
        elif action == "sec":
            seccion_elegida = seccion # ya extraida arriba
            self.supabase_client.update_seccion(noticia_id, seccion_elegida)
            logger.info("Sección actualizada a '%s' para id=%s", seccion_elegida, noticia_id)
            
            if chat_id and message_id:
                texto_actual = callback_query.get("message", {}).get("text", "")
                # Reemplazar la sección en el texto
                nuevo_texto = re.sub(r"📂 Sección: .*", f"📂 Sección: {seccion_elegida}", texto_actual)
                
                keyboard = [
                    [
                        {"text": "✓ Publicar", "callback_data": f"pub_{noticia_id}"},
                        {"text": "✕ Descartar", "callback_data": f"des_{noticia_id}"},
                    ],
                    [
                        {"text": "🔄 Cambiar sección", "callback_data": f"sec_menu_{noticia_id}"}
                    ]
                ]
                self._edit_message(chat_id, message_id, nuevo_texto, keyboard)
            return {"ok": True, "action": "cambio_seccion"}
            
        elif action == "sec_volver":
            if chat_id and message_id:
                texto_actual = callback_query.get("message", {}).get("text", "")
                keyboard = [
                    [
                        {"text": "✓ Publicar", "callback_data": f"pub_{noticia_id}"},
                        {"text": "✕ Descartar", "callback_data": f"des_{noticia_id}"},
                    ],
                    [
                        {"text": "🔄 Cambiar sección", "callback_data": f"sec_menu_{noticia_id}"}
                    ]
                ]
                self._edit_message(chat_id, message_id, texto_actual, keyboard)
            return {"ok": True, "action": "volver"}
            
        else:
            logger.warning("Acción no soportada en callback_data: %s", data)
            return {"ok": False, "error": "acción no soportada"}

