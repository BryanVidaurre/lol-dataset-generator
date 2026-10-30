"""Normalize relevant Match-V5 events; milestone times are milliseconds."""

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

TRACKED_EVENT_TYPES = {
    "CHAMPION_KILL", "ELITE_MONSTER_KILL", "BUILDING_KILL",
    "TURRET_PLATE_DESTROYED", "CHAMPION_SPECIAL_KILL", "DRAGON_SOUL_GIVEN",
    "HORDE_KILL", "VOID_GRUB_KILL", "ATAKHAN_KILL",
}
OBJECTIVE_EVENT_FAMILIES = (
    "DRAGON", "BARON", "HERALD", "HORDE", "VOID_GRUB", "ATAKHAN",
    "INHIBITOR", "TURRET", "TOWER",
)
OBJECTIVE_EVENT_FIELDS = ("monsterType", "buildingType", "towerType")

EVENT_FIELDNAMES = [
    "event_id", "match_id", "patch", "timestamp_ms", "minute",
    "frame_index", "event_index", "event_type", "team_id", "actor_team_id",
    "killer_id", "victim_id", "assisting_participant_ids",
    "monster_type", "monster_sub_type", "building_type", "tower_type",
    "lane_type", "position_x", "position_y", "kill_type", "raw_event",
]
MILESTONE_NAMES = (
    "blood", "tower", "dragon", "herald", "horde", "baron", "atakhan", "inhibitor",
)
MILESTONE_FIELDNAMES = ["match_id", "patch"] + [
    "first_{}_{}".format(name, suffix)
    for name in MILESTONE_NAMES for suffix in ("team", "time")
]


