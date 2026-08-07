CREATE TABLE IF NOT EXISTS players (
    puuid TEXT PRIMARY KEY,
    summoner_id TEXT,
    region TEXT,
    tier TEXT,
    league_points INTEGER,
    wins INTEGER,
    losses INTEGER,
    matches_collected BOOLEAN DEFAULT 0
);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    region_routing TEXT,
    processed BOOLEAN DEFAULT 0,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS checkpoints (
    task_name TEXT PRIMARY KEY,
    region TEXT,
    tier TEXT,
    last_processed_id TEXT
);
