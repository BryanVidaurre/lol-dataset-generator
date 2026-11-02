"""Read-only audit of exported samples. Stream events without loading the CSV."""
import argparse
import csv
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time


OBJECTIVES = ('blood', 'tower', 'dragon', 'herald', 'horde', 'baron', 'atakhan', 'inhibitor')
MONSTERS = {'DRAGON': 'dragon', 'RIFTHERALD': 'herald', 'RIFT_HERALD': 'herald',
            'HORDE': 'horde', 'VOID_GRUB': 'horde', 'VOID_GRUBS': 'horde',
            'BARON_NASHOR': 'baron', 'BARON': 'baron', 'ATAKHAN': 'atakhan'}
ROLES = ('top', 'jungle', 'middle', 'bottom', 'support')


def numeric(values):
    ordered = sorted(values)
    if not ordered:
        return {}
    def percentile(q):
        index = (len(ordered) - 1) * q
        left = int(index)
        return ordered[left] + (ordered[min(left + 1, len(ordered) - 1)] - ordered[left]) * (index - left)
    return {'min': ordered[0], 'p05': percentile(.05), 'median': statistics.median(ordered),
            'mean': statistics.fmean(ordered), 'p95': percentile(.95), 'max': ordered[-1]}


def read_rows(path, fields):
    with path.open(newline='', encoding='utf-8-sig') as source:
        reader = csv.DictReader(source)
        fields.extend(reader.fieldnames or [])
        yield from reader


