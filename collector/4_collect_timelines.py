"""Phase 4: collect relevant timeline events for match IDs already in SQLite."""
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DATA_DIR
from database import db_manager
from utils.csv_export import export_timelines
from utils.logger import get_logger
from utils.objectives import extract_match_context
from utils.riot_api import RiotAPIError, get_match_details, get_match_timeline
from utils.timeline import extract_milestones, extract_timeline_events

logger = get_logger("collector_4")


def process_match(match_id: str, routing_region: str) -> bool:
    if db_manager.is_timeline_processed(match_id):
        return True
    try:
        context = db_manager.get_match_context(match_id)
        if context is None:
            # Legacy databases have no participant map. Fetch once and cache only
            # patch + participantId/teamId; subsequent attempts reuse this context.
            details = get_match_details(routing_region, match_id, raise_on_error=True)
            context = extract_match_context(details)
            db_manager.save_match_context(match_id, *context)
        patch, participant_teams = context
        timeline = get_match_timeline(routing_region, match_id)
        events = extract_timeline_events(match_id, patch, timeline, participant_teams)
        milestones = extract_milestones(match_id, patch, events)
        db_manager.save_timeline(match_id, events, milestones)
        return True
    except RiotAPIError as error:
        db_manager.record_timeline_error(match_id, str(error))
        logger.warning("Timeline pending for %s: %s", match_id, error)
        if error.status_code in (401, 403):
            raise
    except (ValueError, TypeError, KeyError):
        message = "Invalid match or timeline response"
        db_manager.record_timeline_error(match_id, message)
        logger.warning("Timeline pending for %s: %s", match_id, message)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-only", action="store_true", help="Rebuild CSV files without HTTP")
    args = parser.parse_args(argv)
    attempted = succeeded = 0
    exit_code = 0
    try:
        if not args.export_only:
            for match_id, routing in db_manager.iter_pending_matches(timeline=True):
                attempted += 1
                succeeded += process_match(match_id, routing)
                if attempted % 500 == 0:
                    logger.info("Timelines: %s attempted, %s completed", attempted, succeeded)
    except KeyboardInterrupt:
        logger.info("Interrupted; committed events are safe. Exporting CSV snapshots.")
        exit_code = 130
    except RiotAPIError:
        logger.error("Collection stopped: check RIOT_API_KEY before retrying.")
        exit_code = 1
    finally:
        export_timelines(DATA_DIR)
        logger.info("Exported events.csv and milestones.csv from committed SQLite data")
    logger.info("Timelines: %s completed; %s remain pending from this run",
                succeeded, attempted - succeeded)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
