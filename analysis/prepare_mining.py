"""Validate existing CSV preprocessing and build research inputs, without mining.

Uses disk-backed DuckDB and ZSTD Parquet. Source files are never overwritten.
Run --help for paths and limits. No dataset downloads or model fitting.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.preprocess_dataset import CHAMPIONS, COUNTS, MONSTERS, SNAPSHOT_COUNTS, SNAPSHOT_UNKNOWN

FILES = ('dataset_clean.csv', 'milestones_clean.csv', 'events_clean.csv',
         'prematch_features.csv', 'outcomes.csv', 'snapshots.csv',
         'split_membership.csv', 'quality_flags.csv', 'feature_manifest.json',
         'preprocessing_report.json', 'preprocessing_report.md')
ROLES = ('top', 'jungle', 'middle', 'bottom', 'support')
NAMES = {'champion': 'kills', 'tower': 'towers', 'dragon': 'dragons',
         'herald': 'heralds', 'horde': 'grubs', 'baron': 'barons',
         'atakhan': 'atakhans', 'inhibitor': 'inhibitors'}
STATE_FEATURES = (['game_minute', 'game_phase'] +
                  [f'{name}_{suffix}' for name in NAMES.values() for suffix in ('team', 'enemy', 'diff')] +
                  [f'{name}_unattributed' for name in NAMES.values()] +
                  ['recorded_event_count', 'first_blood_observed', 'first_blood_owner',
                   'first_dragon_observed', 'first_dragon_owner'])
CONTEXT_FEATURES = ['patch_group'] + [f'{side}_{role}' for side in ('own', 'enemy') for role in ROLES]
METADATA = ['match_id', 'team_id', 'enemy_team_id', 'patch', 'patch_group', 'region', 'split']
RULE_POLICY = {
    'numeric_bins': {'disadvantage': 'difference < 0', 'equal': 'difference = 0', 'advantage': 'difference > 0'},
    'phase': {'early': 'minute < 15', 'middle': '15 <= minute < 25', 'late': 'minute >= 25'},
    'fitted_on_data': False,
    'rationale': 'Signo exacto de conteos discretos; fases operativas fijadas antes del análisis, no aprendidas de prueba.',
    'constraints': [
        'Minar train; seleccionar reglas/umbrales empíricos en validation; evaluar test una sola vez.',
        'Items de contexto como tiempo/fase se estratifican; no tratarlos como hallazgos estratégicos.',
        'En reglas supervisadas solo target_win es consecuente; nunca se incorpora a items.',
        'Para reglas no supervisadas evitar consecuentes constantes y relaciones definicionales.',
        'No mezclar diferencias numéricas, sus categorías y los conteos originales en una misma regla.',
        'Filtrar reglas con antecedente/consecuente iguales o contradictorios; comparar con tasa base.',
        'Reportar soporte por partidas distintas y equipo; snapshots/equipos de una partida no son independientes.',
        'No interpretar confianza o lift como causalidad ni como recomendación validada.',
    ],
}


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def phase(minute):
    return 'early' if minute < 15 else 'middle' if minute < 25 else 'late'


def balance(value):
    return 'advantage' if value > 0 else 'disadvantage' if value < 0 else 'equal'


def observed_first(events, cutoff, kind):
    """Independent Python reference used by the reproducible sample validator."""
    ordered = sorted((e for e in events if e['timestamp_ms'] <= cutoff),
                     key=lambda e: (e['timestamp_ms'], e['frame_index'], e['event_index']))
    if kind == 'blood':
        explicit = [e for e in ordered if e['event_type'] == 'CHAMPION_SPECIAL_KILL'
                    and e['kill_type'] == 'KILL_FIRST_BLOOD']
        candidates = explicit or [e for e in ordered if e['event_type'] == 'CHAMPION_KILL'
                                  and (e['killer_id'] or 0) > 0]
    else:
        candidates = [e for e in ordered if e['derived_objective'] == 'dragon']
    return candidates[0] if candidates else None


class ValidationError(RuntimeError):
    pass


class Validator:
    def __init__(self, connection):
        self.db = connection
        self.checks = []

    def check(self, name, query, scope='full_dataset'):
        failures = int(self.db.execute(query).fetchone()[0])
        self.checks.append({'name': name, 'scope': scope, 'failures': failures,
                            'passed': failures == 0})
        if failures:
            raise ValidationError(f'{name}: {failures} inconsistent records/groups')

    def equal(self, name, actual, expected, scope='full_dataset'):
        self.checks.append({'name': name, 'scope': scope, 'failures': int(actual != expected),
                            'passed': actual == expected})
        if actual != expected:
            raise ValidationError(f'{name}: actual={actual!r}; expected={expected!r}')


def load_csv(db, name, path):
    db.execute(f'CREATE TABLE {name} AS SELECT * FROM read_csv({literal(path)}, all_varchar=true, header=true)')


def derive_objective_sql():
    monster_cases = ' '.join(f'WHEN {literal(k)} THEN {literal(v)}' for k, v in MONSTERS.items())
    return f"""CASE WHEN event_type='CHAMPION_KILL' THEN 'champion'
        WHEN event_type='ELITE_MONSTER_KILL' THEN CASE upper(monster_type) {monster_cases} ELSE NULL END
        WHEN event_type='BUILDING_KILL' AND (upper(building_type) LIKE '%TOWER%' OR upper(building_type) LIKE '%TURRET%') THEN 'tower'
        WHEN event_type='BUILDING_KILL' AND upper(building_type) LIKE '%INHIBITOR%' THEN 'inhibitor'
        ELSE NULL END"""


def load_sources(db, source, original):
    for name, file in (('matches_raw', 'dataset_clean.csv'), ('milestones', 'milestones_clean.csv'),
                       ('prematch', 'prematch_features.csv'), ('labels', 'outcomes.csv'),
                       ('splits', 'split_membership.csv'), ('flags', 'quality_flags.csv')):
        load_csv(db, name, source / file)
    load_csv(db, 'original_matches', original / 'dataset.csv')
    load_csv(db, 'original_milestones', original / 'milestones.csv')
    db.execute("""CREATE TABLE matches AS SELECT * REPLACE (
        game_duration::DOUBLE AS game_duration, game_creation::BIGINT AS game_creation,
        win_team1::UTINYINT AS win_team1) FROM matches_raw""")
    snap_casts = ', '.join(f'{col}::INTEGER AS {col}' for col in ['minute', 'events_observed'] + SNAPSHOT_COUNTS + SNAPSHOT_UNKNOWN)
    db.execute(f"CREATE TABLE snapshots AS SELECT match_id, {snap_casts} FROM read_csv({literal(source / 'snapshots.csv')}, all_varchar=true)")
    print('Loading compact event CSV into temporary disk-backed DuckDB...', flush=True)
    db.execute(f"""CREATE TABLE events AS SELECT row_number() OVER () AS source_row,
        event_id,match_id,patch,timestamp_ms::BIGINT AS timestamp_ms,minute::DOUBLE AS minute,
        frame_index::INTEGER AS frame_index,event_index::INTEGER AS event_index,
        event_type,team_id::SMALLINT AS team_id,actor_team_id::SMALLINT AS actor_team_id,
        killer_id::SMALLINT AS killer_id,victim_id::SMALLINT AS victim_id,
        monster_type,building_type,kill_type,objective,after_game_end::UTINYINT AS after_game_end,
        {derive_objective_sql()} AS derived_objective
        FROM read_csv({literal(source / 'events_clean.csv')}, all_varchar=true)""")


def validate_sources(db, v, minutes, previous):
    for table in ('matches', 'original_matches', 'original_milestones', 'milestones', 'prematch', 'labels', 'splits'):
        v.check(f'{table}_unique_match_id', f'SELECT count(*) FROM (SELECT match_id FROM {table} GROUP BY match_id HAVING count(*)<>1 OR match_id IS NULL)')
    for table in ('milestones', 'prematch', 'labels', 'splits'):
        v.check(f'{table}_same_match_ids', f"SELECT count(*) FROM {table} FULL JOIN matches USING(match_id) WHERE {table}.match_id IS NULL OR matches.match_id IS NULL")
    v.check('valid_match_id', "SELECT count(*) FROM matches WHERE NOT regexp_full_match(match_id, '[A-Z0-9]+_[0-9]+')")
    v.check('retained_match_membership', f"""SELECT count(*) FROM matches FULL JOIN
        (SELECT match_id FROM original_matches WHERE game_duration::DOUBLE >= {previous['parameters']['min_duration_seconds']}) r USING(match_id)
        WHERE matches.match_id IS NULL OR r.match_id IS NULL""")
    v.check('patches_against_originals', """SELECT count(*) FROM matches m JOIN original_matches r USING(match_id)
        JOIN prematch p USING(match_id) JOIN milestones h USING(match_id)
        WHERE m.patch IS DISTINCT FROM r.patch OR h.patch IS DISTINCT FROM r.patch
        OR m.patch_group IS DISTINCT FROM split_part(r.patch,'.',1)||'.'||split_part(r.patch,'.',2)
        OR p.patch_group IS DISTINCT FROM m.patch_group""")
    comparisons = ['m.game_duration<>r.game_duration::DOUBLE', 'm.game_creation<>r.game_creation::BIGINT',
                   "m.win_team1<>CASE r.win_team1 WHEN 'True' THEN 1 ELSE 0 END", 'm.win_team1<>l.win_team1::INTEGER']
    comparisons += [f'm.{c} IS DISTINCT FROM r.{c} OR m.{c} IS DISTINCT FROM p.{c}' for c in CHAMPIONS]
    for t in (1, 2):
        comparisons += [f"m.t{t}_avg_tier::DOUBLE IS DISTINCT FROM nullif(r.t{t}_avg_tier::DOUBLE,0)",
                        f"m.t{t}_avg_tier_unknown::INTEGER <> (r.t{t}_avg_tier::DOUBLE=0)::INTEGER"]
    for col in SNAPSHOT_COUNTS:
        comparisons.append(f'm.{col}::INTEGER<>r.{col}::INTEGER')
    for t in (1, 2):
        for name in ('blood', 'tower', 'dragon', 'herald', 'horde', 'baron', 'atakhan', 'inhibitor'):
            comparisons.append(f"m.t{t}_first_{name}::INTEGER<>CASE r.t{t}_first_{name} WHEN 'True' THEN 1 ELSE 0 END")
    v.check('summary_transforms_against_originals', 'SELECT count(*) FROM matches m JOIN original_matches r USING(match_id) JOIN prematch p USING(match_id) JOIN labels l USING(match_id) WHERE ' + ' OR '.join(comparisons))
    clauses = []
    for name in ('blood', 'tower', 'dragon', 'herald', 'horde', 'baron', 'atakhan', 'inhibitor'):
        for suffix in ('team', 'time'):
            c = f'first_{name}_{suffix}'
            clauses.append(f'm.{c} IS DISTINCT FROM r.{c}')
        clauses.append(f'm.first_{name}_minute::DOUBLE IS DISTINCT FROM r.first_{name}_time::DOUBLE/60000')
    v.check('milestone_values_and_units', 'SELECT count(*) FROM milestones m JOIN original_milestones r USING(match_id) WHERE ' + ' OR '.join(clauses))
    v.check('snapshot_unique_key', 'SELECT count(*) FROM (SELECT match_id,minute FROM snapshots GROUP BY ALL HAVING count(*)<>1)')
    v.check('snapshot_orphans', 'SELECT count(*) FROM snapshots s LEFT JOIN matches m USING(match_id) WHERE m.match_id IS NULL')
    cuts = ','.join(map(str, minutes))
    v.check('snapshot_cutoffs_and_end', f'SELECT count(*) FROM snapshots s JOIN matches m USING(match_id) WHERE minute NOT IN ({cuts}) OR minute*60>=game_duration')
    v.check('snapshot_nonnegative_counts', 'SELECT count(*) FROM snapshots WHERE ' + ' OR '.join(f'{c} IS NULL OR {c}<0' for c in ['events_observed'] + SNAPSHOT_COUNTS + SNAPSHOT_UNKNOWN))
    v.check('events_unique_id', 'SELECT count(*) FROM (SELECT event_id FROM events GROUP BY event_id HAVING count(*)<>1 OR event_id IS NULL)')
    v.check('events_unique_source_key', 'SELECT count(*) FROM (SELECT match_id,frame_index,event_index FROM events GROUP BY ALL HAVING count(*)<>1)')
    v.check('event_integrity_units_and_patches', """SELECT count(*) FROM events e LEFT JOIN matches m USING(match_id)
        WHERE m.match_id IS NULL OR e.patch IS DISTINCT FROM m.patch
        OR event_id IS DISTINCT FROM e.match_id||':'||frame_index||':'||event_index
        OR timestamp_ms IS NULL OR timestamp_ms<0 OR frame_index<0 OR event_index<0
        OR minute IS NULL OR abs(minute-timestamp_ms/60000.0)>0.00000001
        OR actor_team_id NOT IN (100,200) OR team_id NOT IN (100,200)
        OR killer_id NOT BETWEEN 0 AND 10 OR victim_id NOT BETWEEN 0 AND 10
        OR timestamp_ms>game_duration*1000+120000
        OR after_game_end IS DISTINCT FROM (timestamp_ms>game_duration*1000)::INTEGER
        OR objective IS DISTINCT FROM derived_objective""")
    v.check('building_attacker_versus_owner', """SELECT count(*) FROM events WHERE
        event_type IN ('BUILDING_KILL','TURRET_PLATE_DESTROYED') AND team_id IS NOT NULL
        AND actor_team_id IS DISTINCT FROM 300-team_id""")
    v.check('frame_index_order', """SELECT count(*) FROM (SELECT *,
        lag(struct_pack(f:=frame_index,e:=event_index)) OVER(PARTITION BY match_id ORDER BY source_row) AS prev FROM events)
        WHERE struct_pack(f:=frame_index,e:=event_index)<=prev""")
    v.check('split_domain', "SELECT count(*) FROM splits WHERE split IS NULL OR split NOT IN ('train','validation','test','purged')")
    # The comparison below verifies actual label availability, not only start-date ordering.
    for earlier, later in (('train', 'validation'), ('validation', 'test'), ('train', 'test')):
        v.check(f'chronological_{earlier}_before_{later}', f"""SELECT CASE WHEN
            (SELECT max(game_creation+game_duration*1000) FROM matches JOIN splits USING(match_id) WHERE split='{earlier}')
            < (SELECT min(game_creation) FROM matches JOIN splits USING(match_id) WHERE split='{later}')
            THEN 0 ELSE 1 END""")
    v.check('existing_split_matches_original_policy', """SELECT count(*) FROM (
        SELECT m.match_id, s.split, CASE
            WHEN game_creation < (SELECT min(game_creation) FROM matches JOIN splits USING(match_id) WHERE split='validation')
            THEN CASE WHEN game_creation+game_duration*1000 < (SELECT min(game_creation) FROM matches JOIN splits USING(match_id) WHERE split='validation') THEN 'train' ELSE 'purged' END
            WHEN game_creation < (SELECT min(game_creation) FROM matches JOIN splits USING(match_id) WHERE split='test')
            THEN CASE WHEN game_creation+game_duration*1000 < (SELECT min(game_creation) FROM matches JOIN splits USING(match_id) WHERE split='test') THEN 'validation' ELSE 'purged' END
            ELSE 'test' END expected FROM matches m JOIN splits s USING(match_id)) WHERE split<>expected""")
    for table, key in (('original_matches', 'input_matches'), ('matches', 'retained_matches'), ('snapshots', None), ('events', None)):
        expected = previous[key] if key else previous['events']['snapshots' if table == 'snapshots' else 'output_events']
        v.equal(f'{table}_reported_row_count', db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], expected)


def validate_snapshot_counts(db, v, minutes):
    print('Recomputing all snapshot counters from event timestamps and event types...', flush=True)
    bucket = 'CASE ' + ' '.join(f'WHEN timestamp_ms<={m*60000} THEN {m}' for m in minutes) + ' ELSE NULL END'
    counters = []
    for objective in COUNTS:
        for prefix, team in (('t1', 100), ('t2', 200), ('unknown', None)):
            condition = 'actor_team_id IS NULL' if team is None else f'actor_team_id={team}'
            counters.append(f"count(*) FILTER(WHERE derived_objective='{objective}' AND {condition})::INTEGER AS {prefix}_{objective}_kills")
    db.execute(f"""CREATE TABLE event_buckets AS SELECT match_id,{bucket} AS bucket,
        count(*)::INTEGER AS events_observed,{','.join(counters)} FROM events
        WHERE timestamp_ms<={max(minutes)*60000} GROUP BY match_id,bucket""")
    db.execute('CREATE TABLE expected_snapshots AS SELECT s.match_id,s.minute,' +
               ','.join(f'coalesce(sum(b.{c}),0)::INTEGER AS {c}' for c in ['events_observed'] + SNAPSHOT_COUNTS + SNAPSHOT_UNKNOWN) +
               ' FROM snapshots s LEFT JOIN event_buckets b ON s.match_id=b.match_id AND b.bucket<=s.minute GROUP BY s.match_id,s.minute')
    v.check('all_snapshot_counters_match_events', 'SELECT count(*) FROM snapshots s JOIN expected_snapshots x USING(match_id,minute) WHERE ' +
            ' OR '.join(f's.{c}<>x.{c}' for c in ['events_observed'] + SNAPSHOT_COUNTS + SNAPSHOT_UNKNOWN))
    db.execute('CREATE TABLE cutoffs AS SELECT unnest([' + ','.join(map(str, minutes)) + '])::INTEGER AS minute')
    v.check('complete_snapshot_coverage', """SELECT count(*) FROM (SELECT m.match_id,c.minute
        FROM matches m CROSS JOIN cutoffs c WHERE game_duration>c.minute*60
        AND EXISTS(SELECT 1 FROM events e WHERE e.match_id=m.match_id)
        EXCEPT SELECT match_id,minute FROM snapshots)""")
    diffs = ','.join(f'{col}-lag({col}) OVER(PARTITION BY match_id ORDER BY minute) AS d{i}'
                     for i, col in enumerate(['events_observed'] + SNAPSHOT_COUNTS + SNAPSHOT_UNKNOWN))
    v.check('snapshot_counters_nondecreasing', f'SELECT count(*) FROM (SELECT {diffs} FROM snapshots) WHERE ' +
            ' OR '.join(f'd{i}<0' for i in range(25)))


def first_tables(db):
    for table, condition in (
        ('blood_fallback', "event_type='CHAMPION_KILL' AND killer_id>0"),
        ('blood_explicit', "event_type='CHAMPION_SPECIAL_KILL' AND kill_type='KILL_FIRST_BLOOD'"),
        ('dragon_first', "derived_objective='dragon'")):
        db.execute(f"""CREATE TABLE {table} AS SELECT e.match_id,timestamp_ms,actor_team_id FROM events e
            JOIN matches m USING(match_id) WHERE {condition} AND timestamp_ms<=game_duration*1000
            QUALIFY row_number() OVER(PARTITION BY e.match_id ORDER BY timestamp_ms,frame_index,event_index)=1""")


def create_states(db):
    first_tables(db)
    db.execute("""CREATE TABLE teams AS SELECT m.*,s.split,t.team_id,300-t.team_id AS enemy_team_id
        FROM matches m JOIN splits s USING(match_id) CROSS JOIN (VALUES(100),(200)) t(team_id)
        WHERE s.split<>'purged'""")
    fields = ['t.match_id', 't.team_id::SMALLINT AS team_id', 't.enemy_team_id::SMALLINT AS enemy_team_id',
              't.patch', 't.patch_group', 't.region', 't.split', 's.minute AS game_minute',
              "CASE WHEN s.minute<15 THEN 'early' WHEN s.minute<25 THEN 'middle' ELSE 'late' END AS game_phase",
              's.events_observed AS recorded_event_count']
    for objective, name in NAMES.items():
        own = f'CASE WHEN t.team_id=100 THEN s.t1_{objective}_kills ELSE s.t2_{objective}_kills END'
        enemy = f'CASE WHEN t.team_id=100 THEN s.t2_{objective}_kills ELSE s.t1_{objective}_kills END'
        fields += [f'{own} AS {name}_team', f'{enemy} AS {name}_enemy', f'({own})-({enemy}) AS {name}_diff',
                   f's.unknown_{objective}_kills AS {name}_unattributed']
    for role in ROLES:
        fields += [f'CASE WHEN t.team_id=100 THEN t.t1_{role} ELSE t.t2_{role} END AS own_{role}',
                   f'CASE WHEN t.team_id=100 THEN t.t2_{role} ELSE t.t1_{role} END AS enemy_{role}']
    blood_time = 'CASE WHEN be.timestamp_ms<=s.minute*60000 THEN be.timestamp_ms WHEN bf.timestamp_ms<=s.minute*60000 THEN bf.timestamp_ms ELSE NULL END'
    blood_team = 'CASE WHEN be.timestamp_ms<=s.minute*60000 THEN be.actor_team_id WHEN bf.timestamp_ms<=s.minute*60000 THEN bf.actor_team_id ELSE NULL END'
    dragon_time = 'CASE WHEN df.timestamp_ms<=s.minute*60000 THEN df.timestamp_ms ELSE NULL END'
    for name, stamp, team in (('blood', blood_time, blood_team), ('dragon', dragon_time, 'df.actor_team_id')):
        fields += [f'({stamp}) IS NOT NULL AS first_{name}_observed',
                   f"CASE WHEN ({stamp}) IS NULL THEN 'not_observed' WHEN ({team})=t.team_id THEN 'self' "
                   f"WHEN ({team})=t.enemy_team_id THEN 'enemy' ELSE 'unknown' END AS first_{name}_owner"]
    db.execute('CREATE TABLE strategic_states AS SELECT ' + ','.join(fields) + """ FROM teams t
        JOIN snapshots s USING(match_id) LEFT JOIN blood_explicit be USING(match_id)
        LEFT JOIN blood_fallback bf USING(match_id) LEFT JOIN dragon_first df USING(match_id)""")
    db.execute("""CREATE TABLE team_outcomes AS SELECT match_id,team_id::SMALLINT AS team_id,split,
        CASE WHEN team_id=100 THEN win_team1 ELSE 1-win_team1 END::UTINYINT AS target_win FROM teams""")
    item = lambda col: f"CASE WHEN {col}<0 THEN 'disadvantage' WHEN {col}>0 THEN 'advantage' ELSE 'equal' END"
    items = [f"'kills_balance='||{item('kills_diff')}", f"'dragons_balance='||{item('dragons_diff')}",
             f"'towers_balance='||{item('towers_diff')}", "'phase='||game_phase",
             "'first_blood='||first_blood_owner", "'first_dragon='||first_dragon_owner"]
    db.execute('CREATE TABLE association_transactions AS SELECT ' + ','.join(METADATA) +
               ',game_minute,[' + ','.join(items) + '] AS items FROM strategic_states')


def create_sequences(db):
    # Only exact equal timestamps are simultaneous. Nearby events retain order.
    db.execute("""CREATE TABLE strategic_events AS SELECT e.*,
        CASE WHEN event_type='CHAMPION_KILL' AND coalesce(killer_id,0)=0 THEN 'champion_death_uncredited'
             WHEN derived_objective='champion' THEN 'champion_kill' ELSE derived_objective END AS token
        FROM events e JOIN matches m USING(match_id)
        WHERE derived_objective IS NOT NULL AND timestamp_ms<=game_duration*1000""")
    bundle_query = """SELECT e.match_id,t.team_id,e.timestamp_ms,
        list_sort(list(DISTINCT CASE WHEN actor_team_id=t.team_id THEN 'self:' WHEN actor_team_id=t.enemy_team_id
            THEN 'enemy:' ELSE 'unattributed:' END||token)) AS items,
        list(event_id ORDER BY frame_index,event_index) AS event_ids,count(*)::INTEGER AS event_count
        FROM strategic_events e JOIN teams t USING(match_id)
        WHERE hash(e.match_id)%32={shard} GROUP BY e.match_id,t.team_id,e.timestamp_ms"""
    sequence_query = """SELECT t.match_id,t.team_id::SMALLINT AS team_id,
        t.enemy_team_id::SMALLINT AS enemy_team_id,t.patch,t.patch_group,t.region,t.split,
        coalesce(list(b.timestamp_ms ORDER BY b.timestamp_ms) FILTER(WHERE b.timestamp_ms IS NOT NULL), []::BIGINT[]) AS timestamps_ms,
        coalesce(list(b.items ORDER BY b.timestamp_ms) FILTER(WHERE b.timestamp_ms IS NOT NULL), []::VARCHAR[][]) AS itemsets,
        coalesce(list(b.event_ids ORDER BY b.timestamp_ms) FILTER(WHERE b.timestamp_ms IS NOT NULL), []::VARCHAR[][]) AS event_ids,
        coalesce(list(b.event_count ORDER BY b.timestamp_ms) FILTER(WHERE b.timestamp_ms IS NOT NULL), []::INTEGER[]) AS event_counts,
        coalesce(sum(b.event_count),0)::INTEGER AS strategic_events_observed
        FROM teams t LEFT JOIN sequence_bundles b ON t.match_id=b.match_id AND t.team_id=b.team_id
        WHERE hash(t.match_id)%32={shard}
        GROUP BY t.match_id,t.team_id,t.enemy_team_id,t.patch,t.patch_group,t.region,t.split"""
    # LIST/DISTINCT aggregation does not always spill. Bound each group of games
    # explicitly; never retain all event itemsets in one aggregation's memory.
    db.execute('CREATE TABLE sequence_bundles AS ' + bundle_query.format(shard=0) + ' LIMIT 0')
    db.execute('CREATE TABLE event_sequences AS ' + sequence_query.format(shard=0) + ' LIMIT 0')
    for shard in range(32):
        db.execute('INSERT INTO sequence_bundles ' + bundle_query.format(shard=shard))
        db.execute('INSERT INTO event_sequences ' + sequence_query.format(shard=shard))
        db.execute('DELETE FROM sequence_bundles')
        if (shard + 1) % 8 == 0:
            print(f'Sequence shards: {shard + 1}/32', flush=True)


def sample_validation(db, v, seed, sample_matches):
    ids = [r[0] for r in db.execute('SELECT match_id FROM matches ORDER BY match_id').fetchall()]
    selected = random.Random(seed).sample(ids, min(sample_matches, len(ids)))
    # Also always inspect observed after-end events, unknown objectives and the split boundaries.
    edges = db.execute('SELECT DISTINCT match_id FROM events WHERE after_game_end=1').fetchall()
    edges += db.execute("SELECT DISTINCT match_id FROM events WHERE derived_objective IS NOT NULL AND actor_team_id IS NULL ORDER BY match_id LIMIT 12").fetchall()
    edges += db.execute('SELECT match_id FROM matches ORDER BY game_creation,match_id LIMIT 3').fetchall()
    edges += db.execute('SELECT match_id FROM matches ORDER BY game_creation DESC,match_id LIMIT 3').fetchall()
    selected = sorted(set(selected) | {r[0] for r in edges})
    condition = ','.join(literal(mid) for mid in selected)
    result = db.execute(f'SELECT * FROM events WHERE match_id IN ({condition}) ORDER BY match_id, timestamp_ms,frame_index,event_index')
    columns = [d[0] for d in result.description]
    events = defaultdict(list)
    for row in result.fetchall():
        event = dict(zip(columns, row))
        events[event['match_id']].append(event)
    result = db.execute(f'SELECT * FROM strategic_states WHERE match_id IN ({condition})')
    columns = [d[0] for d in result.description]
    checked = 0
    for values in result.fetchall():
        row = dict(zip(columns, values))
        history = [e for e in events[row['match_id']] if e['timestamp_ms'] <= row['game_minute'] * 60000]
        counts = Counter((e['derived_objective'], e['actor_team_id']) for e in history if e['derived_objective'])
        if row['recorded_event_count'] != len(history):
            raise ValidationError('Independent sample: recorded event count mismatch')
        for objective, name in NAMES.items():
            own, enemy = counts[objective, row['team_id']], counts[objective, row['enemy_team_id']]
            for suffix, expected in (('team', own), ('enemy', enemy), ('diff', own-enemy),
                                     ('unattributed', counts[objective, None])):
                if row[f'{name}_{suffix}'] != expected:
                    raise ValidationError(f'Independent sample mismatch: {row["match_id"]} {name}_{suffix}')
        for kind in ('blood', 'dragon'):
            first = observed_first(events[row['match_id']], row['game_minute'] * 60000, kind)
            owner = ('not_observed' if first is None else 'self' if first['actor_team_id'] == row['team_id']
                     else 'enemy' if first['actor_team_id'] == row['enemy_team_id'] else 'unknown')
            if row[f'first_{kind}_owner'] != owner or row[f'first_{kind}_observed'] != (first is not None):
                raise ValidationError(f'Independent sample: first {kind} mismatch')
        checked += 1
    v.equal('independent_python_sample_states', checked > 0, True, 'seeded_sample_and_edge_matches')
    # Full sequences are checked against their exact event-ID order on sampled games.
    sequence_rows = db.execute(f'SELECT match_id,team_id,timestamps_ms,event_ids,itemsets FROM event_sequences WHERE match_id IN ({condition})').fetchall()
    sequences = 0
    durations = dict(db.execute('SELECT match_id,game_duration*1000 FROM matches').fetchall())
    for mid, team, stamps, bundles, itemsets in sequence_rows:
        expected = [e for e in events[mid] if e['derived_objective'] and e['timestamp_ms'] <= durations[mid]]
        if [eid for bundle in bundles for eid in bundle] != [e['event_id'] for e in expected]:
            raise ValidationError('Independent sample: sequence event IDs/order mismatch')
        if stamps != sorted(set(e['timestamp_ms'] for e in expected)):
            raise ValidationError('Independent sample: sequence timestamp mismatch')
        expected_items = defaultdict(set)
        for event in expected:
            name = event['derived_objective']
            if name == 'champion':
                name = 'champion_kill' if (event['killer_id'] or 0) > 0 else 'champion_death_uncredited'
            actor = event['actor_team_id']
            owner = 'self' if actor == team else 'enemy' if actor == 300-team else 'unattributed'
            expected_items[event['timestamp_ms']].add(owner + ':' + name)
        if itemsets != [sorted(expected_items[stamp]) for stamp in stamps]:
            raise ValidationError('Independent sample: sequence itemsets/team mismatch')
        sequences += 1
    v.equal('independent_python_sample_sequences', sequences > 0, True, 'seeded_sample_and_edge_matches')
    return {'seed': seed, 'selected_match_ids': selected, 'states_checked': checked, 'sequences_checked': sequences}


def validate_outputs(db, v):
    v.check('state_key_uniqueness', 'SELECT count(*) FROM (SELECT match_id,team_id,game_minute FROM strategic_states GROUP BY ALL HAVING count(*)<>1)')
    v.check('state_team_pairing', 'SELECT count(*) FROM (SELECT match_id,game_minute FROM strategic_states GROUP BY ALL HAVING count(*)<>2 OR sum(team_id)<>300)')
    for name in NAMES.values():
        v.check(f'symmetric_{name}', f"""SELECT count(*) FROM strategic_states a JOIN strategic_states b
            ON a.match_id=b.match_id AND a.game_minute=b.game_minute AND a.team_id=b.enemy_team_id
            WHERE a.{name}_team<>b.{name}_enemy OR a.{name}_diff<>-b.{name}_diff
            OR a.{name}_diff<>a.{name}_team-a.{name}_enemy""")
    for table in ('strategic_states', 'association_transactions', 'event_sequences', 'team_outcomes'):
        v.check(f'{table}_no_purged_or_wrong_split', f"SELECT count(*) FROM {table} o JOIN splits s USING(match_id) WHERE o.split='purged' OR o.split<>s.split")
    v.check('symmetric_outcomes', 'SELECT count(*) FROM (SELECT match_id FROM team_outcomes GROUP BY match_id HAVING count(*)<>2 OR sum(target_win)<>1)')
    v.check('sequence_key_uniqueness', 'SELECT count(*) FROM (SELECT match_id,team_id FROM event_sequences GROUP BY ALL HAVING count(*)<>1)')
    v.check('sequence_parallel_arrays_and_order', """SELECT count(*) FROM event_sequences WHERE
        len(timestamps_ms)<>len(itemsets) OR len(timestamps_ms)<>len(event_ids) OR len(timestamps_ms)<>len(event_counts)
        OR timestamps_ms<>list_sort(timestamps_ms) OR len(timestamps_ms)<>len(list_distinct(timestamps_ms))
        OR coalesce(list_sum(event_counts),0)<>strategic_events_observed""")
    v.check('sequence_event_conservation', """SELECT abs(
        (SELECT coalesce(sum(strategic_events_observed),0) FROM event_sequences)-
        (SELECT count(*)*2 FROM strategic_events JOIN splits USING(match_id) WHERE split<>'purged'))""")
    v.check('sequence_events_before_end', """SELECT count(*) FROM event_sequences s JOIN matches m USING(match_id)
        WHERE list_max(timestamps_ms)>game_duration*1000 OR list_min(timestamps_ms)<0""")
    v.check('transaction_items_valid', """SELECT count(*) FROM association_transactions
        WHERE len(items)<>6 OR len(list_distinct(items))<>6
        OR EXISTS(SELECT 1 FROM unnest(items) i(item) WHERE item IS NULL OR item LIKE '%outcome%' OR item LIKE '%target%' OR item LIKE '%win%')""")
    v.equal('states_have_no_outcome_or_final_columns',
            set(r[0] for r in db.execute('DESCRIBE strategic_states').fetchall()) &
            {'win_team1', 'target_win', 'game_duration', 'game_creation', 't1_avg_tier', 't2_avg_tier'}, set())


def quality_summary(db):
    return {
        'existing_flags': dict(db.execute('SELECT issue,count(*) FROM flags GROUP BY issue ORDER BY issue').fetchall()),
        'event_types': dict(db.execute('SELECT event_type,count(*) FROM events GROUP BY event_type ORDER BY event_type').fetchall()),
        'unknown_actor_by_objective': dict(db.execute("SELECT coalesce(derived_objective,'other'),count(*) FROM events WHERE actor_team_id IS NULL GROUP BY derived_objective").fetchall()),
        'post_end_events_excluded_from_sequences': db.execute('SELECT count(*) FROM events WHERE after_game_end=1').fetchone()[0],
        'retained_without_exported_events': db.execute('SELECT count(*) FROM matches m WHERE NOT EXISTS(SELECT 1 FROM events e WHERE e.match_id=m.match_id)').fetchone()[0],
        'original_without_exported_events': None,
        'source_timestamp_inversions': db.execute("""SELECT count(*) FROM (SELECT timestamp_ms,
            lag(timestamp_ms) OVER(PARTITION BY match_id ORDER BY source_row) prev FROM events) WHERE timestamp_ms<prev""").fetchone()[0],
        'unknown_milestone_teams': {},
    }


def manifest():
    return {
        'version': 1, 'patch_type': 'VARCHAR, exact text; never decimal',
        'strategic_states.parquet': {'key': ['match_id', 'team_id', 'game_minute'],
            'metadata': METADATA, 'candidate_predictors': STATE_FEATURES + CONTEXT_FEATURES,
            'recommended_nonredundant_numeric_predictors': [f'{n}_diff' for n in NAMES.values()],
            'warning': 'Counts, differences and categories overlap; select a representation per analysis.'},
        'association_transactions.parquet': {'key': ['match_id', 'team_id', 'game_minute'],
            'antecedent_column': 'items', 'metadata': METADATA + ['game_minute'], 'target': None},
        'event_sequences.parquet': {'key': ['match_id', 'team_id'], 'metadata': METADATA,
            'sequence': 'itemsets', 'timestamps': 'timestamps_ms', 'event_provenance': 'event_ids',
            'scope': 'full_match_history', 'simultaneous_policy': 'same exact timestamp = one itemset; stable event IDs; distinct tokens',
            'contextual_use': 'Must truncate with iter_sequence_prefixes(cutoff_ms=...) before any contextual experiment.',
            'absence': 'No token means not observed in exported event selection; never infer non-occurrence.'},
        'team_outcomes.parquet': {'key': ['match_id', 'team_id'], 'target': 'target_win',
            'usage': 'Join only as label or supervised consequent, never inside items or predictors.'},
        'forbidden_predictors': ['match_id', 'team_id', 'enemy_team_id', 'region', 'split',
            'target_win', 'win_team1', 'game_duration', 'game_creation', 'final objectives',
            'retrospective quality flags', 'ranks', 'full future event sequence'],
        'learned_transformations': 'None fitted by this process. Fit future encoders/scalers/imputers on train only.',
        'unknowns': 'Unattributed captures are explicit counters; first owner uses unknown/not_observed separately.',
        'policies': RULE_POLICY,
        'runtime_compatibility': 'These are observed historical event features. Availability and allowed display in Live Client API still need separate validation.',
    }


def write_report(path, result):
    c, q = result['counts'], result['quality']
    checks = result['checks']
    lines = ['# Validación y preparación del dataset para minería de datos', '',
             '## 1. Fuentes y alcance', '',
             f"Fuente original: `{result['original']}`. Fuente procesada: `{result['processed']}`.",
             'Se reutilizó el preprocesamiento previo; no se descargaron partidas ni se reescribieron CSV. '
             'El archivo original de eventos de más de 21 GB se conserva sin volver a procesar su JSON crudo. '
             'La validación exhaustiva actual cubre los campos del CSV compacto; la auditoría previa del JSON es evidencia histórica, no una comprobación repetida.', '',
             '## 2. Estadísticas originales y derivadas', '',
             '| Estructura | Filas |', '|---|---:|']
    lines += [f'| {name} | {count:,} |' for name, count in c.items()]
    lines += ['', f"Regiones: `{result['regions']}`.",
              f"Versiones completas originales: **{result['patches']['original_full_cardinality']}**; grupos originales: **{result['patches']['original_group_cardinality']}**; grupos conservados: **{result['patches']['retained_group_cardinality']}**.", '',
              '## 3. Diagnóstico inicial y correcciones', '',
              'Los valores de patch y patch_group en los CSV coincidieron con los originales. '
              'No se encontraron versiones perdidas en esos archivos. La conversión a decimal reduciría la cardinalidad '
              f"de grupos de {result['patches']['retained_group_cardinality']} a {result['patches']['numeric_collision_cardinality']}; es una colisión confirmada si se usa lectura numérica. "
              'Se añadió un lector común que fija patch, patch_group y match_id como texto, y se actualizó su uso en análisis y perfilamiento. '
              'Los Parquet nuevos tienen esquema VARCHAR explícito para versiones.',
              'Las estructuras de transacciones y secuencias no existían. Se generaron en una carpeta independiente. '
              'Las particiones anteriores superaron las verificaciones y se conservaron.', '',
              '## 4. Calidad, ausencias y registros especiales', '',
              '| Situación | Política para los experimentos |', '|---|---|',
              '| Duración inferior a 300 s | Se conserva la exclusión previa. No se interpreta automáticamente como remake. |',
              '| Rango cero/desconocido | Nulo con indicador en CSV; excluido de predictores porque no acredita rango histórico. |',
              '| Actor desconocido | Se conserva y cuenta aparte; token unattributed. No se adivina equipo por participantId. |',
              '| Resumen y timeline discrepan | Se mantienen ambas fuentes y las incidencias. Estados/secuencias usan timeline; resultado usa resumen. No se eliminan partidas automáticamente. |',
              '| Primera sangre discrepa | Se usa evento explícito si ya fue observado al corte; en su ausencia, primera baja con killerId positivo. Las banderas finales del resumen no son entradas. |',
              '| Hito con tiempo y equipo desconocido | Se conserva el nulo. Un equipo posterior conocido no reemplaza la primera captura desconocida. |',
              '| Objetivo no observado al corte | Estado not_observed; no significa imposible o no ocurrido en el videojuego. |',
              '| Evento después del final | Permanece en CSV con indicador; excluido de secuencias. Los cortes anteriores al final ya impiden usarlo en estados. |',
              '| Partida sin eventos exportados | No inventar estados de ceros. La cobertura actual se informa a continuación. |',
              '| Objetivos ausentes en determinados parches | Cero indica cero eventos exportados; no certifica disponibilidad del objetivo en ese parche. |', '',
              'Conteos de incidencias (puede haber varias por partida):', '', '```json',
              json.dumps(q, ensure_ascii=False, indent=2), '```', '',
              'Los indicadores retrospectivos de calidad no son predictores. Pueden emplearse en un análisis de sensibilidad '
              'predefinido, informando cuántas partidas afecta; no para seleccionar a posteriori un resultado favorable.', '',
              '## 5. Integración y variables', '',
              'match_id integra las tablas de partidas, resultados, particiones, hitos y eventos. '
              'Los estados usan (match_id, team_id, game_minute); los resultados y secuencias, (match_id, team_id). '
              'Se generan dos perspectivas simétricas por partida: own/team y enemy. La numeración 100/200 queda como metadato, no como predictor.',
              'Se conservan conteos de bajas, torres, dragones, heraldo, larvas, Barón, Atakhan e inhibidores; '
              'se generan conteos propios, enemigos, diferencias y capturas sin atribución. '
              'También se añaden minuto, fase operativa, primera sangre/dragón observados y su propietario relativo. '
              'La composición por rol se expresa desde la perspectiva de cada equipo.',
              'No se ajustan imputadores, escaladores, encoders ni umbrales a estos datos. '
              'Oro, CS, inventario, vida, enfriamientos y posiciones continuas no pueden reconstruirse con estas fuentes.', '',
              '## 6. Construcción y validación temporal', '',
              f"Cortes heredados: `{result['parameters']['minutes']}` minutos. Solo se cuentan eventos con timestamp_ms <= minuto × 60000, "
              'y solo hay estados mientras minuto × 60 < duración. Se recalcularon todos los conteos acumulados '
              'independientemente a partir del tipo y tiempo de evento, sin confiar en la columna objective ya calculada.',
              'timestamp_ms y los tiempos de hitos son milisegundos desde el inicio; game_duration está en segundos; '
              'game_creation es Unix en milisegundos UTC. game_minute es minuto transcurrido.',
              'La atribución de estructuras se verifica contra el propietario destruido (equipo opuesto). '
              'Para bajas se conserva actor_team_id validado por el recolector; estos CSV no contienen un mapa independiente '
              'participantId→equipo que permita volver a certificar esa atribución desde cero.', '',
              '## 7. Transacciones y secuencias', '',
              'Cada transacción representa una partida, equipo y corte. items contiene seis categorías: '
              'balance de bajas, dragones y torres, fase, propietario de primera sangre y primer dragón. '
              'Los balances usan el signo exacto de la diferencia. Las fases son early (<15), middle (15–<25) '
              'y late (>=25): convenciones operativas, no fronteras aprendidas ni afirmaciones universales sobre estrategia.',
              'El resultado se guarda en team_outcomes.parquet. Puede incorporarse como consecuente supervisado, '
              'pero nunca a items ni al vector de características. rule_policy.json documenta restricciones '
              'para evitar reglas definicionales, redundantes o dominadas por categorías de contexto.',
              'Las secuencias se construyen desde eventos individuales, no desde snapshots. '
              'Los eventos de igual timestamp forman un itemset; los próximos pero distintos conservan su orden real. '
              'Se preservan todos los IDs y multiplicidades; tokens iguales simultáneos se deduplican dentro del itemset. '
              'Se omiten placas y eventos especiales duplicados para centrarse en bajas y objetivos identificados. '
              'Las secuencias completas son datos históricos; iter_sequence_prefixes exige un corte para su uso contextual. '
              'No se ejecutó Apriori, FP-Growth, PrefixSpan ni entrenamiento.', '',
              '## 8. Particiones y prevención de fuga', '',
              f"Asignación conservada: `{result['splits']}`. Los registros purged no aparecen en ningún Parquet de experimentos.",
              'Se verificó unicidad por partida, correspondencia de todos los estados y que todos los resultados '
              'de entrenamiento sean anteriores al inicio de validación, y los de validación anteriores a prueba. '
              'Toda transformación aprendida futura debe ajustarse únicamente con entrenamiento; la selección de '
              'hiperparámetros/reglas usa validación y la prueba se reserva para evaluación final.',
              'La evaluación actual es cronológica, pero no constituye una prueba de generalización a un parche nunca visto: '
              'el parche 16.21 ya aparece en entrenamiento y también en validación/prueba en esta muestra. '
              'La tabla patches_by_split en el JSON permite verificarlo sin asumirlo. '
              'Una evaluación entre versiones necesitaría un protocolo aparte, con un parche de prueba totalmente ausente '
              'del desarrollo; no se cambió la partición actual. Tampoco se garantiza separación por jugador.',
              'Para reglas se debe trabajar por corte o corregir la repetición de observaciones: los dos equipos y '
              'varios estados de una partida están relacionados. No tratarlos como ejemplos independientes al estimar incertidumbre.', '',
              '## 9. Validaciones ejecutadas', '',
              f"Comprobaciones superadas: **{sum(x['passed'] for x in checks)}/{len(checks)}**. "
              f"Semilla de muestreo: **{result['sample']['seed']}**; estados contrastados en Python: **{result['sample']['states_checked']}**; "
              f"secuencias contrastadas: **{result['sample']['sequences_checked']}**.",
              'El JSON incluye cada comprobación, su alcance, los IDs muestreados, cardinalidades y distribuciones. '
              'Se contrastaron los conteos de todas las filas de snapshots, además del muestreo independiente y casos límite. '
              'Los ficheros fuente mantuvieron tamaño y fecha de modificación; no se recalcularon hashes de los 21 GB originales.', '',
              '## 10. Almacenamiento y reproducibilidad', '',
              f"Motor: DuckDB {result['software']['duckdb']}; límite configurado: {result['parameters']['memory_limit']}; "
              'base y spill temporales en disco. Las agregaciones anidadas de secuencias se ejecutan en 32 grupos de partidas '
              'para no depender de que LIST/DISTINCT pueda volcar su memoria a disco. Un primer intento global excedió '
              'el límite de 2 GB y no publicó datos; la validación final corresponde al proceso incremental corregido. '
              'Se eligió frente a cargar todo en pandas o añadir motores equivalentes. '
              'Parquet usa ZSTD y tipos explícitos. La base temporal se elimina al terminar; no se duplica permanentemente el archivo grande de eventos.', '',
              '| Archivo | Uso |', '|---|---|',
              '| strategic_states.parquet | Características parciales simétricas por equipo y minuto. |',
              '| association_transactions.parquet | Itemsets de situaciones para reglas de asociación. |',
              '| event_sequences.parquet | Secuencias históricas por equipo, con tiempos y procedencia. |',
              '| team_outcomes.parquet | Resultado final, separado de los predictores. |',
              '| feature_manifest.json | Claves, predictores, metadatos y variables prohibidas. |',
              '| rule_policy.json | Discretización y restricciones para investigar reglas. |',
              '| preprocessing_validation.json | Evidencia de validación y distribuciones. |',
              '| initial_diagnosis.json | Problemas confirmados, riesgos y pendientes al inicio. |',
              '| preprocessing_report.md | Este informe. |', '',
              '```powershell', '.\\.venv\\Scripts\\python.exe -m pip install --only-binary=:all: -r requirements-preprocessing.txt',
              '.\\.venv\\Scripts\\python.exe -B analysis/prepare_mining.py --processed-dir data/processed/ampliacion_50k_nuevos_parches --original-dir data/weekend/ampliacion_50k_nuevos_parches --output-dir data/mining_ready/ampliacion_50k_nuevos_parches',
              '.\\.venv\\Scripts\\python.exe -B -m unittest tests.test_mining_ready tests.test_preprocessing tests.test_patch_types -v', '```', '',
              'Para repetir, elija una nueva carpeta de salida: el proceso no sobrescribe resultados existentes.', '',
              '## 11. Límites y atención posterior', '',
              'La validación estructural no demuestra cobertura completa del videojuego ni causalidad. '
              'Las fuentes no permiten volver a filtrar modo/cola/mapa ni validar inventarios. '
              'La disponibilidad por parche de ciertos objetivos, la atribución de participantes sin mapa de equipos, '
              'la correspondencia con la API local en vivo y la calidad empírica de futuros patrones requieren trabajos separados. '
              'Estas limitaciones no se presentan como comprobaciones superadas. '
              'No hay validaciones implementadas pendientes al publicar: si alguna falla, no se publica la salida final.', '']
    path.write_text('\n'.join(lines), encoding='utf-8')


def source_signatures(paths):
    return {str(p): {'bytes': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns} for p in paths}


def run(processed, original, output, seed=42, sample_matches=100, memory_limit='2GB'):
    processed, original, output = (Path(p).resolve() for p in (processed, original, output))
    if output.exists():
        raise FileExistsError(f'Output already exists; choose another directory: {output}')
    for source in (processed, original):
        if output == source or source in output.parents or output in source.parents:
            raise ValueError('Output must be independent of both source directories')
    if sample_matches <= 0:
        raise ValueError('sample_matches must be positive')
    paths = [processed / n for n in FILES] + [original / n for n in ('dataset.csv', 'milestones.csv', 'events.csv')]
    before = source_signatures(paths)
    previous = json.loads((processed / 'preprocessing_report.json').read_text(encoding='utf-8'))
    minutes = previous['parameters']['snapshot_minutes']
    if not minutes or sorted(set(minutes)) != minutes or any(type(m) is not int or m <= 0 for m in minutes):
        raise ValueError('Invalid snapshot cutoff configuration')
    started = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    diagnosis = {
        'confirmed': ['CSV versions are lexical values; exact original comparison is performed below.',
                      'Default numeric CSV inference can collapse 16.20/16.2 and 16.10/16.1.',
                      'Existing exports omit raw_event and do not include purchases or participant frames.',
                      'Transactions and strategic sequences need new derived structures.'],
        'potential_risks': ['Outcome/future-event leakage', 'Unknown actors confused with no observation',
                            'Repeated states/teams treated as independent', 'Chronological split mistaken for unseen-patch evaluation'],
        'pending_at_start': ['Full snapshot counter reconstruction', 'Split and ID validation',
                             'Team attribution consistency checks', 'Seeded state/sequence checks', 'Parquet roundtrip'],
        'resolution': 'See named checks and limitations in preprocessing_validation.json and the report.'}
    with tempfile.TemporaryDirectory(prefix=output.name + '.building-', dir=output.parent) as temp:
        temp = Path(temp)
        stage = temp / 'result'
        stage.mkdir()
        db = duckdb.connect(str(temp / 'work.duckdb'))
        db.execute(f'SET memory_limit={literal(memory_limit)}')
        db.execute('SET threads=2')
        db.execute(f'SET temp_directory={literal(temp / "spill")}')
        v = Validator(db)
        try:
            load_sources(db, processed, original)
            validate_sources(db, v, minutes, previous)
            validate_snapshot_counts(db, v, minutes)
            print('Building symmetric states and association transactions...', flush=True)
            create_states(db)
            print('Building chronological sequences from individual events...', flush=True)
            create_sequences(db)
            validate_outputs(db, v)
            sample = sample_validation(db, v, seed, sample_matches)
            quality = quality_summary(db)
            for name in ('blood', 'tower', 'dragon', 'herald', 'horde', 'baron', 'atakhan', 'inhibitor'):
                quality['unknown_milestone_teams'][name] = db.execute(f'SELECT count(*) FROM milestones WHERE first_{name}_time IS NOT NULL AND first_{name}_team IS NULL').fetchone()[0]
            tables = ('strategic_states', 'association_transactions', 'event_sequences', 'team_outcomes')
            counts = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                      for name in ('original_matches', 'matches', 'events', 'snapshots') + tables}
            counts['original_events_previously_audited'] = previous['events']['input_events']
            counts['excluded_short_matches'] = previous['excluded_short_matches']
            for name in tables:
                print(f'Writing {name}.parquet ({counts[name]:,} rows)...', flush=True)
                db.execute(f'COPY {name} TO {literal(stage / (name + ".parquet"))} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 16384)')
                shards = range(32) if name == 'event_sequences' else [None]
                for shard in shards:
                    where = '' if shard is None else f' WHERE hash(match_id)%32={shard}'
                    left = f'SELECT * FROM {name}{where}'
                    right = f'SELECT * FROM read_parquet({literal(stage / (name + ".parquet"))}){where}'
                    suffix = '' if shard is None else f'_shard_{shard}'
                    v.check(f'{name}_parquet_roundtrip{suffix}', f'SELECT count(*) FROM (({left} EXCEPT ALL {right}) UNION ALL ({right} EXCEPT ALL {left}))')
                schema = dict((r[0], r[1]) for r in db.execute(f'DESCRIBE SELECT * FROM read_parquet({literal(stage / (name + ".parquet"))})').fetchall())
                if name != 'team_outcomes':
                    v.equal(f'{name}_patch_schema', (schema['patch'], schema['patch_group']), ('VARCHAR', 'VARCHAR'))
            original_full = db.execute('SELECT count(DISTINCT patch) FROM original_matches').fetchone()[0]
            original_groups = db.execute("SELECT count(DISTINCT split_part(patch,'.',1)||'.'||split_part(patch,'.',2)) FROM original_matches").fetchone()[0]
            groups, decimals = db.execute('SELECT count(DISTINCT patch_group),count(DISTINCT patch_group::DOUBLE) FROM matches').fetchone()
            patches_by_split = [dict(zip(('split', 'patch_group', 'matches'), row)) for row in db.execute('SELECT split,patch_group,count(*) FROM matches JOIN splits USING(match_id) GROUP BY ALL ORDER BY split,patch_group').fetchall()]
            result = {'status': 'passed', 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                      'processed': str(processed), 'original': str(original), 'output': str(output),
                      'parameters': {'minutes': minutes, 'seed': seed, 'sample_matches': sample_matches, 'memory_limit': memory_limit, 'sequence_shards': 32},
                      'counts': counts, 'quality': quality, 'checks': v.checks, 'sample': sample,
                      'splits': dict(db.execute('SELECT split,count(*) FROM splits GROUP BY split').fetchall()),
                      'patches': {'original_full_cardinality': original_full, 'original_group_cardinality': original_groups,
                                 'retained_group_cardinality': groups, 'numeric_collision_cardinality': decimals,
                                 'patches_by_split': patches_by_split},
                      'regions': dict(db.execute('SELECT region,count(*) FROM matches GROUP BY region').fetchall()),
                      'source_files': before, 'source_verification': 'size and mtime; no full original-event hash or raw JSON reaudit',
                      'software': {'python': sys.version, 'duckdb': duckdb.__version__},
                      'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      'learned_transformations_fitted': [], 'pending_implemented_checks': [],
                      'limitations_requiring_future_work': ['Event export coverage', 'Unseen-patch evaluation',
                          'Independent participant-to-team mapping', 'Live API feature compatibility', 'Empirical mining/model evaluation'],
                      'elapsed_seconds': round(time.monotonic() - started, 2)}
            v.equal('source_files_unchanged', source_signatures(paths), before)
            result['source_files_unchanged'] = True
            result['output_bytes'] = {p.name: p.stat().st_size for p in stage.iterdir()}
            save_json(stage / 'feature_manifest.json', manifest())
            save_json(stage / 'rule_policy.json', RULE_POLICY)
            save_json(stage / 'initial_diagnosis.json', diagnosis)
            save_json(stage / 'preprocessing_validation.json', result)
            write_report(stage / 'preprocessing_report.md', result)
        except Exception as exc:
            # Preserve concrete failure evidence, not a partially published dataset.
            save_json(output.parent / (output.name + '.failed_validation.json'),
                      {'status': 'failed', 'error': str(exc), 'checks': v.checks, 'diagnosis': diagnosis})
            raise
        finally:
            db.close()
        os.replace(stage, output)
    print(f'Validated and published: {output}', flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--processed-dir', type=Path, required=True)
    parser.add_argument('--original-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--sample-matches', type=int, default=100)
    parser.add_argument('--memory-limit', default='2GB')
    args = parser.parse_args()
    run(args.processed_dir, args.original_dir, args.output_dir, args.seed, args.sample_matches, args.memory_limit)


if __name__ == '__main__':
    main()