def _known_team(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if value in (100, "100"):
        return 100
    if value in (200, "200"):
        return 200
    return None


def _killer_team(
    event: Mapping[str, Any], participant_teams: Mapping[Any, Any]
) -> Optional[int]:
    direct = _known_team(event.get("killerTeamId"))
    if direct is not None:
        return direct
    killer_id = event.get("killerId")
    if killer_id in (None, 0, "0") or isinstance(killer_id, bool):
        return None
    try:
        mapped = participant_teams.get(killer_id)
        if mapped is None:
            mapped = participant_teams.get(str(killer_id))
    except TypeError:
        return None
    return _known_team(mapped)


def _is_tracked(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type", "")
    return (
        event_type in TRACKED_EVENT_TYPES
        or any(event.get(field) for field in OBJECTIVE_EVENT_FIELDS)
        or any(family in event_type.upper() for family in OBJECTIVE_EVENT_FAMILIES)
    )


def extract_timeline_events(
    match_id: str, patch: str, timeline: Dict[str, Any],
    participant_teams: Mapping[Any, Any],
) -> List[Dict[str, Any]]:
    """Return validated events with IDs using unfiltered frame/event indices.

    Explicit frames=[] is valid. Invalid frames or events raise ValueError,
    allowing callers to avoid persisting partial timelines. Source teamId can
    denote a destroyed building's owner, so actor_team_id keeps the attacker.
    """
    if not isinstance(timeline, dict):
        raise ValueError("Timeline must be a JSON object")
    metadata = timeline.get("metadata")
    if isinstance(metadata, dict):
        response_id = metadata.get("matchId")
        if response_id and response_id != match_id:
            raise ValueError("Timeline matchId does not match requested match")
    info = timeline.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("frames"), list):
        raise ValueError("Timeline info.frames must be a list")
    if not isinstance(participant_teams, Mapping):
        raise ValueError("Participant teams must be a mapping")

    rows = []
    for frame_index, frame in enumerate(info["frames"]):
        if not isinstance(frame, dict) or not isinstance(frame.get("events"), list):
            raise ValueError("Timeline frame events must be a list")
        for event_index, event in enumerate(frame["events"]):
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("Timeline event must have a string type")
            timestamp = event.get("timestamp")
            if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
                raise ValueError("Timeline event timestamp must be nonnegative milliseconds")
            if not _is_tracked(event):
                continue
            killer_team = _killer_team(event, participant_teams)
            direct_team = _known_team(event.get("teamId"))
            team_id = _known_team(event.get("killerTeamId")) or direct_team or killer_team
            is_building = (
                event["type"] in ("BUILDING_KILL", "TURRET_PLATE_DESTROYED")
                or bool(event.get("buildingType")) or bool(event.get("towerType"))
            )
            actor_team = killer_team
            if actor_team is None:
                # Environmental/minion structure kills still have an explicit
                # destroyed-building owner; its opponent gets the objective.
                if is_building and direct_team is not None:
                    actor_team = 300 - direct_team
                elif not is_building:
                    actor_team = team_id
            position = event.get("position")
            if not isinstance(position, dict):
                position = {}
            assisting = event.get("assistingParticipantIds")
            if assisting is None:
                assisting = []
            if not isinstance(assisting, list):
                raise ValueError("assistingParticipantIds must be a list")
            rows.append({
                "event_id": "{}:{}:{}".format(match_id, frame_index, event_index),
                "match_id": match_id, "patch": patch,
                "timestamp_ms": timestamp, "minute": timestamp / 60000,
                "frame_index": frame_index, "event_index": event_index,
                "event_type": event["type"], "team_id": team_id,
                "actor_team_id": actor_team, "killer_id": event.get("killerId"),
                "victim_id": event.get("victimId"),
                "assisting_participant_ids": json.dumps(assisting, separators=(",", ":")),
                "monster_type": event.get("monsterType"),
                "monster_sub_type": event.get("monsterSubType"),
                "building_type": event.get("buildingType"),
                "tower_type": event.get("towerType"), "lane_type": event.get("laneType"),
                "position_x": position.get("x"), "position_y": position.get("y"),
                "kill_type": event.get("killType"),
                "raw_event": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            })
    return rows


def _milestone_name(event: Mapping[str, Any]) -> Optional[str]:
    event_type = event.get("event_type")
    if event_type == "CHAMPION_KILL":
        # Executions (killerId 0) do not award first blood.
        killer_id = event.get("killer_id")
        return "blood" if isinstance(killer_id, int) and killer_id > 0 else None
    if event_type == "CHAMPION_SPECIAL_KILL" and event.get("kill_type") == "KILL_FIRST_BLOOD":
        return "blood"
    if event_type == "BUILDING_KILL":
        building = str(event.get("building_type") or "").upper()
        if "TOWER" in building or "TURRET" in building:
            return "tower"
        if "INHIBITOR" in building:
            return "inhibitor"
    if event_type != "ELITE_MONSTER_KILL":
        return None
    monster = str(event.get("monster_type") or "").upper()
    return {
        "DRAGON": "dragon", "BARON_NASHOR": "baron", "BARON": "baron",
        "RIFTHERALD": "herald", "RIFT_HERALD": "herald",
        "HORDE": "horde", "VOID_GRUB": "horde", "VOID_GRUBS": "horde",
        "ATAKHAN": "atakhan",
    }.get(monster)


def extract_milestones(
    match_id: str, patch: str, events: Iterable[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Return first objective times in milliseconds; absent objectives are null.

    The first uncredited kill has a null team and is never replaced by a later
    kill. Building milestones use the attacker, not the destroyed team's ID.
    """
    result = dict.fromkeys(MILESTONE_FIELDNAMES)
    result.update(match_id=match_id, patch=patch)
    ordered_events = sorted(events, key=lambda item: (
        item["timestamp_ms"], item.get("frame_index", 0), item.get("event_index", 0)
    ))
    # Riot\'s explicit first-blood event is authoritative if provided.
    explicit_blood = next((event for event in ordered_events if (
        event.get("event_type") == "CHAMPION_SPECIAL_KILL"
        and event.get("kill_type") == "KILL_FIRST_BLOOD"
    )), None)
    if explicit_blood is not None:
        result["first_blood_time"] = explicit_blood["timestamp_ms"]
        result["first_blood_team"] = explicit_blood.get(
            "actor_team_id", explicit_blood.get("team_id")
        )
    for event in ordered_events:
        name = _milestone_name(event)
        if name is None:
            continue
        time_key = "first_{}_time".format(name)
        if result[time_key] is not None:
            continue
        result[time_key] = event["timestamp_ms"]
        result["first_{}_team".format(name)] = event.get(
            "actor_team_id", event.get("team_id")
        )
    return result
