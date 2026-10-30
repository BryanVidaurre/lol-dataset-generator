import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from utils import timeline


class TimelineTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).parent / "fixtures" / "timeline.json"
        self.payload = json.loads(fixture.read_text(encoding="utf-8"))
        self.teams = {1: 200, 2: 200, 3: 100, 7: 100, 8: 200}

    def extract(self, payload=None, teams=None):
        return timeline.extract_timeline_events(
            "KR_123", "16.1.1", self.payload if payload is None else payload,
            self.teams if teams is None else teams,
        )

    def single(self, event, teams=None):
        return self.extract({"info": {"frames": [{"events": [event]}]}}, teams)

    def test_champion_kill(self):
        row = self.extract()[0]
        self.assertEqual(row["event_type"], "CHAMPION_KILL")
        self.assertEqual(row["team_id"], 100)
        self.assertEqual(row["killer_id"], 3)
        self.assertEqual(row["victim_id"], 7)
        self.assertEqual(json.loads(row["assisting_participant_ids"]), [1, 2])
        self.assertEqual((row["position_x"], row["position_y"]), (100, 200))
        self.assertEqual(row["minute"], 188000 / 60000)
        self.assertEqual(row["patch"], "16.1.1")
        self.assertEqual(json.loads(row["raw_event"]), self.payload["info"]["frames"][0]["events"][0])
        self.assertEqual(set(row), set(timeline.EVENT_FIELDNAMES))

    def test_elite_monster_kill_preserves_types(self):
        row = self.extract()[1]
        self.assertEqual(row["monster_type"], "HORDE")
        self.assertEqual(row["team_id"], 200)
        self.assertEqual(row["timestamp_ms"], 315000)
        dragon = next(row for row in self.extract() if row["monster_type"] == "DRAGON")
        self.assertEqual(dragon["monster_sub_type"], "FIRE_DRAGON")

    def test_building_owner_and_attacker_are_separate(self):
        row = self.extract()[2]
        self.assertEqual(row["event_type"], "BUILDING_KILL")
        self.assertEqual(row["team_id"], 200)
        self.assertEqual(row["actor_team_id"], 100)
        self.assertEqual(row["building_type"], "TOWER_BUILDING")
        self.assertEqual(row["tower_type"], "OUTER_TURRET")
        self.assertEqual(row["lane_type"], "MID_LANE")
        result = timeline.extract_milestones("KR_123", "16.1", [row])
        self.assertEqual(result["first_tower_team"], 100)

    def test_building_destroyed_by_minions_uses_explicit_owner(self):
        row = self.single({
            "type": "BUILDING_KILL", "timestamp": 1000, "killerId": 0,
            "teamId": 100, "buildingType": "INHIBITOR_BUILDING",
        })[0]
        self.assertEqual(row["team_id"], 100)
        self.assertEqual(row["actor_team_id"], 200)

    def test_turret_plate_preserves_owner_and_infers_attacker(self):
        for killer in (3, 0):
            with self.subTest(killer=killer):
                row = self.single({
                    "type": "TURRET_PLATE_DESTROYED", "timestamp": 10,
                    "teamId": 200, "killerId": killer, "laneType": "MID_LANE",
                })[0]
                self.assertEqual(row["team_id"], 200)
                self.assertEqual(row["actor_team_id"], 100)
                result = timeline.extract_milestones("KR_123", "16.1", [row])
                self.assertIsNone(result["first_tower_time"])

    def test_execution_then_explicit_first_blood(self):
        events = self.single({"type": "CHAMPION_KILL", "timestamp": 1, "killerId": 0})
        events += self.single({"type": "CHAMPION_KILL", "timestamp": 10, "killerId": 3})
        events += self.single({
            "type": "CHAMPION_SPECIAL_KILL", "timestamp": 10,
            "killerId": 3, "killType": "KILL_FIRST_BLOOD",
        })
        result = timeline.extract_milestones("KR_123", "16.1", events)
        self.assertEqual(result["first_blood_time"], 10)
        self.assertEqual(result["first_blood_team"], 100)

    def test_team_inferred_from_participant_mapping(self):
        row = self.single({"type": "CHAMPION_KILL", "timestamp": 1, "killerId": 1})[0]
        self.assertEqual(row["team_id"], 200)

    def test_json_mapping_string_keys_are_supported(self):
        row = self.single(
            {"type": "CHAMPION_KILL", "timestamp": 1, "killerId": 3}, {"3": 200},
        )[0]
        self.assertEqual(row["team_id"], 200)

    def test_direct_team_priority(self):
        row = self.single({
            "type": "ELITE_MONSTER_KILL", "timestamp": 1, "killerId": 3,
            "killerTeamId": 200, "teamId": 100,
        })[0]
        self.assertEqual(row["team_id"], 200)

    def test_team_id_precedes_inferred_team(self):
        row = self.single({
            "type": "CHAMPION_KILL", "timestamp": 1, "killerId": 3, "teamId": 200,
        })[0]
        self.assertEqual(row["team_id"], 200)

    def test_zero_or_missing_killer_is_not_inferred(self):
        for killer in (0, None):
            with self.subTest(killer=killer):
                row = self.single(
                    {"type": "CHAMPION_KILL", "timestamp": 1, "killerId": killer}, {0: 100},
                )[0]
                self.assertIsNone(row["team_id"])
                self.assertIsNone(row["actor_team_id"])

    def test_source_indices_produce_unique_stable_ids(self):
        first, second = self.extract(), self.extract()
        self.assertEqual([row["event_id"] for row in first], [row["event_id"] for row in second])
        self.assertEqual(first[1]["event_id"], "KR_123:0:2")
        self.assertEqual(first[1]["event_index"], 2)
        self.assertEqual(len(first), len({row["event_id"] for row in first}))

    def test_unknown_objective_type_and_monster_are_preserved(self):
        row = next(row for row in self.extract() if row["monster_type"] == "NEW_BOSS_NAME")
        self.assertEqual(row["event_type"], "FUTURE_OBJECTIVE_CAPTURED")
        self.assertEqual(json.loads(row["raw_event"])["monsterType"], "NEW_BOSS_NAME")

    def test_objective_family_detected_without_known_event_type(self):
        row = self.single({"type": "NEW_ATAKHAN_TRANSFORM", "timestamp": 1, "teamId": 100})[0]
        self.assertEqual(row["event_type"], "NEW_ATAKHAN_TRANSFORM")

    def test_configurable_event_types(self):
        with patch.object(timeline, "TRACKED_EVENT_TYPES", {"NEW_TRACKED_TYPE"}):
            rows = self.single({"type": "NEW_TRACKED_TYPE", "timestamp": 1})
        self.assertEqual(rows[0]["event_type"], "NEW_TRACKED_TYPE")

    def test_empty_timeline_is_valid(self):
        self.assertEqual(self.extract({"info": {"frames": []}}), [])

    def test_malformed_payloads_raise_without_partial_results(self):
        invalid = [
            None, [], {}, {"info": {}}, {"info": {"frames": {}}},
            {"info": {"frames": [None]}},
            {"info": {"frames": [{"events": None}]}},
            {"info": {"frames": [{"events": [None]}]}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    timeline.extract_timeline_events("KR_123", "16.1", payload, self.teams)
        payload = copy.deepcopy(self.payload)
        payload["info"]["frames"][-1]["events"].append({"type": "CHAMPION_KILL", "timestamp": "bad"})
        with self.assertRaises(ValueError):
            self.extract(payload)

    def test_malformed_event_timestamp_or_assists(self):
        for timestamp in (None, -1, True, "100", 1.5):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    self.single({"type": "CHAMPION_KILL", "timestamp": timestamp})
        with self.assertRaises(ValueError):
            self.single({"type": "CHAMPION_KILL", "timestamp": 1, "assistingParticipantIds": "bad"})

    def test_mismatched_match_id_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["metadata"]["matchId"] = "KR_OTHER"
        with self.assertRaises(ValueError):
            self.extract(payload)

    def test_milestones_use_milliseconds_and_missing_objectives_are_null(self):
        result = timeline.extract_milestones("KR_123", "16.1", self.extract())
        self.assertEqual(set(result), set(timeline.MILESTONE_FIELDNAMES))
        self.assertEqual(result["first_blood_time"], 188000)
        self.assertEqual(result["first_blood_team"], 100)
        self.assertEqual(result["first_horde_time"], 315000)
        self.assertEqual(result["first_horde_team"], 200)
        self.assertEqual(result["first_dragon_time"], 487000)
        self.assertEqual(result["first_baron_time"], 1213000)
        self.assertIsNone(result["first_atakhan_time"])
        self.assertIsNone(result["first_herald_team"])
        self.assertIsNone(result["first_inhibitor_time"])

    def test_first_unknown_team_kill_not_replaced(self):
        events = self.single({"type": "CHAMPION_KILL", "timestamp": 0, "killerId": 99})
        events += self.single({"type": "CHAMPION_KILL", "timestamp": 10, "killerId": 3})
        result = timeline.extract_milestones("KR_123", "16.1", events)
        self.assertEqual(result["first_blood_time"], 0)
        self.assertIsNone(result["first_blood_team"])

    def test_atakhan_herald_and_inhibitor_milestones(self):
        payload = {"info": {"frames": [{"events": [
            {"type": "ELITE_MONSTER_KILL", "timestamp": 10, "killerId": 3, "monsterType": "ATAKHAN"},
            {"type": "ELITE_MONSTER_KILL", "timestamp": 20, "killerId": 3, "monsterType": "RIFTHERALD"},
            {"type": "BUILDING_KILL", "timestamp": 30, "killerId": 3, "teamId": 200, "buildingType": "INHIBITOR_BUILDING"},
        ]}]}}
        result = timeline.extract_milestones("KR_123", "16.1", self.extract(payload))
        self.assertEqual(result["first_atakhan_time"], 10)
        self.assertEqual(result["first_herald_team"], 100)
        self.assertEqual(result["first_inhibitor_team"], 100)

    def test_special_first_blood_event(self):
        rows = self.single({
            "type": "CHAMPION_SPECIAL_KILL", "timestamp": 50, "killerId": 3,
            "killType": "KILL_FIRST_BLOOD",
        })
        result = timeline.extract_milestones("KR_123", "16.1", rows)
        self.assertEqual(result["first_blood_team"], 100)
        self.assertEqual(rows[0]["kill_type"], "KILL_FIRST_BLOOD")

    def test_execution_does_not_award_first_blood(self):
        events = self.single({"type": "CHAMPION_KILL", "timestamp": 1, "killerId": 0})
        events += self.single({"type": "CHAMPION_KILL", "timestamp": 10, "killerId": 3})
        result = timeline.extract_milestones("KR_123", "16.1", events)
        self.assertEqual(result["first_blood_time"], 10)
        self.assertEqual(result["first_blood_team"], 100)

    def test_explicit_first_blood_is_authoritative(self):
        events = self.single({"type": "CHAMPION_KILL", "timestamp": 1, "killerId": 3})
        events += self.single({
            "type": "CHAMPION_SPECIAL_KILL", "timestamp": 10, "killerId": 8,
            "killType": "KILL_FIRST_BLOOD",
        })
        result = timeline.extract_milestones("KR_123", "16.1", events)
        self.assertEqual(result["first_blood_time"], 10)
        self.assertEqual(result["first_blood_team"], 200)

    def test_empty_milestones(self):
        result = timeline.extract_milestones("KR_123", "16.1", [])
        self.assertEqual(result["match_id"], "KR_123")
        self.assertTrue(all(result[key] is None for key in timeline.MILESTONE_FIELDNAMES[2:]))


if __name__ == "__main__":
    unittest.main()
