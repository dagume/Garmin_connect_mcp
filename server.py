"""
Garmin Connect MCP Server
Servidor MCP completo basado en python-garminconnect (cyberjunky).
Cubre: actividades, salud diaria, métricas avanzadas, tendencias, composición corporal,
metas, dispositivos, equipamiento, hidratación, entrenamientos y planes de entrenamiento.
"""

import json
import os
from datetime import date, datetime, timedelta
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from strength_catalog import (
    search_exercises as sc_search,
    list_categories as sc_categories,
    list_muscles as sc_muscles,
    get_exercises_by_muscle as sc_by_muscle,
)

# ─── Autenticación ────────────────────────────────────────────────────────────

EMAIL = os.environ.get("GARMIN_EMAIL", "")
PASSWORD = os.environ.get("GARMIN_PASSWORD", "")
TOKEN_DIR = os.path.expanduser("~/.garmin-mcp-python")

_client = None


def get_client():
    global _client
    if _client is not None:
        return _client
    from garminconnect import Garmin
    os.makedirs(TOKEN_DIR, exist_ok=True)
    client = Garmin(EMAIL, PASSWORD)
    token_file = os.path.join(TOKEN_DIR, "garmin_tokens.json")
    try:
        client.login(token_file)
    except Exception:
        client.login()
        try:
            client.garth.dump(token_file)
        except Exception:
            pass
    _client = client
    return client


def today() -> str:
    return date.today().isoformat()


def yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def ok(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, default=str, ensure_ascii=False, indent=2))]


def err(e: Exception) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


def _date_range(start: str, end: str) -> list[str]:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if e < s:
        s, e = e, s
    return [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]


def collect_range(fn, start: str, end: str) -> list:
    """Itera un metodo de un solo dia sobre un rango y arma una lista por fecha."""
    out = []
    for day in _date_range(start, end):
        try:
            out.append({"date": day, "data": fn(day)})
        except Exception as ex:
            out.append({"date": day, "error": str(ex)})
    return out


def _first_value(dct):
    """Primer value de un dict (para mapas con clave dinamica como deviceId)."""
    if isinstance(dct, dict) and dct:
        return next(iter(dct.values()))
    return None


def athlete_snapshot(gc, d: str) -> dict:
    """Check-in diario en UNA llamada. Cada seccion es independiente: si una
    fuente falla, devuelve {'error': ...} sin tumbar el resto del snapshot."""
    snap = {"date": d}

    # training_readiness (+ recovery_time sale del mismo payload, ahorra 1 llamada)
    try:
        tr = gc.get_training_readiness(d)
        tr0 = tr[0] if isinstance(tr, list) and tr else (tr if isinstance(tr, dict) else {})
        snap["training_readiness"] = {"score": tr0.get("score"), "level": tr0.get("level"), "feedback": tr0.get("feedbackShort")}
        snap["recovery_time"] = {"minutes": tr0.get("recoveryTime")}
    except Exception as e:
        snap["training_readiness"] = {"error": str(e)}
        snap["recovery_time"] = {"error": str(e)}

    # training_status (acute/chronic/ACWR embebidos en acuteTrainingLoadDTO)
    try:
        ts = gc.get_training_status(d)
        latest = _first_value(((ts or {}).get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}) or {}
        load = latest.get("acuteTrainingLoadDTO") or {}
        snap["training_status"] = {
            "status": latest.get("trainingStatus"),
            "phrase": latest.get("trainingStatusFeedbackPhrase"),
            "acute_load": load.get("dailyTrainingLoadAcute"),
            "chronic_load": load.get("dailyTrainingLoadChronic"),
            "acwr": load.get("dailyAcuteChronicWorkloadRatio"),
            "acwr_status": load.get("acwrStatus"),
        }
    except Exception as e:
        snap["training_status"] = {"error": str(e)}

    # hrv_status
    try:
        hrv = (gc.get_hrv_data(d) or {}).get("hrvSummary") or {}
        snap["hrv_status"] = {"status": hrv.get("status"), "last_night_avg": hrv.get("lastNightAvg"), "weekly_avg": hrv.get("weeklyAvg")}
    except Exception as e:
        snap["hrv_status"] = {"error": str(e)}

    # rhr
    try:
        rhr = gc.get_rhr_day(d)
        vals = (((rhr or {}).get("allMetrics") or {}).get("metricsMap") or {}).get("WELLNESS_RESTING_HEART_RATE") or []
        snap["rhr"] = {"bpm": (vals[0].get("value") if vals else None)}
    except Exception as e:
        snap["rhr"] = {"error": str(e)}

    # body_battery
    try:
        bb = gc.get_body_battery(d, d)
        bb0 = bb[0] if isinstance(bb, list) and bb else {}
        levels = [p[1] for p in (bb0.get("bodyBatteryValuesArray") or []) if isinstance(p, list) and len(p) > 1 and p[1] is not None]
        snap["body_battery"] = {
            "charged": bb0.get("charged"), "drained": bb0.get("drained"),
            "current": (levels[-1] if levels else None), "high": (max(levels) if levels else None), "low": (min(levels) if levels else None),
        }
    except Exception as e:
        snap["body_battery"] = {"error": str(e)}

    # last_activity
    try:
        la = gc.get_last_activity() or {}
        snap["last_activity"] = {
            "type": (la.get("activityType") or {}).get("typeKey"),
            "name": la.get("activityName"), "start": la.get("startTimeLocal"),
            "distance_km": (round(la["distance"] / 1000, 2) if la.get("distance") else None),
            "duration_min": (round(la["duration"] / 60, 1) if la.get("duration") else None),
            "avg_hr": la.get("averageHR"), "max_hr": la.get("maxHR"),
        }
    except Exception as e:
        snap["last_activity"] = {"error": str(e)}

    return snap


