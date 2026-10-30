# League of Legends Match Dataset Generator

Collect High Elo (Master, Grandmaster and Challenger) Ranked Solo/Duo matches
through the Riot Games API. The existing player and match-ID collectors feed
two independent collectors: final match summaries and timeline events.

## Installation

Python 3.8+ and a Riot API key are required.

```bash
git clone https://github.com/mehdbenguiza/lol-dataset-generator.git
cd lol-dataset-generator
python -m venv .venv
```

Activate the environment:

- Windows PowerShell: `.venv/Scripts/Activate.ps1`
- Linux/macOS: `source .venv/bin/activate`

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` in the repository root, then set:

```env
RIOT_API_KEY=RGAPI-your-key
```

Get the key from the [Riot Developer Portal](https://developer.riotgames.com/).
Keep it out of Git and logs.

## Collection phases

Run in this order. The forward-slash commands work on Windows and Linux.

```bash
# Phase 1: existing High Elo player collection
python collector/1_collect_players.py

# Phase 2: existing unique Ranked Solo/Duo match-ID collection
python collector/2_collect_matches.py

# Phase 3: draft, final objectives and reusable timeline context
python collector/3_collect_details.py

# Phase 4: timelines for the match IDs already stored in SQLite
python collector/4_collect_timelines.py
```

Phase 2 retains the existing start date of January 1, 2026. Platforms and
Match-V5 regional routing are configured in `config/settings.py`.
Phase 4 never searches for players, histories or additional match IDs. Timeline
requests use the same `make_request`, API key, retries and rate limiter as the
other collectors. It does not add any timeline request to Phase 3.

Default data paths are relative to the repository, regardless of your current
working directory. Optional `RIOT_DATA_DIR` and `RIOT_LOGS_DIR` environment
variables can isolate runs or tests.

## Output files

| File | Granularity | Contents |
| --- | --- | --- |
| `data/dataset.csv` | One row per valid match | Existing draft columns plus final objectives for each team |
| `data/events.csv` | One row per relevant timeline event | Exact timestamps, source fields, attribution and original JSON |
| `data/milestones.csv` | One row per successfully collected timeline | First objective teams and timestamps |

SQLite is the durable source of these files. CSV snapshots are streamed using
bounded memory and replaced atomically at completion or a clean Ctrl+C.
While a long collection is running, newly committed rows are already in SQLite.
Rebuild snapshots without making API calls:

```bash
python collector/3_collect_details.py --export-only
python collector/4_collect_timelines.py --export-only
```

Each CSV is replaced atomically on its own. If interruption happens between the
two timeline exports, run `--export-only` again to refresh both snapshots.
Keep one collector per phase running against a given data directory.

### dataset.csv: draft and final objectives

All original columns and their order are preserved:

```text
match_id, patch, game_duration, game_creation,
t1_top, t1_jungle, t1_middle, t1_bottom, t1_support, t1_avg_tier,
t2_top, t2_jungle, t2_middle, t2_bottom, t2_support, t2_avg_tier,
win_team1
```

The existing names are `middle` and `bottom`, not `mid` and `bot`.
`patch` preserves Riot's full `gameVersion` string. `game_creation` preserves
the original Riot timestamp (milliseconds); `game_duration` is in seconds.
`t1` is team 100 and `t2` is team 200. Average tiers retain the existing scale:
Master=1, Grandmaster=2, Challenger=3, using only locally known players.

The following **32 columns** are appended, with each pair present for both
`t1_` and `t2_`:

| First objective suffix | Final count suffix | Riot objective key |
| --- | --- | --- |
| `first_blood` | `champion_kills` | `champion` |
| `first_tower` | `tower_kills` | `tower` |
| `first_dragon` | `dragon_kills` | `dragon` |
| `first_herald` | `herald_kills` | `riftHerald` |
| `first_horde` | `horde_kills` | `horde` |
| `first_baron` | `baron_kills` | `baron` |
| `first_atakhan` | `atakhan_kills` | `atakhan` |
| `first_inhibitor` | `inhibitor_kills` | `inhibitor` |

Extraction uses `info.teams[].objectives` through the reusable
`extract_team_objectives` helper. Missing objectives normalize to `False` and
`0`, including patches without Horde or Atakhan. These defaults do not
distinguish an unavailable objective from an objective that was not taken.
Drafts without ten participants or one champion per role are excluded, as before.

For prediction at champion selection, do not use the final objective totals,
timeline events or milestones as pregame features: they describe the outcome
of gameplay.

### events.csv: timeline events

Columns:

```text
event_id, match_id, patch, timestamp_ms, minute,
frame_index, event_index, event_type, team_id, actor_team_id,
killer_id, victim_id, assisting_participant_ids,
monster_type, monster_sub_type, building_type, tower_type, lane_type,
position_x, position_y, kill_type, raw_event
```

- `timestamp_ms` is elapsed match time in milliseconds.
- `minute = timestamp_ms / 60000`, without rounding away precision.
- `event_id = match_id:frame_index:event_index`; both indices are zero-based
  indices in the original unfiltered timeline.
- `assisting_participant_ids` and `raw_event` are JSON strings, correctly
  quoted by the CSV writer.
- Missing fields become empty CSV cells. Unknown monster names and event types
  are preserved as Riot sent them, never rewritten to `OTHER`.
- `team_id` prefers `killerTeamId`, then `teamId`, then the killer's team
  from the match's participant map. Killer 0 or an unknown killer does not
  produce a participant-based team inference.
- For building/plate destruction, source `teamId` can identify the **owner of
  the destroyed structure**. `actor_team_id` identifies the attacker using the
  killer's team, or the opponent of the structure's owner when necessary.
  `raw_event` always preserves the original team fields. Milestones use the
  attacker rather than the destroyed structure's owner.

`TRACKED_EVENT_TYPES` in `utils/timeline.py` includes champion kills, elite
monster kills, building kills, turret plates, special kills and dragon souls.
Add types there to expand collection. Events with objective fields or known
objective-family names are also captured even if their event type is new.
Frames, inventories and player position snapshots are not stored. Entirely new
objective events with neither recognizable fields nor names must be added to
the configuration.

### milestones.csv: first objectives

`match_id`, `patch`, and pairs
`first_<objective>_team` / `first_<objective>_time` for:

`blood`, `tower`, `dragon`, `herald`, `horde`, `baron`, `atakhan`,
`inhibitor`.

All milestone times are **milliseconds since match start**, not minutes or
wall-clock timestamps. Missing objectives have empty cells. First blood prefers
Riot's explicit `KILL_FIRST_BLOOD` event, with the first champion kill by a
player as fallback; executions do not award first blood. An unattributed first
objective keeps its timestamp and an empty team rather than using a later kill.
Unknown objective types remain available in `events.csv` even when they have
no milestone column.

## Existing databases and CSV files

The database is initialized and migrated automatically. Existing `players`,
`matches` and `checkpoints` are preserved; nothing is dropped or reset.

Added to `matches`:

- `patch`, `participant_teams`: minimal cached game version and
  participantId-to-teamId map, without additional player identifiers.
- `timeline_processed` (default 0), `timeline_last_error`,
  `timeline_updated_at` (Unix seconds).

Added tables:

- `match_summaries`: one normalized summary per match.
- `events`: stable event primary key and unique
  `(match_id, frame_index, event_index)`.
- `milestones`: one row per successfully collected timeline.
- `dataset_imports`: records completed legacy CSV imports.

Existing `dataset.csv` rows are imported once, transactionally, before Phase 3
exports. Existing values are retained and duplicate match IDs are collapsed.
Objective cells stay blank for legacy CSV rows whose objective data was never
downloaded. To enrich those rows explicitly:

```bash
python collector/3_collect_details.py --backfill-objectives
```

Backfill fetches only rows missing the new objectives and also caches their
timeline context. It does not redownload enriched summaries.

Phase 3 saves patch and the participant-team map alongside each collected
match. Phase 4 reuses that context. For an old database that lacks it, Phase 4
must request match details once, because the old CSV has no participant IDs.
Only the minimal context is cached; later retries do not request those details
again. If you still need final objectives for legacy rows, run Phase 3 backfill
before Phase 4.

## Integrity, retries and interruption

Timeline event insertion, milestone insertion and the completion flag commit
in one SQLite transaction. An interruption before commit rolls everything back;
after commit the match is skipped on restart, even if CSV export had not yet
happened. Regenerating CSV from unique stored rows prevents duplicate events.

Transient failures retain `timeline_processed = 0`, store a safe error and
timestamp, and can be retried by rerunning Phase 4. Each pending match is
attempted once per invocation, with HTTP-level retries handled centrally.
HTTP 429 respects `Retry-After`; 5xx, timeouts and connection failures use the
existing shared retry policy. HTTP 404 remains pending for a later run.
HTTP 401/403 stops the collector so the API key can be fixed without marking
the remaining matches complete. Invalid responses remain pending. An explicit
valid `info.frames = []` completes successfully with no events and empty
milestones.

Ctrl+C preserves committed data and exports CSV snapshots. An abrupt process
kill can leave an older snapshot or an unused temporary file, but SQLite remains
authoritative and the next export recovers the output. Phase 3 now also commits
its summary with its completion flag and leaves failed downloads pending.

## Offline tests

```bash
python -B -m unittest discover -s tests -t . -v
```

Tests use minimal JSON fixtures, mocked Riot responses and disposable SQLite
databases. No API key or real Riot calls are needed. Coverage includes absent
objectives, event extraction and attribution, different patches, duplicate
prevention, migration, rollback, retries, clean interruption and CSV recovery.

The older `collector/test_phase2.py` and `collector/test_phase3.py` are manual
live collection utilities; they are not part of the offline unit test suite.

Optional: install `pandas` to use the pre-existing
`utils/verify_dataset.py` inspection utility.

## API references

- [Riot Match-V5 reference](https://developer.riotgames.com/apis#match-v5)
- [Timeline changes and event examples from Riot DevRel](https://gist.github.com/RiotTuxedo/758ee4d88693b768a880ece93cd78663)
- [Riot DevRel building-kill examples](https://gist.github.com/RiotTuxedo/ab04ac027546f711ce820faed394295b)
