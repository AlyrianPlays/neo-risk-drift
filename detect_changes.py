"""
Change detection: turn consecutive snapshots into events.

Each Sentry and Scout run is compared with the previous run of the same
source, and what moved is recorded in sentry_events and scout_events:
  - added       first time we have seen the object
  - updated     JPL refit the object; one row per changed field, old and new value
  - rescored    Sentry only: just ps_cum/ps_max moved, a calendar effect (see RESCORE_FIELDS)
  - dropped     the object is missing from this run
  - reappeared  the object is back after being dropped

"Previous" means the previous run of the same source, not yesterday's, because
manual runs can add several in one day. The first run of each source is the
baseline and produces no events.

Every run is compared once. detected_runs gets a row per compared run, written
in the same transaction as that run's events, so re-running is always safe and
never duplicates anything.

Events are derived data. They can be rebuilt from the snapshot tables at any
time, and those can be rebuilt from runs.raw_json.

Usage:
  python detect_changes.py                  # compare new runs in neo_risk.db
  python detect_changes.py --db test.db     # use a different database file
  pythonw detect_changes.py --log snapshot.log
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sentry_events (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),  -- run where the change was seen
    des         TEXT NOT NULL,
    event_type  TEXT NOT NULL,        -- added, updated, rescored, dropped, reappeared
    field       TEXT,                 -- sentry_snapshot column that changed (updated/rescored only)
    old_value,                        -- no declared type, so values keep their REAL/INTEGER/TEXT
    new_value                         -- type and a null<->value flip stays visible
);

CREATE TABLE IF NOT EXISTS scout_events (
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    object_name  TEXT NOT NULL,       -- temporary designation
    event_type   TEXT NOT NULL,       -- added, updated, dropped, reappeared
    field        TEXT,                -- scout_snapshot column that changed (updated only)
    old_value,
    new_value
);

CREATE TABLE IF NOT EXISTS detected_runs (
    run_id       INTEGER PRIMARY KEY REFERENCES runs(run_id),  -- Sentry or Scout run compared
    prev_run_id  INTEGER REFERENCES runs(run_id),  -- what it was compared with; NULL = baseline
    event_count  INTEGER NOT NULL,
    detected_at  TEXT NOT NULL        -- UTC, ISO 8601
);
"""

# Every column: under 1% of Sentry objects change per update (T-3.1), so one
# row per changed field stays cheap.
SENTRY_FIELDS = ("fullname", "ip", "ps_cum", "ps_max", "ts_max", "n_imp", "impact_years",
                 "h", "diameter_km", "v_inf_kms", "last_obs")

# Only fields that say something about risk. n_obs, arc_hours and last_run move
# every time Scout re-observes an object, which would bury the signal, and
# ca_dist_ld/v_inf_kms mostly flip between null and a value as fits firm up.
# All of them stay in scout_snapshot.
SCOUT_FIELDS = ("rating", "neo_score", "pha_score", "moid_au", "h")

# JPL re-evaluates the Palermo scale against the current date even when the
# orbit hasn't been refit, so ps_cum/ps_max step up on their own as impact dates
# get closer (T-3.1). A change touching only these fields is not new data.
RESCORE_FIELDS = {"ps_cum", "ps_max"}

# source -> (snapshot table, object key column, events table, fields to compare)
DETECTORS = {
    "sentry_summary": ("sentry_snapshot", "des", "sentry_events", SENTRY_FIELDS),
    "scout_summary": ("scout_snapshot", "object_name", "scout_events", SCOUT_FIELDS),
}

# Order used when summarizing a run in the log
EVENT_TYPES = ("added", "reappeared", "updated", "rescored", "dropped")


def snapshot_rows(conn, table, key, fields, run_id):
    """One run's rows as {object key: tuple of field values}."""
    columns = ", ".join((key,) + fields)
    cur = conn.execute(f"SELECT {columns} FROM {table} WHERE run_id = ?", (run_id,))
    return {row[0]: row[1:] for row in cur}


