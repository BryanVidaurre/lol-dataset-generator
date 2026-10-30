"""End-to-end resume and backfill checks with no real network access."""
import copy
import csv
import importlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database import db_manager
from utils.objectives import extract_match_context
from utils.timeline import extract_milestones, extract_timeline_events

phase3 = importlib.import_module("collector.3_collect_details")
phase4 = importlib.import_module("collector.4_collect_timelines")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lol-recovery-tests-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = str(self.directory / "test.db")
        for override in (
            patch.object(db_manager, "DB_PATH", self.database),
            patch.object(phase3, "DATA_DIR", str(self.directory)),
            patch.object(phase4, "DATA_DIR", str(self.directory)),
            patch("requests.get", side_effect=AssertionError("Unexpected real HTTP")),
        ):
            override.start()
            self.addCleanup(override.stop)
        db_manager.init_db()
        fixtures = Path(__file__).parent / "fixtures"
        self.details = json.loads((fixtures / "match.json").read_text(encoding="utf-8"))
        self.timeline = json.loads((fixtures / "timeline.json").read_text(encoding="utf-8"))
        db_manager.save_match("KR_123", "asia")

    def query(self, sql):
        with closing(sqlite3.connect(self.database)) as conn:
            return conn.execute(sql).fetchall()

    def read_csv(self, name):
        with (self.directory / name).open(newline="", encoding="utf-8") as source:
            return list(csv.DictReader(source))

    def test_full_pipeline_then_restart_does_not_repeat_any_download(self):
        with patch.object(phase3, "get_match_details", return_value=self.details) as details:
            self.assertEqual(phase3.main([]), 0)
            self.assertEqual(phase3.main([]), 0)
        details.assert_called_once()
        with patch.object(phase4, "get_match_details") as details:
            with patch.object(phase4, "get_match_timeline", return_value=self.timeline) as timeline:
                self.assertEqual(phase4.main([]), 0)
                self.assertEqual(phase4.main([]), 0)
        details.assert_not_called()
        timeline.assert_called_once_with("asia", "KR_123")
        summaries = self.read_csv("dataset.csv")
        events = self.read_csv("events.csv")
        milestones = self.read_csv("milestones.csv")
        self.assertEqual(len(summaries), 1)
        self.assertEqual(len(summaries[0]), 49)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(len(events), len({event["event_id"] for event in events}))
        self.assertTrue(events)
        self.assertEqual({row["patch"] for row in events}, {self.details["info"]["gameVersion"]})
        self.assertEqual(json.loads(events[0]["raw_event"])["type"], events[0]["event_type"])

    def test_explicit_legacy_backfill_fetches_missing_objectives_once(self):
        db_manager.mark_match_processed("KR_123")
        output = self.directory / "dataset.csv"
        output.write_text("match_id,patch,t1_middle,win_team1\nKR_123,14.2.123,Cinder,True\n",
                          encoding="utf-8")
        with patch.object(phase3, "get_match_details", return_value=self.details) as details:
            self.assertEqual(phase3.main(["--backfill-objectives"]), 0)
            self.assertEqual(phase3.main(["--backfill-objectives"]), 0)
        details.assert_called_once_with("asia", "KR_123", raise_on_error=True)
        row = self.read_csv("dataset.csv")[0]
        self.assertEqual(row["t1_middle"], "Cinder")
        self.assertEqual(row["t1_dragon_kills"], "3")
        self.assertEqual(row["t1_first_atakhan"], "False")
        self.assertEqual(db_manager.get_match_context("KR_123"), extract_match_context(self.details))

    def test_phase3_interruption_exports_committed_rows_and_resumes(self):
        db_manager.save_match("KR_124", "asia")
        with patch.object(phase3, "get_match_details", side_effect=[self.details, KeyboardInterrupt()]):
            self.assertEqual(phase3.main([]), 130)
        self.assertEqual(self.query("SELECT match_id, processed FROM matches ORDER BY match_id"),
                         [("KR_123", 1), ("KR_124", 0)])
        self.assertEqual([row["match_id"] for row in self.read_csv("dataset.csv")], ["KR_123"])
        second_details = copy.deepcopy(self.details)
        second_details.setdefault("metadata", {})["matchId"] = "KR_124"
        with patch.object(phase3, "get_match_details", return_value=second_details) as get:
            self.assertEqual(phase3.main([]), 0)
        get.assert_called_once_with("asia", "KR_124", raise_on_error=True)
        self.assertEqual(len(self.read_csv("dataset.csv")), 2)

    def test_summary_failure_before_completion_rolls_back_summary_and_context(self):
        row = phase3.extract_match_data("KR_123", self.details)
        _, participant_teams = extract_match_context(self.details)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_summary BEFORE UPDATE OF processed ON matches
                WHEN NEW.processed = 1 BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            db_manager.save_match_summary("KR_123", row, participant_teams)
        self.assertEqual(list(db_manager.iter_saved_rows("match_summaries")), [])
        self.assertEqual(self.query("SELECT processed FROM matches"), [(0,)])
        self.assertIsNone(db_manager.get_match_context("KR_123"))

    def test_ctrl_c_during_event_transaction_rolls_back_then_retries(self):
        _, mapping = extract_match_context(self.details)
        events = extract_timeline_events("KR_123", "14.2.123", self.timeline, mapping)
        milestone = extract_milestones("KR_123", "14.2.123", events)

        def interrupted_events():
            yield events[0]
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            db_manager.save_timeline("KR_123", interrupted_events(), milestone)
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(0,)])
        self.assertFalse(db_manager.is_timeline_processed("KR_123"))
        db_manager.save_timeline("KR_123", events, milestone)
        self.assertTrue(db_manager.is_timeline_processed("KR_123"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(len(events),)])

    def test_legacy_csv_duplicate_match_ids_export_one_summary(self):
        output = self.directory / "dataset.csv"
        output.write_text("match_id,patch,t1_middle\nKR_123,14.2,Cinder\nKR_123,14.2,Cinder\n",
                          encoding="utf-8")
        self.assertEqual(phase3.main(["--export-only"]), 0)
        self.assertEqual(len(self.read_csv("dataset.csv")), 1)


if __name__ == "__main__":
    unittest.main()
