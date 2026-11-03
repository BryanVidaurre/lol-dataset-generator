"""Collect a resumable sample exclusively from game versions 16.20 and 16.21."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from config.settings import DATA_DIR, REGIONS, ROUTING_REGIONS
from database import db_manager, weekend_store as store
from utils.logger import get_logger
from utils.riot_api import RiotAPIError, get_match_ids
from collector.collect_weekend import RunLock, details, timelines, export_sample

logger = get_logger('collector_patch_sample')
PATCHES = ('16.20', '16.21')
# An intentionally broad discovery window; the match's gameVersion is authoritative.
START_TIME = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
END_TIME = int(datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp())


def matches_patch(version):
    return isinstance(version, str) and '.'.join(version.split('.')[:2]) in PATCHES


def initialize(name, target, seed, players_per_region, base_run=None, additional=None):
    store.initialize()
    with closing(db_manager.get_connection()) as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS patch_sample_runs (
            run_name TEXT PRIMARY KEY REFERENCES weekend_runs(name),
            patches_json TEXT NOT NULL, start_time INTEGER NOT NULL, end_time INTEGER NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS patch_sample_sources (
            run_name TEXT NOT NULL REFERENCES patch_sample_runs(run_name),
            puuid TEXT NOT NULL REFERENCES players(puuid), region TEXT NOT NULL,
            next_start INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, PRIMARY KEY(run_name, puuid))''')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(patch_sample_runs)')}
        if 'base_run' not in columns:
            conn.execute('ALTER TABLE patch_sample_runs ADD COLUMN base_run TEXT')
            conn.execute('ALTER TABLE patch_sample_runs ADD COLUMN additional INTEGER')
        conn.row_factory = sqlite3.Row
        run = conn.execute('SELECT * FROM weekend_runs WHERE name = ?', (name,)).fetchone()
        setting = conn.execute('SELECT * FROM patch_sample_runs WHERE run_name = ?', (name,)).fetchone()
        if run is None:
            if base_run:
                base = conn.execute('SELECT selection_done FROM weekend_runs WHERE name = ?', (base_run,)).fetchone()
                if base is None or not base['selection_done']:
                    raise RuntimeError('The base sample must exist and have a frozen selection')
                count = conn.execute('''SELECT COUNT(*) FROM weekend_matches w
                    JOIN matches m ON m.match_id = w.match_id
                    JOIN match_summaries s ON s.match_id = m.match_id
                    WHERE w.run_name = ? AND m.timeline_processed = 1''', (base_run,)).fetchone()[0]
                if not count:
                    raise RuntimeError('The base sample has no complete matches')
                target = count + additional
            started = time.time()
            conn.execute('INSERT INTO weekend_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)',
                         (name, started, started + 40 * 3600, started + 40 * 3240,
                          target, seed, json.dumps(REGIONS), players_per_region))
            conn.execute('''INSERT INTO patch_sample_runs
                (run_name, patches_json, start_time, end_time, base_run, additional)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (name, json.dumps(PATCHES), START_TIME, min(int(started), END_TIME), base_run, additional))
            if base_run:
                conn.execute('''INSERT INTO weekend_matches(run_name, match_id)
                    SELECT ?, w.match_id FROM weekend_matches w
                    JOIN matches m ON m.match_id = w.match_id
                    JOIN match_summaries s ON s.match_id = m.match_id
                    WHERE w.run_name = ? AND m.timeline_processed = 1''', (name, base_run))
        elif setting is None:
            raise RuntimeError('Run name belongs to another collector; use a new --run-name')
        elif ((not base_run and run['target'] != target) or
              (run['seed'], run['regions_json'], setting['patches_json'], setting['base_run'], setting['additional']) != (
                seed, json.dumps(REGIONS), json.dumps(PATCHES), base_run, additional) or
              players_per_region < run['players_per_region']):
            raise RuntimeError('Existing run settings differ; resume with the original arguments')
        elif players_per_region > run['players_per_region']:
            conn.execute('UPDATE weekend_runs SET players_per_region = ? WHERE name = ?',
                         (players_per_region, name))
        return dict(conn.execute('SELECT * FROM weekend_runs WHERE name = ?', (name,)).fetchone())


def seed_cached(run):
    """Reuse complete matching matches, without modifying other samples."""
    with closing(db_manager.get_connection()) as conn, conn:
        conn.create_function('sample_hash', 1, store._hash(run['seed']))
        existing = conn.execute('SELECT COUNT(*) FROM weekend_matches WHERE run_name = ?',
                                (run['name'],)).fetchone()[0]
        regions = json.loads(run['regions_json'])
        region_filters = ' OR '.join('m.match_id GLOB ?' for _ in regions)
        conn.execute(f'''INSERT OR IGNORE INTO weekend_matches(run_name, match_id)
            SELECT ?, m.match_id FROM matches m
            JOIN match_summaries s ON s.match_id = m.match_id
            WHERE m.timeline_processed = 1 AND ({region_filters})
              AND (m.patch = '16.20' OR m.patch LIKE '16.20.%'
                   OR m.patch = '16.21' OR m.patch LIKE '16.21.%')
              AND NOT EXISTS (SELECT 1 FROM weekend_matches w
                              WHERE w.run_name = ? AND w.match_id = m.match_id)
            ORDER BY sample_hash(m.match_id) LIMIT ?''',
            (run['name'], *(p.upper() + '_*' for p in regions.values()), run['name'],
             max(0, run['target'] - existing)))
        return conn.execute('SELECT COUNT(*) FROM weekend_matches WHERE run_name = ?',
                            (run['name'],)).fetchone()[0]


def prepare_sources(run):
    with closing(db_manager.get_connection()) as conn, conn:
        conn.create_function('sample_hash', 1, store._hash(run['seed']))
        for region in json.loads(run['regions_json']):
            for index, tier in enumerate(('CHALLENGER', 'GRANDMASTER', 'MASTER')):
                limit = run['players_per_region'] // 3 + (index < run['players_per_region'] % 3)
                conn.execute('''INSERT OR IGNORE INTO patch_sample_sources(run_name, puuid, region)
                    SELECT ?, puuid, region FROM players WHERE region = ? AND tier = ?
                    ORDER BY sample_hash(puuid) LIMIT ?''', (run['name'], region, tier, limit))


def discover(run):
    prepare_sources(run)
    with closing(db_manager.get_connection()) as conn:
        start_time, end_time = conn.execute(
            'SELECT start_time, end_time FROM patch_sample_runs WHERE run_name = ?',
            (run['name'],)).fetchone()
        sources = conn.execute('''SELECT puuid, region, next_start FROM patch_sample_sources
            WHERE run_name = ? AND done = 0 ORDER BY region, puuid''', (run['name'],)).fetchall()
    logger.info('Searching historical match IDs for %s players', len(sources))
    for index, (puuid, region, start) in enumerate(sources, 1):
        try:
            while True:
                ids = get_match_ids(ROUTING_REGIONS[region], puuid, start=start, count=100,
                                    start_time=start_time, end_time=end_time, raise_on_error=True)
                if any(not isinstance(mid, str) or '_' not in mid for mid in ids):
                    raise RiotAPIError('Invalid match ID history')
                next_start = start + len(ids)
                with closing(db_manager.get_connection()) as conn, conn:
                    conn.executemany('''INSERT OR IGNORE INTO matches
                        (match_id, region_routing, processed, created_at) VALUES (?, ?, 0, ?)''',
                        ((mid, ROUTING_REGIONS[region], int(time.time())) for mid in ids))
                    conn.execute('''UPDATE patch_sample_sources SET next_start = ?, done = ?, last_error = NULL
                        WHERE run_name = ? AND puuid = ?''',
                        (next_start, len(ids) < 100, run['name'], puuid))
                if len(ids) < 100:
                    break
                start = next_start
        except RiotAPIError as error:
            with closing(db_manager.get_connection()) as conn, conn:
                conn.execute('UPDATE patch_sample_sources SET last_error = ? WHERE run_name = ? AND puuid = ?',
                             (str(error), run['name'], puuid))
            logger.warning('History pending in %s: %s', region, error)
            if error.status_code in (401, 403):
                raise
        if index % 100 == 0:
            logger.info('History sources attempted: %s/%s', index, len(sources))


def candidates(run):
    regions = json.loads(run['regions_json'])
    region_filters = ' OR '.join('m.match_id GLOB ?' for _ in regions)
    with closing(db_manager.get_connection()) as conn:
        conn.create_function('sample_hash', 1, store._hash(run['seed']))
        return conn.execute(f'''SELECT m.match_id, m.region_routing, m.processed, m.patch
            FROM matches m LEFT JOIN match_summaries s ON s.match_id = m.match_id
            WHERE ({region_filters}) AND NOT EXISTS (
                SELECT 1 FROM weekend_matches w WHERE w.run_name = ? AND w.match_id = m.match_id)
              AND (m.processed = 0 OR (s.match_id IS NOT NULL AND m.timeline_processed = 0
                   AND (m.patch = '16.20' OR m.patch LIKE '16.20.%'
                        OR m.patch = '16.21' OR m.patch LIKE '16.21.%')))
            ORDER BY m.processed DESC, sample_hash(m.match_id)''',
            (*(p.upper() + '_*' for p in regions.values()), run['name'])).fetchall()


def collect(run):
    count = seed_cached(run)
    logger.info('Matching complete matches reused: %s/%s', count, run['target'])
    if count >= run['target']:
        return True
    discover(run)
    for match_id, routing, processed, patch in candidates(run):
        if count >= run['target']:
            break
        if not processed:
            if not details.process_match(match_id, routing):
                continue
            context = db_manager.get_match_context(match_id)
            patch = context[0] if context else None
        if not matches_patch(patch):
            continue
        if not timelines.process_match(match_id, routing):
            continue
        with closing(db_manager.get_connection()) as conn, conn:
            cursor = conn.execute('INSERT OR IGNORE INTO weekend_matches VALUES (?, ?)',
                                  (run['name'], match_id))
        count += cursor.rowcount
        if count % 100 == 0:
            logger.info('Matching complete matches: %s/%s', count, run['target'])
        if count % 2000 == 0:
            export_sample(run)
    if count < run['target']:
        logger.warning('Only %s/%s complete matches available; rerun to retry pending requests. '
                       'More historical players may be needed via --players-per-region.', count, run['target'])
        return False
    else:
        logger.info('Target reached: %s matching complete matches', count)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-name', default='ampliacion_50k_parches')
    parser.add_argument('--max-matches', type=int, default=50000)
    parser.add_argument('--players-per-region', type=int, default=200)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--base-run', help='Preserve complete matches from this existing sample')
    parser.add_argument('--additional-matches', type=int, help='Number of matching matches to add to the base')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.run_name):
        parser.error('Invalid run name')
    if args.max_matches <= 0 or not 0 <= args.players_per_region <= 5000 or not REGIONS:
        parser.error('Invalid target, player count, or region configuration')
    if bool(args.base_run) != (args.additional_matches is not None):
        parser.error('--base-run and --additional-matches must be used together')
    if args.additional_matches is not None and args.additional_matches <= 0:
        parser.error('--additional-matches must be positive')
    if args.base_run == args.run_name:
        parser.error('The base and new run names must differ')
    run = None
    try:
        with RunLock():
            run = initialize(args.run_name, args.max_matches, args.seed, args.players_per_region,
                             args.base_run, args.additional_matches)
            try:
                complete = collect(run)
            finally:
                if store.progress(run['name'])['selected']:
                    export_sample(run)
        return 0 if complete else 1
    except KeyboardInterrupt:
        logger.info('Interrupted; completed matching matches are preserved')
        return 130
    except (RuntimeError, RiotAPIError) as error:
        logger.error('Collection stopped: %s', error)
        return 1


if __name__ == '__main__':
    sys.exit(main())
