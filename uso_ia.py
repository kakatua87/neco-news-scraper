"""
Registro del consumo de IA (tokens y solicitudes) por proveedor, key, modelo y nota.

Alimenta el panel "Uso de IA" del admin. Cada llamada a Gemini, Groq o Claude (también las que fallan:
429, 503, timeout) deja una fila en `llm_uso`. Las filas se escriben en un hilo aparte: registrar nunca
frena ni rompe una edición, y la API key jamás se guarda (solo un alias con su posición y últimos 4 caracteres).
"""

import contextvars
import logging
import queue
import random
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

import config

logger = logging.getLogger("neconews.uso_ia")

_ctx: contextvars.ContextVar = contextvars.ContextVar("uso_ia_ctx", default={})
_cola: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=5000)
_worker_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
RETENCION_DIAS = 45


@contextmanager
def contexto(**campos):
    """Atribuye las llamadas de este bloque a una nota (`noticia_id`, `grupo_id`) y/o propósito."""
    token = _ctx.set({**_ctx.get(), **{k: v for k, v in campos.items() if v is not None}})
    try:
        yield
    finally:
        _ctx.reset(token)


@contextmanager
def proposito(nombre: str):
    """Fija el propósito de las llamadas del bloque (redaccion, imagen, hechos, clasificador, recorte, respaldo)."""
    with contexto(proposito=nombre):
        yield


def abrir(grupo_id: Optional[str]) -> None:
    """
    Empieza a atribuir las llamadas de este hilo al grupo `grupo_id` (la nota se completa con `fijar_nota`
    cuando se conoce la líder). `abrir(None)` limpia. Se usa en la edición, que procesa un grupo por vuelta.
    """
    _ctx.set({"grupo_id": grupo_id, "ref": {"noticia_id": None}} if grupo_id else {})


def fijar_nota(noticia_id: Optional[str]) -> None:
    """Atribuye a esta nota las llamadas siguientes (y las ya hechas para el grupo en curso, si hay `abrir`)."""
    ctx = _ctx.get()
    ref = ctx.get("ref")
    if ref is not None:
        ref["noticia_id"] = noticia_id
    else:
        _ctx.set({**ctx, "noticia_id": noticia_id})


def alias_key(key: Optional[str], proveedor: str = "gemini") -> str:
    """Identifica una key sin exponerla: 'gemini#2 …JcNg' (posición en GEMINI_API_KEYS y últimos 4 caracteres)."""
    if not key:
        return proveedor
    pos = ""
    if proveedor == "gemini" and key in config.GEMINI_API_KEYS:
        pos = f"#{config.GEMINI_API_KEYS.index(key) + 1}"
    return f"{proveedor}{pos} …{key[-4:]}"


def _tokens(usage: Any) -> tuple:
    """(entrada, salida) desde el `usage` de OpenAI/Gemini/Groq o de Anthropic; (0, 0) si no hay."""
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        get = usage.get
    else:
        def get(nombre, default=None):
            return getattr(usage, nombre, default)
    entrada = get("prompt_tokens") or get("input_tokens") or 0
    salida = get("completion_tokens") or get("output_tokens") or 0
    try:
        return int(entrada), int(salida)
    except (TypeError, ValueError):
        return 0, 0


def registrar(proveedor: str, key: Optional[str], modelo: Optional[str], usage: Any = None, ok: bool = True,
              codigo: Optional[int] = None, restante: Optional[Dict] = None, chars_in: int = 0,
              chars_out: int = 0, proposito_default: str = "redaccion") -> None:
    """Encola una fila de uso. Si la API no devolvió `usage`, estima los tokens (~3,5 caracteres por token)."""
    try:
        t_in, t_out = _tokens(usage)
        if not (t_in or t_out) and ok:
            t_in, t_out = int(chars_in / 3.5), int(chars_out / 3.5)
        ctx = _ctx.get()
        fila = {
            "proveedor": proveedor,
            "key_alias": alias_key(key, proveedor),
            "modelo": modelo,
            "proposito": ctx.get("proposito") or proposito_default,
            "noticia_id": ctx.get("noticia_id") or (ctx.get("ref") or {}).get("noticia_id"),
            "grupo_id": ctx.get("grupo_id"),
            "tokens_in": t_in,
            "tokens_out": t_out,
            "ok": ok,
            "codigo": codigo,
            "restante": restante,
        }
        _cola.put_nowait(fila)
        _asegurar_worker()
    except Exception:
        logger.debug("No se pudo encolar el uso de IA", exc_info=True)


def codigo_de_error(error: Exception) -> Optional[int]:
    """Código HTTP de un error del SDK (429, 503...), o None (timeout/conexión)."""
    return getattr(error, "status_code", None)


def _asegurar_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_vaciar_cola, name="uso-ia-writer", daemon=True)
            _worker.start()


def _vaciar_cola() -> None:
    cliente = None
    while True:
        try:
            lote = [_cola.get(timeout=30)]
        except queue.Empty:
            return  # sin actividad: el hilo termina y se vuelve a crear con la próxima llamada
        while len(lote) < 50:
            try:
                lote.append(_cola.get_nowait())
            except queue.Empty:
                break
        try:
            if cliente is None:
                from supabase_client import SupabaseNewsClient

                cliente = SupabaseNewsClient()
            cliente.client.table("llm_uso").insert(lote).execute()
            if random.randint(1, 200) == 1:  # retención sin cron: limpieza ocasional
                from datetime import datetime, timedelta, timezone

                limite = (datetime.now(timezone.utc) - timedelta(days=RETENCION_DIAS)).isoformat()
                cliente.client.table("llm_uso").delete().lt("ts", limite).execute()
        except Exception:
            logger.warning("No se pudo guardar el uso de IA (%s filas)", len(lote), exc_info=True)
            cliente = None
