import sys
import os
import csv
import sqlite3
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.riot_api import get_match_details
from utils.logger import get_logger
from database import db_manager
from config.settings import DB_PATH

logger = get_logger("collector_3")

TIER_VALUES = {
    "CHALLENGER": 3,
    "GRANDMASTER": 2,
    "MASTER": 1
}

FIELDNAMES = [
    "match_id", "patch", "game_duration", "game_creation",
    "t1_top", "t1_jungle", "t1_middle", "t1_bottom", "t1_support", "t1_avg_tier",
    "t2_top", "t2_jungle", "t2_middle", "t2_bottom", "t2_support", "t2_avg_tier",
    "win_team1"
]

def get_average_tier(team_puuids, player_tiers):
    total, count = 0, 0
    for puuid in team_puuids:
        tier = player_tiers.get(puuid)
        if tier and tier in TIER_VALUES:
            total += TIER_VALUES[tier]
            count += 1
    return round(total / count, 2) if count > 0 else 0

def extract_match_data(match_id, details):
    info = details.get("info", {})
    if not info:
        return None

    game_duration = info.get("gameDuration")
    game_creation = info.get("gameCreation")
    patch = info.get("gameVersion")
    participants = info.get("participants", [])

    if len(participants) != 10:
        return None

    puuids = [p.get("puuid") for p in participants]
    player_tiers = db_manager.get_player_tiers(puuids)

    team1_data, team2_data = {}, {}
    team1_puuids, team2_puuids = [], []
    win_team1 = False

    for p in participants:
        team_id = p.get("teamId")
        position = p.get("teamPosition") or p.get("individualPosition", "")
        pos_key = position.lower()
        if pos_key == "utility":
            pos_key = "support"
        champion = p.get("championName")
        puuid = p.get("puuid")

        if team_id == 100:
            team1_data[f"t1_{pos_key}"] = champion
            team1_puuids.append(puuid)
            win_team1 = p.get("win", False)
        else:
            team2_data[f"t2_{pos_key}"] = champion
            team2_puuids.append(puuid)

    required = ["top", "jungle", "middle", "bottom", "support"]
    for role in required:
        if f"t1_{role}" not in team1_data or f"t2_{role}" not in team2_data:
            return None

    return {
        "match_id": match_id,
        "patch": patch,
        "game_duration": game_duration,
        "game_creation": game_creation,
        "t1_top": team1_data["t1_top"],
        "t1_jungle": team1_data["t1_jungle"],
        "t1_middle": team1_data["t1_middle"],
        "t1_bottom": team1_data["t1_bottom"],
        "t1_support": team1_data["t1_support"],
        "t1_avg_tier": get_average_tier(team1_puuids, player_tiers),
        "t2_top": team2_data["t2_top"],
        "t2_jungle": team2_data["t2_jungle"],
        "t2_middle": team2_data["t2_middle"],
        "t2_bottom": team2_data["t2_bottom"],
        "t2_support": team2_data["t2_support"],
        "t2_avg_tier": get_average_tier(team2_puuids, player_tiers),
        "win_team1": win_team1
    }

def main():
    output_csv = "data/dataset.csv"
    os.makedirs("data", exist_ok=True)
    file_exists = os.path.isfile(output_csv)

    BATCH_SIZE = 1000
    logger.info("Starting Phase 3 (single-thread, rate-limit optimized)")

    total_success = 0
    total_processed = 0

    with open(output_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()

        while True:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT match_id, region_routing FROM matches WHERE processed = 0 LIMIT ?",
                    (BATCH_SIZE,)
                ).fetchall()

            if not rows:
                logger.info("All matches processed! Phase 3 complete.")
                break

            for match_id, routing_region in rows:
                details = get_match_details(routing_region, match_id)

                if details:
                    row = extract_match_data(match_id, details)
                    if row:
                        writer.writerow(row)
                        total_success += 1

                # Mark as processed
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("UPDATE matches SET processed = 1 WHERE match_id = ?", (match_id,))
                    conn.commit()

                total_processed += 1
                if total_processed % 500 == 0:
                    logger.info(
                        f"Progress: {total_processed:,} processed | "
                        f"{total_success:,} valid rows | "
                        f"Ratio: {total_success/total_processed*100:.1f}%"
                    )


if __name__ == "__main__":
    main()
