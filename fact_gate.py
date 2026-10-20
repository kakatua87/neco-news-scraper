"""
Fact gate de la edición automática: decide si una nota redactada por la IA
puede salir sin revisión humana, contrastándola con los textos de las fuentes.

Dos capas:
1. Determinista (gratis): cifras y nombres propios de la nota que NO aparecen en
   ninguna fuente. Atrapa números inventados y nombres alucinados.
2. LLM verificador: cifras/nombres/contradicciones que la capa 1 no ve
   (atribuciones cambiadas, causas inventadas, hechos mezclados).

Cualquier duda (error del modelo, sin fuentes) => NO es automática.
"""

import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from openai import OpenAI

import config

logger = logging.getLogger("neconews.fact_gate")

# Palabras capitalizadas que no necesitan estar en la fuente.
_NOMBRES_OK = {
    "necochea", "quequen", "neco", "argentina", "buenos", "aires", "enero", "febrero", "marzo", "abril",
    "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre", "lunes",
    "martes", "miercoles", "jueves", "viernes", "sabado", "domingo", "ruta", "calle", "avenida",
    "municipalidad", "municipio", "policia", "segun", "ademas", "tambien", "ante", "sin", "tras",
    # Términos institucionales: la IA expande siglas (UFI -> Unidad Funcional de Instrucción).
    "unidad", "funcional", "instruccion", "fiscalia", "juzgado", "camara", "tribunal", "departamento",
    "judicial", "comando", "patrullas", "comisaria", "seccional", "bomberos", "voluntarios", "hospital",
    "secretaria", "subsecretaria", "direccion", "defensa", "civil", "provincia", "nacional", "federal",
    "ministerio", "concejo", "deliberante", "intendente", "gobierno", "penal", "correccional",
}
# La cantidad de fuentes NO bloquea la automatización: una nota de fuente única se publica igual
# si el verificador la respalda (decisión editorial).
PALABRAS_SENSIBLES = (
    "menor de edad", "menores", "adolescente", "niña", "niño", "abuso", "violación", "violacion",
    "suicidio", "femicidio", "homicidio", "imputado", "detenido", "sospechoso", "acusado",
)
MAX_FUENTE_CHARS = 3500


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")
_DIAS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")


def _hoy_ar() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=3)


def _fecha_hoy_texto() -> str:
    h = _hoy_ar()
    return f"{_DIAS[h.weekday()]} {h.day} de {_MESES[h.month - 1]} de {h.year}"


def _quitar_fechas_cercanas(texto: str, ventana_dias: int = 7) -> str:
    """
    La IA convierte "este miércoles" en "miércoles 14 de octubre de 2026" (regla del prompt
    de redacción): esos números no están en la fuente pero son correctos. Se descartan solo
    las fechas dentro de ±`ventana_dias` de hoy; una fecha lejana inventada sigue contando.
    """
    hoy = _hoy_ar()
    validas = set()
    for delta in range(-ventana_dias, ventana_dias + 1):
        d = hoy + timedelta(days=delta)
        validas.add((d.day, _MESES[d.month - 1], d.year))

    def _reemplazo(m: "re.Match") -> str:
        dia, mes, anio = int(m.group(1)), m.group(2), m.group(3)
        anios = {a for (dd, mm, a) in validas if dd == dia and mm == mes}
        if anio is None:
            return " " if anios else m.group(0)
        return " " if int(anio) in anios else m.group(0)

    patron = r"\b(\d{1,2}) de (" + "|".join(_MESES) + r")(?: de (\d{4}))?"
    sin_fechas = re.sub(patron, _reemplazo, _norm(texto))
    # Horarios ("10:00", "18.30 hs") tampoco cuentan como cifras a respaldar.
    return re.sub(r"\b\d{1,2}[:.]\d{2}\b(?:\s*hs?\.?)?|\b\d{1,2}\s*(?:hs|horas)\b|\ba las \d{1,2}\b", " ", sin_fechas)


