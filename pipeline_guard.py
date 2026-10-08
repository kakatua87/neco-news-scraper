"""
Guardas de pipeline_ia. Funciones puras (sin red) para poder probarlas solas.

Problema que resuelven: si se manda a procesar dos veces el mismo grupo (doble clic,
una pestaña vieja de la bandeja), la segunda ejecución encuentra la nota líder ya
convertida en 'pendiente' y le pide a la IA que reescriba el texto que la propia IA
acababa de escribir. Solo se procesan notas que siguen en estado 'raw'.
"""

from typing import Dict, List, Tuple

CODIGO_YA_PROCESADA = "ya_procesada"


def separar_notas_procesables(notas: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """(notas que siguen en 'raw', notas que ya no se pueden procesar)."""
    en_raw = [n for n in notas if n.get("estado") == "raw"]
    otras = [n for n in notas if n.get("estado") != "raw"]
    return en_raw, otras


def mensaje_no_procesable(otras: List[Dict]) -> str:
    estados = sorted({str(n.get("estado") or "desconocido") for n in otras})
    return (
        f"Esta noticia ya fue procesada o descartada (estado: {', '.join(estados)}). "
        "Actualizá la bandeja para ver su estado actual."
    )
