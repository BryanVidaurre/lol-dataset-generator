import sqlite3
import os
from config.settings import DB_PATH

def get_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
    schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = f.read()
    
    with get_connection() as conn:
        conn.executescript(schema)
        conn.commit()

def save_player(puuid, summoner_id, region, tier, league_points, wins, losses):
    with get_connection() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO players (puuid, summoner_id, region, tier, league_points, wins, losses, matches_collected)
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT matches_collected FROM players WHERE puuid = ?), 0))
        """, (puuid, summoner_id, region, tier, league_points, wins, losses, puuid))
        conn.commit()

import time

def save_match(match_id, region_routing):
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO matches (match_id, region_routing, processed, created_at)
            VALUES (?, ?, 0, ?)
        """, (match_id, region_routing, int(time.time())))
        conn.commit()

def mark_player_matches_collected(puuid):
    with get_connection() as conn:
        conn.execute("UPDATE players SET matches_collected = 1 WHERE puuid = ?", (puuid,))
        conn.commit()

def get_unprocessed_players(limit=1000):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT puuid, region FROM players WHERE matches_collected = 0 LIMIT ?", (limit,))
        return cursor.fetchall()

def save_checkpoint(task_name, region, tier, last_processed_id):
    with get_connection() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO checkpoints (task_name, region, tier, last_processed_id)
            VALUES (?, ?, ?, ?)
        """, (task_name, region, tier, last_processed_id))
        conn.commit()

def get_checkpoint(task_name):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT region, tier, last_processed_id FROM checkpoints WHERE task_name = ?", (task_name,))
        return cursor.fetchone()

def get_unprocessed_matches(limit=1000):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT match_id, region_routing FROM matches WHERE processed = 0 LIMIT ?", (limit,))
        return cursor.fetchall()

def mark_match_processed(match_id):
    with get_connection() as conn:
        conn.execute("UPDATE matches SET processed = 1 WHERE match_id = ?", (match_id,))
        conn.commit()

def get_player_tiers(puuid_list):
    """
    Returns a dictionary of {puuid: tier} for the given list of puuids.
    """
    if not puuid_list:
        return {}
    
    with get_connection() as conn:
        placeholders = ','.join('?' for _ in puuid_list)
        cursor = conn.cursor()
        cursor.execute(f"SELECT puuid, tier FROM players WHERE puuid IN ({placeholders})", puuid_list)
        return {row[0]: row[1] for row in cursor.fetchall()}

# Initialize DB on import
init_db()
