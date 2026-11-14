"""Profile the three exported CSV files, streaming the complete events file.

Run with .venv/Scripts/python.exe from the repository root, passing --data-dir
and --output-dir. Source CSVs are opened read-only. Reuses the repository's JSON
and cross-file consistency audit.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils import audit_dataset as audit
from utils.analysis_csv import read_analysis_csv


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False,
                               default=lambda x: x.item() if hasattr(x, 'item') else str(x)),
                    encoding='utf-8')


def table(frame):
    """Small Markdown table without the optional tabulate dependency."""
    def cell(value):
        if pd.isna(value):
            return '—'
        if isinstance(value, (float, np.floating)):
            return f'{value:,.2f}'
        if isinstance(value, (int, np.integer)):
            return f'{value:,}'
        return str(value).replace('|', '\\|')
    return '\n'.join(['| ' + ' | '.join(map(str, frame.columns)) + ' |',
                      '| ' + ' | '.join('---' for _ in frame.columns) + ' |'] +
                     ['| ' + ' | '.join(cell(v) for v in row) + ' |'
                      for row in frame.itertuples(index=False, name=None)])


def column_profile(frame, name):
    rows = []
    for column in frame:
        series = frame[column]
        counts = series.value_counts(dropna=True)
        row = {'file': name, 'column': column, 'dtype': str(series.dtype),
               'rows': len(series), 'missing': int(series.isna().sum()),
               'missing_pct': float(series.isna().mean() * 100),
               'distinct_non_null': int(series.nunique()),
               'constant_non_null': bool(series.nunique() == 1),
               'mode': counts.index[0] if len(counts) else None,
               'mode_count': int(counts.iloc[0]) if len(counts) else 0}
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            row.update({key: float(value) if pd.notna(value) else None for key, value in
                        series.describe(percentiles=[.01, .05, .25, .5, .75, .95, .99]).items()
                        if key != 'count'})
        rows.append(row)
    return rows


class EventProfile:
    """Additional counters collected during the audit's single sequential pass."""
    categorical = ('patch', 'event_type', 'team_id', 'actor_team_id', 'killer_id',
                   'victim_id', 'monster_type', 'monster_sub_type', 'building_type',
                   'tower_type', 'lane_type', 'kill_type')
    numeric = ('timestamp_ms', 'minute', 'frame_index', 'event_index', 'position_x', 'position_y')

    def __init__(self, summaries):
        self.summaries = summaries
        self.rows = 0
        self.missing = Counter()
        self.categories = {c: Counter() for c in self.categorical}
        self.minimum = {}
        self.maximum = {}
        self.totals = Counter()
        self.counts = Counter()
        self.by_match = Counter()
        self.by_patch = Counter()
        self.by_type_missing = defaultdict(Counter)
        self.kills = Counter()
        self.checks = Counter()
        self.raw_length = Counter()
        self.raw_max = 0
        self.fields = []

    def observe(self, row):
        self.rows += 1
        mid, kind = row['match_id'], row['event_type']
        self.by_match[mid] += 1
        self.by_patch['.'.join(row['patch'].split('.')[:2])] += 1
        missing = [c for c, v in row.items() if v in ('', None)]
        self.missing.update(missing)
        self.by_type_missing[kind].update(missing)
        for c, counts in self.categories.items():
            if row[c] != '':
                counts[row[c]] += 1
        for c in self.numeric:
            if row[c] != '':
                value = float(row[c]) if c == 'minute' else int(row[c])
                self.minimum[c] = min(self.minimum.get(c, value), value)
                self.maximum[c] = max(self.maximum.get(c, value), value)
                self.totals[c] += value
                self.counts[c] += 1
        raw_len = len(row['raw_event'])
        self.raw_length['characters'] += raw_len
        self.raw_max = max(self.raw_max, raw_len)
        summary = self.summaries.get(mid)
        if summary:
            self.checks['events_after_exact_duration'] += int(row['timestamp_ms']) > int(summary['game_duration']) * 1000
        if kind == 'CHAMPION_KILL':
            self.checks['champion_kills_without_actor_team'] += not row['actor_team_id']
            self.checks['champion_kills_with_killer_zero'] += row['killer_id'] == '0'
            if int(row['killer_id'] or 0) > 0:
                self.kills[mid, row['actor_team_id']] += 1
        for c in ('killer_id', 'victim_id'):
            if row[c]:
                self.checks['invalid_' + c + '_domain'] += not 0 <= int(row[c]) <= 10
        self.checks['partial_position'] += bool(row['position_x']) != bool(row['position_y'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--base-dir', type=Path)
    args = parser.parse_args()
    source, out = args.data_dir.resolve(), args.output_dir.resolve()
    if out == source or source in out.parents:
        parser.error('Keep reports outside the source data folder.')
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    before = {p.name: {'bytes': p.stat().st_size, 'modified_ns': p.stat().st_mtime_ns}
              for p in (source / n for n in ('dataset.csv', 'milestones.csv', 'events.csv'))}
    csv.field_size_limit(10_000_000)
    summaries, milestones, summary = audit.audit_summaries(source)
    dataset = read_analysis_csv(source / 'dataset.csv')
    milestone_frame = read_analysis_csv(source / 'milestones.csv')
    save_json(out / 'summary_preliminary.json', summary)
    print(json.dumps({'matches': summary['matches'], 'patches': summary['patches'],
                      'team1_win_pct': summary['team1_win_pct'],
                      'short_games': summary['matches_under_5_minutes']}, indent=2), flush=True)

    extra = EventProfile(summaries)
    original_reader = audit.read_rows

    def observed_reader(path, fields):
        for row in original_reader(path, fields):
            if path.name == 'events.csv':
                extra.observe(row)
            yield row
        if path.name == 'events.csv':
            extra.fields = list(fields)

    audit.read_rows = observed_reader
    try:
        events = audit.audit_events(source, summaries, milestones)
    finally:
        audit.read_rows = original_reader
    after = {p.name: {'bytes': p.stat().st_size, 'modified_ns': p.stat().st_mtime_ns}
             for p in (source / n for n in before)}
    if before != after:
        raise RuntimeError('Source CSVs changed during the audit. Rerun on a stable export.')
    report = {'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'source': str(source), 'summary': summary, 'events': events, 'files': before,
              'source_files_unchanged': True,
              'database': {'quick_check': 'Not run: requested scope is the three CSV exports.',
                           'foreign_key_errors': [], 'checked': False},
              'extra_event_checks': dict(extra.checks)}
    audit.write_report(report, out)

    profiles = column_profile(dataset, 'dataset.csv') + column_profile(milestone_frame, 'milestones.csv')
    for c in extra.fields:
        counts = extra.categories.get(c)
        mode = counts.most_common(1) if counts is not None else []
        row = {'file': 'events.csv', 'column': c, 'rows': extra.rows,
               'dtype': 'json' if c in ('raw_event', 'assisting_participant_ids') else
                        ('numeric' if c in extra.numeric else 'categorical' if counts is not None else 'identifier'),
               'missing': extra.missing[c], 'missing_pct': extra.missing[c] / extra.rows * 100,
               'distinct_non_null': len(counts) if counts is not None else None,
               'mode': mode[0][0] if mode else None, 'mode_count': mode[0][1] if mode else None}
        if c in extra.numeric:
            row.update({'min': extra.minimum.get(c), 'max': extra.maximum.get(c),
                        'mean': extra.totals[c] / extra.counts[c] if extra.counts[c] else None})
        profiles.append(row)
    pd.DataFrame(profiles).to_csv(out / 'column_profile.csv', index=False)
    save_json(out / 'event_field_distributions.json', {c: dict(v.most_common()) for c, v in extra.categories.items()})
    pd.DataFrame([{'event_type': kind, 'column': c, 'missing': count,
                   'missing_pct_within_type': count / events['event_types'][kind] * 100}
                  for kind, counts in extra.by_type_missing.items() for c, count in counts.items()]
                 ).to_csv(out / 'event_missing_by_type.csv', index=False)
    pd.DataFrame([{'match_id': k, 'events': extra.by_match[k]} for k in summaries]
                 ).to_csv(out / 'events_per_match.csv', index=False)

    enriched = dataset.assign(region=dataset.match_id.str.split('_').str[0],
                              patch_group=dataset.patch.str.split('.').str[:2].str.join('.'),
                              duration_minutes=dataset.game_duration / 60)
    group_tables = {}
    for col in ('region', 'patch_group'):
        grouped = enriched.groupby(col).agg(matches=('match_id', 'size'),
                  team1_win_pct=('win_team1', lambda s: s.mean() * 100),
                  duration_mean_min=('duration_minutes', 'mean'),
                  duration_median_min=('duration_minutes', 'median')).reset_index()
        grouped['share_pct'] = grouped.matches / len(dataset) * 100
        if col == 'patch_group':
            grouped = grouped.iloc[sorted(range(len(grouped)), key=lambda i: tuple(map(int, grouped.iloc[i][col].split('.'))))]
        group_tables[col] = grouped
        grouped.to_csv(out / f'distribution_{col}.csv', index=False)
    cross = pd.crosstab(enriched.patch_group, enriched.region)
    cross.to_csv(out / 'patch_region_counts.csv')

    champion_rows = []
    for role in audit.ROLES:
        champions = pd.concat([dataset[f't1_{role}'], dataset[f't2_{role}']]).value_counts()
        champion_rows.extend({'role': role, 'champion': champ, 'appearances': int(n),
                              'share_role_selections_pct': n / (2 * len(dataset)) * 100}
                             for champ, n in champions.items())
    pd.DataFrame(champion_rows).to_csv(out / 'champions_by_role.csv', index=False)
    numeric = dataset.select_dtypes(include='number').describe(percentiles=[.01, .05, .25, .5, .75, .95, .99]).T
    numeric.to_csv(out / 'numeric_summary.csv')
    constants = [c for c in dataset if dataset[c].nunique(dropna=False) == 1]
    dates = pd.to_datetime(dataset.game_creation, unit='ms', utc=True)
    enriched.assign(month_utc=dates.dt.strftime('%Y-%m')).groupby('month_utc').size().rename('matches').to_csv(out / 'matches_by_month.csv')

    flags = []
    kill_deltas = Counter()
    for mid, row in summaries.items():
        milestone = milestones.get(mid, {})
        if float(row['game_duration']) < 300:
            flags.append({'match_id': mid, 'issue': 'duration_under_5min', 'detail': row['game_duration']})
        if float(row['t1_avg_tier']) == 0 or float(row['t2_avg_tier']) == 0:
            flags.append({'match_id': mid, 'issue': 'unknown_tier', 'detail': f"{row['t1_avg_tier']}/{row['t2_avg_tier']}"})
        if not extra.by_match[mid]:
            flags.append({'match_id': mid, 'issue': 'no_exported_events', 'detail': row['game_duration']})
        for name in audit.OBJECTIVES:
            team = milestone.get(f'first_{name}_team', '')
            timestamp = milestone.get(f'first_{name}_time', '')
            if timestamp and not team:
                flags.append({'match_id': mid, 'issue': 'milestone_time_without_team', 'detail': name})
            if team and any((row[f't{t}_first_{name}'] == 'True') != (team == tid) for t, tid in ((1, '100'), (2, '200'))):
                flags.append({'match_id': mid, 'issue': 'first_flag_disagreement', 'detail': name})
        for prefix, team in (('t1', '100'), ('t2', '200')):
            actual, expected = int(row[f'{prefix}_champion_kills']), extra.kills[mid, team]
            if actual != expected:
                kill_deltas[actual - expected] += 1
                flags.append({'match_id': mid, 'issue': 'champion_kill_count_disagreement',
                              'detail': f'{prefix}: summary={actual}; timeline={expected}; delta={actual - expected}'})
    pd.DataFrame(flags, columns=['match_id', 'issue', 'detail']).to_csv(out / 'quality_flags.csv', index=False)
    issue_counts = Counter(row['issue'] for row in flags)
    issues_unique = {issue: len({r['match_id'] for r in flags if r['issue'] == issue}) for issue in issue_counts}

    expansion = None
    if args.base_dir and (args.base_dir / 'dataset.csv').exists():
        base = read_analysis_csv(args.base_dir / 'dataset.csv')
        base_ids, current_ids = set(base.match_id), set(dataset.match_id)
        added = enriched[~enriched.match_id.isin(base_ids)]
        overlap = dataset.set_index('match_id').loc[sorted(base_ids & current_ids)]
        old = base.set_index('match_id').loc[overlap.index, overlap.columns]
        changed = int((~((overlap == old) | (overlap.isna() & old.isna()))).any(axis=1).sum())
        expansion = {'base_matches': len(base_ids), 'retained': len(base_ids & current_ids),
                     'missing_base_ids': len(base_ids - current_ids), 'new_matches': len(added),
                     'base_rows_changed': changed, 'new_patch_counts': added.patch_group.value_counts().to_dict(),
                     'new_region_counts': added.region.value_counts().to_dict(),
                     'new_matches_outside_16_20_16_21': int((~added.patch_group.isin(['16.20', '16.21'])).sum())}
        added[['match_id', 'patch_group', 'region']].to_csv(out / 'added_matches.csv', index=False)

    objective_rows = []
    for name in audit.OBJECTIVES:
        ts = milestone_frame[f'first_{name}_time'].dropna() / 60000
        known = milestone_frame[f'first_{name}_team'].isin([100, 200])
        first_won = (milestone_frame[f'first_{name}_team'] == 100) == dataset.win_team1
        objective_rows.append({'objective': name, 'with_time': len(ts), 'with_team': int(known.sum()),
                               'coverage_time_pct': len(ts) / len(dataset) * 100,
                               'median_first_min': float(ts.median()) if len(ts) else None,
                               'first_team_win_pct': float(first_won[known].mean() * 100) if known.any() else None})
    objectives = pd.DataFrame(objective_rows)
    # The win calculation above requires equal row order as well as equal membership.
    assert dataset.match_id.equals(milestone_frame.match_id), 'Summary/milestone rows must align.'
    objectives.to_csv(out / 'objectives.csv', index=False)
    report.update({'constant_dataset_columns': constants, 'expansion': expansion,
                   'quality_flag_rows': dict(issue_counts), 'matches_with_quality_flags': issues_unique,
                   'events_per_match': audit.numeric([extra.by_match[mid] for mid in summaries]),
                   'events_per_nonempty_match': audit.numeric([n for n in extra.by_match.values() if n]),
                   'champion_kill_delta_counts': dict(kill_deltas),
                   'empty_event_matches': {
                       'matches': sum(not extra.by_match[mid] for mid in summaries),
                       'duration_seconds': audit.numeric([int(row['game_duration']) for mid, row in summaries.items() if not extra.by_match[mid]]),
                       'under_5_minutes': sum(int(row['game_duration']) < 300 for mid, row in summaries.items() if not extra.by_match[mid]),
                       'with_positive_final_kills': sum(any(int(v) > 0 for k, v in row.items() if k.endswith('_kills')) for mid, row in summaries.items() if not extra.by_match[mid])},
                   'raw_event_characters': {'mean': extra.raw_length['characters'] / extra.rows, 'max': extra.raw_max},
                   'duration_iqr_outliers': int(((enriched.duration_minutes < enriched.duration_minutes.quantile(.25) - 1.5 * (enriched.duration_minutes.quantile(.75) - enriched.duration_minutes.quantile(.25))) |
                                                 (enriched.duration_minutes > enriched.duration_minutes.quantile(.75) + 1.5 * (enriched.duration_minutes.quantile(.75) - enriched.duration_minutes.quantile(.25)))).sum()),
                   'elapsed_seconds': time.monotonic() - started,
                   'limitations': ['No SQLite audit or API revalidation.',
                                  'Event cardinalities only computed for categorical columns; other cardinalities are blank.',
                                  'Event uniqueness certification depends on global event ordering.',
                                  'Timestamp-duration checks use both exact duration and a 120-second tolerance in the reused audit.',
                                  'Source stability checked through size and modification time, not cryptographic hashes.']})
    save_json(out / 'data_review.json', report)

    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, axes = plt.subplots(2, 2, figsize=(15, 10), layout='constrained')
    enriched.duration_minutes.hist(bins=55, ax=axes[0, 0], color='#247b9e', edgecolor='white')
    axes[0, 0].set(title='Duración de las partidas', xlabel='Minutos', ylabel='Partidas')
    patches = group_tables['patch_group']
    axes[0, 1].bar(patches.patch_group, patches.matches, color='#247b9e')
    axes[0, 1].tick_params(axis='x', rotation=70)
    axes[0, 1].set(title='Distribución por parche', ylabel='Partidas')
    regions = group_tables['region']
    axes[1, 0].bar(regions.region, regions.team1_win_pct, color='#d28b3e')
    axes[1, 0].axhline(50, color='gray', linestyle='--', linewidth=1)
    axes[1, 0].set(title='Victorias del equipo 1 por plataforma', ylabel='Porcentaje', ylim=(0, 60))
    champions = pd.concat([dataset[f't{t}_{r}'] for t in (1, 2) for r in audit.ROLES]).value_counts().head(12)
    axes[1, 1].barh(champions.index[::-1], champions.values[::-1], color='#599b75')
    axes[1, 1].set(title='Campeones con más selecciones', xlabel='Apariciones')
    fig.suptitle(f'Perfilamiento: {len(dataset):,} partidas · {events["rows"]:,} eventos', fontsize=16)
    fig.savefig(out / 'distributions.png', dpi=160)
    plt.close(fig)

    event_table = pd.DataFrame([{'Tipo': k, 'Eventos': v, '%': v / extra.rows * 100} for k, v in events['event_types'].items()])
    files_table = pd.DataFrame([{'Archivo': n, 'Filas': events['rows'] if n == 'events.csv' else len(dataset) if n == 'dataset.csv' else len(milestone_frame),
                                'Columnas': events['columns'] if n == 'events.csv' else len(dataset.columns) if n == 'dataset.csv' else len(milestone_frame.columns),
                                'GB decimales': s['bytes'] / 1e9} for n, s in before.items()])
    duration = summary['numeric']['duration_minutes']
    structural_errors = {k: v for k, v in summary['quality_errors'].items() if v}
    event_errors = {k: v for k, v in events['errors'].items() if v}
    missing_milestones = milestone_frame.isna().sum()
    missing_table = pd.DataFrame([{'Campo': c, 'Nulos': int(v), '%': v / len(dataset) * 100} for c, v in missing_milestones.items() if v])
    quality_table = pd.DataFrame([{'Hallazgo': k, 'Partidas distintas': v, '% de partidas': v / len(dataset) * 100} for k, v in issues_unique.items()])
    lines = ['# Perfilamiento de ampliacion_50k_nuevos_parches', '',
             f'Generado: {report["generated_at_utc"]}. Fuente: `{source}`.', '',
             '## Alcance y resultado', '',
             'Lectura completa de los tres CSV, sin muestreo de eventos ni modificaciones de los datos originales. '
             'Se validaron claves, referencias entre archivos, JSON de cada evento, campos normalizados, orden por partida/frame/índice y coherencia de hitos y conteos. '
             'No se consultó la API ni se auditó SQLite.', '', table(files_table), '',
             f'Las filas del resumen y de hitos son únicas por `match_id`; eventos únicos certificados: **{events["event_uniqueness_certified"]}**. '
             f'Partidas sin hitos: {summary["missing_milestones"]}; sin eventos: {events["summary_matches_without_events"]}; '
             f'eventos huérfanos: {events["errors"].get("event_without_summary", 0)}. '
             f'Errores estructurales no nulos: `{structural_errors}`; errores en eventos no nulos: `{event_errors}`.', '',
             f'El resumen no tiene valores nulos: `{summary["missing_summary_values"]}`. '
             f'Columnas constantes del resumen: `{constants}`. El diccionario completo de las {len(profiles)} columnas está en [column_profile.csv](column_profile.csv).', '',
             '## Estructura y unidades', '',
             '- `dataset.csv`: una fila por partida; composición por rol, rango medio, resultado y objetivos finales. `game_duration` está en segundos y `game_creation` es un timestamp Unix en milisegundos.',
             '- `milestones.csv`: una fila por partida; primer equipo y tiempo de ocho objetivos. Los tiempos están en milisegundos desde el inicio de la partida.',
             '- `events.csv`: una fila por evento seleccionado por el recolector, no por cada evento posible de la API. `timestamp_ms` está en milisegundos y `minute` es su conversión a minutos. `raw_event` y `assisting_participant_ids` contienen JSON.',
             '- En eventos de estructuras, `team_id` puede identificar al dueño de la estructura destruida; `actor_team_id` identifica al atacante al que se atribuye el evento.',
             '- `t1` corresponde al equipo 100 y `t2` al 200. `win_team1=True` indica victoria del equipo 1. El rango usa Master=1, Grandmaster=2 y Challenger=3; cero indica desconocido. El promedio se calcula solo sobre jugadores con rango conocido.', '',
             '## Cobertura y equilibrio', '',
             f'Fechas de creación UTC: **{summary["game_dates_utc"][0]}** a **{summary["game_dates_utc"][1]}**. '
             f'Equipo 1 gana **{summary["team1_win_pct"]:.2f}%** ({summary["team1_wins"]:,} partidas); '
             f'equipo 2 gana **{100 - summary["team1_win_pct"]:.2f}%**. '
             'La etiqueta está aproximadamente equilibrada; esto no certifica representatividad de la población.', '',
             table(group_tables['region']), '', table(group_tables['patch_group']), '',
             'Los parches están agrupados por los dos primeros componentes de `patch`; los archivos originales conservan la versión completa. '
             'La distribución cruzada está en [patch_region_counts.csv](patch_region_counts.csv).', '',
             '## Comparación con la muestra base', '']
    if expansion:
        lines += [f'Base: **{expansion["base_matches"]:,}** partidas; conservadas: **{expansion["retained"]:,}**; '
                  f'faltantes: **{expansion["missing_base_ids"]:,}**; filas base modificadas: **{expansion["base_rows_changed"]:,}**. '
                  f'Partidas añadidas: **{expansion["new_matches"]:,}**.', '',
                  f'Parches nuevos: `{expansion["new_patch_counts"]}`. '
                  f'Nuevas fuera de 16.20/16.21: **{expansion["new_matches_outside_16_20_16_21"]:,}**. '
                  'Las versiones antiguas proceden de la muestra base conservada.', '']
    else:
        lines += ['No se proporcionó una muestra base disponible para comparar.', '']
    lines += ['## Duraciones y rangos', '',
              table(pd.DataFrame([{'Estadístico': k, 'Minutos': v} for k, v in duration.items()])), '',
              f'Partidas de menos de 5 minutos: **{summary["matches_under_5_minutes"]:,}** '
              f'({summary["matches_under_5_minutes"] / len(dataset) * 100:.2f}%); '
              f'mayores de 60 minutos: **{summary["matches_over_60_minutes"]:,}**. '
              f'El criterio de 1,5 IQR marca {report["duration_iqr_outliers"]:,} duraciones atípicas; no prueba que sean erróneas. '
              'Las partidas cortas deben examinarse antes de decidir si corresponden a remakes o abandonos.', '',
              f'Rango medio igual a cero en al menos un equipo: **{summary["matches_with_unknown_tier"]:,}** '
              f'({summary["matches_with_unknown_tier"] / len(dataset) * 100:.2f}%). '
              'El cero codifica rango desconocido. El promedio usa jugadores conocidos localmente y no acredita el rango histórico de todos los participantes.', '',
              '## Calidad semántica y desacuerdos entre fuentes', '', table(quality_table), '',
              f'Desacuerdos en conteos finales por campo: `{events["objective_count_disagreements"]}`. '
              'Los valores cuentan pares partida/campo; una misma partida puede afectar ambos equipos. '
              'El conteo del timeline incluye CHAMPION_KILL con killer_id positivo y atribución al actor. '
              'Estos desacuerdos no demuestran por sí solos cuál fuente es incorrecta.', '',
              f'Distribución del delta resumen menos timeline, por equipo con desacuerdo: `{dict(kill_deltas)}`. '
              'El timeline contiene ejecuciones con killer_id=0, que el auditor excluye del conteo de bajas atribuidas. '
              'Antes de corregir estos valores debe contrastarse la definición de cada fuente; no se atribuyó una causa a las diferencias.', '',
              f'Desacuerdos de primeras capturas entre resumen y timeline: `{events["first_objective_flag_disagreements"]}`. '
              f'Desacuerdos entre hitos y eventos: `{events["milestone_event_disagreements"]}`. '
              f'Hitos con tiempo y sin equipo: `{summary["milestone_time_without_team"]}`. '
              f'Hitos con tiempo pero cero capturas finales: `{summary["milestone_time_with_zero_final_kills"]}`.', '',
              'Los nulos de hitos separan objetivos sin tiempo observado de tiempos conocidos sin equipo atribuible. '
              'No deben rellenarse todos con cero: se perdería esta distinción.', '',
              'Los IDs de partidas afectadas y los deltas de bajas se entregan en [quality_flags.csv](quality_flags.csv).', '',
              '## Hitos y objetivos', '', table(objectives), '',
              'Los tiempos se expresan en minutos; en el CSV fuente son milisegundos. La tasa de victoria del primer equipo '
              'que obtiene un objetivo es una asociación descriptiva, condicionada a que se conozca ese equipo; no representa un efecto causal. '
              'Una cobertura baja puede reflejar la duración o la disponibilidad del objetivo en distintos parches. '
              'No deben interpretarse tasas de objetivos con pocos casos como estimaciones estables.', '',
              '## Eventos y campos vacíos', '', table(event_table), '',
              f'Eventos por partida, incluyendo las partidas con cero eventos: `{report["events_per_match"]}`. '
              f'Comprobaciones adicionales: `{dict(extra.checks)}`. '
              'La auditoría principal admite hasta 120 segundos de margen respecto a la duración final; '
              'el contador adicional events_after_exact_duration usa el límite exacto.', '',
              f'Caracterización de partidas sin eventos exportados: `{report["empty_event_matches"]}`. '
              'Que no haya eventos exportados no demuestra que falte el timeline: el recolector solo exporta tipos seleccionados. '
              'No se determinó automáticamente si estas partidas son remakes.', '',
              'Los campos de monstruos, edificios, víctimas y tipos de baja son condicionales al tipo de evento. '
              'Su ausencia global no debe interpretarse automáticamente como pérdida de datos. '
              'El desglose está en [event_missing_by_type.csv](event_missing_by_type.csv). '
              'Cada raw_event y lista de asistentes fue parseado por el auditor; '
              'raw_event no se trata como un predictor numérico.', '',
              '## Nulos de hitos', '', table(missing_table), '',
              '## Distribuciones', '', '![Distribuciones](distributions.png)', '',
              '## Criterios para preparar minería o modelos', '',
              '1. Separar variables previas al juego de objetivos y resultados finales; usar estos últimos para predecir desde el draft produciría fuga de información.',
              '2. Para predicción durante la partida, usar solo eventos anteriores al instante de corte y particionar por match_id para evitar compartir una partida entre entrenamiento y evaluación.',
              '3. Evaluar por tiempo, parche y plataforma. La mezcla de versiones es desigual y el aumento cambia su composición; una partición aleatoria no mide por sí sola generalización a futuros parches.',
              '4. Codificar rango desconocido y objetivos no observados de forma explícita. Verificar disponibilidad por parche antes de interpretar un cero como una captura posible no realizada.',
              '5. Elegir y documentar la fuente de verdad para primeras bajas y conteos; usar quality_flags.csv para revisar o segmentar discrepancias antes de entrenar.',
              '6. Definir una política de duración corta acorde al objetivo del análisis; los CSV originales se conservan íntegros.', '',
              '## Método, límites y reproducción', '',
              'Las estadísticas de resumen e hitos y los conteos de eventos son exhaustivos. '
              'Los percentiles de eventos por partida son exactos; no se calcularon percentiles numéricos por evento. '
              'La cardinalidad de eventos solo se calculó para campos categóricos; las demás celdas de cardinalidad se dejan vacías. '
              'La unicidad de event_id se certifica por su composición y orden global estricto, según el auditor existente. '
              'Se verificó que tamaños y fechas de modificación de los archivos no cambiaran durante la lectura; no se calcularon hashes de los archivos fuente.', '',
              '```powershell',
              f'.\\.venv\\Scripts\\python.exe analysis/profile_dataset.py --data-dir "{args.data_dir}" --output-dir "{args.output_dir}"' +
              (f' --base-dir "{args.base_dir}"' if args.base_dir else ''), '```', '',
              'Los resultados detallados están en [data_review.json](data_review.json), '
              '[numeric_summary.csv](numeric_summary.csv), [champions_by_role.csv](champions_by_role.csv) '
              'y las tablas de distribución. El informe de auditoría original se conserva en [data_review.md](data_review.md).', '']
    (out / 'perfilamiento.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'Completed in {(time.monotonic() - started) / 60:.1f} min: {out / "perfilamiento.md"}', flush=True)


if __name__ == '__main__':
    main()
