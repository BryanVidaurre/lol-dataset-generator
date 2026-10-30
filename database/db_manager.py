import sqlite3
import json
from contextlib import closing
import os
import time
from config.settings import DB_PATH

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = f.read()
    
    with closing(get_connection()) as conn, conn:
        conn.executescript(schema)
        _migrate_matches(conn)

def save_player(puuid, summoner_id, region, tier, league_points, wins, losses):
    with closing(get_connection()) as conn, conn:
        conn.execute("""
            INSERT OR REPLACE INTO players (puuid, summoner_id, region, tier, league_points, wins, losses, matches_collected)
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT matches_collected FROM players WHERE puuid = ?), 0))
        """, (puuid, summoner_id, region, tier, league_points, wins, losses, puuid))
        conn.commit()

import time

def save_match(match_id, region_routing):
    with closing(get_connection()) as conn, conn:
        conn.execute("""
            INSERT OR IGNORE INTO matches (match_id, region_routing, processed, created_at)
            VALUES (?, ?, 0, ?)
        """, (match_id, region_routing, int(time.time())))
        conn.commit()

def mark_player_matches_collected(puuid):
    with closing(get_connection()) as conn, conn:
        conn.execute("UPDATE players SET matches_collected = 1 WHERE puuid = ?", (puuid,))
        conn.commit()

