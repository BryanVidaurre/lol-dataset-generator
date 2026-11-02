"""Bounded collection tests: no real Riot calls and no waiting."""
import importlib
import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import db_manager
from database import weekend_store as store
from utils import riot_api

weekend = importlib.import_module('collector.collect_weekend')


class WeekendTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='lol-weekend-tests-')
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for override in (
            patch.object(db_manager, 'DB_PATH', str(self.folder / 'test.db')),
            patch.object(weekend, 'DATA_DIR', str(self.folder)),
            patch.object(riot_api.requests, 'get', side_effect=AssertionError('Real HTTP forbidden')),
        ):
            override.start()
            self.addCleanup(override.stop)
        db_manager.init_db()
        store.initialize()

    def run_config(self, regions=None, target=4, players=0):
        return store.open_run('weekend', 40, target, 42,
                              regions or {'EUW': 'euw1', 'KR': 'kr'}, players)

    def test_resume_preserves_sample_and_existing_metadata(self):
        first = self.run_config()
        later = store.open_run('weekend', 2, 1, 99, {'NA': 'na1'}, 100)
        self.assertEqual(first, later)
        self.assertAlmostEqual(first['deadline'] - first['started_at'], 40 * 3600)

    def test_balanced_selection_and_shortage_redistribution(self):
        for mid in ('EUW1_1', 'EUW1_2', 'EUW1_3', 'EUW1_4', 'KR_1'):
            db_manager.save_match(mid, 'europe' if mid.startswith('EUW') else 'asia')
        run = self.run_config()
        self.assertEqual(store.select_matches(run), {'EUW': 3, 'KR': 1})
        self.assertEqual(store.progress('weekend')['selected'], 4)
        db_manager.save_match('KR_0', 'asia')
        self.assertEqual(store.select_matches(run), {'EUW': 3, 'KR': 1})
        self.assertEqual(store.progress('weekend')['selected'], 4)

    def test_no_candidates_is_valid_empty_sample(self):
        self.assertEqual(store.select_matches(self.run_config()), {'EUW': 0, 'KR': 0})
        self.assertEqual(store.progress('weekend')['selected'], 0)
        self.assertEqual(store.load_run('weekend')['selection_done'], 0)

    def test_empty_database_requires_phase1_without_network_or_empty_exports(self):
        with self.assertLogs(weekend.logger, level='ERROR') as captured:
            self.assertEqual(weekend.main([]), 1)
        self.assertIn('1_collect_players.py', ' '.join(captured.output))
        self.assertEqual(store.load_run('weekend')['selection_done'], 0)
        self.assertFalse((self.folder / 'weekend' / 'dataset.csv').exists())

    def test_old_empty_selection_reopens_then_collects_after_phase1(self):
        self.run_config(regions={'KR': 'kr'}, target=1, players=3)
        with closing(db_manager.get_connection()) as conn, conn:
            conn.execute("UPDATE weekend_runs SET selection_done = 1 WHERE name = 'weekend'")
        self.assertEqual(self.run_config()['selection_done'], 0)
        db_manager.save_player('player', None, 'KR', 'MASTER', 1, 1, 1)
        fixtures = Path(__file__).parent / 'fixtures'
        match = json.loads((fixtures / 'match.json').read_text(encoding='utf-8'))
        timeline = json.loads((fixtures / 'timeline.json').read_text(encoding='utf-8'))
        with patch.object(weekend, 'get_match_ids', return_value=['KR_123']) as history:
            with patch.object(weekend.details, 'get_match_details', return_value=match):
                with patch.object(weekend.timelines, 'get_match_timeline', return_value=timeline):
                    self.assertEqual(weekend.main([]), 0)
        history.assert_called_once()
        self.assertEqual(store.progress('weekend')['complete'], 1)

    def test_player_history_without_matches_keeps_selection_open(self):
        db_manager.save_player('player', None, 'KR', 'MASTER', 1, 1, 1)
        run = self.run_config(regions={'KR': 'kr'}, target=1, players=3)
        with patch.object(weekend, 'get_match_ids', return_value=[]):
            self.assertEqual(weekend.worker(run), 1)
        self.assertEqual(store.load_run('weekend')['selection_done'], 0)

    def test_sources_bounded_and_balanced_by_tier(self):
        for tier in ('MASTER', 'GRANDMASTER', 'CHALLENGER'):
            for index in range(5):
                db_manager.save_player(tier + str(index), None, 'KR', tier, 1, 1, 1)
        run = self.run_config(regions={'KR': 'kr'}, players=6)
        store.prepare_sources(run)
        sources = store.pending_sources('weekend')
        self.assertEqual(len(sources), 6)
        self.assertEqual([sum(puuid.startswith(tier) for puuid, _ in sources)
                          for tier in ('MASTER', 'GRANDMASTER', 'CHALLENGER')], [2, 2, 2])
        puuid, _ = sources[0]
        store.save_history('weekend', puuid, ['KR_1', 'KR_1'], 'asia')
        with closing(db_manager.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT matches_collected FROM players WHERE puuid = ?',
                                          (puuid,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM matches').fetchone()[0], 1)
        self.assertEqual(len(store.pending_sources('weekend')), 5)

    def test_history_error_remains_pending(self):
        db_manager.save_player('player', None, 'KR', 'MASTER', 1, 1, 1)
        run = self.run_config(regions={'KR': 'kr'}, players=3)
        store.prepare_sources(run)
        store.source_error('weekend', 'player', 'Riot API HTTP 503')
        self.assertEqual(store.pending_sources('weekend'), [('player', 'KR')])

    def test_expired_legacy_run_collects_exports_and_resumes_without_redownload(self):
        fixtures = Path(__file__).parent / 'fixtures'
        match = json.loads((fixtures / 'match.json').read_text(encoding='utf-8'))
        timeline = json.loads((fixtures / 'timeline.json').read_text(encoding='utf-8'))
        db_manager.save_match('KR_123', 'asia')
        run = self.run_config(regions={'KR': 'kr'}, target=1)
        with closing(db_manager.get_connection()) as conn, conn:
            conn.execute("UPDATE weekend_runs SET deadline = 1, collect_until = 1 WHERE name = 'weekend'")
        run = store.load_run('weekend')
        with patch.object(weekend.details, 'get_match_details', return_value=match) as get_details:
            with patch.object(weekend.timelines, 'get_match_timeline', return_value=timeline) as get_timeline:
                self.assertEqual(weekend.worker(run), 0)
                self.assertEqual(weekend.worker(self.run_config()), 0)
        get_details.assert_called_once()
        get_timeline.assert_called_once()
        self.assertEqual(store.progress('weekend')['complete'], 1)
        self.assertEqual(len(list(store.iter_complete_rows('weekend', 'match_summaries'))), 1)
        self.assertTrue((self.folder / 'weekend' / 'events.csv').exists())

    def test_partial_pair_is_cached_but_not_exported(self):
        match = json.loads((Path(__file__).parent / 'fixtures' / 'match.json').read_text(encoding='utf-8'))
        db_manager.save_match('KR_123', 'asia')
        run = self.run_config(regions={'KR': 'kr'}, target=1)
        with patch.object(weekend.details, 'get_match_details', return_value=match):
            with patch.object(weekend.timelines, 'get_match_timeline', side_effect=riot_api.RiotAPIError('503', 503)):
                weekend.worker(run)
        self.assertEqual(list(store.iter_complete_rows('weekend', 'match_summaries')), [])
        self.assertEqual(store.progress('weekend')['pending'], 1)
        self.assertIsNotNone(db_manager.get_match_context('KR_123'))

    def test_auth_failure_preserves_pending_sources_and_sample(self):
        db_manager.save_player('player', None, 'KR', 'MASTER', 1, 1, 1)
        run = self.run_config(regions={'KR': 'kr'}, players=3)
        with patch.object(weekend, 'get_match_ids', side_effect=riot_api.RiotAPIError('403', 403)):
            self.assertEqual(weekend.worker(run), 1)
        self.assertEqual(len(store.pending_sources('weekend')), 1)
        self.assertEqual(store.open_run('weekend', 40, 4, 42, {}, 0)['target'], run['target'])

    def test_plan_does_not_start_run_or_make_requests(self):
        with patch.object(weekend, 'worker') as start:
            self.assertEqual(weekend.main(['--plan']), 0)
        start.assert_not_called()
        with closing(db_manager.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM weekend_runs').fetchone()[0], 0)

    def test_hours_are_only_a_planning_reference(self):
        self.assertEqual(weekend.main(['--hours', '80', '--plan']), 0)

    def test_invalid_planning_hours_are_rejected(self):
        for hours in ('0', '-1', 'nan', 'inf'):
            with self.subTest(hours=hours), self.assertRaises(SystemExit) as error:
                weekend.main(['--hours', hours, '--plan'])
            self.assertEqual(error.exception.code, 2)

    def test_export_only_never_starts_a_new_run(self):
        self.assertEqual(weekend.main(['--export-only']), 1)
        self.assertIsNone(store.load_run('weekend'))

    def test_expired_legacy_run_resumes_with_the_same_sample(self):
        run = self.run_config()
        with closing(db_manager.get_connection()) as conn, conn:
            conn.execute("UPDATE weekend_runs SET deadline = 1, collect_until = 1 WHERE name = 'weekend'")
        with patch.object(weekend, 'worker', return_value=0) as worker:
            self.assertEqual(weekend.main([]), 0)
        worker.assert_called_once()
        self.assertEqual(worker.call_args[0][0]['target'], run['target'])
        self.assertEqual(store.load_run('weekend')['deadline'], 1)
        self.assertEqual(store.load_run('weekend')['started_at'], run['started_at'])

    def test_export_ignores_expired_legacy_deadlines(self):
        self.run_config()
        with closing(db_manager.get_connection()) as conn, conn:
            conn.execute("UPDATE weekend_runs SET deadline = 1, collect_until = 1 WHERE name = 'weekend'")
        self.assertEqual(weekend.main(['--export-only']), 0)
        self.assertTrue((self.folder / 'weekend' / 'dataset.csv').exists())

    def test_second_collector_lock_is_rejected_and_released(self):
        with weekend.RunLock():
            with self.assertRaises(RuntimeError):
                with weekend.RunLock():
                    self.fail('A second supervisor acquired the same lock')
        with weekend.RunLock():
            pass


if __name__ == '__main__':
    unittest.main()
