"""Filtered collection tests using an isolated database and no real requests."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector import collect_patch_sample as sample
from database import db_manager, weekend_store
from utils.riot_api import RiotAPIError


class PatchSampleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='lol-patch-tests-')
        self.addCleanup(folder.cleanup)
        for override in (
            patch.object(db_manager, 'DB_PATH', str(Path(folder.name) / 'test.db')),
            patch.object(sample, 'DATA_DIR', folder.name),
            patch.object(sample, 'REGIONS', {'KR': 'kr'}),
        ):
            override.start()
            self.addCleanup(override.stop)
        db_manager.init_db()

    def complete_match(self, match_id, version, timeline=True):
        db_manager.save_match(match_id, 'asia')
        db_manager.save_match_summary(match_id, {'match_id': match_id, 'patch': version}, {1: 100})
        with closing(db_manager.get_connection()) as conn, conn:
            conn.execute('UPDATE matches SET timeline_processed = ? WHERE match_id = ?',
                         (timeline, match_id))

    def test_complete_sample_reuses_matching_cache_and_rejects_other_patches(self):
        self.complete_match('KR_1', '16.20.1.2')
        self.complete_match('KR_2', '16.19.1.2')
        self.complete_match('KR_3', '16.21.1.2')
        for match_id in ('KR_4', 'KR_5'):
            db_manager.save_match(match_id, 'asia')
        run = sample.initialize('filtered', 3, 42, 0)
        def get_details(match_id, routing):
            version = '16.22.1.0' if match_id == 'KR_4' else '16.20.2.0'
            db_manager.save_match_summary(match_id, {'match_id': match_id, 'patch': version}, {1: 100})
            return True
        def get_timeline(match_id, routing):
            with closing(db_manager.get_connection()) as conn, conn:
                conn.execute('UPDATE matches SET timeline_processed = 1 WHERE match_id = ?', (match_id,))
            return True
        with patch.object(sample, 'discover'), patch.object(sample.details, 'process_match', side_effect=get_details):
            with patch.object(sample.timelines, 'process_match', side_effect=get_timeline) as timelines:
                self.assertTrue(sample.collect(run))
        with closing(db_manager.get_connection()) as conn:
            selected = {r[0] for r in conn.execute("SELECT match_id FROM weekend_matches WHERE run_name='filtered'")}
        self.assertEqual(selected, {'KR_1', 'KR_3', 'KR_5'})
        self.assertEqual(timelines.call_count, 1)
        self.assertTrue(sample.collect(run))  # Completed sample makes no additional requests.

    def test_failed_timeline_remains_retryable_and_short_sample_is_reported(self):
        self.complete_match('KR_1', '16.21.1.2', timeline=False)
        run = sample.initialize('filtered', 1, 42, 0)
        with patch.object(sample, 'discover'), patch.object(sample.timelines, 'process_match', return_value=False):
            self.assertFalse(sample.collect(run))
        self.assertEqual(weekend_store.progress('filtered')['selected'], 0)
        with patch.object(sample, 'discover'), patch.object(sample.timelines, 'process_match', return_value=True):
            # The timeline collector commits its results before returning True.
            with closing(db_manager.get_connection()) as conn, conn:
                conn.execute("UPDATE matches SET timeline_processed=1 WHERE match_id='KR_1'")
            self.assertTrue(sample.collect(run))

    def test_discovery_resumes_historical_pagination_after_error(self):
        db_manager.save_player('player', None, 'KR', 'CHALLENGER', 1, 1, 1)
        run = sample.initialize('filtered', 200, 42, 1)
        ids = [f'KR_{i}' for i in range(100)]
        with patch.object(sample, 'get_match_ids', side_effect=[ids, RiotAPIError('HTTP 503', 503)]):
            sample.discover(run)
        with closing(db_manager.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT next_start, done FROM patch_sample_sources').fetchone(), (100, 0))
            self.assertEqual(conn.execute('SELECT count(*) FROM matches').fetchone()[0], 100)
        with patch.object(sample, 'get_match_ids', return_value=['KR_100']) as api:
            sample.discover(run)
        self.assertEqual(api.call_args.kwargs['start'], 100)
        self.assertEqual(api.call_args.kwargs['start_time'], sample.START_TIME)
        self.assertLessEqual(api.call_args.kwargs['end_time'], sample.END_TIME)
        with closing(db_manager.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT next_start, done FROM patch_sample_sources').fetchone(), (101, 1))
            self.assertEqual(conn.execute('SELECT count(*) FROM matches').fetchone()[0], 101)

    def test_existing_unfiltered_run_cannot_be_reused(self):
        weekend_store.open_run('old', 40, 10, 42, {'KR': 'kr'}, 1)
        with self.assertRaises(RuntimeError):
            sample.initialize('old', 10, 42, 1)

    def test_extension_preserves_all_complete_base_matches_and_filters_only_additions(self):
        self.complete_match('KR_1', '16.19.1.2')
        self.complete_match('KR_2', '16.20.1.2')
        self.complete_match('KR_3', '16.21.1.2')
        self.complete_match('KR_4', '16.18.1.2')
        self.complete_match('KR_5', '16.21.1.2', timeline=False)
        base = weekend_store.open_run('old', 40, 2, 42, {'KR': 'kr'}, 0)
        with closing(db_manager.get_connection()) as conn, conn:
            conn.executemany('INSERT INTO weekend_matches VALUES (?, ?)',
                             [('old', 'KR_1'), ('old', 'KR_2'), ('old', 'KR_5')])
            conn.execute("UPDATE weekend_runs SET selection_done=1 WHERE name='old'")
        run = sample.initialize('expanded', 50000, 42, 0, 'old', 1)
        self.assertEqual(run['target'], 3)
        with patch.object(sample, 'discover') as discover:
            self.assertTrue(sample.collect(run))
            discover.assert_not_called()
        with closing(db_manager.get_connection()) as conn:
            chosen = {r[0] for r in conn.execute("SELECT match_id FROM weekend_matches WHERE run_name='expanded'")}
        self.assertEqual(chosen, {'KR_1', 'KR_2', 'KR_3'})
        self.assertEqual(sample.initialize('expanded', 50000, 42, 0, 'old', 1)['target'], 3)
        with self.assertRaises(RuntimeError):
            sample.initialize('expanded', 50000, 42, 0, 'old', 2)

    def test_run_can_expand_player_search_without_resetting_progress(self):
        sample.initialize('filtered', 10, 42, 1)
        updated = sample.initialize('filtered', 10, 42, 5)
        self.assertEqual(updated['players_per_region'], 5)
        with self.assertRaises(RuntimeError):
            sample.initialize('filtered', 10, 42, 1)
        with self.assertRaises(RuntimeError):
            sample.initialize('filtered', 11, 42, 5)


if __name__ == '__main__':
    unittest.main()