def get_unprocessed_players(limit=1000):
    with closing(get_connection()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("SELECT puuid, region FROM players WHERE matches_collected = 0 LIMIT ?", (limit,))
        return cursor.fetchall()

def save_checkpoint(task_name, region, tier, last_processed_id):
    with closing(get_connection()) as conn, conn:
        conn.execute("""
            INSERT OR REPLACE INTO checkpoints (task_name, region, tier, last_processed_id)
            VALUES (?, ?, ?, ?)
        """, (task_name, region, tier, last_processed_id))
        conn.commit()

def get_checkpoint(task_name):
    with closing(get_connection()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("SELECT region, tier, last_processed_id FROM checkpoints WHERE task_name = ?", (task_name,))
        return cursor.fetchone()

def get_unprocessed_matches(limit=1000):
    with closing(get_connection()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("SELECT match_id, region_routing FROM matches WHERE processed = 0 LIMIT ?", (limit,))
        return cursor.fetchall()

def mark_match_processed(match_id):
    with closing(get_connection()) as conn, conn:
        conn.execute("UPDATE matches SET processed = 1 WHERE match_id = ?", (match_id,))
        conn.commit()

def get_player_tiers(puuid_list):
    """
    Returns a dictionary of {puuid: tier} for the given list of puuids.
    """
    if not puuid_list:
        return {}
    
    with closing(get_connection()) as conn, conn:
        placeholders = ','.join('?' for _ in puuid_list)
        cursor = conn.cursor()
        cursor.execute(f"SELECT puuid, tier FROM players WHERE puuid IN ({placeholders})", puuid_list)
        return {row[0]: row[1] for row in cursor.fetchall()}


# Added state is migrated in place; original player/match helper APIs remain valid.
def _migrate_matches(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(matches)")}
    additions = {
        "patch": "TEXT",
        "participant_teams": "TEXT",
        "timeline_processed": "INTEGER NOT NULL DEFAULT 0",
        "timeline_last_error": "TEXT",
        "timeline_updated_at": "INTEGER",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_timeline ON matches(timeline_processed, match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_details ON matches(processed, match_id)")


def iter_pending_matches(timeline=False, batch_size=1000):
    """Keyset pagination: failed matches are attempted once per invocation."""
    column = "timeline_processed" if timeline else "processed"
    last_id = ""
    while True:
        with closing(get_connection()) as conn:
            rows = conn.execute(
                f"SELECT match_id, region_routing FROM matches "
                f"WHERE {column} = 0 AND match_id > ? ORDER BY match_id LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
        if not rows:
            return
        for row in rows:
            yield row
        last_id = rows[-1][0]


def get_match_context(match_id):
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT patch, participant_teams FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    return row[0], {int(key): value for key, value in json.loads(row[1]).items()}


def save_match_context(match_id, patch, participant_teams):
    with closing(get_connection()) as conn, conn:
        conn.execute(
            "UPDATE matches SET patch = ?, participant_teams = ? WHERE match_id = ?",
            (patch, json.dumps(participant_teams), match_id),
        )


def save_match_summary(match_id, row, participant_teams):
    """Commit summary, reusable timeline context and completion together."""
    with closing(get_connection()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO match_summaries(match_id, row_json) VALUES (?, ?)",
            (match_id, json.dumps(row, ensure_ascii=False)),
        )
        cursor = conn.execute(
            "UPDATE matches SET processed = 1, patch = ?, participant_teams = ? WHERE match_id = ?",
            (row["patch"], json.dumps(participant_teams), match_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Cannot save details for an unknown match")


def dataset_was_imported(source_path):
    with closing(get_connection()) as conn:
        return conn.execute(
            "SELECT 1 FROM dataset_imports WHERE source_path = ?", (source_path,)
        ).fetchone() is not None


def import_dataset_rows(source_path, rows):
    """The import marker commits with every imported row, including on restart."""
    count = 0
    with closing(get_connection()) as conn, conn:
        if conn.execute("SELECT 1 FROM dataset_imports WHERE source_path = ?", (source_path,)).fetchone():
            return 0
        for row in rows:
            if not row.get("match_id"):
                raise ValueError("Existing dataset contains a row without match_id")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO match_summaries(match_id, row_json) VALUES (?, ?)",
                (row["match_id"], json.dumps(row, ensure_ascii=False)),
            )
            count += cursor.rowcount
        conn.execute("INSERT INTO dataset_imports VALUES (?, ?)", (source_path, int(time.time())))
    return count


def is_timeline_processed(match_id):
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT timeline_processed FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    return bool(row and row[0])


def record_timeline_error(match_id, message):
    with closing(get_connection()) as conn, conn:
        conn.execute(
            "UPDATE matches SET timeline_last_error = ?, timeline_updated_at = ? "
            "WHERE match_id = ? AND timeline_processed = 0",
            (message, int(time.time()), match_id),
        )


def save_timeline(match_id, events, milestone_row):
    """A crash rolls back both the event inserts and the completion flag."""
    with closing(get_connection()) as conn, conn:
        row = conn.execute("SELECT timeline_processed FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        if row is None:
            raise ValueError("Cannot save timeline for an unknown match")
        if row[0]:
            return False
        for event in events:
            if event["match_id"] != match_id:
                raise ValueError("Event belongs to a different match")
            conn.execute(
                "INSERT OR IGNORE INTO events(event_id, match_id, frame_index, event_index, row_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (event["event_id"], match_id, event["frame_index"], event["event_index"],
                 json.dumps(event, ensure_ascii=False)),
            )
        conn.execute(
            "INSERT OR REPLACE INTO milestones(match_id, row_json) VALUES (?, ?)",
            (match_id, json.dumps(milestone_row, ensure_ascii=False)),
        )
        conn.execute(
            "UPDATE matches SET timeline_processed = 1, timeline_last_error = NULL, timeline_updated_at = ? "
            "WHERE match_id = ?", (int(time.time()), match_id),
        )
    return True


def iter_saved_rows(table):
    """Bounded-memory export; table names cannot come from arbitrary SQL."""
    if table not in {"match_summaries", "events", "milestones"}:
        raise ValueError("Unknown export table")
    order = "match_id, frame_index, event_index" if table == "events" else "match_id"
    with closing(get_connection()) as conn:
        cursor = conn.execute(f"SELECT row_json FROM {table} ORDER BY {order}")
        for row in cursor:
            yield json.loads(row[0])



def iter_legacy_detail_matches(objective_fields, batch_size=1000):
    """Only explicitly requested legacy rows require another details request."""
    last_id = ""
    while True:
        with closing(get_connection()) as conn:
            rows = conn.execute(
                "SELECT m.match_id, m.region_routing, s.row_json FROM matches m "
                "JOIN match_summaries s ON s.match_id = m.match_id "
                "WHERE m.match_id > ? ORDER BY m.match_id LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
        if not rows:
            return
        for match_id, routing, encoded in rows:
            row = json.loads(encoded)
            if any(row.get(field) in (None, "") for field in objective_fields):
                yield match_id, routing
        last_id = rows[-1][0]

# Initialize after migration helpers have been defined.
init_db()
