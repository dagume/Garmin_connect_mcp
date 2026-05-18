"""
build_catalog.py
----------------
Genera `exercises_catalog.json` (formato compacto) a partir del JSON crudo
descargado de Garmin Connect.

Uso:
    python build_catalog.py <ruta_al_json_crudo>

El catálogo compacto usa claves cortas (c/e/p/s/bw/cp) para reducir tamaño:
    c  = category
    e  = exercise_name
    p  = primary_muscles
    s  = secondary_muscles
    bw = is_bodyweight (omitido si False)
    cp = counterpart (omitido si None)

Categorías excluidas (no son ejercicios de fuerza de gimnasio): BIKE_OUTDOOR,
ELLIPTICAL, INDOOR_BIKE, RUN, RUN_INDOOR, STAIR_STEPPER, FLOOR_CLIMB, LADDER.
WARM_UP se mantiene porque los estiramientos son útiles. CARDIO se mantiene
porque incluye jump_rope, burpees, etc. que sí van en fuerza.
"""
import json
import sys
import os

NON_STRENGTH_CATEGORIES = {
    "BIKE_OUTDOOR", "ELLIPTICAL", "INDOOR_BIKE",
    "RUN", "RUN_INDOOR", "STAIR_STEPPER",
    "FLOOR_CLIMB", "LADDER",
}


def build(raw_path: str, out_path: str) -> None:
    with open(raw_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cats = data.get("categories", data)  # acepta ambos formatos
    flat = []
    for cat_name, cat_data in cats.items():
        if cat_name in NON_STRENGTH_CATEGORIES:
            continue
        for ex_name, ex_data in cat_data.get("exercises", {}).items():
            entry = {
                "c": cat_name,
                "e": ex_name,
                "p": [m for m in ex_data.get("primaryMuscles", []) if m],
                "s": [m for m in ex_data.get("secondaryMuscles", []) if m],
            }
            if ex_data.get("isBodyWeight"):
                entry["bw"] = True
            if ex_data.get("counterpart"):
                entry["cp"] = ex_data["counterpart"]
            flat.append(entry)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(flat, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(out_path) / 1024
    print(f"Catálogo construido: {len(flat)} ejercicios, {size_kb:.1f} KB")
    print(f"Guardado en: {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python build_catalog.py <ruta_al_json_crudo>")
        sys.exit(1)
    raw = sys.argv[1]
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exercises_catalog.json")
    build(raw, out)
