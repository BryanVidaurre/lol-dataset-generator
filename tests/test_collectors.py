"""Collector integration tests with mocked HTTP and disposable local files."""
import copy
import csv
from contextlib import closing
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database import db_manager
from utils.objectives import extract_match_context
from utils.riot_api import RiotAPIError

collect_details = importlib.import_module("collector.3_collect_details")
collect_timelines = importlib.import_module("collector.4_collect_timelines")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lol-collector-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database_path = self.directory / "test.db"
        for override in (
            patch.object(db_manager, "DB_PATH", str(self.database_path)),
            patch.object(collect_details, "DATA_DIR", str(self.directory)),
            patch.object(collect_timelines, "DATA_DIR", str(self.directory)),
            patch("requests.get", side_effect=AssertionError("Real HTTP is forbidden in tests")),
        ):
            override.start()
            self.addCleanup(override.stop)
        db_manager.init_db()
        fixtures = Path(__file__).parent / "fixtures"
        self.match = json.loads((fixtures / "match.json").read_text(encoding="utf-8"))
        self.timeline = json.loads((fixtures / "timeline.json").read_text(encoding="utf-8"))
        db_manager.save_match("KR_123", "asia")

    def query(self, sql):
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            return connection.execute(sql).fetchall()

    def cache_context(self, match_id="KR_123"):
        db_manager.save_match_context(match_id, *extract_match_context(self.match))

    def read_csv(self, name):
        with (self.directory / name).open(newline="", encoding="utf-8") as source:
            return list(csv.DictReader(source))

    def test_phase3_commits_summary_and_reusable_context(self):
        with patch.object(collect_details, "get_match_details", return_value=self.match) as get:
            self.assertTrue(collect_details.process_match("KR_123", "asia"))
        get.assert_called_once_with("asia", "KR_123", raise_on_error=True)
        self.assertEqual(self.query("SELECT processed FROM matches"), [(1,)])
        self.assertEqual(db_manager.get_match_context("KR_123"), extract_match_context(self.match))
        summary = list(db_manager.iter_saved_rows("match_summaries"))[0]
        self.assertEqual(summary["match_id"], "KR_123")
        self.assertIn("t1_first_atakhan", summary)
        self.assertIn("t2_horde_kills", summary)

    def test_phase3_temporary_api_failure_does_not_mark_complete(self):
        with patch.object(collect_details, "get_match_details", side_effect=RiotAPIError("Riot API HTTP 503", 503)):
            self.assertFalse(collect_details.process_match("KR_123", "asia"))
        self.assertEqual(self.query("SELECT processed FROM matches"), [(0,)])
        self.assertEqual(list(db_manager.iter_saved_rows("match_summaries")), [])

    def test_phase3_invalid_response_remains_pending(self):
        with patch.object(collect_details, "get_match_details", return_value={}):
            self.assertFalse(collect_details.process_match("KR_123", "asia"))
        self.assertEqual(self.query("SELECT processed FROM matches"), [(0,)])

    def test_phase3_filtered_draft_keeps_context_for_timelines(self):
        malformed = copy.deepcopy(self.match)
        malformed["info"]["participants"][0]["teamPosition"] = ""
        malformed["info"]["participants"][0]["individualPosition"] = ""
        with patch.object(collect_details, "get_match_details", return_value=malformed):
            self.assertFalse(collect_details.process_match("KR_123", "asia"))
        self.assertEqual(self.query("SELECT processed FROM matches"), [(1,)])
        self.assertEqual(db_manager.get_match_context("KR_123"), extract_match_context(self.match))

    def test_phase4_retry_reuses_context_and_clears_recorded_error(self):
        with patch.object(collect_timelines, "get_match_details", return_value=self.match) as details:
            with patch.object(collect_timelines, "get_match_timeline", side_effect=RiotAPIError("Riot API timed out")):
                self.assertFalse(collect_timelines.process_match("KR_123", "asia"))
        details.assert_called_once()
        self.assertFalse(db_manager.is_timeline_processed("KR_123"))
        self.assertIn("timed out", self.query("SELECT timeline_last_error FROM matches")[0][0])
        with patch.object(collect_timelines, "get_match_details") as details:
            with patch.object(collect_timelines, "get_match_timeline", return_value=self.timeline):
                self.assertTrue(collect_timelines.process_match("KR_123", "asia"))
        details.assert_not_called()
        self.assertTrue(db_manager.is_timeline_processed("KR_123"))
        self.assertEqual(self.query("SELECT timeline_last_error FROM matches"), [(None,)])

    def test_completed_timeline_is_not_downloaded_again(self):
        self.cache_context()
        with patch.object(collect_timelines, "get_match_timeline", return_value=self.timeline):
            self.assertTrue(collect_timelines.process_match("KR_123", "asia"))
        with patch.object(collect_timelines, "get_match_timeline") as timeline:
            with patch.object(collect_timelines, "get_match_details") as details:
                self.assertTrue(collect_timelines.process_match("KR_123", "asia"))
        timeline.assert_not_called()
        details.assert_not_called()
        ids = [row[0] for row in self.query("SELECT event_id FROM events")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_timeline_completes_with_no_events(self):
        self.cache_context()
        with patch.object(collect_timelines, "get_match_timeline", return_value={"info": {"frames": []}}):
            self.assertTrue(collect_timelines.process_match("KR_123", "asia"))
        self.assertTrue(db_manager.is_timeline_processed("KR_123"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(0,)])
        milestone = list(db_manager.iter_saved_rows("milestones"))[0]
        self.assertIsNone(milestone["first_dragon_time"])

    def test_invalid_timeline_remains_pending_with_error(self):
        self.cache_context()
        with patch.object(collect_timelines, "get_match_timeline", return_value={}):
            self.assertFalse(collect_timelines.process_match("KR_123", "asia"))
        self.assertFalse(db_manager.is_timeline_processed("KR_123"))
        self.assertIsNotNone(self.query("SELECT timeline_last_error FROM matches")[0][0])
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(0,)])

    def test_auth_failure_stops_run_and_preserves_pending_match(self):
        self.cache_context()
        with patch.object(collect_timelines, "get_match_timeline", side_effect=RiotAPIError("Riot API HTTP 403", 403)) as get:
            self.assertEqual(collect_timelines.main([]), 1)
        get.assert_called_once()
        self.assertFalse(db_manager.is_timeline_processed("KR_123"))
        self.assertEqual(self.read_csv("events.csv"), [])

    def test_export_only_rebuilds_committed_data_without_http(self):
        self.cache_context()
        with patch.object(collect_timelines, "get_match_timeline", return_value=self.timeline):
            collect_timelines.process_match("KR_123", "asia")
        with patch.object(collect_timelines, "get_match_timeline") as timeline:
            with patch.object(collect_timelines, "get_match_details") as details:
                self.assertEqual(collect_timelines.main(["--export-only"]), 0)
                self.assertEqual(collect_timelines.main(["--export-only"]), 0)
        timeline.assert_not_called()
        details.assert_not_called()
        rows = self.read_csv("events.csv")
        self.assertGreater(len(rows), 0)
        self.assertEqual(len(rows), len({row["event_id"] for row in rows}))
        self.assertEqual(len(self.read_csv("milestones.csv")), 1)

    def test_ctrl_c_exports_completed_matches_and_keeps_current_one_pending(self):
        db_manager.save_match("KR_124", "asia")
        self.cache_context("KR_123")
        self.cache_context("KR_124")
        with patch.object(collect_timelines, "get_match_timeline", side_effect=[self.timeline, KeyboardInterrupt()]):
            self.assertEqual(collect_timelines.main([]), 130)
        self.assertTrue(db_manager.is_timeline_processed("KR_123"))
        self.assertFalse(db_manager.is_timeline_processed("KR_124"))
        rows = self.read_csv("events.csv")
        self.assertGreater(len(rows), 0)
        self.assertEqual({row["match_id"] for row in rows}, {"KR_123"})

    def test_legacy_dataset_export_only_preserves_rows_without_http(self):
        path = self.directory / "dataset.csv"
        path.write_text("match_id,patch,t1_middle,win_team1\nKR_123,15.1,Ahri,True\n", encoding="utf-8")
        with patch.object(collect_details, "get_match_details") as get:
            self.assertEqual(collect_details.main(["--export-only"]), 0)
        get.assert_not_called()
        rows = self.read_csv("dataset.csv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["t1_middle"], "Ahri")
        self.assertEqual(rows[0]["t1_first_atakhan"], "")


if __name__ == "__main__":
    unittest.main()
