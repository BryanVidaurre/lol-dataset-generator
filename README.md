# League of Legends Match Dataset Generator

Collect High Elo (Master, Grandmaster and Challenger) Ranked Solo/Duo matches
through the Riot Games API. The existing player and match-ID collectors feed
two independent collectors: final match summaries and timeline events.

## Weekend collection: a sample sized for roughly a weekend

The **40-hour figure is a planning reference, not a time limit**. Collection and
CSV export continue beyond that reference until the selected sample has been
attempted. There is no clock-based cancellation or supervisor terminating the
process. Actual duration depends on latency, rate limits, retries and API-key
availability.

**Stop the previous collectors first**, then run this command for the sample
(it replaces the manual Phase 2/3/4 sequence):

```powershell
# Required once when starting with an empty database:
.\.venv\Scripts\python.exe collector/1_collect_players.py

# Reuse those players to build the sample:
.\.venv\Scripts\python.exe collector/collect_weekend.py
```

If both players and match IDs are absent, the sample collector exits with an
explicit Phase 1 instruction instead of reporting a successful empty export.
After Phase 1, rerun the same sample command. Empty selections from earlier
versions are reopened automatically; existing nonempty samples are preserved.

The default target remains **up to 30,000 complete matches**, retaining all
existing columns, relevant events and milestones. With the configured limiter,
60,000 requests have an ideal lower bound of 20 hours; this is not an ETA.
The final count can be smaller if drafts are excluded, IDs are unavailable, or
requests fail. Failed requests remain pending for retry by rerunning the command.

- Existing match IDs and downloaded details/timelines are reused. For configured
  regions without enough IDs, discovery requests only **one recent history page
  of up to 100 matches per player**, for at most 100 players per region by default.
  Players are sampled across Master, Grandmaster and Challenger. Partial history
  requests do not mark full histories as collected.
- Match selection uses a fixed seed and equal quotas across configured platforms,
  redistributing shortages to available regions. Selection freezes after bounded
  discovery, and reruns process the same selected matches. This does not guarantee
  population representativeness or equal final counts after API failures.
- Details and timeline are collected together per match. **Only matches with both
  a valid summary and a successfully processed timeline are exported**. Failed
  timelines retain their downloaded details for a later retry.
- Ctrl+C preserves the selection and committed results, then exports CSV. An API
  authentication error stops with a message to update `.env`; rerun to continue.
  Runs created by the older timed version resume normally: their stored deadline
  fields are ignored. No migration or reset of collected data is required.
- The existing database, CSV files and standalone collectors are retained.
  Sampling tables `weekend_runs`, `weekend_matches` and `weekend_sources` are
  initialized through `database/weekend_store.py` using the existing connection.
- Output files are **`data/weekend/dataset.csv`**, **`data/weekend/events.csv`**
  and **`data/weekend/milestones.csv`**. Snapshots are exported periodically,
  at completion and on a clean interruption, using atomic file replacement.
- An OS file lock prevents two sample collectors from running simultaneously.
  Other standalone collectors do not share this lock or the process-local rate
  limiter: **do not run them concurrently with this mode**.

Inspect the plan without API calls or starting collection:

```powershell
.\.venv\Scripts\python.exe collector/collect_weekend.py --plan
```

Choose a different sample size on the first launch, for example:

```powershell
.\.venv\Scripts\python.exe collector/collect_weekend.py --max-matches 15000
```

`--hours` changes only the time reference displayed by `--plan`; it does not
change the sample size or impose a deadline. `--max-matches` sets the sample
size. Persisted sample settings take precedence when resuming the same run.
To deliberately create a new selection, use a different `--run-name`; its files
are written to `data/weekend/<run-name>/`.

Rebuild CSV snapshots without any downloads:

```powershell
.\.venv\Scripts\python.exe collector/collect_weekend.py --export-only
```

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

## Start mining the collected data

The optional mining experiment uses `dataset.csv` and `milestones.csv`; it does
not load the multi-gigabyte events CSV or call Riot. Use Python 3.11+ for the
optional analysis environment.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mining.txt
.\.venv\Scripts\python.exe analysis/mine_dataset.py
```

Results are written to **`reports/mining/mining_report.md`**. The experiment:

- Excludes matches shorter than five minutes from this analysis.
- Uses one row per match and chronological training/validation/test blocks
  (approximately 70/15/15). Boundary matches whose outcomes were not yet
  available when the next block started are purged.
- Trains regularized logistic regression on champions by role and patch. Its
  strength is selected on validation, then the final model is compared with a
  Dummy classifier on the latest held-out test block. A
  [scikit-learn pipeline](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage)
  fits categorical preprocessing only on the corresponding training data.
- Excludes final objectives, duration, match identifiers and current team tiers
  from predictive inputs. Current rank snapshots are not historical ranks.
- Calculates descriptive association rules for the first objectives captured
  by minute 15 and victory, reporting cases, support, confidence and lift. It
  excludes unattributed objectives, first blood and rules with fewer than 30 cases.
  These are pre-specified single-condition rules, not an exhaustive Apriori search.
- Produces ROC/confusion/feature-weight plots, an exploratory learning curve,
  test predictions, split memberships, JSON metrics and a saved model.

Only the champion draft and patch are used for the pregame prediction. Objective
rules are a separate observational analysis; they do not establish causality.
A weak result is still informative: compare with the reference before deciding
whether to collect more data or build a more complex model. A later experiment
using the first 10-15 minutes must exclude every event after its prediction time.

To use a different sample or output location:

```powershell
.\.venv\Scripts\python.exe analysis/mine_dataset.py --dataset data/weekend/dataset.csv --milestones data/weekend/milestones.csv --output reports/mining
```

Mining tests are optional and skip automatically if the extra dependencies are
not installed:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_mining -v
```

## API references

- [Riot Match-V5 reference](https://developer.riotgames.com/apis#match-v5)
- [Timeline changes and event examples from Riot DevRel](https://gist.github.com/RiotTuxedo/758ee4d88693b768a880ece93cd78663)
- [Riot DevRel building-kill examples](https://gist.github.com/RiotTuxedo/ab04ac027546f711ce820faed394295b)