def _months_between(start: str, end: str) -> list[tuple]:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if e < s:
        s, e = e, s
    out, y, m = [], s.year, s.month
    while (y, m) <= (e.year, e.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def scheduled_workouts(gc, start: str, end: str) -> list:
    """Workouts programados en el calendario de Garmin entre start y end (YYYY-MM-DD).
    Itera por mes (la API es por year/month) y mapea a un schema plano. `completed`
    es best-effort: True si hubo alguna actividad registrada ese dia. Las activities
    del calendario no exponen el deporte de forma fiable (sportTypeKey viene null),
    asi que el match es solo por fecha; sin llamadas extra."""
    items = []
    for (y, m) in _months_between(start, end):
        try:
            items.extend((gc.get_scheduled_workouts(y, m) or {}).get("calendarItems") or [])
        except Exception:
            pass
    activity_dates = {it.get("date") for it in items if it.get("itemType") == "activity" and it.get("date")}
    out, seen = [], set()
    for it in items:
        if it.get("itemType") != "workout":
            continue
        d = it.get("date")
        if not d or not (start <= d <= end) or it.get("id") in seen:
            continue
        seen.add(it.get("id"))
        out.append({
            "scheduled_workout_id": it.get("id"),
            "workout_id": it.get("workoutId"),
            "workout_name": it.get("title"),
            "date": d,
            "sport_type": it.get("sportTypeKey"),
            "estimated_duration_secs": it.get("duration"),
            "completed": d in activity_dates,
        })
    out.sort(key=lambda w: (w["date"], w["scheduled_workout_id"] or 0))
    return out


# ─── Definición de herramientas ───────────────────────────────────────────────

TOOLS: list[Tool] = [

    # ── 1. PERFIL Y USUARIO ──────────────────────────────────────────────────
    Tool(name="get_user_profile", description="Perfil social del usuario: nombre, ubicación, imagen, preferencias.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_user_settings", description="Configuración del usuario: sistema de unidades, formato de hora/fecha, horario de sueño, zonas de FC.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_user_summary", description="Resumen diario completo del usuario para una fecha dada.", inputSchema={"type": "object", "properties": {"date": {"type": "string", "description": "Fecha YYYY-MM-DD (por defecto hoy)"}}}),
    Tool(name="get_personal_records", description="Récords personales: carrera más larga, 5K, 10K, media maratón, maratón más rápidos.", inputSchema={"type": "object", "properties": {}}),

    # ── 2. SALUD DIARIA ───────────────────────────────────────────────────────
    Tool(name="get_stats", description="Estadísticas del día: pasos, calorías, distancia, pisos, minutos activos, FC.", inputSchema={"type": "object", "properties": {"date": {"type": "string", "description": "Fecha YYYY-MM-DD (por defecto hoy)"}}}),
    Tool(name="get_steps", description="Pasos del día.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_heart_rates", description="FC del día: en reposo, máxima, mínima, series temporales.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_resting_heart_rate", description="FC en reposo para una fecha.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_stress_data", description="Niveles de estrés del día: puntuación, tiempo en reposo/bajo/medio/alto.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_body_battery", description="Body Battery: cargado, drenado, máximo, mínimo.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_respiration_data", description="Frecuencia respiratoria del día.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_spo2_data", description="Saturación de oxígeno en sangre (SpO2) del día.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_intensity_minutes_data", description="Minutos de intensidad moderada y vigorosa del día.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_floors", description="Pisos subidos durante el día.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),

    # ── 3. SUEÑO ─────────────────────────────────────────────────────────────
    Tool(name="get_sleep_data", description="Datos de sueño de una noche: duración, fases (profundo, ligero, REM), puntuación, hora de acostarse/levantarse.", inputSchema={"type": "object", "properties": {"date": {"type": "string", "description": "Fecha de inicio del sueño (noche anterior)"}}}),

    # ── 4. MÉTRICAS AVANZADAS ─────────────────────────────────────────────────
    Tool(name="get_hrv_data", description="Variabilidad de FC (HRV) nocturna: estado, valores, tendencia de 5 días.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_training_readiness", description="Training Readiness (0-100): combinación de sueño, recuperación, carga y HRV.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_training_status", description="Training Status (productivo, manteniendo, desentrenando, sobrecargado, etc.). Incluye Acute/Chronic Load y ACWR en mostRecentTrainingStatus.latestTrainingStatusData.<userId>.acuteTrainingLoadDTO.", inputSchema={"type": "object", "properties": {"date": {"type": "string", "description": "Fecha YYYY-MM-DD (default: hoy)"}}}),
    Tool(name="get_morning_training_readiness", description="Training Readiness matutino detallado: nivel, score y factores (sueño, recovery time, ACWR, HRV, stress) con su feedback.", inputSchema={"type": "object", "properties": {"date": {"type": "string", "description": "Fecha YYYY-MM-DD (default: hoy)"}}}),
    Tool(name="get_running_tolerance", description="Running Tolerance: tolerancia/carga de carrera en un rango de fechas, agregada por semana o dia.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}, "aggregation": {"type": "string", "description": "weekly (default) o daily"}}}),
    Tool(name="get_athlete_snapshot", description="Check-in diario en UNA sola llamada: training_readiness, training_status (con ACWR y acute/chronic load), recovery_time, hrv_status, rhr, body_battery y last_activity. Reemplaza ~7 llamadas del workflow diario.", inputSchema={"type": "object", "properties": {"date": {"type": "string", "description": "Fecha YYYY-MM-DD (default: hoy)"}}}),
    Tool(name="get_vo2max", description="Estimación de VO2 Max para carrera y ciclismo.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="get_lactate_threshold", description="Umbral de lactato: FC y ritmo al umbral.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_race_predictions", description="Predicciones de tiempo en carrera: 5K, 10K, media maratón, maratón.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_fitness_age", description="Edad de forma física estimada por Garmin.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_endurance_score", description="Puntuación de resistencia aeróbica.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_hill_score", description="Puntuación de capacidad en subidas.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_cycling_ftp", description="FTP (Functional Threshold Power) de ciclismo.", inputSchema={"type": "object", "properties": {}}),

    # ── 5. TENDENCIAS HISTÓRICAS ──────────────────────────────────────────────
    Tool(name="get_steps_data_range", description="Pasos diarios en un rango de fechas.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_weekly_steps", description="Pasos semanales agregados.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_weekly_stress", description="Estrés semanal agregado.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_weekly_intensity_minutes", description="Minutos de intensidad semanales.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_heart_rate_range", description="FC en reposo diaria en un rango de fechas.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_hrv_range", description="HRV diario en un rango de fechas.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_sleep_data_range", description="Datos de sueño diarios en un rango de fechas.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_stress_range", description="Estrés diario en un rango de fechas.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_body_battery_range", description="Body Battery diario en un rango de fechas.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),

    # ── 6. ACTIVIDADES ────────────────────────────────────────────────────────
    Tool(name="get_activities", description="Lista de actividades recientes con paginación.", inputSchema={"type": "object", "properties": {"start": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 20}}}),
    Tool(name="get_activities_by_date", description="Actividades en un rango de fechas, opcionalmente filtradas por tipo (running, cycling, lap_swimming, strength_training, etc.).", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}, "activity_type": {"type": "string", "description": "Tipo de actividad (opcional)"}}, "required": ["start_date", "end_date"]}),
    Tool(name="get_last_activity", description="Última actividad registrada.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_activity_details", description="Detalles completos de una actividad: métricas, zonas, splits, clima.", inputSchema={"type": "object", "properties": {"activity_id": {"type": "integer"}}, "required": ["activity_id"]}),
    Tool(name="get_activity_hr_zones", description="Tiempo en cada zona de FC para una actividad.", inputSchema={"type": "object", "properties": {"activity_id": {"type": "integer"}}, "required": ["activity_id"]}),
    Tool(name="get_activity_splits", description="Splits por km/milla de una actividad.", inputSchema={"type": "object", "properties": {"activity_id": {"type": "integer"}}, "required": ["activity_id"]}),
    Tool(name="get_activity_weather", description="Condiciones climáticas durante una actividad.", inputSchema={"type": "object", "properties": {"activity_id": {"type": "integer"}}, "required": ["activity_id"]}),
    Tool(name="get_activity_exercise_sets", description="Series de ejercicio de una sesión de fuerza: repeticiones, peso, duración.", inputSchema={"type": "object", "properties": {"activity_id": {"type": "integer"}}, "required": ["activity_id"]}),
    Tool(name="get_progress_summary", description="Resumen de progreso en un rango de fechas. metric: distance (default), duration, elevationGain, etc.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}, "metric": {"type": "string", "default": "distance"}}, "required": ["start_date", "end_date"]}),

    # ── 7. ENTRENAMIENTOS (CREAR / PROGRAMAR / ELIMINAR) ─────────────────────
    Tool(
        name="add_workout",
        description=(
            "Crea un entrenamiento estructurado en Garmin Connect. "
            "Soporta running, cycling, swimming, strength_training, multi_sport (triatlón/brick). "
            "Para grupos de repetición usa step_type='repeat' con 'iterations' y 'steps' anidados. "
            "Para multideporte usa 'segments' en vez de 'steps'. "
            "Pace en m/s (ej. 5:00/km = 3.333 m/s). Velocidad ciclismo en m/s (ej. 30km/h = 8.333 m/s)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workout_name": {"type": "string"},
                "sport_type": {"type": "string", "description": "running, cycling, swimming, strength_training, cardio, yoga, multi_sport, other"},
                "estimated_duration_secs": {"type": "integer"},
                "description": {"type": "string"},
                "pool_length": {"type": "number", "description": "Longitud de piscina en metros (25 o 50). Solo para swimming."},
                "steps": {
                    "type": "array",
                    "description": "Pasos para deporte único. Para multideporte usar 'segments'.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_type":   {"type": "string", "description": "warmup, cooldown, interval, recovery, rest, main, repeat"},
                            "duration_type": {"type": "string", "description": "distance (m), time (s), lap_button, reps, fixed_rest"},
                            "duration_value": {"type": "number", "description": "Metros, segundos o repeticiones según duration_type"},
                            "target_type": {"type": "string", "description": "no_target, heart_rate, pace, speed, power, cadence"},
                            "target_value_low":  {"type": "number", "description": "FC mínima (ppm), pace (m/s) o velocidad (m/s)"},
                            "target_value_high": {"type": "number", "description": "FC máxima, pace o velocidad máxima"},
                            "zone_number": {"type": "integer", "description": "Zona numerada 1-5 (alternativa a valores absolutos)"},
                            "secondary_target_type": {"type": "string", "description": "Target secundario: heart_rate, power, speed (para ciclismo)"},
                            "secondary_target_value_low":  {"type": "number"},
                            "secondary_target_value_high": {"type": "number"},
                            "secondary_zone_number": {"type": "integer"},
                            "stroke_type": {"type": "string", "description": "Natación: free, back, breast, fly, medley, any"},
                            "equipment_type": {"type": "string", "description": "Natación: none, fins, kickboard, paddles, buoy (pull_buoy), snorkel"},
                            "drill_type": {"type": "string", "description": "Natación: kick, pull, drill"},
                            "category":      {"type": "string", "description": "Fuerza: PULL_UP, SQUAT, DEADLIFT, BENCH_PRESS, CARDIO, etc."},
                            "exercise_name": {"type": "string", "description": "Fuerza: WEIGHTED_PULL_UP, WEIGHTED_SQUAT, DEADLIFT, etc."},
                            "weight_kg":     {"type": "number", "description": "Fuerza: peso en kg"},
                            "description":   {"type": "string"},
                            "iterations":    {"type": "integer", "description": "Solo 'repeat': número de series"},
                            "skip_last_rest":{"type": "boolean", "description": "Solo 'repeat': omite descanso final"},
                            "steps":         {"type": "array",   "description": "Solo 'repeat': pasos hijos"}
                        }
                    }
                },
                "segments": {
                    "type": "array",
                    "description": "Para multideporte/triatlón: lista de segmentos, uno por disciplina.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sport_type":     {"type": "string", "description": "running, cycling, swimming"},
                            "segment_order":  {"type": "integer"},
                            "steps":          {"type": "array", "description": "Pasos del segmento (igual estructura que steps raíz)"}
                        }
                    }
                }
            },
            "required": ["workout_name", "sport_type"]
        }
    ),
    Tool(
        name="schedule_workout",
        description="Programa un entrenamiento existente en el calendario de Garmin Connect.",
        inputSchema={
            "type": "object",
            "properties": {
                "workout_id": {"type": "integer", "description": "ID del entrenamiento"},
                "scheduled_date": {"type": "string", "description": "Fecha YYYY-MM-DD"}
            },
            "required": ["workout_id", "scheduled_date"]
        }
    ),
    Tool(
        name="unschedule_workout",
        description="Quita un entrenamiento programado del calendario. Usa el scheduled_workout_id (de get_scheduled_workouts o de la respuesta de schedule_workout), NO el workout_id.",
        inputSchema={"type": "object", "properties": {"scheduled_workout_id": {"type": "integer", "description": "ID de la programacion, no el del workout"}}, "required": ["scheduled_workout_id"]}
    ),
    Tool(
        name="delete_workout",
        description="Elimina un entrenamiento de Garmin Connect.",
        inputSchema={"type": "object", "properties": {"workout_id": {"type": "integer"}}, "required": ["workout_id"]}
    ),
    Tool(name="get_workouts", description="Lista todos los entrenamientos guardados en Garmin Connect.", inputSchema={"type": "object", "properties": {"start": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 20}}}),
    Tool(name="get_scheduled_workouts", description="Workouts programados en el calendario entre start_date y end_date (YYYY-MM-DD). Devuelve por cada uno: scheduled_workout_id (el que pide unschedule_workout), workout_id, workout_name, date, sport_type, estimated_duration_secs y completed (best-effort).", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}, "required": ["start_date", "end_date"]}),
    Tool(name="get_workout", description="Obtiene los detalles de un entrenamiento específico.", inputSchema={"type": "object", "properties": {"workout_id": {"type": "integer"}}, "required": ["workout_id"]}),

    # ── 8. COMPOSICIÓN CORPORAL Y PESO ────────────────────────────────────────
    Tool(name="get_body_composition", description="Composición corporal en un rango: peso, IMC, % grasa, masa muscular.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="get_latest_weight", description="Último registro de peso.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_weigh_ins", description="Registros de peso en un rango de fechas.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),
    Tool(name="add_weigh_in", description="Registra un nuevo peso.", inputSchema={"type": "object", "properties": {"weight_kg": {"type": "number", "description": "Peso en kilogramos"}, "date": {"type": "string", "description": "Fecha YYYY-MM-DD (por defecto hoy)"}}, "required": ["weight_kg"]}),
    Tool(name="get_blood_pressure", description="Lecturas de presión arterial en un rango de fechas.", inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}),

    # ── 9. METAS Y LOGROS ─────────────────────────────────────────────────────
    Tool(name="get_goals", description="Metas activas: pasos, actividad, peso y su progreso.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_earned_badges", description="Insignias ganadas y logros.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_available_badges", description="Insignias disponibles que se pueden ganar.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_badge_challenges", description="Desafíos de insignias completados.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_adhoc_challenges", description="Desafíos ad-hoc históricos.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_inprogress_virtual_challenges", description="Desafíos virtuales en curso.", inputSchema={"type": "object", "properties": {}}),

    # ── 10. DISPOSITIVOS ──────────────────────────────────────────────────────
    Tool(name="get_devices", description="Dispositivos Garmin registrados: modelo, firmware, última sincronización.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_device_last_used", description="Último dispositivo Garmin utilizado.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_primary_training_device", description="Dispositivo principal de entrenamiento.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_device_settings", description="Configuración de un dispositivo específico.", inputSchema={"type": "object", "properties": {"device_id": {"type": "integer"}}, "required": ["device_id"]}),

    # ── 11. EQUIPAMIENTO ──────────────────────────────────────────────────────
    Tool(name="get_gear", description="Todo el equipamiento registrado: zapatillas, bicicletas y otros.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_gear_stats", description="Estadísticas de uso de un equipo (distancia total, actividades).", inputSchema={"type": "object", "properties": {"gear_uuid": {"type": "string"}}, "required": ["gear_uuid"]}),

    # ── 12. HIDRATACIÓN Y BIENESTAR ───────────────────────────────────────────
    Tool(name="get_hydration_data", description="Ingesta de agua del día.", inputSchema={"type": "object", "properties": {"date": {"type": "string"}}}),
    Tool(name="add_hydration", description="Registra ingesta de agua.", inputSchema={"type": "object", "properties": {"value_in_ml": {"type": "integer", "description": "Mililitros de agua"}, "date": {"type": "string"}}, "required": ["value_in_ml"]}),

    # ── 13. PLANES DE ENTRENAMIENTO ───────────────────────────────────────────
    Tool(name="get_training_plans", description="Planes de entrenamiento disponibles (Garmin Coach y personalizados).", inputSchema={"type": "object", "properties": {}}),

    # ── 14. CATÁLOGO DE EJERCICIOS DE FUERZA ──────────────────────────────────
    Tool(
        name="search_strength_exercises",
        description=(
            "Busca ejercicios de fuerza en el catálogo de Garmin. Soporta español "
            "(ej. 'press banca', 'dominadas lastradas', 'sentadilla con barra') "
            "e inglés. Devuelve category y exercise_name exactos para usar en add_workout. "
            "SIEMPRE usar antes de crear un entreno de fuerza para validar nombres."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto a buscar (es/en)"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 25},
                "category": {"type": "string", "description": "Filtrar por categoría exacta (opcional)"}
            },
            "required": ["query"]
        }
    ),
    Tool(
        name="list_strength_categories",
        description=(
            "Lista las 39 categorías de ejercicios de fuerza disponibles "
            "(SQUAT, DEADLIFT, BENCH_PRESS, PULL_UP, PLANK, LUNGE, etc.) "
            "con la cantidad de ejercicios en cada una."
        ),
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="list_strength_muscles",
        description=(
            "Lista los 17 grupos musculares con la cantidad de ejercicios que los trabajan "
            "(CHEST, QUADS, GLUTES, HAMSTRINGS, BICEPS, etc.)."
        ),
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="get_strength_exercises_by_muscle",
        description=(
            "Devuelve ejercicios que trabajan un grupo muscular específico. "
            "Útil para diseñar sesiones de fuerza balanceadas o encontrar alternativas."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "muscle": {"type": "string", "description": "Músculo en mayúsculas (CHEST, QUADS, GLUTES, etc.)"},
                "primary_only": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50}
            },
            "required": ["muscle"]
        }
    ),
]

