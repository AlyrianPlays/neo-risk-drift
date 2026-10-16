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

Views (derived on the fly, no stored data):

| View | Contents |
|---|---|
| `sentry_drop_check` | Every Sentry `dropped` event with the removal that explains it (`removed_at` between the last run that had the object and the first that didn't). A NULL `removed_at` means the object vanished without JPL listing a removal. |
| `sentry_removed_status` | Every removal with `back_in_sentry_run`: the first later run in which the object was in Sentry again. JPL sometimes takes a removal back (2026 TB8 was removed on 2026-10-13 and listed again the next day), so the removed list isn't strictly cumulative upstream. Rows are never deleted; this view says which removals still stand. |
| `sentry_removal_outcomes` | The removed list (back to 2002) as outcome labels for modeling: each removal with the discovery half-month decoded from its provisional designation (`2026 TB8`: T = Oct 1-15), `days_to_removal` (an upper bound, counted from the start of that half-month), `before_sentry` for objects found before Sentry began in 2002, `still_removed`, and `reprocessed_2021` (see Data notes). Across 3,548 removals since 2002, the median is about 19 days from discovery and 69% happen within 30 days. |

## Data notes

Things the first days of snapshots showed, and how the pipeline handles them:

- **Churn is small for Sentry, large for Scout.** Under 1% of Sentry objects change per
  update, and some days JPL publishes no change at all. Scout turns over 10-20% of its
  list per day, and 16-44% of the objects that stay change something.
- **Most Sentry "changes" are calendar effects.** Of the 20 Sentry objects that changed on
  2026-10-16, 17 changed only their Palermo values, all upward by 0.01-0.06, with impact
  probability and observations untouched. JPL's object details show their orbit
  computation is years old. Palermo depends on time to impact, and JPL appears to count
  it in whole years rounded up, so a value steps up when an impact date passes its
  anniversary. These are logged as `rescored`, not `updated`.
- **The removed list isn't strictly cumulative.** JPL can take a removal back: 2026 TB8
  was removed, listed again a day later, then removed again. `sentry_removed` keeps
  every removal ever seen, and `sentry_removed_status` says which ones still stand.
- **Timestamps differ by field.** `fetched_at` is ISO 8601 UTC (`2026-10-16T14:44:55+00:00`),
  JPL's `removed_at` is UTC as `2026-10-16 14:14:42`, and `last_obs` can carry a fractional
  day (`2020-10-3.80160`). Compare times through SQLite's `datetime()`, not as strings.
- **2021 is not a normal year for removals.** JPL removed 453 objects in 2021, against
  100-190 in other years, because it recomputed its whole catalog twice: for the DE441
  planetary ephemeris in April 2021, and for the new Sentry-II impact-monitoring
  algorithm in July-August 2021 ("all objects in the NEA catalog have been reprocessed
  ... The objects for which the new algorithm cannot find virtual impactors have been
  moved to the Removed Objects table", [Sentry operational notes](https://cneos.jpl.nasa.gov/sentry/notes.html),
  2021-Apr-13 and 2021-Aug-23). In a sample of 40 removals from the busiest days, 37
  had no observations for over a year before removal, so their removal came from
  the new model, not new data. JPL also rewrote removal dates for about 2020-10-15 to
  2021-08-31 (Apophis now shows 2021-02-21; JPL announced its removal on 2021-03-26), so
  those dates aren't when objects left the public list. `sentry_removal_outcomes.reprocessed_2021`
  flags that window: leave it out of removal-rate and timing analyses, or model it
  separately.
- **Scout raw records always differ between pulls**, because sky position and
  ephemeris time are computed at fetch time. Changes are detected on parsed fields only.

## Roadmap

- [x] Daily snapshots with raw and parsed storage
- [x] Change tracking: per-object events between consecutive runs, idempotent re-runs,
      drops cross-checked against the removed list
- [x] Removed-objects history (back to 2002) as outcome labels
- [ ] Per-object Sentry details: observation arc, observation counts, orbit computation date
- [ ] Analysis: how risk estimates evolve
- [ ] Prediction: estimate whether an object will be removed from the risk list

## License

MIT, see [LICENSE](LICENSE). The data itself comes from NASA/JPL (see below).

## Data credit

Data courtesy of NASA/JPL's Center for Near-Earth Object Studies (CNEOS).
This project is independent and not affiliated with NASA or JPL.
