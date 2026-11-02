import sys
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import REGIONS, ROUTING_REGIONS, DB_PATH
from utils.riot_api import get_match_ids
from utils.logger import get_logger
from database import db_manager

logger = get_logger("collector_2")

# Thread-safe lock for DB writes
db_lock = threading.Lock()

def process_player(puuid, region):
    """Collect all match IDs for a single player. Called in parallel by threads."""
    routing_region = ROUTING_REGIONS.get(region)
    if not routing_region:
        with db_lock:
            db_manager.mark_player_matches_collected(puuid)
        return 0

    start_index = 0
    matches_count = 0
    collected = []

    while True:
        matches = get_match_ids(
            routing_region=routing_region,
            puuid=puuid,
            start=start_index,
            count=100
        )

        if not matches:
            break

        collected.extend([(m, routing_region) for m in matches])
        matches_count += len(matches)

        if len(matches) < 100:
            break

        start_index += 100

    # Batch insert + mark player done in a single lock
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        import time
        try:
            for match_id, routing in collected:
                conn.execute(
                    "INSERT OR IGNORE INTO matches (match_id, region_routing, processed, created_at) VALUES (?, ?, 0, ?)",
                    (match_id, routing, int(time.time()))
                )
            conn.execute("UPDATE players SET matches_collected = 1 WHERE puuid = ?", (puuid,))
            conn.commit()
        finally:
            conn.close()

    return matches_count


def main():
    logger.info("Starting Phase 2 (multi-threaded): Match ID collection")

    # Number of parallel threads - safe with our rate limiter
    NUM_THREADS = 4

    while True:
        # Load batch of unprocessed players
        players = db_manager.get_unprocessed_players(limit=500, regions=REGIONS)

        if not players:
            logger.info("All players in configured regions processed! Phase 2 complete.")
            break

        logger.info(f"Processing batch of {len(players)} players with {NUM_THREADS} threads...")

        total_new = 0
        completed = 0

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            futures = {executor.submit(process_player, puuid, region): (puuid, region)
                       for puuid, region in players}

            for future in as_completed(futures):
                puuid, region = futures[future]
                try:
                    count = future.result()
                    total_new += count
                    completed += 1
                    if completed % 50 == 0:
                        logger.info(f"Progress: {completed}/{len(players)} players | New matches: {total_new}")
                except Exception as e:
                    logger.error(f"Error processing player {puuid}: {e}")
                    with db_lock:
                        db_manager.mark_player_matches_collected(puuid)

        logger.info(f"Batch done. New matches this batch: {total_new}")

        # Check total
        conn = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        conn.close()
        logger.info(f"Total match IDs in DB: {total}")


if __name__ == "__main__":
    main()