def audit_summaries(folder):
    summaries, milestones = {}, {}
    header, milestone_header = [], []
    errors, missing, patches, regions, champions = (Counter() for _ in range(5))
    numbers = defaultdict(list)
    for row in read_rows(folder / 'dataset.csv', header):
        mid = row['match_id']
        errors['duplicate_match_id'] += mid in summaries
        summaries[mid] = row
        missing.update(key for key, value in row.items() if value in ('', None))
        errors['invalid_row_width'] += None in row
        errors['invalid_win_boolean'] += row['win_team1'] not in ('True', 'False')
        patches['.'.join(row['patch'].split('.')[:2])] += 1
        regions[mid.split('_')[0]] += 1
        duration = float(row['game_duration'])
        numbers['duration_minutes'].append(duration / 60)
        numbers['creation_ms'].append(int(row['game_creation']))
        champs = [row[f't{team}_{role}'] for team in (1, 2) for role in ROLES]
        errors['repeated_champion_within_match'] += len(set(champs)) != 10
        champions.update(champs)
        for prefix in ('t1', 't2'):
            numbers[prefix + '_avg_tier'].append(float(row[prefix + '_avg_tier']))
            for field, value in row.items():
                if field.startswith(prefix + '_first_'):
                    errors['invalid_objective_boolean'] += value not in ('True', 'False')
                if field.startswith(prefix + '_') and field.endswith('_kills'):
                    errors['negative_objective_count'] += int(value) < 0
    for row in read_rows(folder / 'milestones.csv', milestone_header):
        mid = row['match_id']
        errors['duplicate_milestone_match'] += mid in milestones
        milestones[mid] = row
        if mid not in summaries:
            errors['milestone_without_summary'] += 1
            continue
        errors['milestone_patch_mismatch'] += row['patch'] != summaries[mid]['patch']
        for name in OBJECTIVES:
            team, timestamp = row[f'first_{name}_team'], row[f'first_{name}_time']
            errors['invalid_milestone_team'] += team not in ('', '100', '200')
            errors['milestone_team_without_time'] += bool(team) and not timestamp
            if timestamp:
                errors['milestone_outside_duration_margin'] += not 0 <= int(timestamp) <= (
                    float(summaries[mid]['game_duration']) * 1000 + 120000)
    wins = sum(row['win_team1'] == 'True' for row in summaries.values())
    flag_disagreements, uncredited_times, zero_kills_with_time = Counter(), Counter(), Counter()
    for mid, milestone in milestones.items():
        if mid not in summaries:
            continue
        summary = summaries[mid]
        for name in OBJECTIVES:
            team = milestone[f'first_{name}_team']
            timestamp = milestone[f'first_{name}_time']
            if team and any((summary[f'{prefix}_first_{name}'] == 'True') != (team == team_id)
                            for prefix, team_id in (('t1', '100'), ('t2', '200'))):
                flag_disagreements[name] += 1
            if timestamp and not team:
                uncredited_times[name] += 1
            suffix = 'champion' if name == 'blood' else name
            if timestamp and int(summary[f't1_{suffix}_kills']) + int(summary[f't2_{suffix}_kills']) == 0:
                zero_kills_with_time[name] += 1
    dates = numbers.pop('creation_ms')
    report = {'matches': len(summaries), 'milestones': len(milestones),
              'columns': {'dataset': len(header), 'milestones': len(milestone_header)},
              'headers': {'dataset': header, 'milestones': milestone_header},
              'missing_summary_values': dict(missing), 'quality_errors': dict(errors),
              'missing_milestones': len(set(summaries) - set(milestones)),
              'first_flag_disagreement_matches': dict(flag_disagreements),
              'milestone_time_without_team': dict(uncredited_times),
              'milestone_time_with_zero_final_kills': dict(zero_kills_with_time),
              'patches': dict(patches.most_common()), 'regions': dict(regions.most_common()),
              'team1_wins': wins, 'team1_win_pct': wins * 100 / len(summaries),
              'numeric': {name: numeric(values) for name, values in numbers.items()},
              'unknown_tier_teams': {prefix: sum(value == 0 for value in numbers[prefix + '_avg_tier'])
                                     for prefix in ('t1', 't2')},
              'matches_with_unknown_tier': sum(float(row['t1_avg_tier']) == 0 or float(row['t2_avg_tier']) == 0
                                               for row in summaries.values()),
              'objective_present_matches': {name: sum(int(row[f't1_{name}_kills']) + int(row[f't2_{name}_kills']) > 0
                                                     for row in summaries.values())
                                            for name in OBJECTIVES if name != 'blood'},
              'matches_under_5_minutes': sum(value < 5 for value in numbers['duration_minutes']),
              'matches_over_60_minutes': sum(value > 60 for value in numbers['duration_minutes']),
              'game_dates_utc': [datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
                                 for value in (min(dates), max(dates))],
              'top_champions': dict(champions.most_common(15))}
    return summaries, milestones, report


