# NeoRiskDrift

Daily snapshots of NASA/JPL asteroid impact-risk data, tracking how risk assessments
evolve as new observations arrive.

## Why

When a new near-Earth object (NEO) is discovered, its orbit is uncertain, and so is its
chance of hitting Earth. As telescopes gather more observations, JPL refits the orbit and
the impact odds rise, fall, or drop to zero. JPL publishes only the *current* assessment,
so the story of how each estimate changed is lost.

NeoRiskDrift captures that story. It pulls JPL's risk feeds every day and keeps every
version, building a history that can answer questions like:

- How quickly do early impact-risk estimates usually collapse?
- Which early signals predict that an object will be ruled out?
- How often do risk estimates rise before they fall?

## Data sources

All from JPL's free [SSD/CNEOS API](https://ssd-api.jpl.nasa.gov/), no key needed:

| Feed | What it is |
|---|---|
| [Sentry](https://cneos.jpl.nasa.gov/sentry/) summary | Objects with a nonzero chance of hitting Earth in roughly the next century |
| Sentry removed | Objects later ruled out, with removal date (history back to 2002) |
| [Scout](https://cneos.jpl.nasa.gov/scout/) summary | Newly detected, unconfirmed objects |

## How it works

```
JPL API ──► snapshot.py ──► runs            (raw JSON, exactly as received)
                        └─► sentry_snapshot  (parsed, typed rows per run)
                            scout_snapshot
                            sentry_removed   (new removals only)
                              │
            detect_changes.py ◄┘  (called at the end of every snapshot.py run)
                        └─► sentry_events    (what changed since the previous run)
                            scout_events
                            detected_runs    (which runs have been compared)
```

- **Two storage layers.** Raw responses are kept alongside parsed rows. Past API
  responses can never be re-downloaded, so if parsing logic changes, history can be
  re-parsed from the raw layer instead of lost.
- **Atomic writes.** Each feed's raw and parsed data are saved in a single transaction,
  so a failed run never leaves partial data.
- **Fault isolation.** One failing feed doesn't stop the others; the script exits
  nonzero so the scheduler records the failure. Change detection runs only after every
  feed has been saved, so a problem there is logged as `FAILED` but can never lose a
  snapshot.
- **Change data capture.** Each Sentry and Scout run is compared with the previous run
  of the same source (not "yesterday": manual runs can add several a day). Every run is
  compared exactly once, so re-running is always safe. A run that stored fewer rows than
  JPL reported (an empty or cut-off response) is skipped with a warning instead of being
  read as thousands of objects disappearing. Events are derived data and can be
  recomputed at any time with `detect_changes.py --rebuild`.
- **Calendar effects are kept apart from new data.** JPL recomputes Palermo ratings
  against the current date even when nothing new was observed, so they creep upward as
  impact dates approach. A change that touches only the Palermo values is logged as
  `rescored`, so `updated` always means JPL actually refit the orbit.

## Setup

Requires Python 3.9+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python snapshot.py
```

This creates `neo_risk.db`, prints how many rows each feed stored, then prints a
one-line summary of what changed per source (e.g. `run 31 vs run 28: 1 added,
3 updated, 17 rescored, 3 dropped`). The first run with change detection on an
existing database also compares all of its earlier runs.

Options:

```
python snapshot.py --db other.db       # use a different database file
python snapshot.py --log snapshot.log  # also append output to a log file
python snapshot.py --from-dir DIR      # load saved JSON files instead of calling JPL
```

Change detection can also be run on its own:

```
python detect_changes.py --db other.db   # compare any runs not compared yet
python detect_changes.py --rebuild       # recompute every event (back up the database first)
```

Tests use saved responses and a throwaway database, never the real one:

```
python -m unittest discover tests -v
```

## Run it daily

**Windows:** in Task Scheduler, use Create Task with a Daily trigger and this action:

- Program/script: full path to `.venv\Scripts\pythonw.exe` (the `w` version runs with no window)
- Add arguments: `snapshot.py --log snapshot.log`
- Start in: full path to this folder

On the Settings tab, check "Run task as soon as possible after a scheduled start is missed."
For a manual run with visible output, use `.venv\Scripts\python.exe snapshot.py` instead.

**macOS / Linux** (`crontab -e`), runs at 6:17 AM:

```
17 6 * * * cd /path/to/neo-risk-drift && .venv/bin/python snapshot.py --log snapshot.log
```

## Database tables

| Table | Contents |
|---|---|
| `runs` | One row per feed per pull: source, timestamp, API version, record count, raw JSON |
| `sentry_snapshot` | Impact probability, Palermo and Torino ratings, size, velocity per object per run |
| `scout_snapshot` | Observation count, arc length, NEO/PHA likelihood scores per object per run |
| `sentry_removed` | Every removal seen, with the run where it was first observed |
| `sentry_events` | What changed for each Sentry object since the previous run: `added`, `updated` (one row per changed field, with old and new value), `rescored` (only the Palermo values moved), `dropped`, `reappeared` (with field rows against how the object looked when last seen), `renamed` (the object was numbered, so its designation changed) |
| `scout_events` | The same for Scout objects, comparing only rating, NEO/PHA scores, MOID and H (observation counts change on every re-observation and stay in `scout_snapshot`) |
| `detected_runs` | Each Sentry/Scout run already compared, the run it was compared with, its status (`baseline`, `compared`, or `skipped` when rows were missing) and how many events it produced (makes re-runs safe) |

## Roadmap

- [x] Daily snapshots with raw and parsed storage
- [ ] Change tracking: detect what moved between runs
- [ ] Backfill from the removed-objects history
- [ ] Analysis: how risk estimates evolve
- [ ] Prediction: estimate whether an object will be removed from the risk list

## Data credit

Data courtesy of NASA/JPL's Center for Near-Earth Object Studies (CNEOS).
This project is independent and not affiliated with NASA or JPL.