def keys_seen_before(conn, table, key, run_id):
    cur = conn.execute(f"SELECT DISTINCT {key} FROM {table} WHERE run_id < ?", (run_id,))
    return {row[0] for row in cur}


def diff_runs(prev, curr, seen_before, fields):
    """Compare two runs' rows and return (key, event_type, field, old, new) tuples."""
    events = []
    for key in sorted(curr.keys() - prev.keys()):
        event_type = "reappeared" if key in seen_before else "added"
        events.append((key, event_type, None, None, None))
    for key in sorted(prev.keys() - curr.keys()):
        events.append((key, "dropped", None, None, None))
    for key in sorted(prev.keys() & curr.keys()):
        # None == None, so a field that stays null is not a change, but a
        # null<->value flip is
        changed = [(f, old, new) for f, old, new in zip(fields, prev[key], curr[key]) if old != new]
        if changed:
            event_type = "rescored" if {f for f, _, _ in changed} <= RESCORE_FIELDS else "updated"
            events.extend((key, event_type, f, old, new) for f, old, new in changed)
    return events


def pending_runs(conn, source):
    """Runs of this source that haven't been compared yet, oldest first."""
    cur = conn.execute(
        "SELECT run_id FROM runs WHERE source = ? "
        "AND run_id NOT IN (SELECT run_id FROM detected_runs) ORDER BY run_id",
        (source,),
    )
    return [row[0] for row in cur]


def previous_run(conn, source, run_id):
    # A failed feed rolls back its runs row, so every run listed is a
    # successful pull and the latest earlier one is the right baseline.
    row = conn.execute(
        "SELECT MAX(run_id) FROM runs WHERE source = ? AND run_id < ?", (source, run_id)
    ).fetchone()
    return row[0]


def detect_run(conn, source, run_id):
    """Compare one run with its predecessor and store the events. Returns (prev_run_id, events)."""
    table, key, events_table, fields = DETECTORS[source]
    prev_id = previous_run(conn, source, run_id)
    events = []
    if prev_id is not None:
        events = diff_runs(
            snapshot_rows(conn, table, key, fields, prev_id),
            snapshot_rows(conn, table, key, fields, run_id),
            keys_seen_before(conn, table, key, run_id),
            fields,
        )
    detected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # The detected_runs row and the events commit together, so a run is never
    # half-processed, and its primary key refuses a second pass over the run.
    with conn:
        conn.execute(
            "INSERT INTO detected_runs VALUES (?, ?, ?, ?)",
            (run_id, prev_id, len(events), detected_at),
        )
        conn.executemany(
            f"INSERT INTO {events_table} VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id,) + event for event in events],
        )
    return prev_id, events


def summarize(events):
    """'1 added, 3 updated, ...' counting objects, not field rows."""
    objects = {(event[0], event[1]) for event in events}
    counts = [(sum(1 for _, t in objects if t == event_type), event_type) for event_type in EVENT_TYPES]
    return ", ".join(f"{n} {event_type}" for n, event_type in counts if n) or "no changes"


def detect_changes(conn, log):
    """Compare every new Sentry and Scout run. Returns the number of sources that failed."""
    conn.executescript(SCHEMA)
    failures = 0
    for source in DETECTORS:
        try:
            for run_id in pending_runs(conn, source):
                prev_id, events = detect_run(conn, source, run_id)
                if prev_id is None:
                    log(f"{source} events: run {run_id} is the baseline")
                else:
                    log(f"{source} events: run {run_id} vs run {prev_id}: {summarize(events)}")
        except Exception as exc:  # keep going so a Sentry problem doesn't block Scout
            failures += 1
            log(f"{source} events: FAILED - {exc}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="neo_risk.db")
    parser.add_argument("--log", help="also append output to this file (for scheduled runs)")
    args = parser.parse_args()

    def log(message):
        # print() goes nowhere under pythonw.exe, same as in snapshot.py
        print(message)
        if args.log:
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(message + "\n")

    conn = sqlite3.connect(args.db)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"===== change detection at {started} UTC =====")
    failures = detect_changes(conn, log)
    conn.close()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