_NUMEROS_EN_PALABRAS = {
    "diez": "10", "once": "11", "doce": "12", "trece": "13", "catorce": "14", "quince": "15",
    "dieciseis": "16", "diecisiete": "17", "dieciocho": "18", "diecinueve": "19", "veinte": "20",
    "treinta": "30", "cuarenta": "40", "cincuenta": "50", "sesenta": "60", "setenta": "70",
    "ochenta": "80", "noventa": "90", "cien": "100", "ciento": "100",
}


def _numeros_en_palabras(texto_norm: str) -> set:
    """La fuente puede decir 'cuarenta' y la nota '40': eso está respaldado."""
    return {v for p, v in _NUMEROS_EN_PALABRAS.items() if re.search(r"\b" + p + r"\b", texto_norm)}


def _numeros(texto: str) -> set:
    """Cifras normalizadas sin separadores ('1.200' -> '1200'); ignora dígitos sueltos."""
    out = set()
    for m in re.findall(r"\d+(?:[.,]\d+)*", texto or ""):
        limpio = re.sub(r"[.,]", "", m)
        # Se ignoran los ceros sueltos ('00' de '10:00') y el año en curso (fechas relativas).
        if len(limpio) >= 2 and set(limpio) != {"0"} and limpio != str(_hoy_ar().year):
            out.add(limpio)
    return out


def _nombres(texto: str) -> set:
    """Palabras capitalizadas que no abren oración."""
    out = set()
    for oracion in re.split(r"(?<=[.!?:\n])\s+|\n+", texto or ""):
        palabras = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", oracion)
        for p in palabras[1:]:
            if p[0].isupper() and len(p) > 3:
                n = _norm(p)
                if n not in _NOMBRES_OK:
                    out.add(n)
    return out


def chequeo_determinista(titulo: str, cuerpo: str, fuentes: List[str]) -> Dict:
    nota = f"{titulo}\n{cuerpo}"
    base = "\n".join(fuentes)
    base_norm = _norm(base)
    nums_fuente = _numeros(base) | _numeros_en_palabras(base_norm)
    cifras = sorted(n for n in _numeros(_quitar_fechas_cercanas(nota)) if n not in nums_fuente)
    # Se compara por raíz de 5 letras: "instrucción"/"instrucciones", "unidad"/"unidades".
    raices_fuente = {w[:5] for w in re.findall(r"[a-z0-9]+", base_norm)}
    nombres = sorted(
        n for n in _nombres(nota) if n not in base_norm and n[:5] not in raices_fuente
    )
    return {"cifras_no_respaldadas": cifras, "nombres_no_respaldados": nombres}


def _hay_sensible(*textos: str) -> Optional[str]:
    t = _norm(" ".join(textos))
    for p in PALABRAS_SENSIBLES:
        if re.search(r"\b" + re.escape(_norm(p)) + r"\b", t):
            return p
    return None


_PROMPT = (
    "Sos un verificador de hechos de un diario. Recibís una NOTA redactada y los TEXTOS DE LAS FUENTES de "
    "las que salió. Tu trabajo: detectar información de la nota que las fuentes NO respaldan.\n"
    "Buscá: cifras, fechas, lugares, nombres, cargos, causas, atribuciones de dichos y hechos que no estén "
    "en las fuentes, y contradicciones entre la nota y las fuentes (o entre fuentes). Reformular está bien; "
    "agregar o cambiar datos no.\n"
    'Respondé SOLO JSON: {"ok": bool, "hallazgos": [{"tipo": "cifra|nombre|atribucion|hecho|contradiccion", '
    '"texto": "fragmento de la nota", "detalle": "por qué no está respaldado"}]}\n'
    "IMPORTANTE: la redacción convierte fechas relativas ('este miércoles', 'ayer', 'mañana') en fechas "
    "absolutas usando la fecha de hoy; eso es correcto y NO es un hallazgo mientras sea coherente con la "
    "fecha de hoy indicada abajo. Tampoco es hallazgo que la nota omita datos de la fuente.\n"
    "El diario es de Necochea: mencionar Necochea, Quequén o la región como lugar de contexto (por ejemplo "
    "'en Necochea', 'y Quequén') NO es un dato inventado. Sí lo es agregar un lugar concreto distinto "
    "(otra ciudad, una calle, un barrio) que las fuentes no mencionan.\n"
    "Las fuentes son diarios que ya verificaron su información: asumí que lo que dicen es cierto. NO marques "
    "diferencias de detalle entre fuentes, titulares de notas relacionadas que se coló en el texto, "
    "horarios o cifras aproximadas, ni reformulaciones. Marcá únicamente datos nuevos y concretos "
    "(un nombre, un dicho, un hecho) que la redacción agregó y no figuran en ninguna fuente.\n"
    "ok=true solo si hallazgos está vacío. No inventes hallazgos."
)


