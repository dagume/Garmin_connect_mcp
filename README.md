# Catálogo de Ejercicios de Fuerza — Estrategia de Búsqueda

## Por qué este enfoque

Garmin tiene **1494 ejercicios de fuerza** en 39 categorías. El JSON crudo pesa 565 KB ≈ 140k tokens. Cargarlo entero al contexto consume ~1/5 del context window — inviable para cada planificación de fuerza.

**Solución:** mantener el catálogo en disco dentro del servidor MCP y exponer 4 herramientas livianas que devuelven solo lo necesario por búsqueda (~200-500 tokens cada una).

## Archivos generados

```
garmin_mcp_strength/
├── exercises_catalog.json     # Catálogo compacto (164 KB) — vive en disco
├── strength_catalog.py        # Módulo de búsqueda (importar en server.py)
├── build_catalog.py           # Script para regenerar el catálogo
├── PATCH_server.py.txt        # Código a copiar dentro de server.py
└── README.md                  # Este documento
```

## Instalación

1. Copiar `exercises_catalog.json` y `strength_catalog.py` al mismo directorio que `server.py` del MCP.
2. Aplicar el parche descrito en `PATCH_server.py.txt`.
3. Reiniciar Claude Desktop.

Si Garmin actualiza ejercicios, basta con descargar el JSON crudo nuevo y correr:
```bash
python build_catalog.py /ruta/al/nuevo/all-workout-list.json
```

## Las 4 herramientas nuevas en el MCP

### `search_strength_exercises(query, limit=10, category=None)`
La principal. Soporta español e inglés con aliases automáticos:

| Query (es) | Devuelve (en) |
|---|---|
| `"press banca"` | BENCH_PRESS / BENCH_PRESS, BARBELL_BENCH_PRESS, DUMBBELL_BENCH_PRESS |
| `"dominadas lastradas"` | PULL_UP / WEIGHTED_PULL_UP, WEIGHTED_CHIN_UP |
| `"sentadilla con barra"` | SQUAT / BARBELL_BACK_SQUAT, BARBELL_BOX_SQUAT |
| `"peso muerto rumano"` | DEADLIFT / ROMANIAN_DEADLIFT |
| `"plancha lateral"` | PLANK / SIDE_PLANK, ROLLING_SIDE_PLANK |
| `"bulgarian split squat"` | LUNGE / BARBELL_BULGARIAN_SPLIT_SQUAT |

Filtrado opcional por categoría: `search("barbell", category="SQUAT")`.

### `list_strength_categories()`
Las 39 categorías con cantidad. Para explorar cuando no sabes qué buscar.

### `list_strength_muscles()`
Los 17 grupos musculares con cantidad. Útil para diseñar sesiones equilibradas.

### `get_strength_exercises_by_muscle(muscle, primary_only=True, limit=20)`
Ejercicios que trabajan un músculo específico. Esencial para:
- Planificar sesiones por grupo muscular ("hoy traccion + biceps")
- Encontrar alternativas cuando no hay equipo

## Cómo Claude (yo) debe usar esto

**Flujo correcto al crear una sesión de fuerza:**

```
1. search_strength_exercises("press banca")
   → devuelve ~5 opciones con category + exercise_name exactos
2. Elegir la variante correcta según contexto (barra/mancuerna/inclinado/etc.)
3. add_workout(... category=..., exercise_name=..., weight_kg=...)
```

**Flujo INCORRECTO (no hacer):**
- Adivinar nombres como `"BARBELL_BENCH"` o `"PRESS_BANCA"`. Si el nombre no existe en el catálogo de Garmin, el ejercicio se guarda pero no se vincula al historial, no muestra gif y no contabiliza estadísticas musculares.

## Convenciones del catálogo

### Categoría vs exercise_name
- **categoría** = familia del movimiento (SQUAT, DEADLIFT, BENCH_PRESS, PULL_UP...)
- **exercise_name** = variante específica (BARBELL_BACK_SQUAT, ROMANIAN_DEADLIFT...)
- Ambos son requeridos por Garmin. Algunos ejercicios tienen el mismo nombre que la categoría (ej. SQUAT/SQUAT) — Garmin acepta esto como "genérico".

### isBodyWeight + counterpart
Muchos ejercicios vienen en pares peso-corporal/lastrado:
- `PULL_UP/PULL_UP` (isBodyWeight=true, counterpart=WEIGHTED_PULL_UP)
- `PULL_UP/WEIGHTED_PULL_UP` (counterpart=PULL_UP)

Regla: si vas a usar peso (`weight_kg`), busca la variante **WEIGHTED_*** o con material (BARBELL_*, DUMBBELL_*). Si es peso corporal, usa la versión sin prefijo.

### Categorías que NO son de gimnasio (excluidas del catálogo)
`BIKE_OUTDOOR`, `ELLIPTICAL`, `INDOOR_BIKE`, `RUN`, `RUN_INDOOR`, `STAIR_STEPPER`, `FLOOR_CLIMB`, `LADDER`. Estas son disciplinas o cardio puro, no van en un workout de strength_training.

### Categorías mantenidas que parecen cardio
`CARDIO` (jump_rope, burpees, jumping_jacks) y `WARM_UP` (estiramientos) se mantienen porque sí aparecen en sesiones de fuerza como calentamiento o finishers.

## Grupos musculares disponibles

```
ABDUCTORS, ABS, ADDUCTORS, BICEPS, CALVES, CHEST, FOREARM,
GLUTES, HAMSTRINGS, HIPS, LATS, LOWER_BACK, OBLIQUES,
QUADS, SHOULDERS, TRAPS, TRICEPS
```

## Aliases español incluidos

El módulo expande automáticamente estos términos en español a inglés antes de buscar. Listado no exhaustivo:

- **Movimientos**: press, banca, sentadilla(s), peso muerto, dominada(s), fondos, remo, curl, encogimiento(s), elevación(es)
- **Equipo**: mancuerna(s)→dumbbell, barra→barbell, kettlebell, polea/cable, máquina→machine, balón→ball, banda/elástico→band
- **Patrones**: lastrado/a→weighted, isométrico/a→isometric, explosivo/a→explosive/plyo
- **Anatomía**: pecho, espalda, hombros, piernas, glúteos, abdomen, pantorrillas, antebrazos, bíceps, tríceps

Si necesitas agregar aliases, editar `_ALIASES_ES` en `strength_catalog.py`.

## Costos en tokens (estimado)

| Operación | Tokens aprox |
|---|---|
| Cargar JSON crudo completo | ~140,000 |
| `search_strength_exercises(query, limit=10)` | ~300-500 |
| `list_strength_categories()` | ~600 |
| `list_strength_muscles()` | ~300 |
| `get_exercises_by_muscle(muscle, limit=20)` | ~700-1000 |

Una sesión típica de planificación de fuerza con 6-8 ejercicios:
**~3,000-4,000 tokens** vs los 140k que costaría sin esta estrategia.
