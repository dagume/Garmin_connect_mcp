"""
strength_catalog.py
-------------------
Módulo de búsqueda para el catálogo de ejercicios de fuerza de Garmin Connect.

Carga el catálogo desde `exercises_catalog.json` (mismo directorio) una sola vez
al importar. Expone funciones de búsqueda que devuelven solo lo necesario,
nunca el catálogo completo.

Diseñado para minimizar tokens en cada respuesta MCP:
- search_exercises: ~20-50 tokens por resultado
- list_categories: ~150 tokens fijos
- list_muscles: ~100 tokens fijos
- get_exercises_by_muscle: ~10-30 tokens por resultado
"""

import json
import os
import re
import unicodedata
from typing import Optional

# --- Carga única del catálogo ----------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG_PATH = os.path.join(_HERE, "exercises_catalog.json")

# Estructura interna:
#   _ENTRIES: List[Dict] con todos los ejercicios planos
#   _BY_MUSCLE: Dict[muscle -> List[idx]]  (índice invertido)
#   _BY_CATEGORY: Dict[category -> List[idx]]
#   _SEARCH_TOKENS: List[Set[str]]  (tokens normalizados por ejercicio para fuzzy)

_ENTRIES: list = []
_BY_MUSCLE: dict = {}
_BY_CATEGORY: dict = {}
_SEARCH_TOKENS: list = []


# Aliases español → inglés para que `search("press banca")` encuentre BENCH_PRESS
_ALIASES_ES = {
    # Movimientos
    "press": ["press", "bench", "shoulder"],
    "banca": ["bench"],
    "sentadilla": ["squat"],
    "sentadillas": ["squat"],
    "peso muerto": ["deadlift"],
    "dominada": ["pull_up", "chin_up"],
    "dominadas": ["pull_up", "chin_up"],
    "fondos": ["dip", "triceps"],
    "remo": ["row"],
    "curl": ["curl", "biceps"],
    "biceps": ["biceps", "curl"],
    "biceps": ["biceps", "curl"],
    "triceps": ["triceps"],
    "hombros": ["shoulder", "lateral", "press"],
    "hombro": ["shoulder"],
    "espalda": ["lat", "row", "pull"],
    "pecho": ["chest", "bench", "fly", "push_up"],
    "pierna": ["squat", "lunge", "leg", "quad", "deadlift"],
    "piernas": ["squat", "lunge", "leg", "quad", "deadlift"],
    "gluteo": ["glute", "hip", "deadlift"],
    "gluteos": ["glute", "hip", "deadlift"],
    "core": ["plank", "abs", "crunch", "sit_up", "core"],
    "abdomen": ["abs", "crunch", "sit_up", "core"],
    "abdominal": ["abs", "crunch", "sit_up", "core"],
    "abdominales": ["abs", "crunch", "sit_up", "core"],
    "plancha": ["plank"],
    "estocada": ["lunge"],
    "estocadas": ["lunge"],
    "zancada": ["lunge"],
    "zancadas": ["lunge"],
    "elevacion": ["raise", "lift"],
    "elevaciones": ["raise", "lift"],
    "lateral": ["lateral"],
    "frontal": ["front"],
    "pantorrilla": ["calf"],
    "pantorrillas": ["calf"],
    "gemelo": ["calf"],
    "gemelos": ["calf"],
    "encogimiento": ["shrug"],
    "encogimientos": ["shrug"],
    "antebrazo": ["forearm", "wrist"],
    "antebrazos": ["forearm", "wrist"],
    "salto": ["jump"],
    "saltos": ["jump"],
    "lastrada": ["weighted"],
    "lastradas": ["weighted"],
    "lastrado": ["weighted"],
    "lastrados": ["weighted"],
    "mancuerna": ["dumbbell"],
    "mancuernas": ["dumbbell"],
    "barra": ["barbell", "bar"],
    "kettlebell": ["kettlebell"],
    "polea": ["cable"],
    "cable": ["cable"],
    "maquina": ["machine", "smith"],
    "balon": ["ball", "medicine_ball", "swiss_ball"],
    "fitball": ["swiss_ball"],
    "banda": ["band", "banded"],
    "elastico": ["band", "banded"],
    "isometrico": ["isometric", "static"],
    "isometrica": ["isometric", "static"],
    "explosivo": ["explosive", "jump", "plyo"],
    "explosiva": ["explosive", "jump", "plyo"],
    "pliometria": ["plyo", "jump"],
    "pliometrico": ["plyo", "jump"],
}


def _normalize(text: str) -> str:
    """Lowercase, sin tildes, alfanumérico + espacios."""
    text = text.lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokenize_exercise(category: str, exercise_name: str, muscles: list) -> set:
    """Tokens buscables: nombre del ejercicio + categoría + músculos."""
    parts = []
    # Separar exercise_name por _ y números
    name_tokens = re.split(r"[_0-9]+", exercise_name.lower())
    parts.extend([t for t in name_tokens if t])
    # Categoría
    cat_tokens = category.lower().split("_")
    parts.extend(cat_tokens)
    # Músculos
    for m in muscles:
        parts.extend(m.lower().split("_"))
    return set(parts)


