"""Phase 3: preserve draft columns and add patch-safe final objectives."""
import argparse
import os
import sys
from itertools import islice
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DATA_DIR
from database import db_manager
from utils.csv_export import export_dataset, import_existing_dataset
from utils.logger import get_logger
from utils.objectives import OBJECTIVE_FIELDNAMES, extract_match_context, extract_team_objectives
from utils.riot_api import RiotAPIError, get_match_details

logger = get_logger("collector_3")
TIER_VALUES = {"CHALLENGER": 3, "GRANDMASTER": 2, "MASTER": 1}
# Keep all original names and their order, including middle and bottom.
FIELDNAMES = [
    "match_id", "patch", "game_duration", "game_creation",
    "t1_top", "t1_jungle", "t1_middle", "t1_bottom", "t1_support", "t1_avg_tier",
    "t2_top", "t2_jungle", "t2_middle", "t2_bottom", "t2_support", "t2_avg_tier",
    "win_team1",
] + OBJECTIVE_FIELDNAMES


def get_average_tier(team_puuids, player_tiers):
    values = [TIER_VALUES[player_tiers[puuid]] for puuid in team_puuids
              if player_tiers.get(puuid) in TIER_VALUES]
    return round(sum(values) / len(values), 2) if values else 0


def extract_match_data(match_id, details):
    if not isinstance(details, dict) or not isinstance(details.get("info"), dict):
        return None
    info = details["info"]
    participants = info.get("participants")
    if not isinstance(participants, list) or len(participants) != 10:
        return None
    if any(not isinstance(participant, dict) for participant in participants):
        return None
    player_tiers = db_manager.get_player_tiers([p.get("puuid") for p in participants])
    row = {
        "match_id": match_id, "patch": info.get("gameVersion"),
        "game_duration": info.get("gameDuration"), "game_creation": info.get("gameCreation"),
    }
    for team_id, prefix in ((100, "t1"), (200, "t2")):
        team_players = [p for p in participants if p.get("teamId") == team_id]
        if len(team_players) != 5:
            return None
        for participant in team_players:
            position = participant.get("teamPosition") or participant.get("individualPosition", "")
            if not isinstance(position, str):
                return None
            position = position.lower()
            if position == "utility":
                position = "support"
            row[f"{prefix}_{position}"] = participant.get("championName")
        if any(not row.get(f"{prefix}_{role}") for role in ("top", "jungle", "middle", "bottom", "support")):
            return None
        row[f"{prefix}_avg_tier"] = get_average_tier([p.get("puuid") for p in team_players], player_tiers)
        if team_id == 100:
            row["win_team1"] = team_players[0].get("win", False)
        teams = info.get("teams") or []
        if not isinstance(teams, list):
            return None
        team_data = next((team for team in teams if isinstance(team, dict) and team.get("teamId") == team_id), {})
        row.update({f"{prefix}_{key}": value for key, value in extract_team_objectives(team_data).items()})
    return row


def process_match(match_id: str, routing_region: str) -> bool:
    try:
        details = get_match_details(routing_region, match_id, raise_on_error=True)
        patch, participant_teams = extract_match_context(details)
        row = extract_match_data(match_id, details)
        if row is None:
            # Preserve the existing policy of excluding matches without all draft roles.
            db_manager.save_match_context(match_id, patch, participant_teams)
            db_manager.mark_match_processed(match_id)
            logger.info("Skipping match %s: incomplete draft roles", match_id)
            return False
        db_manager.save_match_summary(match_id, row, participant_teams)
        return True
    except RiotAPIError as error:
        logger.warning("Details pending for %s: %s", match_id, error)
        if error.status_code in (401, 403):
            raise
    except ValueError:
        logger.warning("Details pending for %s: invalid match response", match_id)
    return False


def process_batch(limit=1000, output_csv=None):
    """Compatibility entry point used by collector/test_phase3.py."""
    output_csv = Path(output_csv) if output_csv else Path(DATA_DIR) / "dataset.csv"
    import_existing_dataset(output_csv)
    success = 0
    try:
        for match_id, routing in islice(db_manager.iter_pending_matches(), limit):
            success += process_match(match_id, routing)
    finally:
        export_dataset(output_csv, FIELDNAMES)
    return success


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-only", action="store_true", help="Rebuild CSV from SQLite without HTTP")
    parser.add_argument("--backfill-objectives", action="store_true",
                        help="Fetch details only for imported legacy rows missing objective columns")
    args = parser.parse_args(argv)
    output = Path(DATA_DIR) / "dataset.csv"
    imported = import_existing_dataset(output)
    if imported:
        logger.info("Preserved %s existing CSV rows in SQLite", imported)
    success = attempted = 0
    exit_code = 0
    try:
        if not args.export_only:
            if args.backfill_objectives:
                for match_id, routing in db_manager.iter_legacy_detail_matches(OBJECTIVE_FIELDNAMES):
                    attempted += 1
                    success += process_match(match_id, routing)
            for match_id, routing in db_manager.iter_pending_matches():
                attempted += 1
                success += process_match(match_id, routing)
                if attempted % 500 == 0:
                    logger.info("Details: %s attempted, %s valid rows saved", attempted, success)
    except KeyboardInterrupt:
        logger.info("Interrupted; committed summaries are safe. Exporting dataset.")
        exit_code = 130
    except RiotAPIError:
        logger.error("Collection stopped: check RIOT_API_KEY before retrying.")
        exit_code = 1
    finally:
        count = export_dataset(output, FIELDNAMES)
        logger.info("Exported %s unique matches to %s", count, output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