def audit_events(folder, summaries, milestones):
    fields, errors, event_types, monsters, missing = [], Counter(), Counter(), Counter(), Counter()
    count_mismatches, first_mismatches, milestone_mismatches = Counter(), Counter(), Counter()
    examples = defaultdict(list)
    seen_matches = set()
    previous_key = None
    current_match, objective_counts, firsts = None, Counter(), {}
    explicit_blood = None
    rows = 0
    started = time.monotonic()

    def finish_match():
        if current_match not in summaries:
            return
        summary = summaries[current_match]
        stored = milestones.get(current_match)
        effective = dict(firsts)
        if explicit_blood is not None:
            effective['blood'] = explicit_blood
        for name in OBJECTIVES:
            first = effective.get(name)
            if stored:
                actual_time = stored[f'first_{name}_time']
                actual_team = stored[f'first_{name}_team']
                expected_time = str(first[0][0]) if first else ''
                expected_team = first[1] if first else ''
                if (actual_time, actual_team) != (expected_time, expected_team):
                    milestone_mismatches[name] += 1
                    if len(examples['milestone_' + name]) < 3:
                        examples['milestone_' + name].append(current_match)
            for team, prefix in (('100', 't1'), ('200', 't2')):
                suffix = 'champion' if name == 'blood' else name
                if int(summary[f'{prefix}_{suffix}_kills']) != objective_counts[name, team]:
                    count_mismatches[f'{prefix}_{suffix}_kills'] += 1
                if first and first[1] in ('100', '200'):
                    if (summary[f'{prefix}_first_{name}'] == 'True') != (first[1] == team):
                        first_mismatches[f'{prefix}_first_{name}'] += 1

    for row in read_rows(folder / 'events.csv', fields):
        rows += 1
        mid = row['match_id']
        if mid != current_match:
            if current_match is not None:
                finish_match()
            current_match, objective_counts, firsts, explicit_blood = mid, Counter(), {}, None
        seen_matches.add(mid)
        kind = row['event_type']
        event_types[kind] += 1
        if row['monster_type']:
            monsters[row['monster_type']] += 1
        missing.update(key for key in ('team_id', 'actor_team_id', 'position_x', 'killer_id') if not row[key])
        errors['invalid_row_width'] += None in row
        timestamp = int(row['timestamp_ms'])
        frame, index = int(row['frame_index']), int(row['event_index'])
        key = (mid, frame, index)
        errors['duplicate_adjacent_event'] += key == previous_key
        errors['event_order_violation'] += previous_key is not None and key < previous_key
        previous_key = key
        errors['invalid_event_id'] += row['event_id'] != f'{mid}:{frame}:{index}'
        errors['invalid_minute'] += not math.isclose(float(row['minute']), timestamp / 60000, abs_tol=1e-9)
        errors['invalid_timestamp'] += timestamp < 0
        errors['invalid_team'] += row['team_id'] not in ('', '100', '200')
        errors['invalid_actor_team'] += row['actor_team_id'] not in ('', '100', '200')
        summary = summaries.get(mid)
        if summary:
            errors['event_patch_mismatch'] += row['patch'] != summary['patch']
            errors['event_beyond_duration_margin'] += timestamp > float(summary['game_duration']) * 1000 + 120000
        else:
            errors['event_without_summary'] += 1
        try:
            raw = json.loads(row['raw_event'])
            assisting = json.loads(row['assisting_participant_ids'])
            errors['invalid_assisting_list'] += not isinstance(assisting, list)
            errors['raw_type_mismatch'] += raw.get('type') != kind
            errors['raw_timestamp_mismatch'] += raw.get('timestamp') != timestamp
            for source, normalized in (('monsterType', 'monster_type'), ('monsterSubType', 'monster_sub_type'),
                                       ('buildingType', 'building_type'), ('towerType', 'tower_type'),
                                       ('laneType', 'lane_type'), ('killType', 'kill_type')):
                errors['raw_field_mismatch'] += (raw.get(source) or '') != row[normalized]
            for source, normalized in (('killerId', 'killer_id'), ('victimId', 'victim_id')):
                expected = '' if raw.get(source) is None else str(raw[source])
                errors['raw_participant_mismatch'] += expected != row[normalized]
        except (ValueError, TypeError, AttributeError):
            errors['invalid_raw_or_assisting_json'] += 1
        objective = None
        actor = row['actor_team_id']
        credited_kill = kind == 'CHAMPION_KILL' and int(row['killer_id'] or 0) > 0
        if credited_kill:
            objective = 'blood'
        elif kind == 'ELITE_MONSTER_KILL':
            objective = MONSTERS.get(row['monster_type'].upper())
        elif kind == 'BUILDING_KILL':
            building = row['building_type'].upper()
            objective = 'tower' if ('TOWER' in building or 'TURRET' in building) else (
                'inhibitor' if 'INHIBITOR' in building else None)
        if objective:
            objective_counts[objective, actor] += 1
            candidate = ((timestamp, frame, index), actor)
            if objective not in firsts or candidate[0] < firsts[objective][0]:
                firsts[objective] = candidate
        if kind == 'CHAMPION_SPECIAL_KILL' and row['kill_type'] == 'KILL_FIRST_BLOOD':
            candidate = ((timestamp, frame, index), actor)
            if explicit_blood is None or candidate[0] < explicit_blood[0]:
                explicit_blood = candidate
        if rows % 250000 == 0:
            print(f'Events checked: {rows:,}; elapsed {(time.monotonic() - started) / 60:.1f} min', flush=True)
    if current_match is not None:
        finish_match()
    return {'rows': rows, 'columns': len(fields), 'header': fields, 'event_types': dict(event_types.most_common()),
            'monster_types': dict(monsters.most_common()), 'missing_optional_fields': dict(missing),
            'errors': dict(errors), 'matches_with_events': len(seen_matches),
            'summary_matches_without_events': len(set(summaries) - seen_matches),
            'objective_count_disagreements': dict(count_mismatches),
            'first_objective_flag_disagreements': dict(first_mismatches),
            'milestone_event_disagreements': dict(milestone_mismatches), 'examples': dict(examples),
            'event_uniqueness_certified': not any(errors[key] for key in (
                'event_order_violation', 'duplicate_adjacent_event', 'invalid_event_id'))}


