"""Normalize final objectives and the minimal context needed by timelines."""
from typing import Any, Dict, Tuple

OBJECTIVE_TYPES = (
    ("champion", "blood", "champion"),
    ("tower", "tower", "tower"),
    ("dragon", "dragon", "dragon"),
    ("riftHerald", "herald", "herald"),
    ("horde", "horde", "horde"),
    ("baron", "baron", "baron"),
    ("atakhan", "atakhan", "atakhan"),
    ("inhibitor", "inhibitor", "inhibitor"),
)
OBJECTIVE_FIELDNAMES = [
    f"t{team}_{column}"
    for team in (1, 2)
    for _, first_name, kills_name in OBJECTIVE_TYPES
    for column in (f"first_{first_name}", f"{kills_name}_kills")
]


def extract_team_objectives(team_data: Dict[str, Any]) -> Dict[str, Any]:
    """Missing objectives in older patches normalize to False / 0."""
    objectives = team_data.get("objectives") if isinstance(team_data, dict) else {}
    objectives = objectives if isinstance(objectives, dict) else {}
    result = {}
    for riot_name, first_name, kills_name in OBJECTIVE_TYPES:
        objective = objectives.get(riot_name) or {}
        objective = objective if isinstance(objective, dict) else {}
        result[f"first_{first_name}"] = bool(objective.get("first", False))
        result[f"{kills_name}_kills"] = objective.get("kills") or 0
    return result


def extract_match_context(details: Dict[str, Any]) -> Tuple[str, Dict[int, int]]:
    """Keep participant IDs and teams only; never cache player identifiers."""
    if not isinstance(details, dict) or not isinstance(details.get("info"), dict):
        raise ValueError("Invalid match details: missing info")
    info = details["info"]
    patch = info.get("gameVersion")
    participants = info.get("participants")
    if not isinstance(patch, str) or not patch:
        raise ValueError("Invalid match details: missing gameVersion")
    if not isinstance(participants, list) or not participants:
        raise ValueError("Invalid match details: missing participants")
    teams = {}
    for participant in participants:
        if not isinstance(participant, dict):
            raise ValueError("Invalid match participant")
        participant_id = participant.get("participantId")
        team_id = participant.get("teamId")
        if not isinstance(participant_id, int) or participant_id <= 0 or team_id not in (100, 200):
            raise ValueError("Invalid participant ID or team")
        if participant_id in teams:
            raise ValueError("Duplicate participant ID")
        teams[participant_id] = team_id
    return patch, teams
