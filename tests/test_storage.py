"""Storage integration tests against disposable SQLite databases."""
import copy
import csv
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database import db_manager
from utils.csv_export import export_timelines, import_existing_dataset
from utils.timeline import extract_milestones, extract_timeline_events


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lol-storage-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database_path = self.directory / "test.db"
        override = patch.object(db_manager, "DB_PATH", str(self.database_path))
        override.start()
        self.addCleanup(override.stop)
        db_manager.init_db()

    def query(self, sql, parameters=()):
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            return connection.execute(sql, parameters).fetchall()

    def timeline_rows(self, match_id="KR_123"):
        payload = {"info": {"frames": [{"events": [{
            "type": "CHAMPION_KILL", "timestamp": 188000,
            "killerId": 3, "victimId": 7,
        }]}]}}
        events = extract_timeline_events(match_id, "16.1", payload, {3: 100, 7: 200})
        return events, extract_milestones(match_id, "16.1", events)

    def test_old_schema_migrates_in_place_and_is_repeatable(self):
        old_path = self.directory / "old.db"
        with closing(sqlite3.connect(old_path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE players (puuid TEXT PRIMARY KEY, summoner_id TEXT,
                    region TEXT, tier TEXT, league_points INTEGER, wins INTEGER,
                    losses INTEGER, matches_collected BOOLEAN DEFAULT 0);
                CREATE TABLE matches (match_id TEXT PRIMARY KEY, region_routing TEXT,
                    processed BOOLEAN DEFAULT 0, created_at INTEGER);
                CREATE TABLE checkpoints (task_name TEXT PRIMARY KEY, region TEXT,
                    tier TEXT, last_processed_id TEXT);
                INSERT INTO players VALUES ('player', NULL, 'KR', 'MASTER', 20, 2, 1, 1);
                INSERT INTO matches VALUES ('KR_123', 'asia', 1, 12345);
                INSERT INTO checkpoints VALUES ('collect_players', 'KR', 'master', 'FINISHED');
            """)
        with patch.object(db_manager, "DB_PATH", str(old_path)):
            db_manager.init_db()
            db_manager.init_db()
        with closing(sqlite3.connect(old_path)) as connection, connection:
            match = connection.execute("SELECT match_id, region_routing, processed, created_at, timeline_processed FROM matches").fetchone()
            self.assertEqual(match, ("KR_123", "asia", 1, 12345, 0))
            self.assertEqual(connection.execute("SELECT wins, matches_collected FROM players").fetchone(), (2, 1))
            self.assertEqual(connection.execute("SELECT last_processed_id FROM checkpoints").fetchone(), ("FINISHED",))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(matches)")}
            self.assertTrue({"patch", "participant_teams", "timeline_processed", "timeline_last_error", "timeline_updated_at"} <= columns)

    def test_event_uniqueness_and_already_completed_timeline(self):
        db_manager.save_match("KR_123", "asia")
        events, milestones = self.timeline_rows()
        self.assertTrue(db_manager.save_timeline("KR_123", events + events, milestones))
        self.assertFalse(db_manager.save_timeline("KR_123", events, milestones))
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(1,)])
        self.assertTrue(db_manager.is_timeline_processed("KR_123"))

    def test_invalid_second_event_rolls_back_entire_match(self):
        db_manager.save_match("KR_123", "asia")
        events, milestones = self.timeline_rows()
        invalid = copy.deepcopy(events[0])
        invalid.update(event_id="KR_999:0:0", match_id="KR_999")
        with self.assertRaises(ValueError):
            db_manager.save_timeline("KR_123", events + [invalid], milestones)
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM milestones"), [(0,)])
        self.assertFalse(db_manager.is_timeline_processed("KR_123"))

    def test_failure_after_events_before_completion_rolls_back_and_retries(self):
        db_manager.save_match("KR_123", "asia")
        events, milestones = self.timeline_rows()
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_completion BEFORE UPDATE OF timeline_processed ON matches
                WHEN NEW.timeline_processed = 1 BEGIN SELECT RAISE(ABORT, 'simulated interruption'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            db_manager.save_timeline("KR_123", events, milestones)
        self.assertFalse(db_manager.is_timeline_processed("KR_123"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(0,)])
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("DROP TRIGGER fail_completion")
        self.assertTrue(db_manager.save_timeline("KR_123", events, milestones))
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(1,)])

    def test_error_is_retryable_and_cleared_only_after_success(self):
        db_manager.save_match("KR_123", "asia")
        db_manager.record_timeline_error("KR_123", "Riot API HTTP 503")
        state = self.query("SELECT timeline_processed, timeline_last_error, timeline_updated_at FROM matches")[0]
        self.assertEqual(state[:2], (0, "Riot API HTTP 503"))
        self.assertIsInstance(state[2], int)
        db_manager.save_timeline("KR_123", *self.timeline_rows())
        self.assertEqual(self.query("SELECT timeline_processed, timeline_last_error FROM matches"), [(1, None)])

    def test_atomic_export_failure_preserves_csv_and_can_be_rebuilt(self):
        db_manager.save_match("KR_123", "asia")
        db_manager.save_timeline("KR_123", *self.timeline_rows())
        output = self.directory / "events.csv"
        output.write_text("previous complete snapshot", encoding="utf-8")
        with patch("utils.csv_export.os.replace", side_effect=OSError("simulated export failure")):
            with self.assertRaises(OSError):
                export_timelines(self.directory)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous complete snapshot")
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        self.assertTrue(db_manager.is_timeline_processed("KR_123"))
        export_timelines(self.directory)
        export_timelines(self.directory)
        with output.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], "KR_123:0:0")
        self.assertEqual(rows[0]["patch"], "16.1")

    def test_legacy_csv_import_is_once_only_and_preserves_rows(self):
        path = self.directory / "dataset.csv"
        path.write_text("match_id,patch,t1_middle,win_team1\nKR_123,15.1,Ahri,True\n", encoding="utf-8")
        self.assertEqual(import_existing_dataset(path), 1)
        self.assertEqual(import_existing_dataset(path), 0)
        rows = list(db_manager.iter_saved_rows("match_summaries"))
        self.assertEqual(rows, [{"match_id": "KR_123", "patch": "15.1", "t1_middle": "Ahri", "win_team1": "True"}])
        self.assertTrue(db_manager.dataset_was_imported(str(path.resolve())))

    def test_interrupted_import_rolls_back_rows_and_import_marker(self):
        rows = [{"match_id": "KR_123", "patch": "15.1"}, {"match_id": "", "patch": "15.1"}]
        with self.assertRaises(ValueError):
            db_manager.import_dataset_rows("legacy-source", rows)
        self.assertEqual(list(db_manager.iter_saved_rows("match_summaries")), [])
        self.assertFalse(db_manager.dataset_was_imported("legacy-source"))
        self.assertEqual(db_manager.import_dataset_rows("legacy-source", rows[:1]), 1)

    def test_pending_keyset_visits_failures_once_without_skipping_next_match(self):
        for match_id in ("KR_1", "KR_2", "KR_3"):
            db_manager.save_match(match_id, "asia")
        visited = []
        for match_id, routing in db_manager.iter_pending_matches(timeline=True, batch_size=1):
            visited.append(match_id)
            if match_id != "KR_1":
                db_manager.save_timeline(match_id, *self.timeline_rows(match_id))
        self.assertEqual(visited, ["KR_1", "KR_2", "KR_3"])
        self.assertEqual(list(db_manager.iter_pending_matches(timeline=True)), [("KR_1", "asia")])


if __name__ == "__main__":
    unittest.main()