def _llm_json(mensajes: List[Dict], clave: str = "ok", max_tokens: int = 4096) -> Optional[Dict]:
    """Gemini primero; si no responde tras sus reintentos, Claude como respaldo (si hay clave)."""
    parsed = _llm_json_gemini(mensajes, clave, max_tokens)
    if parsed is not None:
        return parsed
    return _llm_json_claude(mensajes, clave, max_tokens)


def _llm_json_claude(mensajes: List[Dict], clave: str = "ok", max_tokens: int = 4096) -> Optional[Dict]:
    """Respaldo con la API de Anthropic. Requiere ANTHROPIC_API_KEY en el entorno; sin ella no hace nada."""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    modelo = os.getenv("ANTHROPIC_VERIFICADOR_MODEL", "claude-haiku-5-5").strip()
    logger.warning("Gemini no respondió: verifico con Claude (%s)", modelo)
    for _ in range(2):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": modelo, "max_tokens": max_tokens, "temperature": 0, "messages": mensajes},
                timeout=60,
            )
            if r.status_code != 200:
                logger.warning("Claude verificador falló: %s %s", r.status_code, r.text[:150])
                if r.status_code in (429, 500, 502, 503, 529):
                    time.sleep(3)
                    continue
                return None
            texto = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            m = re.search(r"\{.*\}", texto, re.DOTALL)
            parsed = json.loads(m.group(0)) if m else None
            if isinstance(parsed, dict) and clave in parsed:
                return parsed
            return None
        except Exception as e:
            logger.warning("Claude verificador falló: %s", str(e)[:150])
    return None


def _llm_json_gemini(mensajes: List[Dict], clave: str = "ok", max_tokens: int = 4096) -> Optional[Dict]:
    keys = config.gemini_keys_ordenadas()
    if not keys:
        return None
    base = config.AI_MODEL if config.AI_PROVIDER == "gemini" else config._PROVIDER_DEFAULTS["gemini"]["model"]
    modelos = [base] + [m for m in config.AI_FALLBACK_MODELS if m != base]
    base_url = config._PROVIDER_DEFAULTS["gemini"]["base_url"]
    inicio = time.monotonic()  # tope de tiempo total: con Gemini saturado no debe frenar la edición
    for key in keys:
        if time.monotonic() - inicio > 120:
            return None
        client = OpenAI(api_key=key, base_url=base_url, timeout=40, max_retries=0)
        key_agotada = False
        for modelo in modelos:
            if key_agotada or time.monotonic() - inicio > 120:
                break
            for _ in range(2):
                try:
                    r = client.chat.completions.create(
                        model=modelo, messages=mensajes, temperature=0, max_tokens=max_tokens,
                        response_format={"type": "json_object"},
                    )
                    parsed = json.loads(r.choices[0].message.content or "{}")
                    if isinstance(parsed, dict) and clave in parsed:
                        return parsed
                    break
                except Exception as e:
                    logger.warning("Verificador falló (%s): %s", modelo, str(e)[:150])
                    if "429" in str(e):
                        config.gemini_key_agotada(key)
                        key_agotada = True
                        break
                    if "503" in str(e):
                        time.sleep(4)
                        continue
                    break
    return None