TOOL_NAMES = {t.name for t in TOOLS}

# ─── Manejador de llamadas ─────────────────────────────────────────────────────

async def handle_tool(name: str, args: dict) -> list[TextContent]:
    try:
        gc = get_client()
        d = args.get("date") or today()
        sd = args.get("start_date") or (date.today() - timedelta(days=30)).isoformat()
        ed = args.get("end_date") or today()

        # ── PERFIL ────────────────────────────────────────────────────────────
        if name == "get_user_profile":
            return ok(gc.get_user_profile())
        if name == "get_user_settings":
            return ok(gc.get_user_settings())
        if name == "get_user_summary":
            return ok(gc.get_user_summary(d))
        if name == "get_personal_records":
            return ok(gc.get_personal_record())

        # ── SALUD DIARIA ──────────────────────────────────────────────────────
        if name == "get_stats":
            return ok(gc.get_stats(d))
        if name == "get_steps":
            return ok(gc.get_steps_data(d))
        if name == "get_heart_rates":
            return ok(gc.get_heart_rates(d))
        if name == "get_resting_heart_rate":
            return ok(gc.get_rhr_day(d))
        if name == "get_stress_data":
            return ok(gc.get_stress_data(d))
        if name == "get_body_battery":
            return ok(gc.get_body_battery(sd, ed))
        if name == "get_respiration_data":
            return ok(gc.get_respiration_data(d))
        if name == "get_spo2_data":
            return ok(gc.get_spo2_data(d))
        if name == "get_intensity_minutes_data":
            return ok(gc.get_intensity_minutes_data(d))
        if name == "get_floors":
            return ok(gc.get_floors(d))

        # ── SUEÑO ─────────────────────────────────────────────────────────────
        if name == "get_sleep_data":
            return ok(gc.get_sleep_data(d))

        # ── MÉTRICAS AVANZADAS ────────────────────────────────────────────────
        if name == "get_hrv_data":
            return ok(gc.get_hrv_data(d))
        if name == "get_training_readiness":
            return ok(gc.get_training_readiness(d))
        if name == "get_training_status":
            return ok(gc.get_training_status(d))
        if name == "get_morning_training_readiness":
            return ok(gc.get_morning_training_readiness(d))
        if name == "get_running_tolerance":
            return ok(gc.get_running_tolerance(sd, ed, args.get("aggregation", "weekly")))
        if name == "get_athlete_snapshot":
            return ok(athlete_snapshot(gc, d))
        if name == "get_vo2max":
            return ok(gc.get_max_metrics(d))
        if name == "get_lactate_threshold":
            return ok(gc.get_lactate_threshold())
        if name == "get_race_predictions":
            return ok(gc.get_race_predictions())
        if name == "get_fitness_age":
            return ok(gc.get_fitness_age())
        if name == "get_endurance_score":
            return ok(gc.get_endurance_score())
        if name == "get_hill_score":
            return ok(gc.get_hill_score())
        if name == "get_cycling_ftp":
            return ok(gc.get_cycling_ftp())

        # ── TENDENCIAS ────────────────────────────────────────────────────────
        if name == "get_steps_data_range":
            return ok(gc.get_daily_steps(sd, ed))
        if name == "get_weekly_steps":
            return ok(gc.get_weekly_steps(sd, ed))
        if name == "get_weekly_stress":
            return ok(gc.get_weekly_stress(sd, ed))
        if name == "get_weekly_intensity_minutes":
            return ok(gc.get_weekly_intensity_minutes(sd, ed))
        if name == "get_heart_rate_range":
            return ok(collect_range(gc.get_rhr_day, sd, ed))
        if name == "get_hrv_range":
            return ok(collect_range(gc.get_hrv_data, sd, ed))
        if name == "get_sleep_data_range":
            return ok(collect_range(gc.get_sleep_data, sd, ed))
        if name == "get_stress_range":
            return ok(collect_range(gc.get_stress_data, sd, ed))
        if name == "get_body_battery_range":
            return ok(gc.get_body_battery(sd, ed))

        # ── ACTIVIDADES ───────────────────────────────────────────────────────
        if name == "get_activities":
            return ok(gc.get_activities(args.get("start", 0), args.get("limit", 20)))
        if name == "get_activities_by_date":
            return ok(gc.get_activities_by_date(sd, ed, args.get("activity_type")))
        if name == "get_last_activity":
            return ok(gc.get_last_activity())
        if name == "get_activity_details":
            return ok(gc.get_activity_details(args["activity_id"]))
        if name == "get_activity_hr_zones":
            return ok(gc.get_activity_hr_in_timezones(args["activity_id"]))
        if name == "get_activity_splits":
            return ok(gc.get_activity_splits(args["activity_id"]))
        if name == "get_activity_weather":
            return ok(gc.get_activity_weather(args["activity_id"]))
        if name == "get_activity_exercise_sets":
            return ok(gc.get_activity_exercise_sets(args["activity_id"]))
        if name == "get_progress_summary":
            return ok(gc.get_progress_summary_between_dates(sd, ed, args.get("metric", "distance")))

        # ── ENTRENAMIENTOS ────────────────────────────────────────────────────
        if name == "get_workouts":
            return ok(gc.get_workouts(args.get("start", 0), args.get("limit", 20)))
        if name == "get_workout":
            return ok(gc.get_workout_by_id(args["workout_id"]))
        if name == "delete_workout":
            return ok(gc.delete_workout(args["workout_id"]))
        if name == "schedule_workout":
            return ok(gc.schedule_workout(args["workout_id"], args["scheduled_date"]))
        if name == "unschedule_workout":
            return ok(gc.unschedule_workout(args["scheduled_workout_id"]))
        if name == "get_scheduled_workouts":
            return ok(scheduled_workouts(gc, sd, ed))

        if name == "add_workout":
            # ── Mapas verificados con payloads reales de Garmin Connect ──────────
            SPORT_MAP = {
                "running":           (1,  "running",           1),
                "cycling":           (2,  "cycling",           2),
                "swimming":          (4,  "swimming",          3),  # 4 = pool swimming
                "strength_training": (5,  "strength_training", 5),
                "cardio":            (15, "cardio_training",   15),
                "yoga":              (87, "yoga",              87),
                "multi_sport":       (10, "multi_sport",       4),
                "other":             (165,"other",             165),
            }
            STEP_TYPE_MAP = {
                "warmup":   {"stepTypeId": 1, "stepTypeKey": "warmup",   "displayOrder": 1},
                "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
                "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
                "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
                "rest":     {"stepTypeId": 5, "stepTypeKey": "rest",     "displayOrder": 5},
                "repeat":   {"stepTypeId": 6, "stepTypeKey": "repeat",   "displayOrder": 6},
                "main":     {"stepTypeId": 8, "stepTypeKey": "main",     "displayOrder": 8},
            }
            # IDs verificados contra payloads reales
            DUR_MAP = {
                "lap_button": {"conditionTypeId": 1, "conditionTypeKey": "lap.button",  "displayOrder": 1,  "displayable": True},
                "time":       {"conditionTypeId": 2, "conditionTypeKey": "time",        "displayOrder": 2,  "displayable": True},
                "distance":   {"conditionTypeId": 3, "conditionTypeKey": "distance",    "displayOrder": 3,  "displayable": True},
                "iterations": {"conditionTypeId": 7, "conditionTypeKey": "iterations",  "displayOrder": 7,  "displayable": False},
                "fixed_rest": {"conditionTypeId": 8, "conditionTypeKey": "fixed.rest",  "displayOrder": 8,  "displayable": True},
                "reps":       {"conditionTypeId": 10,"conditionTypeKey": "reps",        "displayOrder": 10, "displayable": True},
                "open":       {"conditionTypeId": 1, "conditionTypeKey": "lap.button",  "displayOrder": 1,  "displayable": True},
            }
            TGT_MAP = {
                "no_target":  {"workoutTargetTypeId": 1,  "workoutTargetTypeKey": "no.target",       "displayOrder": 1},
                "power":      {"workoutTargetTypeId": 2,  "workoutTargetTypeKey": "power.zone",      "displayOrder": 2},
                "cadence":    {"workoutTargetTypeId": 3,  "workoutTargetTypeKey": "cadence",         "displayOrder": 3},
                "heart_rate": {"workoutTargetTypeId": 4,  "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
                "speed":      {"workoutTargetTypeId": 5,  "workoutTargetTypeKey": "speed.zone",      "displayOrder": 5},
                "pace":       {"workoutTargetTypeId": 6,  "workoutTargetTypeKey": "pace.zone",       "displayOrder": 6},
            }
            STROKE_MAP = {
                "any":        {"strokeTypeId": 1, "strokeTypeKey": "any_stroke", "displayOrder": 1},
                "back":       {"strokeTypeId": 2, "strokeTypeKey": "back",       "displayOrder": 2},
                "breast":     {"strokeTypeId": 3, "strokeTypeKey": "breast",     "displayOrder": 3},
                "medley":     {"strokeTypeId": 4, "strokeTypeKey": "medley",     "displayOrder": 4},
                "fly":        {"strokeTypeId": 5, "strokeTypeKey": "fly",        "displayOrder": 5},
                "free":       {"strokeTypeId": 6, "strokeTypeKey": "free",       "displayOrder": 6},
            }
            # IDs reales de Garmin (/workout-service/workout/types -> workoutEquipmentTypes)
            EQUIPMENT_MAP = {
                "none":      {"equipmentTypeId": 0, "equipmentTypeKey": None,        "displayOrder": 0},
                "fins":      {"equipmentTypeId": 1, "equipmentTypeKey": "fins",      "displayOrder": 1},
                "kickboard": {"equipmentTypeId": 2, "equipmentTypeKey": "kickboard", "displayOrder": 2},
                "paddles":   {"equipmentTypeId": 3, "equipmentTypeKey": "paddles",   "displayOrder": 3},
                "buoy":      {"equipmentTypeId": 4, "equipmentTypeKey": "pull_buoy", "displayOrder": 4},
                "pull_buoy": {"equipmentTypeId": 4, "equipmentTypeKey": "pull_buoy", "displayOrder": 4},
                "snorkel":   {"equipmentTypeId": 5, "equipmentTypeKey": "snorkel",   "displayOrder": 5},
            }
            DRILL_MAP = {
                "kick":   {"drillTypeId": 1, "drillTypeKey": "kick",   "displayOrder": 1},
                "pull":   {"drillTypeId": 2, "drillTypeKey": "pull",   "displayOrder": 2},
                "drill":  {"drillTypeId": 3, "drillTypeKey": "drill",  "displayOrder": 3},
            }

            sport_key_input = args.get("sport_type", "other").lower()
            sport_id, sport_key_str, sport_disp = SPORT_MAP.get(sport_key_input, SPORT_MAP["other"])
            sport_type_obj = {"sportTypeId": sport_id, "sportTypeKey": sport_key_str, "displayOrder": sport_disp}

            step_id_counter = {"v": 0}
            step_order_counter = {"v": 0}

            def get_pref_unit(dt, seg_sport):
                """Devuelve preferredEndConditionUnit según deporte y tipo de duración."""
                if dt != "distance":
                    return None
                if seg_sport in ("swimming",):
                    return {"unitId": 1, "unitKey": "meter", "factor": 100.0}
                return {"unitId": 2, "unitKey": "kilometer", "factor": 100000.0}

            def build_executable_step(s, seg_sport="running"):
                step_id_counter["v"] += 1
                step_order_counter["v"] += 1

                st = s.get("step_type", "interval")
                dt = s.get("duration_type", "distance")
                tt = s.get("target_type", "no_target")

                end_condition = dict(DUR_MAP.get(dt, DUR_MAP["distance"]))
                target_obj = dict(TGT_MAP.get(tt, TGT_MAP["no_target"]))

                step = {
                    "type": "ExecutableStepDTO",
                    "stepId": step_id_counter["v"],
                    "stepOrder": step_order_counter["v"],
                    "stepType": dict(STEP_TYPE_MAP.get(st, STEP_TYPE_MAP["interval"])),
                    "endCondition": end_condition,
                    "endConditionValue": float(s.get("duration_value", 0)),
                    "targetType": target_obj,
                    "strokeType": dict(STROKE_MAP.get(s.get("stroke_type", ""), STROKE_MAP.get("none", {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0}))),
                    "equipmentType": dict(EQUIPMENT_MAP.get(s.get("equipment_type", "none"), EQUIPMENT_MAP["none"])),
                }

                pref_unit = get_pref_unit(dt, seg_sport)
                if pref_unit:
                    step["preferredEndConditionUnit"] = pref_unit

                # FC / pace / speed como valores absolutos
                if s.get("target_value_low") is not None:
                    step["targetValueOne"] = s["target_value_low"]
                if s.get("target_value_high") is not None:
                    step["targetValueTwo"] = s["target_value_high"]
                # Zona numerada (1-5) para FC/potencia/velocidad
                if s.get("zone_number") is not None:
                    step["zoneNumber"] = str(s["zone_number"])

                # Target secundario (ciclismo: FC + potencia simultáneo)
                if s.get("secondary_target_type"):
                    st2 = s["secondary_target_type"]
                    step["secondaryTargetType"] = dict(TGT_MAP.get(st2, TGT_MAP["no_target"]))
                    if s.get("secondary_target_value_low") is not None:
                        step["secondaryTargetValueOne"] = s["secondary_target_value_low"]
                    if s.get("secondary_target_value_high") is not None:
                        step["secondaryTargetValueTwo"] = s["secondary_target_value_high"]
                    if s.get("secondary_zone_number") is not None:
                        step["secondaryZoneNumber"] = s["secondary_zone_number"]

                # Fuerza: ejercicio, reps, peso
                if s.get("category"):
                    step["category"] = s["category"].upper()
                if s.get("exercise_name"):
                    step["exerciseName"] = s["exercise_name"].upper()
                if s.get("weight_kg") is not None:
                    step["weightValue"] = float(s["weight_kg"])
                    step["weightUnit"] = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

                # Natación: drill type
                if s.get("drill_type"):
                    step["drillType"] = dict(DRILL_MAP.get(s["drill_type"], DRILL_MAP["kick"]))

                if s.get("description"):
                    step["description"] = s["description"]

                return step

            def build_repeat_group(g, seg_sport="running"):
                step_id_counter["v"] += 1
                step_order_counter["v"] += 1
                group_id = step_id_counter["v"]
                group_order = step_order_counter["v"]

                child_steps_def = g.get("steps", [])
                child_index = 1
                child_steps = []
                for cs in child_steps_def:
                    built = build_executable_step(cs, seg_sport)
                    built["childStepId"] = child_index
                    child_steps.append(built)
                    child_index += 1

                return {
                    "type": "RepeatGroupDTO",
                    "stepId": group_id,
                    "stepOrder": group_order,
                    "stepType": dict(STEP_TYPE_MAP["repeat"]),
                    "childStepId": 1,
                    "numberOfIterations": int(g.get("iterations", 1)),
                    "smartRepeat": False,
                    "endCondition": dict(DUR_MAP["iterations"]),
                    "endConditionValue": float(g.get("iterations", 1)),
                    "skipLastRestStep": bool(g.get("skip_last_rest", False)),
                    "workoutSteps": child_steps,
                }

            def build_segment_steps(steps_raw, seg_sport):
                result = []
                for s in steps_raw:
                    if s.get("step_type") == "repeat" or s.get("iterations") is not None:
                        result.append(build_repeat_group(s, seg_sport))
                    else:
                        result.append(build_executable_step(s, seg_sport))
                return result

            # ── Determinar si es multideporte o deporte único ─────────────────
            segments_raw = args.get("segments")  # Para multideporte: lista de segmentos
            steps_raw    = args.get("steps", []) # Para deporte único

            has_swimming = False
            workout_segments = []

            if segments_raw:
                # MULTIDEPORTE: cada segmento es un deporte distinto
                for seg in segments_raw:
                    seg_sport_key = seg.get("sport_type", "running").lower()
                    seg_sport_id, seg_sport_str, seg_sport_disp = SPORT_MAP.get(seg_sport_key, SPORT_MAP["running"])
                    if seg_sport_key == "swimming":
                        has_swimming = True
                    seg_steps = build_segment_steps(seg.get("steps", []), seg_sport_key)
                    seg_obj = {
                        "segmentOrder": seg.get("segment_order", len(workout_segments) + 1),
                        "sportType": {"sportTypeId": seg_sport_id, "sportTypeKey": seg_sport_str, "displayOrder": seg_sport_disp},
                        "workoutSteps": seg_steps,
                    }
                    workout_segments.append(seg_obj)
            else:
                # DEPORTE ÚNICO
                if sport_key_input == "swimming":
                    has_swimming = True
                seg_steps = build_segment_steps(steps_raw, sport_key_input)
                workout_segments.append({
                    "segmentOrder": 1,
                    "sportType": sport_type_obj,
                    "workoutSteps": seg_steps,
                })

            pool_length     = args.get("pool_length", 25.0) if has_swimming else None
            pool_length_unit = {"unitId": 1, "unitKey": "meter", "factor": 100.0} if has_swimming else None

            payload = {
                "sportType": sport_type_obj if not segments_raw else {"sportTypeId": 10, "sportTypeKey": "multi_sport", "displayOrder": 4},
                "subSportType": None,
                "workoutName": args["workout_name"],
                "description": args.get("description", ""),
                "estimatedDistanceUnit": {"unitKey": None},
                "workoutSegments": workout_segments,
                "avgTrainingSpeed": 0,
                "estimatedDurationInSecs": args.get("estimated_duration_secs", 0),
                "estimatedDistanceInMeters": 0,
                "estimateType": None,
                "isWheelchair": False,
                "isSessionTransitionEnabled": bool(segments_raw),
                "poolLength": pool_length,
                "poolLengthUnit": pool_length_unit,
            }
            return ok(gc.upload_workout(payload))

        # ── COMPOSICIÓN CORPORAL ──────────────────────────────────────────────
        if name == "get_body_composition":
            return ok(gc.get_body_composition(sd, ed))
        if name == "get_latest_weight":
            return ok(gc.get_latest_weight())
        if name == "get_weigh_ins":
            return ok(gc.get_weigh_ins(sd, ed))
        if name == "add_weigh_in":
            w = args["weight_kg"]
            return ok(gc.add_body_composition(
                args.get("date", today()),
                weight=w
            ))
        if name == "get_blood_pressure":
            return ok(gc.get_blood_pressure(sd, ed))

        # ── METAS Y LOGROS ────────────────────────────────────────────────────
        if name == "get_goals":
            return ok(gc.get_goals("active"))
        if name == "get_earned_badges":
            return ok(gc.get_earned_badges())
        if name == "get_available_badges":
            return ok(gc.get_available_badges())
        if name == "get_badge_challenges":
            return ok(gc.get_badge_challenges(0))
        if name == "get_adhoc_challenges":
            return ok(gc.get_adhoc_challenges(0, 20))
        if name == "get_inprogress_virtual_challenges":
            return ok(gc.get_inprogress_virtual_challenges(0, 20))

        # ── DISPOSITIVOS ──────────────────────────────────────────────────────
        if name == "get_devices":
            return ok(gc.get_devices())
        if name == "get_device_last_used":
            return ok(gc.get_device_last_used())
        if name == "get_primary_training_device":
            return ok(gc.get_primary_training_device())
        if name == "get_device_settings":
            return ok(gc.get_device_settings(args["device_id"]))

        # ── EQUIPAMIENTO ──────────────────────────────────────────────────────
        if name == "get_gear":
            return ok(gc.get_gear(gc.get_user_profile()["userName"]))
        if name == "get_gear_stats":
            return ok(gc.get_gear_stats(args["gear_uuid"]))

        # ── HIDRATACIÓN ───────────────────────────────────────────────────────
        if name == "get_hydration_data":
            return ok(gc.get_hydration_data(d))
        if name == "add_hydration":
            return ok(gc.add_hydration_data(args.get("date", today()), args["value_in_ml"]))

        # ── PLANES DE ENTRENAMIENTO ───────────────────────────────────────────
        if name == "get_training_plans":
            return ok(gc.get_training_plans())

        # ── CATÁLOGO DE EJERCICIOS DE FUERZA ─────────────────────────────────
        if name == "search_strength_exercises":
            results = sc_search(
                query=args["query"],
                limit=args.get("limit", 10),
                category=args.get("category"),
            )
            return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))]

        if name == "list_strength_categories":
            results = sc_categories()
            return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))]

        if name == "list_strength_muscles":
            results = sc_muscles()
            return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))]

        if name == "get_strength_exercises_by_muscle":
            results = sc_by_muscle(
                muscle=args["muscle"],
                primary_only=args.get("primary_only", True),
                limit=args.get("limit", 20),
            )
            return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))]

        return [TextContent(type="text", text=json.dumps({"error": f"Herramienta desconocida: {name}"}))]

    except Exception as e:
        return err(e)


