"""Persistent, bounded sampling; existing collection progress is never reset."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing

from database.db_manager import get_connection


def initialize() -> None:
    with closing(get_connection()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS weekend_runs (
                name TEXT PRIMARY KEY, started_at REAL NOT NULL,
                deadline REAL NOT NULL, collect_until REAL NOT NULL,
                target INTEGER NOT NULL, seed INTEGER NOT NULL,
                regions_json TEXT NOT NULL, players_per_region INTEGER NOT NULL,
                selection_done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS weekend_matches (
                run_name TEXT NOT NULL REFERENCES weekend_runs(name),
                match_id TEXT NOT NULL REFERENCES matches(match_id),
                PRIMARY KEY(run_name, match_id)
            );
            CREATE TABLE IF NOT EXISTS weekend_sources (
                run_name TEXT NOT NULL REFERENCES weekend_runs(name),
                puuid TEXT NOT NULL REFERENCES players(puuid),
                region TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, PRIMARY KEY(run_name, puuid)
            );
        """)


def open_run(name, hours, target, seed, regions, players_per_region):
    initialize()
    with closing(get_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM weekend_runs WHERE name = ?", (name,)).fetchone()
        if row is None:
            started = time.time()
            deadline = started + hours * 3600
            # Keep legacy timestamp columns for compatibility with existing DBs.
            # They are planning metadata only and never stop a run or an export.
            conn.execute("INSERT INTO weekend_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                         (name, started, deadline, started + hours * 3240,
                          target, seed, json.dumps(regions), players_per_region))
            row = conn.execute("SELECT * FROM weekend_runs WHERE name = ?", (name,)).fetchone()
        if row['selection_done'] and not conn.execute(
            'SELECT 1 FROM weekend_matches WHERE run_name = ? LIMIT 1', (name,)
        ).fetchone():
            # Older versions froze an empty selection after a fresh database reset.
            # Reopen only empty runs; never change an existing nonempty sample.
            conn.execute('UPDATE weekend_runs SET selection_done = 0 WHERE name = ?', (name,))
            row = conn.execute('SELECT * FROM weekend_runs WHERE name = ?', (name,)).fetchone()
        return dict(row)


