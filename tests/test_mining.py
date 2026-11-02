"""Optional mining checks: temporal separation, leakage and objective attribution."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

AVAILABLE = all(importlib.util.find_spec(name) is not None
                for name in ('pandas', 'numpy', 'sklearn', 'matplotlib', 'joblib'))
if AVAILABLE:
    import numpy as np
    import pandas as pd
    from analysis.mine_dataset import CHAMPIONS, load_data, make_model, objective_rules, temporal_split


@unittest.skipUnless(AVAILABLE, 'Install requirements-mining.txt for mining tests')
class MiningTests(unittest.TestCase):
    def frame(self, count=200):
        frame = pd.DataFrame({column: ['Champion_' + column] * count for column in CHAMPIONS})
        frame['match_id'] = [f'KR_{index}' for index in range(count)]
        frame['patch'] = '16.20.1'
        frame['patch_group'] = '16.20'
        frame['started_at'] = pd.date_range('2026-01-01', periods=count, freq='h', tz='UTC')
        frame['ended_at'] = frame['started_at'] + pd.Timedelta(minutes=20)
        frame['target'] = [index % 2 for index in range(count)]
        return frame

    def test_temporal_split_purges_unavailable_labels_and_keeps_ids_disjoint(self):
        frame = self.frame()
        frame.loc[139, 'ended_at'] += pd.Timedelta(hours=3)
        frame.loc[169, 'ended_at'] += pd.Timedelta(hours=3)
        train, validation, test = temporal_split(frame)
        self.assertNotIn('KR_139', set(train['match_id']))
        self.assertNotIn('KR_169', set(validation['match_id']))
        self.assertLess(train['ended_at'].max(), validation['started_at'].min())
        self.assertLess(validation['ended_at'].max(), test['started_at'].min())
        self.assertFalse(set(train['match_id']) & set(test['match_id']))

    def test_future_result_columns_do_not_change_predictions(self):
        frame = self.frame(50)
        frame['win_team1'] = frame['target']
        frame['t1_tower_kills'] = frame['target'] * 11
        frame['game_duration'] = frame['target'] * 1000 + 1000
        model = make_model().fit(frame, frame['target'])
        before = model.predict_proba(frame)
        changed = frame.copy()
        changed['win_team1'] = 1 - changed['win_team1']
        changed['t1_tower_kills'] = 999
        changed['game_duration'] = 99999
        changed['t1_avg_tier'] = 999
        np.testing.assert_array_equal(before, model.predict_proba(changed))

    def test_unknown_test_champions_are_not_learned_during_prediction(self):
        frame = self.frame(50)
        model = make_model().fit(frame, frame['target'])
        unseen = frame.iloc[:1].copy()
        unseen['t1_top'] = 'UnseenChampion'
        self.assertTrue(np.isfinite(model.predict_proba(unseen)).all())
        encoder = model.named_steps['encode'].named_transformers_['categories']
        self.assertNotIn('UnseenChampion', encoder.categories_[0])

    def test_objective_rules_exclude_unattributed_and_late_events(self):
        frame = self.frame(4)
        frame['target'] = [1, 0, 1, 0]
        milestones = frame[['match_id']].copy()
        for objective in ('tower', 'dragon', 'herald', 'horde', 'baron', 'inhibitor'):
            milestones[f'first_{objective}_team'] = np.nan
            milestones[f'first_{objective}_time'] = np.nan
        milestones['first_tower_team'] = [100, np.nan, 200, 100]
        milestones['first_tower_time'] = [5 * 60000, 5 * 60000, 16 * 60000, 6 * 60000]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'milestones.csv'
            milestones.to_csv(path, index=False)
            rules = objective_rules(frame, path, minute=15, min_cases=1)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules.iloc[0]['cases'], 2)
            self.assertEqual(rules.iloc[0]['confidence'], .5)
            self.assertTrue(objective_rules(frame, path, minute=15, min_cases=30).empty)

    def test_duplicate_matches_are_rejected_before_training(self):
        frame = self.frame(2)
        frame['match_id'] = 'KR_duplicate'
        frame['game_creation'] = frame['started_at'].astype('int64') // 1000000
        frame['game_duration'] = 1200
        frame['win_team1'] = ['True', 'False']
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dataset.csv'
            frame.to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, 'unique'):
                load_data(path)


if __name__ == '__main__':
    unittest.main()
