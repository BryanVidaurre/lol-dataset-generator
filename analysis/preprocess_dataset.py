"""Offline, streaming preprocessing of the three exported match CSVs.

Originals are read-only. Publish a new output directory only after validation.
No fitting, imputation, scaling, Riot requests or database access is performed.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.timeline import EVENT_FIELDNAMES, MILESTONE_NAMES, MILESTONE_FIELDNAMES

CHAMPIONS = [f't{team}_{role}' for team in (1, 2)
             for role in ('top', 'jungle', 'middle', 'bottom', 'support')]
COUNTS = ('champion', 'tower', 'dragon', 'herald', 'horde', 'baron', 'atakhan', 'inhibitor')
SNAPSHOT_COUNTS = [f't{team}_{name}_kills' for team in (1, 2) for name in COUNTS]
SNAPSHOT_UNKNOWN = [f'unknown_{name}_kills' for name in COUNTS]
MONSTERS = {'DRAGON': 'dragon', 'RIFTHERALD': 'herald', 'RIFT_HERALD': 'herald',
            'HORDE': 'horde', 'VOID_GRUB': 'horde', 'VOID_GRUBS': 'horde',
            'BARON_NASHOR': 'baron', 'BARON': 'baron', 'ATAKHAN': 'atakhan'}
BOOLEAN_FIELDS = ['win_team1'] + [f't{t}_first_{o}' for t in (1, 2) for o in MILESTONE_NAMES]
EVENT_FIELDS = [c for c in EVENT_FIELDNAMES if c != 'raw_event'] + ['objective', 'after_game_end']


def rows(path, required):
    with path.open(newline='', encoding='utf-8-sig') as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not set(required).issubset(reader.fieldnames):
            raise ValueError(f'{path.name}: missing required columns')
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f'{path.name}: duplicate column names')
        for number, row in enumerate(reader, 2):
            if None in row or None in row.values():
                raise ValueError(f'{path.name}:{number}: invalid row width')
            yield row


def number(value, *, integer=False, minimum=0):
    n = float(value)
    if not math.isfinite(n) or n < minimum or (integer and not n.is_integer()):
        raise ValueError(f'Invalid numeric value: {value!r}')
    return int(n) if integer else n


def boolean(value):
    if value not in ('True', 'False', '1', '0'):
        raise ValueError(f'Invalid boolean: {value!r}')
    return int(value in ('True', '1'))


def patch_group(patch):
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)*', patch):
        raise ValueError(f'Invalid patch: {patch!r}')
    return '.'.join(str(int(v)) for v in patch.split('.')[:2])


def unique_records(path, required, flags):
    result = {}
    for row in rows(path, required):
        mid = row['match_id']
        if not re.fullmatch(r'[A-Z0-9]+_\d+', mid):
            raise ValueError(f'{path.name}: invalid match ID {mid!r}')
        if mid in result:
            if row != result[mid]:
                raise ValueError(f'{path.name}: conflicting duplicate {mid}')
            flags.append((mid, 'exact_duplicate_removed', path.name))
        else:
            result[mid] = row
    return result


def prepare_matches(source, min_duration, flags):
    required = ['match_id', 'patch', 'game_duration', 'game_creation'] + CHAMPIONS + BOOLEAN_FIELDS
    required += [f't{t}_{o}_kills' for t in (1, 2) for o in COUNTS]
    required += ['t1_avg_tier', 't2_avg_tier']
    raw = unique_records(source / 'dataset.csv', required, flags)
    clean = {}
    for mid, original in raw.items():
        row = original.copy()
        row['patch_group'] = patch_group(row['patch'])
        row['region'] = mid.split('_')[0]
        row['game_duration'] = number(row['game_duration'], minimum=1)
        row['game_creation'] = number(row['game_creation'], integer=True, minimum=1)
        if len({row[c] for c in CHAMPIONS}) != 10 or any(not row[c].strip() for c in CHAMPIONS):
            raise ValueError(f'{mid}: missing or repeated champion')
        for col in BOOLEAN_FIELDS:
            row[col] = boolean(row[col])
        for col in SNAPSHOT_COUNTS:
            row[col] = number(row[col], integer=True)
        for team in (1, 2):
            col = f't{team}_avg_tier'
            tier = number(row[col])
            if tier > 3 or (tier and tier < 1):
                raise ValueError(f'{mid}: invalid tier')
            row[f'{col}_unknown'] = int(tier == 0)
            row[col] = tier if tier else None
            if tier == 0:
                flags.append((mid, 'unknown_tier', col))
        if row['game_duration'] < min_duration:
            flags.append((mid, 'excluded_short_duration', str(row['game_duration'])))
            continue
        row['duration_minutes'] = row['game_duration'] / 60
        row['started_at_utc'] = datetime.fromtimestamp(row['game_creation'] / 1000, timezone.utc).isoformat()
        clean[mid] = row
    if not clean:
        raise ValueError('No matches remain after duration filtering')
    return raw, clean


def prepare_milestones(source, raw, clean, flags):
    records = unique_records(source / 'milestones.csv', MILESTONE_FIELDNAMES, flags)
    if set(records) != set(raw):
        raise ValueError('Milestone and summary match IDs must agree')
    result = []
    for mid, row in records.items():
        if row['patch'] != raw[mid]['patch']:
            raise ValueError(f'{mid}: milestone patch mismatch')
        out = {'match_id': mid, 'patch': row['patch']}
        for name in MILESTONE_NAMES:
            tk, sk = f'first_{name}_team', f'first_{name}_time'
            team = number(row[tk], integer=True) if row[tk] else None
            stamp = number(row[sk], integer=True) if row[sk] else None
            if team not in (None, 100, 200) or (team is not None and stamp is None):
                raise ValueError(f'{mid}: invalid milestone {name}')
            if stamp is not None and stamp > float(raw[mid]['game_duration']) * 1000 + 120000:
                raise ValueError(f'{mid}: milestone outside duration margin')
            out[tk], out[sk] = team, stamp
            out[f'first_{name}_minute'] = stamp / 60000 if stamp is not None else None
            if stamp is not None and team is None:
                flags.append((mid, 'milestone_unknown_team', name))
            if stamp is not None and stamp > float(raw[mid]['game_duration']) * 1000:
                flags.append((mid, 'milestone_after_game_end', name))
            if team is not None and any(boolean(raw[mid][f't{t}_first_{name}']) != int(team == tid)
                                        for t, tid in ((1, 100), (2, 200))):
                flags.append((mid, 'first_flag_disagreement', name))
        if mid in clean:
            result.append(out)
    return result


def temporal_membership(clean):
    ordered = sorted(clean.values(), key=lambda r: (r['game_creation'], r['match_id']))
    validation = ordered[int(len(ordered) * .70)]['game_creation']
    test = ordered[int(len(ordered) * .85)]['game_creation']
    result = []
    for row in ordered:
        start = row['game_creation']
        end = start + row['game_duration'] * 1000
        if start < validation:
            split = 'train' if end < validation else 'purged'
        elif start < test:
            split = 'validation' if end < test else 'purged'
        else:
            split = 'test'
        result.append({'match_id': row['match_id'], 'split': split})
    return result, {'validation_start_ms': validation, 'test_start_ms': test}


def objective(row):
    kind = row['event_type']
    if kind == 'CHAMPION_KILL':
        return 'champion'
    if kind == 'ELITE_MONSTER_KILL':
        return MONSTERS.get(row['monster_type'].upper(), '')
    if kind == 'BUILDING_KILL':
        building = row['building_type'].upper()
        if 'TOWER' in building or 'TURRET' in building:
            return 'tower'
        if 'INHIBITOR' in building:
            return 'inhibitor'
    return ''


def normalize_event(row, raw):
    mid = row['match_id']
    if mid not in raw or row['patch'] != raw[mid]['patch']:
        raise ValueError(f'{mid}: orphan event or patch mismatch')
    out = {col: row[col] for col in EVENT_FIELDNAMES if col != 'raw_event'}
    for col in ('timestamp_ms', 'frame_index', 'event_index'):
        out[col] = number(row[col], integer=True)
    if row['event_id'] != f"{mid}:{out['frame_index']}:{out['event_index']}":
        raise ValueError(f'{mid}: inconsistent event ID')
    stamp = out['timestamp_ms']
    duration_ms = float(raw[mid]['game_duration']) * 1000
    if stamp > duration_ms + 120000:
        raise ValueError(f'{mid}: event outside duration margin')
    if not math.isclose(float(row['minute']), stamp / 60000, abs_tol=1e-8):
        raise ValueError(f'{mid}: inconsistent minute')
    out['minute'] = stamp / 60000
    for col in ('team_id', 'actor_team_id'):
        out[col] = number(row[col], integer=True) if row[col] else None
        if out[col] not in (None, 100, 200):
            raise ValueError(f'{mid}: invalid {col}')
    for col in ('killer_id', 'victim_id'):
        out[col] = number(row[col], integer=True) if row[col] else None
        if out[col] is not None and out[col] > 10:
            raise ValueError(f'{mid}: invalid participant')
    assists = json.loads(row['assisting_participant_ids'])
    if not isinstance(assists, list) or any(type(v) is not int or not 1 <= v <= 10 for v in assists):
        raise ValueError(f'{mid}: invalid assists')
    out['assisting_participant_ids'] = json.dumps(assists, separators=(',', ':'))
    for col in ('position_x', 'position_y'):
        out[col] = number(row[col]) if row[col] else None
    out['objective'] = objective(row)
    out['after_game_end'] = int(stamp > duration_ms)
    return out


def writer(stack, output, filename, fields):
    handle = stack.enter_context((output / filename).open('w', encoding='utf-8', newline=''))
    result = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
    result.writeheader()
    return result


def write_rows(output, filename, records, fields):
    with ExitStack() as stack:
        target = writer(stack, output, filename, fields)
        target.writerows(records)


def process_events(source, output, raw, clean, minutes, flags):
    stats = Counter()
    types = Counter()
    completed = set()
    current = None
    batch = []
    seen = {}
    previous_key = None
    start = time.monotonic()

    with ExitStack() as stack:
        event_writer = writer(stack, output, 'events_clean.csv', EVENT_FIELDS)
        snap_fields = ['match_id', 'minute', 'events_observed'] + SNAPSHOT_COUNTS + SNAPSHOT_UNKNOWN
        snap_writer = writer(stack, output, 'snapshots.csv', snap_fields)

        def finish():
            if current is None:
                return
            completed.add(current)
            if current not in clean:
                return
            ordered = sorted(batch, key=lambda e: (e['timestamp_ms'], e['frame_index'], e['event_index']))
            counts = Counter()
            index = 0
            for minute in minutes:
                if minute * 60 >= clean[current]['game_duration']:
                    continue
                while index < len(ordered) and ordered[index]['timestamp_ms'] <= minute * 60000:
                    event = ordered[index]
                    name, team = event['objective'], event['actor_team_id']
                    if name:
                        prefix = {100: 't1', 200: 't2'}.get(team, 'unknown')
                        counts[f'{prefix}_{name}_kills'] += 1
                    index += 1
                snap_writer.writerow({'match_id': current, 'minute': minute, 'events_observed': index,
                                      **{c: counts[c] for c in SNAPSHOT_COUNTS + SNAPSHOT_UNKNOWN}})
                stats['snapshots'] += 1
            # Compare full timeline with summary without overwriting either source.
            finals = Counter()
            for event in ordered:
                name, team = event['objective'], event['actor_team_id']
                if name and team in (100, 200):
                    finals[f"t{1 if team == 100 else 2}_{name}_kills"] += 1
            for col in SNAPSHOT_COUNTS:
                if finals[col] != clean[current][col]:
                    flags.append((current, 'final_count_disagreement',
                                  f'{col}: summary={clean[current][col]}; timeline={finals[col]}'))

        for original in rows(source / 'events.csv', EVENT_FIELDNAMES):
            stats['input_events'] += 1
            event = normalize_event(original, raw)
            mid = event['match_id']
            if mid != current:
                finish()
                if mid in completed:
                    raise ValueError('Events must be grouped by match_id; a completed match reappeared')
                current, batch, seen, previous_key = mid, [], {}, None
            key = (event['frame_index'], event['event_index'])
            if key in seen:
                if (event, original['raw_event']) != seen[key]:
                    raise ValueError(f"Conflicting duplicate event {event['event_id']}")
                stats['exact_duplicate_events_removed'] += 1
                continue
            if previous_key is not None and key < previous_key:
                raise ValueError(f'{mid}: frame/event order violation')
            previous_key = key
            seen[key] = (event, original['raw_event'])
            if mid in clean:
                event_writer.writerow(event)
                batch.append(event)
                stats['output_events'] += 1
                types[event['event_type']] += 1
                if event['after_game_end']:
                    flags.append((mid, 'event_after_game_end', event['event_id']))
            else:
                stats['events_excluded_short_match'] += 1
            if stats['input_events'] % 250000 == 0:
                print(f"Events: {stats['input_events']:,}; elapsed: {time.monotonic() - start:.0f}s", flush=True)
        finish()
    missing = set(clean) - completed
    for mid in sorted(missing):
        flags.append((mid, 'no_exported_events', 'No snapshots; zero events is not an observed game state'))
    stats['matches_without_events'] = len(missing)
    stats['matches_with_events'] = len(set(clean) & completed)
    return dict(stats), dict(types)


def signatures(source):
    return {name: {'bytes': (source / name).stat().st_size,
                   'mtime_ns': (source / name).stat().st_mtime_ns}
            for name in ('dataset.csv', 'events.csv', 'milestones.csv')}


def run(source, output, min_duration=300, minutes=(5, 10, 15, 20, 25, 30)):
    source, output = Path(source).resolve(), Path(output).resolve()
    minutes = sorted(set(minutes))
    if min_duration < 0 or not minutes or any(type(m) is not int or m <= 0 for m in minutes):
        raise ValueError('Duration must be nonnegative and snapshot minutes positive integers')
    if output == source or source in output.parents or output in source.parents:
        raise ValueError('Output must be separate from the source directory')
    if output.exists():
        raise FileExistsError(f'Choose a new output directory: {output}')
    before = signatures(source)
    flags = []
    raw, clean = prepare_matches(source, min_duration, flags)
    milestones = prepare_milestones(source, raw, clean, flags)
    splits, boundaries = temporal_membership(clean)
    print(f'Matches: {len(raw):,}; retained: {len(clean):,}; reading full events file...', flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=output.name + '.building-', dir=output.parent) as temp:
        stage = Path(temp) / 'result'
        stage.mkdir()
        summary_fields = list(next(iter(clean.values())))
        write_rows(stage, 'dataset_clean.csv', clean.values(), summary_fields)
        write_rows(stage, 'prematch_features.csv', clean.values(), ['match_id', 'patch_group'] + CHAMPIONS)
        write_rows(stage, 'outcomes.csv', clean.values(), ['match_id', 'win_team1'])
        milestone_fields = list(MILESTONE_FIELDNAMES) + [f'first_{o}_minute' for o in MILESTONE_NAMES]
        write_rows(stage, 'milestones_clean.csv', milestones, milestone_fields)
        write_rows(stage, 'split_membership.csv', splits, ['match_id', 'split'])
        stats, event_types = process_events(source, stage, raw, clean, minutes, flags)
        write_rows(stage, 'quality_flags.csv',
                   (dict(zip(('match_id', 'issue', 'detail'), f)) for f in flags),
                   ['match_id', 'issue', 'detail'])
        if signatures(source) != before:
            raise RuntimeError('Source files changed during preprocessing; output not published')
        feature_manifest = {
            'prematch_features.csv': {'keys': ['match_id'], 'features': ['patch_group'] + CHAMPIONS},
            'snapshots.csv': {'keys': ['match_id', 'minute'],
                              'features': ['minute', 'events_observed'] + SNAPSHOT_COUNTS + SNAPSHOT_UNKNOWN},
            'outcomes.csv': {'keys': ['match_id'], 'target': 'win_team1'},
            'excluded_predictors': ['game_duration', 'game_creation', 'started_at_utc', 'region',
                                    'average tiers', 'final objectives', 'milestones', 'event positions'],
            'notes': [
                'Snapshots contain only events with timestamp_ms <= minute * 60000.',
                'Snapshots exist only for games still running at the cutoff with some exported events.',
                'Zero means no matching exported event up to the cutoff, not proof of complete API coverage.',
                'actor_team_id attributes captures; team_id can instead identify a destroyed structure owner.',
                'Unknown actor counts are separate; executions are not credited to a team.',
                'No buys, items, CS, levels, gold, health or participant-role mapping can be reconstructed from these CSVs.',
                'Do not assume participant IDs map to roles or team halves.',
                'Champion identifiers are source canonical names, not translated display names or numeric IDs.',
                'Join features to outcomes and splits by match_id; exclude purged; never randomly split snapshots.',
                'Imputers, encoders and scalers must be fitted on train only in a later mining pipeline.',
                'All patches retained and grouped by major.minor; no mode/queue field available in these exports.',
            ],
        }
        (stage / 'feature_manifest.json').write_text(json.dumps(feature_manifest, indent=2), encoding='utf-8')
        report = {'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'source': str(source),
                  'source_files': before, 'source_files_unchanged': True,
                  'source_verification': 'size and mtime before/after; no full-source cryptographic hashes',
                  'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'parameters': {'min_duration_seconds': min_duration, 'snapshot_minutes': minutes},
                  'input_matches': len(raw), 'retained_matches': len(clean),
                  'excluded_short_matches': len(raw) - len(clean), 'milestone_rows': len(milestones),
                  'patches': dict(Counter(r['patch_group'] for r in clean.values())),
                  'splits': dict(Counter(r['split'] for r in splits)), 'split_boundaries': boundaries,
                  'quality_flags': dict(Counter(f[1] for f in flags)), 'events': stats, 'event_types': event_types,
                  'outputs': {p.name: p.stat().st_size for p in stage.iterdir()}}
        (stage / 'preprocessing_report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        lines = ['# Preprocesamiento para minería', '',
                 f"Partidas originales: **{len(raw):,}**. Conservadas: **{len(clean):,}**.",
                 f"Excluidas por duración menor a {min_duration}s: **{len(raw) - len(clean):,}**.",
                 f"Eventos leídos: **{stats.get('input_events', 0):,}**; conservados: **{stats.get('output_events', 0):,}**.",
                 f"Estados temporales: **{stats.get('snapshots', 0):,}**. Cortes: {minutes} minutos.", '',
                 '## Archivos', '',
                 '- `dataset_clean.csv`: resumen normalizado; contiene resultados finales, no usar todas sus columnas como predictores.',
                 '- `prematch_features.csv`: composición por rol y parche, sin resultados finales ni rango actual.',
                 '- `outcomes.csv`: etiqueta de victoria del equipo 1 (1/0).',
                 '- `events_clean.csv`: secuencias ordenadas por partida/frame/índice; conservar timestamp para orden temporal.',
                 '- `milestones_clean.csv`: hitos históricos con tiempos en ms y minutos; nulos conservados.',
                 '- `snapshots.csv`: conteos acumulados de eventos hasta cada corte, sin información posterior.',
                 '- `split_membership.csv`: grupos temporales 70/15/15 aproximados; `purged` excluye resultados aún no disponibles en la siguiente frontera.',
                 '- `quality_flags.csv`: incidencias y diferencias entre fuentes para filtrar o segmentar.',
                 '- `feature_manifest.json`: entradas permitidas y límites para minería.', '',
                 '## Decisiones y límites', '',
                 'Los originales no se modificaron (tamaño y mtime comprobados al inicio y al final).',
                 'El umbral de duración es una política de análisis; no identifica por sí solo remakes.',
                 'Se conservan todos los parches y partidas largas; no se eliminan atípicos por ser raros.',
                 'Rango cero pasa a nulo con indicador; se excluye de predictores porque no acredita rango histórico.',
                 'Booleanos normalizados a 1/0. Nulos condicionales de hitos y eventos siguen vacíos.',
                 'No se imputan tiempos/equipos desconocidos ni se sobrescriben diferencias entre resumen y timeline.',
                 'Los conteos temporales usan actor_team_id, no el dueño del edificio destruido.',
                 'No se generan estados para partidas sin eventos exportados ni en/después del final.',
                 'raw_event se omite de la copia compacta; permanece íntegro en el archivo original.',
                 'La validación cubre campos normalizados; no vuelve a auditar cada campo del JSON raw_event.',
                 'Las secuencias exportadas son una selección de eventos, no el timeline completo.',
                 'No hay compras, inventarios, CS ni estadísticas individuales por minuto: aún no permite minería de builds.',
                 'No se puede volver a filtrar cola/mapa con estos CSV porque no incluyen esos campos.',
                 'No se escalan ni codifican categorías aquí: el ajuste debe hacerse solo con entrenamiento.',
                 'Los snapshots de una partida comparten split. La separación no garantiza jugadores distintos.',
                 'Los patrones serán asociaciones observacionales, no efectos causales de una decisión.', '',
                 '## Indicadores de calidad', '', '```json',
                 json.dumps(report['quality_flags'], indent=2), '```', '']
        (stage / 'preprocessing_report.md').write_text('\n'.join(lines), encoding='utf-8')
        os.replace(stage, output)
    print(f'Published: {output}', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--min-duration', type=int, default=300)
    parser.add_argument('--minutes', type=int, nargs='+', default=[5, 10, 15, 20, 25, 30])
    args = parser.parse_args()
    run(args.data_dir, args.output_dir, args.min_duration, args.minutes)


if __name__ == '__main__':
    main()
