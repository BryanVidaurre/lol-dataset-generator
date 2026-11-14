"""Small end-to-end cases for contextual datasets; no data mining or model fit."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from tests import test_preprocessing as base_tests
from tests.test_preprocessing import read, write
from analysis.preprocess_dataset import run as preprocess
from utils.timeline import EVENT_FIELDNAMES

AVAILABLE = importlib.util.find_spec('duckdb') is not None
if AVAILABLE:
    import duckdb
    from analysis.prepare_mining import ValidationError, balance, literal, phase, run
    from analysis.mining_inputs import iter_sequence_prefixes, predictor_query


@unittest.skipUnless(AVAILABLE, 'Install requirements-preprocessing.txt')
class MiningReadyTests(unittest.TestCase):
    def fixture(self, root):
        original = root / 'original'
        original.mkdir()
        summaries, milestones, events = base_tests.PreprocessingTests().fixture(original)
        # Exercise simultaneous itemsets, a first blood with environmental prior death,
        # a first dragon with unknown team, and a later credited dragon.
        for i in range(3):
            extra = dict(events[0])
            extra.update(event_id=f'KR_0:1:{10+i}', frame_index=1, event_index=10+i,
                         timestamp_ms=[300000, 400000, 500000][i],
                         minute=[5, 400000/60000, 500000/60000][i],
                         event_type=['CHAMPION_KILL', 'ELITE_MONSTER_KILL', 'ELITE_MONSTER_KILL'][i],
                         actor_team_id=[100, '', 200][i], team_id=[100, '', 200][i],
                         killer_id=[1, '', 6][i], monster_type=['', 'DRAGON', 'DRAGON'][i])
            events.append(extra)
        events.sort(key=lambda e: int(e['timestamp_ms']))
        for i, event in enumerate(events):
            event.update(event_id=f'KR_0:1:{i}', frame_index=1, event_index=i)
        # Existing champion kill has no killer ID; don't assume first blood from it.
        write(original / 'events.csv', events, EVENT_FIELDNAMES)
        for i, patch in enumerate(['16.20', '16.2', '16.10', '16.1']):
            summaries[i]['patch'] = patch
            milestones[i]['patch'] = patch
        for event in events:
            event['patch'] = '16.20'
        write(original / 'dataset.csv', summaries, list(summaries[0]))
        write(original / 'milestones.csv', milestones, list(milestones[0]))
        write(original / 'events.csv', events, EVENT_FIELDNAMES)
        processed = root / 'processed'
        preprocess(original, processed, minutes=[5, 10, 20])
        return original, processed

    def test_end_to_end_symmetric_states_sequences_and_patch_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, processed = self.fixture(root)
            output = root / 'ready'
            result = run(processed, original, output, sample_matches=5, memory_limit='512MB')
            self.assertTrue(all(c['passed'] for c in result['checks']))
            db = duckdb.connect()
            try:
                states = db.execute(f'SELECT team_id,kills_diff,first_blood_owner,first_dragon_owner FROM read_parquet({literal(output / "strategic_states.parquet")}) WHERE match_id=\'KR_0\' AND game_minute=10 ORDER BY team_id').fetchall()
                self.assertEqual(states, [(100, 0, 'self', 'unknown'), (200, 0, 'enemy', 'unknown')])
                schema = dict((r[0], r[1]) for r in db.execute(f'DESCRIBE SELECT * FROM read_parquet({literal(output / "strategic_states.parquet")})').fetchall())
                self.assertEqual(schema['patch'], 'VARCHAR')
                self.assertEqual(schema['patch_group'], 'VARCHAR')
                seq = list(iter_sequence_prefixes(output / 'event_sequences.parquet', cutoff_ms=300000))
                team = next(r for r in seq if r['match_id'] == 'KR_0' and r['team_id'] == 100)
                self.assertEqual(team['timestamps_ms'], [240000, 300000])
                self.assertEqual(team['itemsets'][1], ['self:champion_kill', 'self:tower'])
                query = predictor_query(output)
                self.assertNotIn('target_win', query)
                self.assertNotIn('team_id', query)
                self.assertNotIn('win_team1', query)
                db.execute(query).fetchall()
                with self.assertRaises(ValueError):
                    predictor_query(output, 'purged')
                with self.assertRaises(FileExistsError):
                    run(processed, original, output)
            finally:
                db.close()

    def test_corrupt_snapshot_fails_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, processed = self.fixture(root)
            snapshots = read(processed / 'snapshots.csv')
            snapshots[0]['t1_tower_kills'] = '999'
            write(processed / 'snapshots.csv', snapshots, list(snapshots[0]))
            output = root / 'failed'
            with self.assertRaisesRegex(ValidationError, 'all_snapshot_counters_match_events'):
                run(processed, original, output)
            self.assertFalse(output.exists())
            failure = json.loads((root / 'failed.failed_validation.json').read_text(encoding='utf-8'))
            self.assertEqual(failure['status'], 'failed')

    def test_corrupt_patch_is_detected_against_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, processed = self.fixture(root)
            frame = read(processed / 'prematch_features.csv')
            frame[0]['patch_group'] = '16.2'
            write(processed / 'prematch_features.csv', frame, list(frame[0]))
            with self.assertRaisesRegex(ValidationError, 'patches_against_originals'):
                run(processed, original, root / 'failed')

    def test_fixed_bins_and_phase_boundaries(self):
        self.assertEqual([balance(v) for v in [-1, 0, 1]], ['disadvantage', 'equal', 'advantage'])
        self.assertEqual([phase(m) for m in [14, 15, 24, 25]], ['early', 'middle', 'middle', 'late'])


if __name__ == '__main__':
    unittest.main()