def audit_database(path, physical_integrity=False):
    print('Checking SQLite references and export row counts...', flush=True)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as conn:
        conn.execute('BEGIN')
        counts = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('players', 'matches', 'match_summaries', 'events', 'milestones')}
        foreign_key_errors = []
        for child, column, parent, parent_column in (
            ('events', 'match_id', 'matches', 'match_id'),
            ('milestones', 'match_id', 'matches', 'match_id'),
            ('weekend_matches', 'match_id', 'matches', 'match_id'),
            ('weekend_matches', 'run_name', 'weekend_runs', 'name'),
            ('weekend_sources', 'puuid', 'players', 'puuid'),
            ('weekend_sources', 'run_name', 'weekend_runs', 'name'),
        ):
            orphaned = conn.execute(f'SELECT COUNT(*) FROM {child} c LEFT JOIN {parent} p '
                                    f'ON p.{parent_column} = c.{column} WHERE p.{parent_column} IS NULL').fetchone()[0]
            if orphaned:
                foreign_key_errors.append([child, column, orphaned])
        return {'counts': counts,
                'quick_check': conn.execute('PRAGMA quick_check').fetchall() if physical_integrity else
                               'Not run: physical verification is optional (--physical-integrity)',
                'foreign_key_errors': foreign_key_errors,
                'timeline_errors': conn.execute('SELECT timeline_last_error, COUNT(*) FROM matches '
                                               'WHERE timeline_last_error IS NOT NULL GROUP BY 1').fetchall(),
                'sample': conn.execute('''SELECT w.run_name, COUNT(*),
                    SUM(s.match_id IS NOT NULL AND m.timeline_processed=1),
                    SUM(m.processed=1 AND s.match_id IS NULL)
                    FROM weekend_matches w JOIN matches m ON m.match_id=w.match_id
                    LEFT JOIN match_summaries s ON s.match_id=m.match_id GROUP BY w.run_name''').fetchall()}


