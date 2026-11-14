"""Patch categories must survive CSV inference and model input loading."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from analysis.preprocess_dataset import patch_group


class PatchTypeTests(unittest.TestCase):
    def test_group_preserves_minor_component_digits(self):
        self.assertEqual(patch_group('16.20.1'), '16.20')
        self.assertEqual(patch_group('16.10.1'), '16.10')
        self.assertEqual(len({patch_group(p) for p in ['16.1.1', '16.10.1', '16.2.1', '16.20.1']}), 4)

    @unittest.skipUnless(importlib.util.find_spec('pandas'), 'Requires pandas')
    def test_shared_reader_preserves_both_version_columns(self):
        from utils.analysis_csv import read_analysis_csv
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'versions.csv'
            path.write_text('match_id,patch,patch_group\nKR_1,16.20,16.20\nKR_2,16.2,16.2\nKR_3,16.10,16.10\nKR_4,16.1,16.1\n', encoding='utf-8')
            frame = read_analysis_csv(path)
            for col in ('patch', 'patch_group'):
                self.assertEqual(frame[col].tolist(), ['16.20', '16.2', '16.10', '16.1'])
                self.assertEqual(frame[col].nunique(), 4)
            with self.assertRaises(ValueError):
                read_analysis_csv(path, dtype={'patch_group': float})

    @unittest.skipUnless(all(importlib.util.find_spec(m) for m in ('pandas', 'sklearn', 'matplotlib', 'joblib')), 'Requires mining dependencies')
    def test_existing_mining_loader_keeps_two_component_versions(self):
        import pandas as pd
        from analysis.mine_dataset import CHAMPIONS, load_data
        data = pd.DataFrame({c: [c] * 4 for c in CHAMPIONS})
        data['match_id'] = ['KR_1', 'KR_2', 'KR_3', 'KR_4']
        data['patch'] = ['16.20', '16.2', '16.10', '16.1']
        data['game_duration'] = 1200
        data['game_creation'] = [1760000000000 + i * 3600000 for i in range(4)]
        data['win_team1'] = ['True', 'False', 'True', 'False']
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'input.csv'
            data.to_csv(path, index=False)
            loaded, _ = load_data(path)
            self.assertEqual(loaded.patch_group.tolist(), data.patch.tolist())


if __name__ == '__main__':
    unittest.main()
