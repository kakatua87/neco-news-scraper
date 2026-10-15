"""
Edición matutina automática (Fase 0).

Elige los grupos 'raw' más importantes de las últimas horas, los procesa con
`pipeline_ia` y los deja en 'pendiente' registrando el score en `edicion_log`.
Todavía NO publica: la auto-publicación depende de los gates de imagen y de
verificación (fases 1-3).
"""

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("neconews.edicion")

VENTANA_HORAS = 14
SECCIONES_EXCLUIDAS_DEFAULT = ("Obituarios", "Farmacias")
PESOS_DEFAULT = {
    "fuentes": 3.0,       # por fuente distinta (log-escalado)
    "frescura": 2.0,      # máx. puntos por nota reciente
    "imagen": 0.5,        # tiene foto
    "secciones": {"Policiales": 1.5, "Política": 1.2, "Local": 1.5, "Deportes": 0.8},
    "max_por_seccion": 3,
}


def _parse_ts(valor: str) -> datetime:
    return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))


def _n_fuentes(notas: List[Dict]) -> int:
    return len({(n.get("fuente") or "").strip().lower() for n in notas} - {""}) or 1


def candidatos(client, horas: int = VENTANA_HORAS) -> List[Dict]:
    """Agrupa notas raw recientes por grupo_id."""
    desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
    resp = (
        client.client.table("noticias")
        .select("id, grupo_id, titulo, fuente, seccion, imagen_url, created_at")
        .eq("estado", "raw")
        .not_.is_("grupo_id", "null")
        .gte("created_at", desde)
        .execute()
    )
    grupos: Dict[str, List[Dict]] = defaultdict(list)
    for fila in resp.data or []:
        grupos[fila["grupo_id"]].append(fila)
    return [{"grupo_id": gid, "notas": notas} for gid, notas in grupos.items()]


def puntuar(grupo: Dict, pesos: Dict, ahora: Optional[datetime] = None) -> float:
    ahora = ahora or datetime.now(timezone.utc)
    notas = grupo["notas"]
    score = pesos["fuentes"] * math.log2(1 + _n_fuentes(notas))
    ultima = max(_parse_ts(n["created_at"]) for n in notas)
    horas = max((ahora - ultima).total_seconds() / 3600, 0)
    score += pesos["frescura"] * max(0.0, 1 - horas / VENTANA_HORAS)
    if any(n.get("imagen_url") for n in notas):
        score += pesos["imagen"]
    score += pesos["secciones"].get(notas[0].get("seccion"), 1.0)
    return round(score, 3)


def seleccionar(cands: List[Dict], cfg: Dict) -> List[Dict]:
    """Rankea y devuelve el top N respetando exclusiones y tope por sección."""
    pesos = {**PESOS_DEFAULT, **(cfg.get("edicion_pesos") or {})}
    excluidas = set(cfg.get("edicion_secciones_excluidas") or SECCIONES_EXCLUIDAS_DEFAULT)
    maximo = int(cfg.get("edicion_max") or 8)

    for g in cands:
        g["seccion"] = g["notas"][0].get("seccion") or "Local"
        g["n_fuentes"] = _n_fuentes(g["notas"])
        g["score"] = puntuar(g, pesos)

    elegibles = [g for g in cands if g["seccion"] not in excluidas]
    elegibles.sort(key=lambda g: g["score"], reverse=True)

    elegidos: List[Dict] = []
    por_seccion: Dict[str, int] = defaultdict(int)
    for g in elegibles:
        if len(elegidos) >= maximo:
            break
        if por_seccion[g["seccion"]] >= pesos["max_por_seccion"]:
            continue
        por_seccion[g["seccion"]] += 1
        elegidos.append(g)
    return elegidos


def _log(client, modo: str, g: Dict, resultado: str, motivo: str = "", noticia_id: Optional[str] = None) -> None:
    try:
        client.client.table("edicion_log").insert({
            "modo": modo,
            "grupo_id": g["grupo_id"],
            "noticia_id": noticia_id,
            "titulo": (g["notas"][0].get("titulo") or "")[:200],
            "seccion": g["seccion"],
            "score": g["score"],
            "n_fuentes": g["n_fuentes"],
            "resultado": resultado,
            "motivo": motivo[:500],
        }).execute()
    except Exception:
        logger.exception("No se pudo escribir edicion_log")


def ejecutar_edicion(client, procesar: Callable[..., Dict], dry_run: bool = False) -> Dict:
    """
    `procesar` es `pipeline_ia` (inyectado para evitar import circular).
    dry_run=True solo rankea y loguea, sin llamar a la IA.
    """
    cfg = client.get_scraper_config() or {}
    if not cfg.get("edicion_auto_activa") and not dry_run:
        return {"ok": True, "omitida": "edicion_auto_activa=false"}

    top = seleccionar(candidatos(client), cfg)
    modo = "sombra"  # Fase 0: nunca publica
    resumen = {"ok": True, "seleccionados": len(top), "procesadas": 0, "errores": 0, "items": []}

    for g in top:
        item = {"grupo_id": g["grupo_id"], "titulo": g["notas"][0].get("titulo"), "score": g["score"],
                "seccion": g["seccion"], "n_fuentes": g["n_fuentes"]}
        if dry_run:
            _log(client, modo, g, "omitida", "dry_run")
            resumen["items"].append({**item, "resultado": "dry_run"})
            continue
        ids = [n["id"] for n in g["notas"]]
        try:
            r = procesar(g["grupo_id"], ids, None, g["seccion"])
        except Exception as e:  # una nota no debe frenar la edición
            logger.exception("Error procesando grupo %s", g["grupo_id"])
            r = {"ok": False, "error": str(e)[:200]}
        if r.get("ok"):
            resumen["procesadas"] += 1
            client.client.table("noticias").update(
                {"score": g["score"], "auto_motivo": "edicion_matutina: pendiente de revision"}
            ).eq("id", r["noticia_id"]).execute()
            _log(client, modo, g, "procesada", noticia_id=r["noticia_id"])
        else:
            resumen["errores"] += 1
            _log(client, modo, g, "error", r.get("error", ""))
        resumen["items"].append({**item, "resultado": "ok" if r.get("ok") else "error"})
    return resumen
