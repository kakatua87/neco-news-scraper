"""
Edición matutina automática (Fase 0).

Elige los grupos 'raw' más importantes de las últimas horas, los procesa con
`pipeline_ia` y los deja en 'pendiente' registrando el score en `edicion_log`.
Todavía NO publica: la auto-publicación depende de los gates de imagen y de
verificación (fases 1-3).
"""

import logging
import math
import re
import unicodedata
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


_STOP = {
    "de", "la", "el", "los", "las", "un", "una", "unos", "unas", "en", "por", "con", "para", "del",
    "al", "y", "a", "o", "que", "se", "su", "sus", "lo", "es", "fue", "ser", "como", "mas", "sin",
    "sobre", "entre", "tras", "ante", "este", "esta", "estos", "estas", "ya", "no", "si", "le",
}
UMBRAL_DUPLICADO = 0.5
MIN_COMPARTIDOS = 3


def _tokens(titulo: str) -> set:
    """Tokens de contenido, sin tildes y truncados a 5 letras (despistó ~ despiste)."""
    t = unicodedata.normalize("NFKD", (titulo or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return {w[:5] for w in re.findall(r"[a-z0-9]+", t) if w not in _STOP and len(w) > 1}


def son_misma_noticia(titulo_a: str, titulo_b: str) -> bool:
    a, b = _tokens(titulo_a), _tokens(titulo_b)
    if not a or not b:
        return False
    comunes = len(a & b)
    return comunes >= MIN_COMPARTIDOS and comunes / min(len(a), len(b)) >= UMBRAL_DUPLICADO


def fusionar_duplicados(cands: List[Dict]) -> List[Dict]:
    """
    Junta grupos distintos que cuentan el mismo hecho (el agrupador del scraper
    no los une si cambia la sección o el enfoque del título). El grupo con más
    fuentes manda; las notas de los demás se suman para que la IA las use como
    fuentes adicionales y para que `n_fuentes` refleje la cobertura real.
    """
    orden = sorted(cands, key=lambda g: (-_n_fuentes(g["notas"]), g["grupo_id"]))
    resultado: List[Dict] = []
    for g in orden:
        titulo = g["notas"][0].get("titulo", "")
        destino = next((r for r in resultado if son_misma_noticia(r["notas"][0].get("titulo", ""), titulo)), None)
        if destino is None:
            g.setdefault("fusionados", [])
            resultado.append(g)
        else:
            destino["notas"] = destino["notas"] + g["notas"]
            destino["fusionados"].append(g["grupo_id"])
    return resultado


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


def verificar_imagen_nota(client, noticia_id: str, g: Dict) -> str:
    """
    Image gate: revisa la foto que quedó en la nota y, si tiene marca/logo, prueba
    las de las otras fuentes del grupo. Devuelve el motivo para `auto_motivo`.
    """
    import image_gate
    from image_handler import ImageHandler

    fila = client.client.table("noticias").select("imagen_url").eq("id", noticia_id).single().execute().data or {}
    actual = fila.get("imagen_url")
    originales = [n.get("imagen_url") for n in g["notas"] if n.get("imagen_url")]
    elegida = image_gate.elegir_imagen_limpia([actual] + originales)

    if elegida["url"] is None:
        analizadas = [d for d in elegida["detalle"] if d["resultado"]]
        if not analizadas:
            return "imagen: no se pudo verificar"
        r = analizadas[0]["resultado"]
        return "imagen: con marca/logo (%s)" % (r.get("nombre_medio") or r.get("motivo") or "sin detalle")
    if elegida["url"] == actual:
        return "imagen: limpia"
    nueva = ImageHandler(client).upload_image(elegida["url"], noticia_id)
    if not nueva:
        return "imagen: alternativa limpia pero no se pudo subir"
    client.update_imagen(noticia_id, nueva)
    return "imagen: reemplazada por alternativa limpia"


def verificar_hechos_nota(client, noticia_id: str, g: Dict, textos_fuente: List[str]) -> str:
    """Fact gate: contrasta la nota redactada con los textos de sus fuentes."""
    import fact_gate

    fila = (
        client.client.table("noticias").select("titulo, cuerpo, seccion")
        .eq("id", noticia_id).single().execute().data or {}
    )
    ev = fact_gate.evaluar_nota(
        fila.get("titulo", ""), fila.get("cuerpo", ""), textos_fuente,
        seccion=fila.get("seccion") or g["seccion"], n_fuentes=g["n_fuentes"],
    )
    if ev["automatica"]:
        return "hechos: verificados"
    return "hechos: " + "; ".join(ev["motivos"])


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

    top = seleccionar(fusionar_duplicados(candidatos(client)), cfg)
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
        # Los textos de las fuentes hay que leerlos ANTES: pipeline_ia pisa el cuerpo de la líder.
        try:
            textos_fuente = [n.get("cuerpo") or "" for n in client.get_notas_by_ids(ids)]
        except Exception:
            logger.exception("No se pudieron leer las fuentes de %s", g["grupo_id"])
            textos_fuente = []
        try:
            r = procesar(g["grupo_id"], ids, None, g["seccion"])
        except Exception as e:  # una nota no debe frenar la edición
            logger.exception("Error procesando grupo %s", g["grupo_id"])
            r = {"ok": False, "error": str(e)[:200]}
        if r.get("ok"):
            resumen["procesadas"] += 1
            # Las notas de grupos fusionados quedan raw si no se las descarta.
            sobrantes = [i for i in ids if i != r["noticia_id"]]
            if sobrantes:
                client.client.table("noticias").update({"estado": "descartada"})                     .in_("id", sobrantes).eq("estado", "raw").execute()
            try:
                motivo_img = verificar_imagen_nota(client, r["noticia_id"], g)
            except Exception:
                logger.exception("Image gate falló para %s", r["noticia_id"])
                motivo_img = "imagen: no se pudo verificar"
            try:
                motivo_hechos = verificar_hechos_nota(client, r["noticia_id"], g, textos_fuente)
            except Exception:
                logger.exception("Fact gate falló para %s", r["noticia_id"])
                motivo_hechos = "hechos: no se pudo verificar"
            motivo = f"{motivo_hechos} | {motivo_img}"
            client.client.table("noticias").update(
                {"score": g["score"], "auto_motivo": f"pendiente de revision | {motivo}"[:500]}
            ).eq("id", r["noticia_id"]).execute()
            _log(client, modo, g, "procesada", motivo=motivo, noticia_id=r["noticia_id"])
        else:
            resumen["errores"] += 1
            _log(client, modo, g, "error", r.get("error", ""))
        resumen["items"].append({**item, "resultado": "ok" if r.get("ok") else "error"})
    return resumen