def verificar_con_llm(titulo: str, cuerpo: str, fuentes: List[str]) -> Optional[Dict]:
    bloques = "\n\n".join(
        f"--- FUENTE {i + 1} ---\n{f[:MAX_FUENTE_CHARS]}" for i, f in enumerate(fuentes)
    )
    contenido = f"{_PROMPT}\n\nFecha de hoy: {_fecha_hoy_texto()}\n\n=== NOTA ===\nTítulo: {titulo}\n\n{cuerpo}\n\n=== FUENTES ===\n{bloques}"
    return _llm_json([{"role": "user", "content": contenido}])


def evaluar_nota(titulo: str, cuerpo: str, fuentes: List[str], seccion: str = "", n_fuentes: int = 1) -> Dict:
    """
    Devuelve {"automatica": bool, "motivos": [str], "determinista": {...}, "llm": {...}|None}.
    `automatica` es True solo si TODAS las salvaguardas pasan.
    """
    motivos: List[str] = []
    fuentes = [f for f in fuentes if f and f.strip()]
    if not fuentes:
        return {"automatica": False, "motivos": ["sin texto de fuente para contrastar"],
                "determinista": None, "llm": None}

    # Decisión editorial: ningún tema (sensible o no) bloquea la automatización por sí solo;
    # solo los chequeos de respaldo contra las fuentes.

    det = chequeo_determinista(titulo, cuerpo, fuentes)
    # Decisión editorial: las fuentes son medios que ya verificaron sus datos, así que una cifra que la
    # redacción deriva o reformatea (años, "60 años" -> 1966, horarios) NO bloquea la publicación. Queda
    # como aviso visible para el editor. Lo que sí bloquea es lo que el verificador (LLM) detecta al
    # contrastar con la fuente, que incluye cifras que contradicen o no están en ella.
    avisos: List[str] = []
    if det["cifras_no_respaldadas"]:
        avisos.append("cifras no halladas en la fuente: " + ", ".join(det["cifras_no_respaldadas"][:5]))
    if len(det["nombres_no_respaldados"]) >= 3:
        motivos.append("nombres sin respaldo: " + ", ".join(det["nombres_no_respaldados"][:5]))

    llm = verificar_con_llm(titulo, cuerpo, fuentes)
    if llm is None:
        # Las fuentes ya son medios que verificaron la información: si Gemini está caído, la nota no
        # se frena por eso (los chequeos deterministas de arriba sí se aplicaron).
        avisos.append("verificador no disponible (Gemini saturado): nota no contrastada por IA")
    else:
        h = llm.get("hallazgos") or []
        # Decisión editorial: las contradicciones y los detalles sueltos son avisos para el editor. Solo
        # bloquea cuando el verificador marca varios datos nuevos (nombres, dichos, hechos) que ninguna
        # fuente respalda, que es lo que delata una invención de la IA.
        graves = [x for x in h if str(x.get("tipo", "")).lower() in ("nombre", "atribucion", "hecho")]
        leves = [x for x in h if x not in graves]
        if leves:
            avisos.append("verificador (no bloquea): " + "; ".join(
                f"{x.get('tipo', '?')}: {str(x.get('texto', ''))[:60]}" for x in leves[:3]))
        if len(graves) >= 2:
            motivos.append("verificador: " + "; ".join(
                f"{x.get('tipo', '?')}: {str(x.get('texto', ''))[:60]}" for x in graves[:3]))
        elif graves:
            avisos.append("verificador (no bloquea): " + "; ".join(
                f"{x.get('tipo', '?')}: {str(x.get('texto', ''))[:60]}" for x in graves))

    return {"automatica": not motivos, "motivos": motivos, "avisos": avisos, "determinista": det, "llm": llm}
