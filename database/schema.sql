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
    created_at INTEGER,
    patch TEXT,
    participant_teams TEXT,
    timeline_processed INTEGER NOT NULL DEFAULT 0,
    timeline_last_error TEXT,
    timeline_updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS checkpoints (
    task_name TEXT PRIMARY KEY,
    region TEXT,
    tier TEXT,
    last_processed_id TEXT
);

CREATE TABLE IF NOT EXISTS match_summaries (
    match_id TEXT PRIMARY KEY,
    row_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    frame_index INTEGER NOT NULL,
    event_index INTEGER NOT NULL,
    row_json TEXT NOT NULL,
    UNIQUE (match_id, frame_index, event_index)
);

CREATE TABLE IF NOT EXISTS milestones (
    match_id TEXT PRIMARY KEY REFERENCES matches(match_id),
    row_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_imports (
    source_path TEXT PRIMARY KEY,
    imported_at INTEGER NOT NULL
);
