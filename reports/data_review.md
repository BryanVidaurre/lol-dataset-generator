# Revision de datos recolectados

Generado (UTC): 2026-10-31T17:01:57.465525+00:00

- Partidas validas: **29,965**.
- Eventos revisados: **4,145,001**.
- Filas de hitos: **29,965**.
- Equipo 1 ganador: **47.62%**.
- IDs de evento unicos certificados: **True**.

## Distribucion por plataforma

| Plataforma | Partidas |
|---|---:|
| KR | 5,995 |
| NA1 | 5,995 |
| EUW1 | 5,994 |
| LA2 | 5,992 |
| BR1 | 5,989 |

## Distribucion por parche

| Parche | Partidas |
|---|---:|
| 16.20 | 8,972 |
| 16.21 | 8,557 |
| 16.19 | 4,762 |
| 16.18 | 2,898 |
| 16.17 | 2,173 |
| 16.16 | 1,066 |
| 16.15 | 579 |
| 16.14 | 376 |
| 16.13 | 137 |
| 16.11 | 79 |
| 16.8 | 67 |
| 16.10 | 56 |
| 16.6 | 52 |
| 16.12 | 49 |
| 16.7 | 43 |
| 16.9 | 31 |
| 16.1 | 24 |
| 16.3 | 17 |
| 16.4 | 12 |
| 16.5 | 7 |
| 16.2 | 6 |
| 15.24 | 2 |

## Duracion (minutos)

| Estadistico | Valor |
|---|---:|
| min | 1.00 |
| p05 | 15.28 |
| median | 26.82 |
| mean | 26.44 |
| p95 | 36.90 |
| max | 60.50 |

## Aspectos que requieren una decision de analisis

- 437 partidas duran menos de cinco minutos.
- 737 partidas tienen al menos un promedio de rango igual a cero (desconocido).
- Atakhan tiene capturas en 2 partidas.
- Diferencias en primeras capturas entre resumen y timeline (partidas): {'blood': 60}.
- Timestamps de hitos sin equipo atribuido: {'herald': 2083, 'horde': 29}.
- Timestamps con cero capturas finales del objetivo: {'herald': 2083, 'horde': 28}.
- Los valores cero de rango deben interpretarse como desconocidos, no como un rango inferior a Master.

## Calidad y consistencia

Los campos opcionales de eventos pueden estar vacios segun su tipo.

```json
{
  "summary_errors": {
    "duplicate_match_id": 0,
    "invalid_row_width": 0,
    "invalid_win_boolean": 0,
    "repeated_champion_within_match": 0,
    "invalid_objective_boolean": 0,
    "negative_objective_count": 0,
    "duplicate_milestone_match": 0,
    "milestone_patch_mismatch": 0,
    "invalid_milestone_team": 0,
    "milestone_team_without_time": 0,
    "milestone_outside_duration_margin": 0
  },
  "missing_summary_values": {},
  "event_errors": {
    "invalid_row_width": 0,
    "duplicate_adjacent_event": 0,
    "event_order_violation": 0,
    "invalid_event_id": 0,
    "invalid_minute": 0,
    "invalid_timestamp": 0,
    "invalid_team": 0,
    "invalid_actor_team": 0,
    "event_patch_mismatch": 0,
    "event_beyond_duration_margin": 0,
    "invalid_assisting_list": 0,
    "raw_type_mismatch": 0,
    "raw_timestamp_mismatch": 0,
    "raw_field_mismatch": 0,
    "raw_participant_mismatch": 0
  },
  "objective_count_disagreements": {
    "t2_champion_kills": 2199,
    "t1_champion_kills": 2215
  },
  "first_objective_flag_disagreements": {
    "t1_first_blood": 59,
    "t2_first_blood": 54
  },
  "milestone_event_disagreements": {},
  "sqlite": "Not run: physical verification is optional (--physical-integrity)",
  "foreign_key_errors": []
}
```

## Consideraciones para mineria

- Los objetivos finales y eventos posteriores al momento de prediccion no deben usarse como variables de entrada previas al juego.
- El rango promedio usa solo jugadores conocidos localmente y no representa necesariamente su rango historico.
- Una columna con objetivos ausentes normalizados a cero no distingue ausencia de ese objetivo en el parche de cero capturas.
- Los conteos y primeras capturas de Match-V5 y Timeline pueden diferir; revisar los desacuerdos antes de elegir la fuente para un modelo.
- Esta revision no modifica ni vuelve a descargar datos. El informe JSON contiene el detalle completo.

## Coincidencia de conteos CSV/SQLite

{"dataset": true, "events": true, "milestones": true}

## Casos para revisar

El archivo [summary_quality_flags.csv](summary_quality_flags.csv) identifica 3262 partidas con banderas de duracion, rango, falta de eventos, primera sangre o hitos sin equipo. Son indicadores para revisar, no errores confirmados ni una instruccion de eliminar esas filas. Las diferencias en conteos de asesinatos se reportan por equipo en el JSON.

Los 60 desacuerdos de primera sangre comienzan con un evento CHAMPION_KILL cuyo killerId es 0. Los 322 partidos sin eventos duran entre 60 y 147 segundos. La verificacion fisica de todas las paginas de SQLite no se completo; si se requiere, se puede ejecutar el auditor con --physical-integrity.
