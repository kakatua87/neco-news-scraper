"""
Seguimiento de los trabajos de IA para avisar cuando alguno queda sin procesar, aunque el navegador esté cerrado.

- `iniciar` / `terminar` registran cada trabajo (un grupo de la Bandeja o una edición completa) en `trabajos_ia`.
- Si termina con error, o si el servicio se reinicia a mitad de camino (`revisar_interrumpidos` al arrancar), se
  avisa en la campana del panel admin (`avisar`; ahí se enchufaría un canal externo, p. ej. WhatsApp).
Todo es "mejor esfuerzo": un fallo acá nunca debe romper el procesamiento.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("neconews.trabajos")


def avisar(client, tipo: str, titulo: str, detalle: Optional[Dict] = None) -> None:
    """Aviso en la campana del admin."""
    try:
        client.client.table("admin_notificaciones").insert(
            {"tipo": tipo, "titulo": titulo[:200], "detalle": detalle}
        ).execute()
    except Exception:
        logger.exception("No se pudo guardar el aviso para el admin")


def _titulo_del_grupo(client, grupo_id: str) -> str:
    try:
        r = client.client.table("noticias").select("titulo").eq("grupo_id", grupo_id).limit(1).execute()
        if r.data:
            return str(r.data[0].get("titulo") or "")[:100]
    except Exception:
        pass
    return ""


def iniciar(client, origen: str, grupo_id: Optional[str] = None, detalle: Optional[Dict] = None) -> Optional[int]:
    """Registra un trabajo en curso y devuelve su id (None si no se pudo registrar)."""
    try:
        r = client.client.table("trabajos_ia").insert(
            {"origen": origen, "grupo_id": grupo_id, "detalle": detalle}
        ).execute()
        return r.data[0]["id"] if r.data else None
    except Exception:
        logger.exception("No se pudo registrar el trabajo de IA")
        return None


def terminar(client, trabajo_id: Optional[int], ok: bool, error: str = "", aviso: Optional[str] = None,
             grupo_id: Optional[str] = None) -> None:
    """Cierra el trabajo. Si falló, avisa con `aviso` (o un texto por defecto con el título del grupo)."""
    if trabajo_id is not None:
        try:
            client.client.table("trabajos_ia").update(
                {"estado": "ok" if ok else "error", "error": error[:300] if error else None, "fin": _ahora()}
            ).eq("id", trabajo_id).execute()
        except Exception:
            logger.exception("No se pudo cerrar el trabajo de IA %s", trabajo_id)
    if ok:
        return
    if aviso is None:
        titulo = _titulo_del_grupo(client, grupo_id) if grupo_id else ""
        aviso = f"Una nota quedó sin procesar con IA{f': {titulo}' if titulo else ''}. Motivo: {error[:160] or 'error desconocido'}. Sigue en la Bandeja para reintentar."
    avisar(client, "sin_procesar", aviso, {"grupo_id": grupo_id, "error": error[:300]})


def revisar_interrumpidos(client) -> int:
    """
    Al arrancar el servicio no puede haber nada procesando en este proceso: lo que figure 'procesando' quedó
    cortado por un reinicio o un redespliegue. Se marca 'interrumpido' y se avisa. Devuelve cuántos eran.
    """
    try:
        r = client.client.table("trabajos_ia").select("id,origen,grupo_id").eq("estado", "procesando").execute()
        filas: List[Dict] = r.data or []
        if not filas:
            return 0
        client.client.table("trabajos_ia").update(
            {"estado": "interrumpido", "error": "el servicio se reinició", "fin": _ahora()}
        ).in_("id", [f["id"] for f in filas]).execute()
        grupos = [f for f in filas if f["origen"] == "bandeja"]
        ediciones = [f for f in filas if f["origen"] == "edicion"]
        partes = []
        if grupos:
            partes.append(f"{len(grupos)} nota(s) de la Bandeja")
        if ediciones:
            partes.append(f"{len(ediciones)} edición(es) con IA")
        avisar(
            client, "sin_procesar",
            f"El servicio se reinició y quedó sin terminar: {' y '.join(partes)}. Revisá la Bandeja y volvé a procesar lo que falte.",
            {"trabajos": filas},
        )
        return len(filas)
    except Exception:
        logger.exception("No se pudo revisar los trabajos interrumpidos")
        return 0


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()