def write_report(report, folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'data_review.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    summary, events, database = report['summary'], report['events'], report['database']
    lines = ['# Revision de datos recolectados', '', f"Generado (UTC): {report['generated_at_utc']}", '',
             f"- Partidas validas: **{summary['matches']:,}**.",
             f"- Eventos revisados: **{events['rows']:,}**.",
             f"- Filas de hitos: **{summary['milestones']:,}**.",
             f"- Equipo 1 ganador: **{summary['team1_win_pct']:.2f}%**.",
             f"- IDs de evento unicos certificados: **{events['event_uniqueness_certified']}**.",
             '', '## Distribucion por plataforma', '', '| Plataforma | Partidas |', '|---|---:|']
    lines.extend(f'| {name} | {count:,} |' for name, count in summary['regions'].items())
    lines += ['', '## Distribucion por parche', '', '| Parche | Partidas |', '|---|---:|']
    lines.extend(f'| {name} | {count:,} |' for name, count in summary['patches'].items())
    lines += ['', '## Duracion (minutos)', '', '| Estadistico | Valor |', '|---|---:|']
    lines.extend(f'| {name} | {value:.2f} |' for name, value in summary['numeric']['duration_minutes'].items())
    lines += ['', '## Aspectos que requieren una decision de analisis', '',
              f"- {summary['matches_under_5_minutes']:,} partidas duran menos de cinco minutos.",
              f"- {summary['matches_with_unknown_tier']:,} partidas tienen al menos un promedio de rango igual a cero (desconocido).",
              f"- Atakhan tiene capturas en {summary['objective_present_matches']['atakhan']:,} partidas.",
              f"- Diferencias en primeras capturas entre resumen y timeline (partidas): {summary['first_flag_disagreement_matches']}.",
              f"- Timestamps de hitos sin equipo atribuido: {summary['milestone_time_without_team']}.",
              f"- Timestamps con cero capturas finales del objetivo: {summary['milestone_time_with_zero_final_kills']}.",
              '- Los valores cero de rango deben interpretarse como desconocidos, no como un rango inferior a Master.']
    lines += ['', '## Calidad y consistencia', '', 'Los campos opcionales de eventos pueden estar vacios segun su tipo.', '', '```json',
              json.dumps({'summary_errors': summary['quality_errors'], 'missing_summary_values': summary['missing_summary_values'],
                          'event_errors': events['errors'], 'objective_count_disagreements': events['objective_count_disagreements'],
                          'first_objective_flag_disagreements': events['first_objective_flag_disagreements'],
                          'milestone_event_disagreements': events['milestone_event_disagreements'],
                          'sqlite': database['quick_check'], 'foreign_key_errors': database['foreign_key_errors']}, indent=2),
              '```', '', '## Consideraciones para mineria', '',
              '- Los objetivos finales y eventos posteriores al momento de prediccion no deben usarse como variables de entrada previas al juego.',
              '- El rango promedio usa solo jugadores conocidos localmente y no representa necesariamente su rango historico.',
              '- Una columna con objetivos ausentes normalizados a cero no distingue ausencia de ese objetivo en el parche de cero capturas.',
              '- Los conteos y primeras capturas de Match-V5 y Timeline pueden diferir; revisar los desacuerdos antes de elegir la fuente para un modelo.',
              '- Esta revision no modifica ni vuelve a descargar datos. El informe JSON contiene el detalle completo.', '']
    (folder / 'data_review.md').write_text('\n'.join(lines), encoding='utf-8')


def main():
    base = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=base / 'data' / 'weekend')
    parser.add_argument('--database', type=Path, default=base / 'data' / 'riot_dataset.db')
    parser.add_argument('--output-dir', type=Path, default=base / 'reports')
    parser.add_argument('--physical-integrity', action='store_true',
                        help='Also scan every SQLite page; can take a long time for large databases')
    args = parser.parse_args()
    csv.field_size_limit(10_000_000)
    summaries, milestones, summary = audit_summaries(args.data_dir)
    print(json.dumps({key: summary[key] for key in ('matches', 'team1_win_pct', 'regions', 'numeric', 'quality_errors')}, indent=2), flush=True)
    events = audit_events(args.data_dir, summaries, milestones)
    database = audit_database(args.database, args.physical_integrity)
    report = {'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'summary': summary,
              'events': events, 'database': database,
              'files': {path.name: {'bytes': path.stat().st_size, 'modified_ns': path.stat().st_mtime_ns}
                        for path in args.data_dir.glob('*.csv')}}
    write_report(report, args.output_dir)
    print('Audit finished: ' + str(args.output_dir / 'data_review.md'), flush=True)


if __name__ == '__main__':
    main()
