# Revision de datos recolectados

Generado (UTC): 2026-11-04T01:41:08.857893+00:00

- Partidas validas: **48,000**.
- Eventos revisados: **6,634,160**.
- Filas de hitos: **48,000**.
- Equipo 1 ganador: **47.77%**.
- IDs de evento unicos certificados: **True**.

## Distribucion por plataforma

| Plataforma | Partidas |
|---|---:|
| KR | 10,108 |
| EUW1 | 9,971 |
| NA1 | 9,750 |
| BR1 | 9,167 |
| LA2 | 9,004 |

## Distribucion por parche

| Parche | Partidas |
|---|---:|
| 16.20 | 16,657 |
| 16.21 | 14,625 |
| 16.19 | 6,436 |
| 16.18 | 3,876 |
| 16.17 | 2,909 |
| 16.16 | 1,433 |
| 16.15 | 763 |
| 16.14 | 496 |
| 16.13 | 202 |
| 16.11 | 101 |
| 16.8 | 93 |
| 16.10 | 78 |
| 16.6 | 71 |
| 16.12 | 67 |
| 16.7 | 61 |
| 16.9 | 44 |
| 16.1 | 34 |
| 16.3 | 21 |
| 16.4 | 13 |
| 16.5 | 11 |
| 16.2 | 6 |
| 15.24 | 3 |

## Duracion (minutos)

| Estadistico | Valor |
|---|---:|
| min | 1.00 |
| p05 | 15.28 |
| median | 26.77 |
| mean | 26.43 |
| p95 | 36.97 |
| max | 60.50 |

## Aspectos que requieren una decision de analisis

- 677 partidas duran menos de cinco minutos.
- 1,279 partidas tienen al menos un promedio de rango igual a cero (desconocido).
- Atakhan tiene capturas en 3 partidas.
- Diferencias en primeras capturas entre resumen y timeline (partidas): {'blood': 90}.
- Timestamps de hitos sin equipo atribuido: {'herald': 3287, 'horde': 45}.
- Timestamps con cero capturas finales del objetivo: {'herald': 3287, 'horde': 44}.
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
    "t2_champion_kills": 3472,
    "t1_champion_kills": 3599
  },
  "first_objective_flag_disagreements": {
    "t1_first_blood": 87,
    "t2_first_blood": 83
  },
  "milestone_event_disagreements": {},
  "sqlite": "Not run: requested scope is the three CSV exports.",
  "foreign_key_errors": []
}
```

## Consideraciones para mineria

- Los objetivos finales y eventos posteriores al momento de prediccion no deben usarse como variables de entrada previas al juego.
- El rango promedio usa solo jugadores conocidos localmente y no representa necesariamente su rango historico.
- Una columna con objetivos ausentes normalizados a cero no distingue ausencia de ese objetivo en el parche de cero capturas.
- Los conteos y primeras capturas de Match-V5 y Timeline pueden diferir; revisar los desacuerdos antes de elegir la fuente para un modelo.
- Esta revision no modifica ni vuelve a descargar datos. El informe JSON contiene el detalle completo.