def _load_catalog() -> None:
    global _ENTRIES, _BY_MUSCLE, _BY_CATEGORY, _SEARCH_TOKENS
    if _ENTRIES:
        return  # ya cargado

    if not os.path.exists(_CATALOG_PATH):
        raise FileNotFoundError(
            f"Catálogo no encontrado en {_CATALOG_PATH}. "
            "Ejecuta build_catalog.py una vez para generarlo."
        )

    with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
        _ENTRIES = json.load(f)

    for idx, e in enumerate(_ENTRIES):
        all_muscles = e["p"] + e["s"]
        for m in all_muscles:
            _BY_MUSCLE.setdefault(m, []).append(idx)
        _BY_CATEGORY.setdefault(e["c"], []).append(idx)
        _SEARCH_TOKENS.append(_tokenize_exercise(e["c"], e["e"], all_muscles))


def _expand_query_es(query: str) -> set:
    """Expande una query en español a tokens en inglés usando aliases."""
    norm = _normalize(query)
    tokens = set(norm.split())
    expanded = set(tokens)
    for tok in tokens:
        if tok in _ALIASES_ES:
            expanded.update(_ALIASES_ES[tok])
    # También expandir 2-grams
    bigrams = [f"{a} {b}" for a, b in zip(norm.split(), norm.split()[1:])]
    for bg in bigrams:
        if bg in _ALIASES_ES:
            expanded.update(_ALIASES_ES[bg])
    return expanded


def _score(entry_tokens: set, query_tokens: set, exercise_name: str) -> int:
    """Score simple: cuántos tokens de query aparecen en el ejercicio."""
    matches = query_tokens & entry_tokens
    score = len(matches) * 10
    # Bonus si el nombre del ejercicio contiene los tokens de query como substring
    ex_lower = exercise_name.lower()
    for qt in query_tokens:
        if len(qt) >= 3 and qt in ex_lower:
            score += 5
    # Penalización ligera si el ejercicio es muy específico (nombre largo)
    score -= len(exercise_name) // 20
    return score


# --- API pública -----------------------------------------------------------

def search_exercises(query: str, limit: int = 10, category: Optional[str] = None) -> list:
    """
    Busca ejercicios por texto libre en español o inglés.

    Args:
        query: texto a buscar (ej. "press banca", "bench press", "dominadas lastradas").
        limit: número máximo de resultados (1-25).
        category: opcionalmente filtrar por categoría exacta.

    Returns:
        Lista de dicts: [{category, exercise_name, is_bodyweight, primary_muscles, secondary_muscles, counterpart}].
    """
    _load_catalog()
    limit = max(1, min(limit, 25))

    query_tokens = _expand_query_es(query)
    if not query_tokens:
        return []

    # Indices candidatos
    if category:
        cat_upper = category.upper()
        candidates = _BY_CATEGORY.get(cat_upper, [])
    else:
        candidates = range(len(_ENTRIES))

    scored = []
    for idx in candidates:
        entry = _ENTRIES[idx]
        s = _score(_SEARCH_TOKENS[idx], query_tokens, entry["e"])
        if s > 0:
            scored.append((s, idx))

    scored.sort(key=lambda x: (-x[0], len(_ENTRIES[x[1]]["e"])))
    results = []
    for _, idx in scored[:limit]:
        e = _ENTRIES[idx]
        results.append({
            "category": e["c"],
            "exercise_name": e["e"],
            "is_bodyweight": e.get("bw", False),
            "primary_muscles": e["p"],
            "secondary_muscles": e["s"],
            "counterpart": e.get("cp"),
        })
    return results


def list_categories() -> list:
    """Lista todas las categorías de ejercicios con cantidad."""
    _load_catalog()
    return [
        {"category": cat, "count": len(idxs)}
        for cat, idxs in sorted(_BY_CATEGORY.items())
    ]


def list_muscles() -> list:
    """Lista todos los grupos musculares con cantidad de ejercicios."""
    _load_catalog()
    return [
        {"muscle": m, "count": len(idxs)}
        for m, idxs in sorted(_BY_MUSCLE.items())
    ]


def get_exercises_by_muscle(muscle: str, primary_only: bool = True, limit: int = 20) -> list:
    """
    Devuelve ejercicios que trabajan un músculo específico.

    Args:
        muscle: nombre del músculo (ej. "CHEST", "QUADS", "HAMSTRINGS").
        primary_only: si True, solo cuando es músculo primario.
        limit: máximo de resultados (1-50).
    """
    _load_catalog()
    limit = max(1, min(limit, 50))
    muscle = muscle.upper()

    results = []
    for idx in _BY_MUSCLE.get(muscle, []):
        e = _ENTRIES[idx]
        if primary_only and muscle not in e["p"]:
            continue
        results.append({
            "category": e["c"],
            "exercise_name": e["e"],
            "is_bodyweight": e.get("bw", False),
            "primary_muscles": e["p"],
            "secondary_muscles": e["s"],
        })
        if len(results) >= limit:
            break
    return results


def get_exercise(category: str, exercise_name: str) -> Optional[dict]:
    """Recupera un ejercicio exacto por (category, exercise_name)."""
    _load_catalog()
    cat_upper = category.upper()
    ex_upper = exercise_name.upper()
    for idx in _BY_CATEGORY.get(cat_upper, []):
        e = _ENTRIES[idx]
        if e["e"] == ex_upper:
            return {
                "category": e["c"],
                "exercise_name": e["e"],
                "is_bodyweight": e.get("bw", False),
                "primary_muscles": e["p"],
                "secondary_muscles": e["s"],
                "counterpart": e.get("cp"),
            }
    return None
