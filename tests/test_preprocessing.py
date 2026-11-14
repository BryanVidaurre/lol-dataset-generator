"""Check cutoff leakage, team semantics, missingness and safe publication."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from analysis.preprocess_dataset import (BOOLEAN_FIELDS, CHAMPIONS, COUNTS,
                                         run, temporal_membership)
from utils.timeline import EVENT_FIELDNAMES, MILESTONE_FIELDNAMES, MILESTONE_NAMES


def write(path, records, fields):
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def read(path):
    with path.open(newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


class PreprocessingTests(unittest.TestCase):
    def fixture(self, source):
        summaries, milestones = [], []
        for i in range(20):
            row = {'match_id': f'KR_{i}', 'patch': '16.20.1',
                   'game_creation': 1760000000000 + i * 3600000,
                   'game_duration': 1200, 't1_avg_tier': 0, 't2_avg_tier': 1.5}
            row.update({col: 'Champion' + str(j) for j, col in enumerate(CHAMPIONS)})
            row.update({col: 'False' for col in BOOLEAN_FIELDS})
            row.update({f't{t}_{o}_kills': 0 for t in (1, 2) for o in COUNTS})
            summaries.append(row)
            milestone = dict.fromkeys(MILESTONE_FIELDNAMES, '')
            milestone.update(match_id=f'KR_{i}', patch='16.20.1')
            milestones.append(milestone)
        summaries[-1]['game_duration'] = 60
        summaries[0]['t1_first_herald'] = 'False'
        milestones[0].update(first_herald_time=240000)  # known time, unknown actor
        write(source / 'dataset.csv', summaries, list(summaries[0]))
        write(source / 'milestones.csv', milestones, MILESTONE_FIELDNAMES)
        events = []
        for idx, (stamp, kind, actor, owner, monster, building) in enumerate([
            (240000, 'ELITE_MONSTER_KILL', '', '', 'RIFTHERALD', ''),
            (300000, 'BUILDING_KILL', 100, 200, '', 'TOWER_BUILDING'),
            (300001, 'CHAMPION_KILL', 200, 200, '', ''),
            (1200500, 'CHAMPION_KILL', 100, 100, '', ''),
        ]):
            event = dict.fromkeys(EVENT_FIELDNAMES, '')
            event.update(event_id=f'KR_0:1:{idx}', match_id='KR_0', patch='16.20.1',
                         timestamp_ms=stamp, minute=stamp / 60000, frame_index=1,
                         event_index=idx, event_type=kind, actor_team_id=actor,
                         team_id=owner, monster_type=monster, building_type=building,
                         assisting_participant_ids='[]', raw_event='{}')
            events.append(event)
        write(source / 'events.csv', events, EVENT_FIELDNAMES)
        return summaries, milestones, events

    def test_end_to_end_preserves_sources_and_excludes_future_features(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            source.mkdir()
            summaries, milestones, events = self.fixture(source)
            original = {p.name: p.read_bytes() for p in source.iterdir()}
            out = Path(temp) / 'processed'
            report = run(source, out, minutes=[5, 10, 20])
            self.assertEqual(report['retained_matches'], 19)
            self.assertEqual(report['events']['matches_without_events'], 18)
            snapshots = read(out / 'snapshots.csv')
            self.assertEqual(len(snapshots), 2)  # no snapshot at exact end
            early, late = snapshots
            self.assertEqual(early['t1_tower_kills'], '1')  # attacker, not owner
            self.assertEqual(early['t2_champion_kills'], '0')  # event one ms later excluded
            self.assertEqual(late['t2_champion_kills'], '1')
            self.assertEqual(late['t1_champion_kills'], '0')  # after end excluded
            self.assertEqual(early['unknown_herald_kills'], '1')
            self.assertNotIn('win_team1', early)
            self.assertNotIn('game_duration', early)
            self.assertNotIn('t1_avg_tier', read(out / 'prematch_features.csv')[0])
            clean = read(out / 'dataset_clean.csv')[0]
            self.assertEqual(clean['t1_avg_tier'], '')
            self.assertEqual(clean['t1_avg_tier_unknown'], '1')
            ms = read(out / 'milestones_clean.csv')[0]
            self.assertEqual(ms['first_herald_team'], '')
            self.assertEqual(ms['first_herald_minute'], '4.0')
            self.assertEqual(ms['first_dragon_minute'], '')
            self.assertNotIn('raw_event', read(out / 'events_clean.csv')[0])
            for name, content in original.items():
                self.assertEqual((source / name).read_bytes(), content)
            self.assertTrue((out / 'preprocessing_report.md').exists())
            with self.assertRaises(FileExistsError):
                run(source, out)

    def test_duplicates_removed_but_conflicting_ids_fail_without_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            source.mkdir()
            _, _, events = self.fixture(source)
            write(source / 'events.csv', [events[0], events[0]] + events[1:], EVENT_FIELDNAMES)
            report = run(source, Path(temp) / 'dedup')
            self.assertEqual(report['events']['exact_duplicate_events_removed'], 1)
            changed = events[0].copy()
            changed['raw_event'] = '{"different":true}'
            write(source / 'events.csv', [events[0], changed] + events[1:], EVENT_FIELDNAMES)
            output = Path(temp) / 'failed'
            with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
                run(source, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(temp).glob('failed.building-*')), [])

    def test_temporal_split_purges_results_unavailable_at_boundary(self):
        clean = {str(i): {'match_id': str(i), 'game_creation': i * 3600000,
                          'game_duration': 1200} for i in range(20)}
        clean['13']['game_duration'] = 7200
        clean['16']['game_duration'] = 7200
        membership, boundaries = temporal_membership(clean)
        groups = {r['match_id']: r['split'] for r in membership}
        self.assertEqual(groups['13'], 'purged')
        self.assertEqual(groups['16'], 'purged')
        self.assertEqual(groups['14'], 'validation')
        self.assertEqual(groups['17'], 'test')

    def test_missing_milestone_fails_without_creating_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            source.mkdir()
            _, milestones, _ = self.fixture(source)
            write(source / 'milestones.csv', milestones[1:], MILESTONE_FIELDNAMES)
            output = Path(temp) / 'failed'
            with self.assertRaisesRegex(ValueError, 'match IDs must agree'):
                run(source, output)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