# ─── Servidor MCP ─────────────────────────────────────────────────────────────

server = Server("garmin-python-mcp")


# Conjunto "core": tools criticas del coaching diario + ciclo de workouts.
# Quedan bajo el cap de tools por servidor del cliente, asi que estan SIEMPRE
# disponibles (no dependen de la loteria del indice dinamico).
CORE_TOOLS = {
    "get_athlete_snapshot", "get_training_readiness", "get_training_status",
    "get_morning_training_readiness", "get_hrv_data", "get_hrv_range",
    "get_sleep_data", "get_sleep_data_range", "get_resting_heart_rate",
    "get_stats", "get_body_battery",
    "get_activities", "get_activities_by_date", "get_activity_details",
    "get_activity_hr_zones", "get_progress_summary", "get_vo2max",
    "add_workout", "schedule_workout", "unschedule_workout", "delete_workout",
    "get_workouts", "get_workout", "get_scheduled_workouts", "search_strength_exercises",
    "get_strength_exercises_by_muscle",
}


def select_tools() -> list[Tool]:
    """Reparte el conector en 2 servers segun la env GARMIN_TOOLS, para que cada
    uno quepa bajo el cap de tools del cliente:
      core  -> solo CORE_TOOLS (coaching diario + workouts)  [~25, garantizadas]
      extra -> el resto
      (sin definir) -> todas (comportamiento por defecto, retrocompatible)"""
    sel = os.environ.get("GARMIN_TOOLS", "").strip().lower()
    if sel == "core":
        return [t for t in TOOLS if t.name in CORE_TOOLS]
    if sel == "extra":
        return [t for t in TOOLS if t.name not in CORE_TOOLS]
    return TOOLS