def load_run(name):
    initialize()
    with closing(get_connection()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM weekend_runs WHERE name = ?', (name,)).fetchone()
        return dict(row) if row is not None else None


def availability(regions):
    with closing(get_connection()) as conn:
        return {region: conn.execute("SELECT COUNT(*) FROM matches WHERE match_id GLOB ?",
                                    (platform.upper() + '_*',)).fetchone()[0]
                for region, platform in regions.items()}


def has_inputs(regions):
    """A fresh DB needs Phase 1; cached IDs can be used without player discovery."""
    if not regions:
        return False
    with closing(get_connection()) as conn:
        for platform in regions.values():
            if conn.execute('SELECT 1 FROM matches WHERE match_id GLOB ? LIMIT 1',
                            (platform.upper() + '_*',)).fetchone():
                return True
        placeholders = ','.join('?' for _ in regions)
        return conn.execute(
            f'SELECT 1 FROM players WHERE region IN ({placeholders}) '
            "AND tier IN ('MASTER', 'GRANDMASTER', 'CHALLENGER') LIMIT 1",
            tuple(regions),
        ).fetchone() is not None


def _hash(seed):
    return lambda value: hashlib.sha256((str(seed) + ':' + value).encode()).hexdigest()


def prepare_sources(run):
    """One history page per sampled player, balanced across the existing tiers."""
    regions = json.loads(run['regions_json'])
    counts = availability(regions)
    quota = (run['target'] + len(regions) - 1) // len(regions)
    with closing(get_connection()) as conn, conn:
        conn.create_function('sample_hash', 1, _hash(run['seed']))
        for region in regions:
            if counts[region] >= quota:
                continue
            for index, tier in enumerate(('CHALLENGER', 'GRANDMASTER', 'MASTER')):
                limit = run['players_per_region'] // 3 + (index < run['players_per_region'] % 3)
                conn.execute("""INSERT OR IGNORE INTO weekend_sources(run_name, puuid, region)
                    SELECT ?, puuid, region FROM players WHERE region = ? AND tier = ?
                    ORDER BY sample_hash(puuid) LIMIT ?""", (run['name'], region, tier, limit))


def pending_sources(run_name):
    with closing(get_connection()) as conn:
        return conn.execute("SELECT puuid, region FROM weekend_sources "
                            "WHERE run_name = ? AND done = 0 ORDER BY region, puuid",
                            (run_name,)).fetchall()


def save_history(run_name, puuid, match_ids, routing):
    """Partial histories never set players.matches_collected (full-history flag)."""
    with closing(get_connection()) as conn, conn:
        conn.executemany("INSERT OR IGNORE INTO matches(match_id, region_routing, processed, created_at) "
                         "VALUES (?, ?, 0, ?)", ((mid, routing, int(time.time())) for mid in match_ids))
        conn.execute("UPDATE weekend_sources SET done = 1, last_error = NULL "
                     "WHERE run_name = ? AND puuid = ?", (run_name, puuid))


def source_error(run_name, puuid, message):
    with closing(get_connection()) as conn, conn:
        conn.execute("UPDATE weekend_sources SET last_error = ? WHERE run_name = ? AND puuid = ?",
                     (message, run_name, puuid))


def select_matches(run):
    """Seeded equal allocation, redistributing shortages across available regions."""
    regions = json.loads(run['regions_json'])
    with closing(get_connection()) as conn:
        if conn.execute('SELECT selection_done FROM weekend_runs WHERE name = ?',
                        (run['name'],)).fetchone()[0]:
            return {region: conn.execute('''SELECT COUNT(*) FROM weekend_matches w
                JOIN matches m ON m.match_id = w.match_id
                WHERE w.run_name = ? AND m.match_id GLOB ?''',
                (run['name'], platform.upper() + '_*')).fetchone()[0]
                for region, platform in regions.items()}
    counts = availability(regions)
    quotas = dict.fromkeys(regions, 0)
    remaining = min(run['target'], sum(counts.values()))
    while remaining:
        for region in regions:
            if remaining and quotas[region] < counts[region]:
                quotas[region] += 1
                remaining -= 1
    if not any(quotas.values()):
        # Do not freeze a run which can acquire input data on the next invocation.
        return quotas
    with closing(get_connection()) as conn, conn:
        # Every row and selection_done commit together. Resume cannot change the sample.
        conn.create_function('sample_hash', 1, _hash(run['seed']))
        for region, platform in regions.items():
            conn.execute("""INSERT OR IGNORE INTO weekend_matches(run_name, match_id)
                SELECT ?, match_id FROM matches WHERE match_id GLOB ?
                ORDER BY sample_hash(match_id) LIMIT ?""",
                         (run['name'], platform.upper() + '_*', quotas[region]))
        conn.execute("UPDATE weekend_runs SET selection_done = 1 WHERE name = ?", (run['name'],))
    return quotas


def pending_matches(run_name):
    with closing(get_connection()) as conn:
        return conn.execute("""SELECT m.match_id, m.region_routing, m.processed
            FROM weekend_matches w JOIN matches m ON m.match_id = w.match_id
            LEFT JOIN match_summaries s ON s.match_id = m.match_id
            WHERE w.run_name = ? AND (m.processed = 0 OR
                  (s.match_id IS NOT NULL AND m.timeline_processed = 0))
            ORDER BY w.match_id""", (run_name,)).fetchall()


def iter_complete_rows(run_name, table):
    if table not in {'match_summaries', 'events', 'milestones'}:
        raise ValueError('Unknown export table')
    order = 'r.match_id, r.frame_index, r.event_index' if table == 'events' else 'r.match_id'
    with closing(get_connection()) as conn:
        rows = conn.execute(f"""SELECT r.row_json FROM {table} r
            JOIN weekend_matches w ON w.match_id = r.match_id
            JOIN matches m ON m.match_id = r.match_id
            JOIN match_summaries s ON s.match_id = r.match_id
            WHERE w.run_name = ? AND m.timeline_processed = 1 ORDER BY {order}""", (run_name,))
        for row in rows:
            yield json.loads(row[0])


def progress(run_name):
    with closing(get_connection()) as conn:
        selected, complete, excluded = conn.execute("""SELECT COUNT(*),
            COALESCE(SUM(s.match_id IS NOT NULL AND m.timeline_processed = 1), 0),
            COALESCE(SUM(m.processed = 1 AND s.match_id IS NULL), 0)
            FROM weekend_matches w JOIN matches m ON m.match_id = w.match_id
            LEFT JOIN match_summaries s ON s.match_id = m.match_id WHERE w.run_name = ?""", (run_name,)).fetchone()
    return {'selected': selected, 'complete': complete, 'excluded': excluded,
            'pending': selected - complete - excluded}
