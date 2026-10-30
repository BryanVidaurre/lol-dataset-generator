import copy
import importlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from utils.objectives import (
    OBJECTIVE_FIELDNAMES, extract_match_context, extract_team_objectives,
)

collect_details = importlib.import_module("collector.3_collect_details")

ORIGINAL_COLUMNS = [
    "match_id", "patch", "game_duration", "game_creation",
    "t1_top", "t1_jungle", "t1_middle", "t1_bottom", "t1_support", "t1_avg_tier",
    "t2_top", "t2_jungle", "t2_middle", "t2_bottom", "t2_support", "t2_avg_tier",
    "win_team1",
]
OBJECTIVE_PAIRS = [
    ("blood", "champion"), ("tower", "tower"), ("dragon", "dragon"),
    ("herald", "herald"), ("horde", "horde"), ("baron", "baron"),
    ("atakhan", "atakhan"), ("inhibitor", "inhibitor"),
]
NEW_COLUMNS = [
    "t{}_{}".format(team, column)
    for team in (1, 2) for first, kills in OBJECTIVE_PAIRS
    for column in ("first_" + first, kills + "_kills")
]


class ObjectiveExtractionTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / "fixtures" / "match.json"
        self.match = json.loads(path.read_text(encoding="utf-8"))
        self.team = next(team for team in self.match["info"]["teams"] if team["teamId"] == 100)

    def test_normal_objectives(self):
        team = copy.deepcopy(self.team)
        team["objectives"]["horde"] = {"first": True, "kills": 6}
        team["objectives"]["atakhan"] = {"first": True, "kills": 1}
        row = extract_team_objectives(team)
        self.assertEqual(len(row), 16)
        expected = {
            "blood": ("champion", 20), "tower": ("tower", 7),
            "dragon": ("dragon", 3), "herald": ("herald", 1),
            "horde": ("horde", 6), "baron": ("baron", 1),
            "atakhan": ("atakhan", 1), "inhibitor": ("inhibitor", 3),
        }
        for first_name, (kills_name, kills) in expected.items():
            self.assertIs(row["first_" + first_name], True)
            self.assertEqual(row[kills_name + "_kills"], kills)

    def test_missing_objective(self):
        del self.team["objectives"]["dragon"]
        row = extract_team_objectives(self.team)
        self.assertIs(row["first_dragon"], False)
        self.assertEqual(row["dragon_kills"], 0)
        self.assertEqual(row["tower_kills"], 7)

    def test_missing_atakhan(self):
        self.assertNotIn("atakhan", self.team["objectives"])
        row = extract_team_objectives(self.team)
        self.assertIs(row["first_atakhan"], False)
        self.assertEqual(row["atakhan_kills"], 0)

    def test_missing_horde(self):
        self.assertNotIn("horde", self.team["objectives"])
        row = extract_team_objectives(self.team)
        self.assertIs(row["first_horde"], False)
        self.assertEqual(row["horde_kills"], 0)

    def test_null_or_missing_objectives(self):
        for team in ({}, {"objectives": None}, {"objectives": []}, None):
            with self.subTest(team=team):
                row = extract_team_objectives(team)
                self.assertEqual(len(row), 16)
                self.assertTrue(all(
                    value is False if name.startswith("first_") else value == 0
                    for name, value in row.items()
                ))

    def test_null_individual_objective_and_values(self):
        for objective in (None, [], {"first": None, "kills": None}):
            with self.subTest(objective=objective):
                row = extract_team_objectives({"objectives": {"atakhan": objective}})
                self.assertIs(row["first_atakhan"], False)
                self.assertEqual(row["atakhan_kills"], 0)

    def test_unknown_objective_does_not_break_known_columns(self):
        self.team["objectives"]["futureBoss"] = {"first": True, "kills": 99}
        row = extract_team_objectives(self.team)
        self.assertEqual(row["dragon_kills"], 3)
        self.assertEqual(len(row), 16)


class MatchContextTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / "fixtures" / "match.json"
        self.match = json.loads(path.read_text(encoding="utf-8"))

    def test_context_uses_real_participant_ids_without_player_identifiers(self):
        self.match["info"]["participants"][0]["participantId"] = 41
        game_patch, mapping = extract_match_context(self.match)
        self.assertEqual(game_patch, "14.2.123")
        self.assertEqual(mapping[41], 100)
        self.assertEqual(mapping[6], 200)
        self.assertNotIn(1, mapping)
        serialized = json.dumps((game_patch, mapping))
        self.assertNotIn("puuid", serialized)
        self.assertNotIn("test-puuid", serialized)
        self.assertNotIn("Aster", serialized)
        self.assertEqual(len(mapping), 10)

    def test_invalid_responses(self):
        payloads = [
            None, [], {}, {"info": None}, {"info": {}},
            {"info": {"gameVersion": "14.2", "participants": []}},
            {"info": {"gameVersion": "", "participants": [{"participantId": 1, "teamId": 100}]}},
            {"info": {"gameVersion": "14.2", "participants": [None]}},
            {"info": {"gameVersion": "14.2", "participants": [{"teamId": 100}]}},
            {"info": {"gameVersion": "14.2", "participants": [{"participantId": 0, "teamId": 100}]}},
            {"info": {"gameVersion": "14.2", "participants": [{"participantId": 1, "teamId": 300}]}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                extract_match_context(payload)

    def test_duplicate_participant_ids_are_invalid(self):
        self.match["info"]["participants"][1]["participantId"] = 1
        with self.assertRaises(ValueError):
            extract_match_context(self.match)


class MatchDatasetTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / "fixtures" / "match.json"
        self.match = json.loads(path.read_text(encoding="utf-8"))
        self.player_tiers = {
            "test-puuid-1": "CHALLENGER",
            "test-puuid-2": "MASTER",
            "test-puuid-6": "GRANDMASTER",
        }
        self.tier_lookup = patch.object(
            collect_details.db_manager, "get_player_tiers", return_value=self.player_tiers,
        )
        self.tier_lookup.start()
        self.addCleanup(self.tier_lookup.stop)

    def test_preserves_all_original_columns_and_values(self):
        row = collect_details.extract_match_data("KR_123", self.match)
        self.assertEqual(collect_details.FIELDNAMES[:len(ORIGINAL_COLUMNS)], ORIGINAL_COLUMNS)
        self.assertTrue(set(ORIGINAL_COLUMNS).issubset(row))
        self.assertEqual(row["match_id"], "KR_123")
        self.assertEqual(row["patch"], "14.2.123")
        self.assertEqual(row["game_duration"], 1800)
        self.assertEqual(row["game_creation"], 1706000000000)
        self.assertEqual(row["t1_top"], "Aster")
        self.assertEqual(row["t1_jungle"], "Bramble")
        self.assertEqual(row["t1_middle"], "Cinder")
        self.assertEqual(row["t1_bottom"], "Dune")
        self.assertEqual(row["t1_support"], "Ember")
        self.assertEqual(row["t2_top"], "Frost")
        self.assertEqual(row["t2_middle"], "Harbor")
        self.assertEqual(row["t2_bottom"], "Iris")
        self.assertEqual(row["t2_support"], "Juniper")
        self.assertEqual(row["t1_avg_tier"], 2)
        self.assertEqual(row["t2_avg_tier"], 2)
        self.assertIs(row["win_team1"], True)
        self.assertNotIn("t1_mid", row)
        self.assertNotIn("t1_bot", row)

    def test_adds_exactly_32_new_objective_columns(self):
        row = collect_details.extract_match_data("KR_123", self.match)
        self.assertEqual(len(NEW_COLUMNS), 32)
        self.assertEqual(OBJECTIVE_FIELDNAMES, NEW_COLUMNS)
        self.assertEqual(set(row), set(ORIGINAL_COLUMNS + NEW_COLUMNS))
        self.assertEqual(len(collect_details.FIELDNAMES), 49)
        self.assertEqual(len(collect_details.FIELDNAMES), len(set(collect_details.FIELDNAMES)))

    def test_teams_are_mapped_by_id_even_with_reversed_source_order(self):
        self.assertEqual(self.match["info"]["teams"][0]["teamId"], 200)
        row = collect_details.extract_match_data("KR_123", self.match)
        self.assertIs(row["t1_first_blood"], True)
        self.assertIs(row["t2_first_blood"], False)
        self.assertEqual(row["t1_champion_kills"], 20)
        self.assertEqual(row["t2_champion_kills"], 10)
        self.assertEqual(row["t1_tower_kills"], 7)
        self.assertEqual(row["t2_tower_kills"], 2)
        self.assertEqual(row["t1_dragon_kills"], 3)
        self.assertEqual(row["t2_dragon_kills"], 1)

    def test_old_patch_without_atakhan_or_horde_is_safe(self):
        row = collect_details.extract_match_data("KR_123", self.match)
        for prefix in ("t1", "t2"):
            self.assertIs(row[prefix + "_first_atakhan"], False)
            self.assertIs(row[prefix + "_first_horde"], False)
            self.assertEqual(row[prefix + "_atakhan_kills"], 0)
            self.assertEqual(row[prefix + "_horde_kills"], 0)

    def test_missing_one_team_objectives_does_not_change_other_team(self):
        self.match["info"]["teams"][0]["objectives"] = None
        row = collect_details.extract_match_data("KR_123", self.match)
        self.assertEqual(row["t1_dragon_kills"], 3)
        self.assertEqual(row["t2_dragon_kills"], 0)
        self.assertIs(row["t2_first_dragon"], False)

    def test_role_fallback_to_individual_position(self):
        player = self.match["info"]["participants"][2]
        player["teamPosition"] = ""
        row = collect_details.extract_match_data("KR_123", self.match)
        self.assertEqual(row["t1_middle"], "Cinder")

    def test_invalid_match_payloads_return_none(self):
        payloads = [None, [], {}, {"info": None}, {"info": {}},
                    {"info": {"participants": [None] * 10}}]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(collect_details.extract_match_data("KR_123", payload))

    def test_incomplete_team_or_roles_are_rejected(self):
        missing_player = copy.deepcopy(self.match)
        missing_player["info"]["participants"].pop()
        duplicate_role = copy.deepcopy(self.match)
        duplicate_role["info"]["participants"][0]["teamPosition"] = "JUNGLE"
        invalid_team = copy.deepcopy(self.match)
        invalid_team["info"]["participants"][0]["teamId"] = 300
        missing_champion = copy.deepcopy(self.match)
        missing_champion["info"]["participants"][0]["championName"] = None
        for payload in (missing_player, duplicate_role, invalid_team, missing_champion):
            with self.subTest(payload=payload):
                self.assertIsNone(collect_details.extract_match_data("KR_123", payload))


if __name__ == "__main__":
    unittest.main()