@server.list_tools()
async def list_tools() -> list[Tool]:
    return select_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    return await handle_tool(name, arguments)


async def _main_stdio():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _run_stdio():
    import asyncio
    asyncio.run(_main_stdio())


def _run_http():
    """
    Sirve el MCP por HTTP usando el transporte 'streamable-http' del SDK oficial.
    Compatible con los Custom Connectors de Claude.ai.

    Variables de entorno relevantes:
      PORT            puerto HTTP (por defecto 8000)
      MCP_AUTH_TOKEN  si se define, se exige header `Authorization: Bearer <token>` en /mcp
    """
    import contextlib
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    auth_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    async def health(_request):
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    import starlette.types as st_types

    class MCPEndpoint:
        """ASGI app que aplica auth y delega al StreamableHTTPSessionManager.
        Starlette detecta que esto es una clase (no una función) y la trata
        como ASGI directo, sin envolverla en request/response — necesario
        para que el streaming SSE del manager funcione."""

        async def __call__(self, scope: st_types.Scope, receive: st_types.Receive, send: st_types.Send) -> None:
            if auth_token:
                headers = dict(scope.get("headers") or [])
                provided = headers.get(b"authorization", b"").decode()
                if provided != f"Bearer {auth_token}":
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
            await session_manager.handle_request(scope, receive, send)

    mcp_endpoint = MCPEndpoint()

    app = Starlette(
        debug=False,
        routes=[
            Route("/mcp", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
            Route("/mcp/", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
            Route("/health", health, methods=["GET"]),
        ],
        lifespan=lifespan,
    )

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    if not EMAIL or not PASSWORD:
        raise SystemExit("ERROR: Define GARMIN_EMAIL y GARMIN_PASSWORD como variables de entorno.")

    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        _run_http()
    else:
        _run_stdio()
