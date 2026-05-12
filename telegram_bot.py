"""
Bot de Telegram para Neco News.
Envía previews de noticias con botones inline para publicar/descartar.
Gestiona grupos de notas raw con selección interactiva de fuentes antes de procesar con IA.
"""

import json
import logging
import re
from typing import Dict, List, Optional

import requests

import config
from supabase_client import SupabaseNewsClient

logger = logging.getLogger("neconews.telegram")

SECCIONES = [
    "Política", "Economía", "Policiales", "Local",
    "Deportes", "Sociedad", "Salud", "Cultura",
]


class TelegramBotClient:
    def __init__(self, supabase_client: SupabaseNewsClient) -> None:
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        if not self.token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID son obligatorios.")
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.supabase_client = supabase_client
        # Estado en memoria de grupos activos (por grupo_id)
        self.grupos_estado: Dict[str, Dict] = {}

    # ─── Helpers de teclados ─────────────────────────────────────────

    def _keyboard_grupo(self, grupo_id: str) -> List[List[Dict]]:
        """Construye el teclado inline para un grupo según su estado actual."""
        estado = self.grupos_estado.get(grupo_id)
        if not estado:
            return []

        notas = estado["notas"]
        seleccionadas = set(estado["seleccionadas"])
        imagen_id = estado.get("imagen_id")
        seccion = estado.get("seccion", "Local")
        keyboard = []

        # Fila por nota: toggle de selección
        for nota in notas:
            nota_id = nota.get("id", "")
            fuente = nota.get("fuente", "Fuente")[:18]
            tick = "✓" if nota_id in seleccionadas else "○"
            keyboard.append([{
                "text": f"{tick} {fuente}",
                "callback_data": f"toggle_{grupo_id}_{nota_id}",
            }])

        # Botones de imagen (una fila por nota que tenga imagen)
        notas_con_imagen = [n for n in notas if n.get("imagen_url")]
        if notas_con_imagen:
            for nota in notas_con_imagen:
                nota_id = nota.get("id", "")
                fuente = nota.get("fuente", "Fuente")[:12]
                activa = "✅" if nota_id == imagen_id else "🖼"
                keyboard.append([{
                    "text": f"{activa} Imagen: {fuente}",
                    "callback_data": f"img_{grupo_id}_{nota_id}",
                }])

        # Sección y acciones
        keyboard.append([{
            "text": f"📂 Sección: {seccion}",
            "callback_data": f"sec_grupo_{grupo_id}",
        }])
        keyboard.append([{
            "text": "⚡ Procesar con IA",
            "callback_data": f"procesar_{grupo_id}",
        }])
        keyboard.append([{
            "text": "✕ Descartar todo",
            "callback_data": f"des_grupo_{grupo_id}",
        }])

        return keyboard

    def _keyboard_secciones_grupo(self, grupo_id: str) -> List[List[Dict]]:
        """Teclado de selección de sección para grupos."""
        keyboard = []
        for i in range(0, len(SECCIONES), 2):
            fila = [{"text": SECCIONES[i], "callback_data": f"sec_grupo_set_{grupo_id}_{SECCIONES[i]}"}]
            if i + 1 < len(SECCIONES):
                fila.append({"text": SECCIONES[i + 1], "callback_data": f"sec_grupo_set_{grupo_id}_{SECCIONES[i + 1]}"})
            keyboard.append(fila)
        keyboard.append([{"text": "← Volver", "callback_data": f"sec_grupo_volver_{grupo_id}"}])
        return keyboard

    def _build_grupo_text(self, grupo_id: str) -> str:
        """Construye el texto del mensaje para un grupo."""
        estado = self.grupos_estado.get(grupo_id)
        if not estado:
            return "(grupo no encontrado)"

        notas = estado["notas"]
        seccion = estado.get("seccion", "Local")
        n = len(notas)

        lines = [f"📰 GRUPO DE NOTICIAS — {n} {'fuente' if n == 1 else 'fuentes'}\n"]
        for nota in notas:
            fuente = nota.get("fuente", "Desconocida").upper()
            titulo = nota.get("titulo_original", "(sin título)")[:100]
            img_label = "🖼 Con imagen" if nota.get("imagen_url") else "❌ Sin imagen"
            lines.append(f"📡 {fuente}\n{titulo}\n{img_label}\n")

        lines.append(f"📂 Sección sugerida: {seccion}")
        return "\n".join(lines)

    # ─── Envío de mensajes ───────────────────────────────────────────

    def _send_message(self, text: str, keyboard: Optional[List[List[Dict]]] = None) -> Optional[Dict]:
        """Envía un mensaje al chat y retorna el objeto message de Telegram."""
        payload: Dict = {
            "chat_id": self.chat_id,
            "text": text[:4096],
        }
        if keyboard is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        try:
            resp = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json().get("result")
        except Exception as e:
            logger.error("Error enviando mensaje a Telegram: %s", e)
            return None

    def _edit_message(self, chat_id, message_id, text: str, keyboard=None) -> None:
        """Edita un mensaje existente."""
        payload: Dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "reply_markup": json.dumps({"inline_keyboard": keyboard if keyboard is not None else []}),
        }
        try:
            requests.post(f"{self.base_url}/editMessageText", json=payload, timeout=10)
        except Exception as e:
            logger.error("Error al editar mensaje en Telegram: %s", e)

    # ─── send_preview (nota individual procesada por IA) ─────────────

    def send_preview(self, noticia_data: Dict) -> None:
        """Envía preview de una noticia IA-procesada con botones Publicar/Descartar."""
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
        keyboard = [
            [
                {"text": "✓ Publicar", "callback_data": f"pub_{noticia_id}"},
                {"text": "✕ Descartar", "callback_data": f"des_{noticia_id}"},
            ],
            [{"text": "🔄 Cambiar sección", "callback_data": f"sec_menu_{noticia_id}"}],
        ]
        result = self._send_message(text, keyboard)
        if result:
            logger.info("Preview enviada a Telegram para noticia id=%s", noticia_id)
        else:
            logger.error("No se pudo enviar preview para noticia id=%s", noticia_id)

    # ─── send_grupo_preview (nuevo flujo raw-first) ──────────────────

    def send_grupo_preview(self, grupo_data: Dict) -> None:
        """
        Envía preview interactiva de un grupo de notas raw para que el editor
        seleccione fuentes antes de procesar con IA.

        grupo_data = {
            "grupo_id": "uuid",
            "notas": [{"id", "titulo_original", "fuente", "imagen_url", "seccion", "url_original"}],
            "seccion_sugerida": "Local"
        }
        """
        grupo_id = grupo_data.get("grupo_id", "")
        notas = grupo_data.get("notas", [])
        seccion_sugerida = grupo_data.get("seccion_sugerida", "Local")

        if not notas or not grupo_id:
            logger.warning("send_grupo_preview: grupo_id o notas vacíos.")
            return

        # Determinar imagen inicial: la primera nota que tenga imagen_url
        imagen_id = next((n["id"] for n in notas if n.get("imagen_url") and n.get("id")), None)

        # Inicializar estado en memoria
        self.grupos_estado[grupo_id] = {
            "seleccionadas": [n["id"] for n in notas if n.get("id")],
            "imagen_id": imagen_id,
            "seccion": seccion_sugerida,
            "message_id": None,
            "chat_id": None,
            "notas": notas,
        }

        text = self._build_grupo_text(grupo_id)
        keyboard = self._keyboard_grupo(grupo_id)

        result = self._send_message(text, keyboard)
        if result:
            self.grupos_estado[grupo_id]["message_id"] = result.get("message_id")
            self.grupos_estado[grupo_id]["chat_id"] = result.get("chat", {}).get("id")
            logger.info("Grupo preview enviado: grupo_id=%s | notas=%s", grupo_id, len(notas))
        else:
            logger.error("No se pudo enviar grupo preview: grupo_id=%s", grupo_id)

    # ─── callback_handler ────────────────────────────────────────────

    def callback_handler(self, data: str, callback_query: dict = None) -> Dict:
        """Despacha callbacks de botones inline de Telegram."""
        if not data:
            return {"ok": False, "error": "callback vacío"}

        # Extraer contexto del mensaje
        chat_id = None
        message_id = None
        text_msg = ""

        if callback_query:
            message = callback_query.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")
            text_msg = message.get("text", "")

        # ── Parseo de action e IDs ────────────────────────────────────

        # Orden importa: prefijos más específicos primero
        if data.startswith("toggle_"):
            # toggle_{grupo_id}_{nota_id}  — grupo_id es UUID (36 chars con guiones)
            rest = data[len("toggle_"):]
            grupo_id, nota_id = self._split_uuid_prefix(rest)
            return self._handle_toggle(grupo_id, nota_id, chat_id, message_id)

        elif data.startswith("img_"):
            rest = data[len("img_"):]
            grupo_id, nota_id = self._split_uuid_prefix(rest)
            return self._handle_img(grupo_id, nota_id, chat_id, message_id)

        elif data.startswith("sec_grupo_set_"):
            # sec_grupo_set_{grupo_id}_{seccion}
            rest = data[len("sec_grupo_set_"):]
            grupo_id, seccion = self._split_uuid_prefix(rest)
            return self._handle_sec_grupo_set(grupo_id, seccion, chat_id, message_id)

        elif data.startswith("sec_grupo_volver_"):
            grupo_id = data[len("sec_grupo_volver_"):]
            return self._handle_sec_grupo_volver(grupo_id, chat_id, message_id)

        elif data.startswith("sec_grupo_"):
            grupo_id = data[len("sec_grupo_"):]
            return self._handle_sec_grupo_menu(grupo_id, chat_id, message_id)

        elif data.startswith("procesar_"):
            grupo_id = data[len("procesar_"):]
            return self._handle_procesar(grupo_id, chat_id, message_id)

        elif data.startswith("des_grupo_"):
            grupo_id = data[len("des_grupo_"):]
            return self._handle_des_grupo(grupo_id, chat_id, message_id)

        elif data.startswith("sec_menu_"):
            noticia_id = data[len("sec_menu_"):]
            return self._handle_sec_menu(noticia_id, text_msg, chat_id, message_id)

        elif data.startswith("sec_volver_"):
            noticia_id = data[len("sec_volver_"):]
            return self._handle_sec_volver(noticia_id, text_msg, chat_id, message_id)

        elif data.startswith("sec_"):
            # sec_{noticia_id}_{seccion}
            rest = data[len("sec_"):]
            noticia_id, seccion = self._split_uuid_prefix(rest)
            return self._handle_sec_set(noticia_id, seccion, text_msg, chat_id, message_id)

        elif data.startswith("pub_"):
            noticia_id = data[len("pub_"):]
            titulo = self._extract_titulo(text_msg)
            return self._handle_pub(noticia_id, titulo, chat_id, message_id)

        elif data.startswith("des_"):
            noticia_id = data[len("des_"):]
            titulo = self._extract_titulo(text_msg)
            return self._handle_des(noticia_id, titulo, chat_id, message_id)

        else:
            logger.warning("Acción no soportada en callback_data: %s", data)
            return {"ok": False, "error": "acción no soportada"}

    # ─── Helpers de parseo ───────────────────────────────────────────

    def _split_uuid_prefix(self, s: str):
        """
        Divide una cadena con formato "{uuid}_{resto}" donde uuid tiene guiones.
        UUID estándar: 8-4-4-4-12 = 36 chars. Si hay menos de 36 chars hace split normal.
        """
        if len(s) > 36 and s[36] == "_":
            return s[:36], s[37:]
        # fallback: split en el primer guión bajo
        parts = s.split("_", 1)
        return (parts[0], parts[1]) if len(parts) == 2 else (s, "")

    def _extract_titulo(self, text_msg: str) -> str:
        match = re.search(r"Título: (.*?)\n", text_msg)
        return match.group(1).strip() if match else "(Noticia)"

    # ─── Handlers de grupos ──────────────────────────────────────────

    def _handle_toggle(self, grupo_id: str, nota_id: str, chat_id, message_id) -> Dict:
        estado = self.grupos_estado.get(grupo_id)
        if not estado:
            return {"ok": False, "error": "grupo no encontrado en memoria"}

        seleccionadas: List[str] = estado["seleccionadas"]
        if nota_id in seleccionadas:
            if len(seleccionadas) <= 1:
                # No dejar vacío: ignorar silenciosamente
                return {"ok": True, "action": "toggle_ignorado_min1"}
            seleccionadas.remove(nota_id)
        else:
            seleccionadas.append(nota_id)

        estado["seleccionadas"] = seleccionadas
        if chat_id and message_id:
            text = self._build_grupo_text(grupo_id)
            keyboard = self._keyboard_grupo(grupo_id)
            self._edit_message(chat_id, message_id, text, keyboard)

        logger.info("Toggle nota_id=%s en grupo=%s | seleccionadas=%s", nota_id, grupo_id, seleccionadas)
        return {"ok": True, "action": "toggle"}

    def _handle_img(self, grupo_id: str, nota_id: str, chat_id, message_id) -> Dict:
        estado = self.grupos_estado.get(grupo_id)
        if not estado:
            return {"ok": False, "error": "grupo no encontrado en memoria"}

        estado["imagen_id"] = nota_id
        if chat_id and message_id:
            text = self._build_grupo_text(grupo_id)
            keyboard = self._keyboard_grupo(grupo_id)
            self._edit_message(chat_id, message_id, text, keyboard)

        logger.info("Imagen actualizada: nota_id=%s en grupo=%s", nota_id, grupo_id)
        return {"ok": True, "action": "img"}

    def _handle_sec_grupo_menu(self, grupo_id: str, chat_id, message_id) -> Dict:
        if chat_id and message_id:
            texto = self._build_grupo_text(grupo_id)
            keyboard = self._keyboard_secciones_grupo(grupo_id)
            self._edit_message(chat_id, message_id, texto, keyboard)
        return {"ok": True, "action": "sec_grupo_menu"}

    def _handle_sec_grupo_set(self, grupo_id: str, seccion: str, chat_id, message_id) -> Dict:
        estado = self.grupos_estado.get(grupo_id)
        if not estado:
            return {"ok": False, "error": "grupo no encontrado"}

        estado["seccion"] = seccion
        logger.info("Sección de grupo actualizada: grupo_id=%s → %s", grupo_id, seccion)

        if chat_id and message_id:
            text = self._build_grupo_text(grupo_id)
            keyboard = self._keyboard_grupo(grupo_id)
            self._edit_message(chat_id, message_id, text, keyboard)

        return {"ok": True, "action": "sec_grupo_set"}

    def _handle_sec_grupo_volver(self, grupo_id: str, chat_id, message_id) -> Dict:
        if chat_id and message_id:
            text = self._build_grupo_text(grupo_id)
            keyboard = self._keyboard_grupo(grupo_id)
            self._edit_message(chat_id, message_id, text, keyboard)
        return {"ok": True, "action": "sec_grupo_volver"}

    def _handle_procesar(self, grupo_id: str, chat_id, message_id) -> Dict:
        estado = self.grupos_estado.get(grupo_id)
        if not estado:
            return {"ok": False, "error": "grupo no encontrado en memoria"}

        seleccionadas = estado.get("seleccionadas", [])
        imagen_id = estado.get("imagen_id")
        seccion = estado.get("seccion", "Local")

        if not seleccionadas:
            return {"ok": False, "error": "no hay fuentes seleccionadas"}

        # Obtener imagen_url de la nota elegida
        imagen_url = None
        if imagen_id:
            nota_imagen = next((n for n in estado["notas"] if n.get("id") == imagen_id), None)
            if nota_imagen:
                imagen_url = nota_imagen.get("imagen_url")

        # Editar mensaje: procesando…
        if chat_id and message_id:
            self._edit_message(chat_id, message_id, "⏳ Procesando con IA…", [])

        # Llamar al endpoint /procesar-grupo
        try:
            resp = requests.post(
                f"{config.PORTAL_URL.rstrip('/')}/procesar-grupo",
                json={
                    "grupo_id": grupo_id,
                    "fuentes_ids": seleccionadas,
                    "imagen_url": imagen_url,
                    "seccion": seccion,
                },
                timeout=120,
            )
            result = resp.json()
            if not result.get("ok"):
                error_msg = result.get("error", "error desconocido")
                if chat_id and message_id:
                    self._edit_message(
                        chat_id, message_id,
                        f"❌ Error al procesar:\n{error_msg}",
                        [[{"text": "🔁 Reintentar", "callback_data": f"procesar_{grupo_id}"}]],
                    )
                return {"ok": False, "error": error_msg}

            # Limpiar estado del grupo procesado
            self.grupos_estado.pop(grupo_id, None)
            logger.info("Grupo procesado con IA: grupo_id=%s | noticia_id=%s", grupo_id, result.get("noticia_id"))
            return {"ok": True, "action": "procesar", "noticia_id": result.get("noticia_id")}

        except Exception as e:
            logger.exception("Error llamando a /procesar-grupo: %s", e)
            if chat_id and message_id:
                self._edit_message(
                    chat_id, message_id,
                    f"❌ Error de conexión:\n{str(e)[:200]}",
                    [[{"text": "🔁 Reintentar", "callback_data": f"procesar_{grupo_id}"}]],
                )
            return {"ok": False, "error": str(e)}

    def _handle_des_grupo(self, grupo_id: str, chat_id, message_id) -> Dict:
        estado = self.grupos_estado.get(grupo_id)
        if not estado:
            return {"ok": False, "error": "grupo no encontrado en memoria"}

        # Descartar todas las notas del grupo en Supabase
        for nota in estado.get("notas", []):
            nota_id = nota.get("id")
            if nota_id:
                try:
                    self.supabase_client.update_estado(nota_id, "descartada")
                except Exception:
                    logger.exception("Error descartando nota_id=%s del grupo=%s", nota_id, grupo_id)

        self.grupos_estado.pop(grupo_id, None)
        logger.info("Grupo descartado: grupo_id=%s", grupo_id)

        if chat_id and message_id:
            self._edit_message(chat_id, message_id, f"🗑 DESCARTADO\n\ngrupo_id: {grupo_id[:8]}…")

        return {"ok": True, "action": "des_grupo"}

    # ─── Handlers de notas individuales ─────────────────────────────

    def _handle_pub(self, noticia_id: str, titulo: str, chat_id, message_id) -> Dict:
        self.supabase_client.update_estado(noticia_id, "publicada")
        noticia = self.supabase_client.get_noticia_by_id(noticia_id)
        if noticia.get("estado") != "publicada":
            logger.error("Verificación falló: noticia id=%s no publicada.", noticia_id)
            return {"ok": False, "error": "no se pudo verificar publicación"}
        logger.info("Publicada: id=%s", noticia_id)
        if chat_id and message_id:
            self._edit_message(chat_id, message_id, f"✅ PUBLICADA\n\n{titulo}\n\nPublicada en el portal.")
        return {"ok": True, "action": "publicada"}

    def _handle_des(self, noticia_id: str, titulo: str, chat_id, message_id) -> Dict:
        self.supabase_client.update_estado(noticia_id, "descartada")
        logger.info("Descartada: id=%s", noticia_id)
        if chat_id and message_id:
            self._edit_message(chat_id, message_id, f"🗑 DESCARTADA\n\n{titulo}")
        return {"ok": True, "action": "descartada"}

    def _handle_sec_menu(self, noticia_id: str, text_msg: str, chat_id, message_id) -> Dict:
        if chat_id and message_id:
            keyboard = []
            for i in range(0, len(SECCIONES), 2):
                fila = [{"text": SECCIONES[i], "callback_data": f"sec_{noticia_id}_{SECCIONES[i]}"}]
                if i + 1 < len(SECCIONES):
                    fila.append({"text": SECCIONES[i + 1], "callback_data": f"sec_{noticia_id}_{SECCIONES[i + 1]}"})
                keyboard.append(fila)
            keyboard.append([{"text": "← Volver", "callback_data": f"sec_volver_{noticia_id}"}])
            self._edit_message(chat_id, message_id, text_msg, keyboard)
        return {"ok": True, "action": "sec_menu"}

    def _handle_sec_set(self, noticia_id: str, seccion: str, text_msg: str, chat_id, message_id) -> Dict:
        self.supabase_client.update_seccion(noticia_id, seccion)
        logger.info("Sección actualizada: id=%s → %s", noticia_id, seccion)
        if chat_id and message_id:
            nuevo_texto = re.sub(r"📂 Sección: .*", f"📂 Sección: {seccion}", text_msg)
            keyboard = [
                [
                    {"text": "✓ Publicar", "callback_data": f"pub_{noticia_id}"},
                    {"text": "✕ Descartar", "callback_data": f"des_{noticia_id}"},
                ],
                [{"text": "🔄 Cambiar sección", "callback_data": f"sec_menu_{noticia_id}"}],
            ]
            self._edit_message(chat_id, message_id, nuevo_texto, keyboard)
        return {"ok": True, "action": "cambio_seccion"}

    def _handle_sec_volver(self, noticia_id: str, text_msg: str, chat_id, message_id) -> Dict:
        if chat_id and message_id:
            keyboard = [
                [
                    {"text": "✓ Publicar", "callback_data": f"pub_{noticia_id}"},
                    {"text": "✕ Descartar", "callback_data": f"des_{noticia_id}"},
                ],
                [{"text": "🔄 Cambiar sección", "callback_data": f"sec_menu_{noticia_id}"}],
            ]
            self._edit_message(chat_id, message_id, text_msg, keyboard)
        return {"ok": True, "action": "volver"}
