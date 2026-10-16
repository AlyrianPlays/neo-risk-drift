"""
Change detection: turn consecutive snapshots into events.

Each Sentry and Scout run is compared with the previous run of the same
source, and what moved is recorded in sentry_events and scout_events:
  - added       first time we have seen the object
  - updated     JPL refit the object; one row per changed field, old and new value
  - rescored    Sentry only: just ps_cum/ps_max moved, a calendar effect (see RESCORE_FIELDS)
  - dropped     the object is missing from this run
  - reappeared  the object is back after being dropped; its field rows compare
                with how it looked when last seen, since a refit usually brings it back
  - renamed     Sentry only: the object got numbered, so its des changed

"Previous" means the previous usable run of the same source, not yesterday's,
because manual runs can add several in one day. The first run of each source is
the baseline and produces no events. A run whose snapshot table holds fewer rows
than the feed reported (an empty or cut-off response) is skipped with a warning
instead of turning every missing object into a "dropped" event.

Every run is compared once. detected_runs gets a row per run, written in the
same transaction as that run's events, so re-running never duplicates anything.

Events are derived data: --rebuild clears them and recomputes everything from
the snapshot tables, which in turn can be rebuilt from runs.raw_json.

Usage:
  python detect_changes.py                  # compare new runs in neo_risk.db
  python detect_changes.py --db test.db     # use a different database file
  python detect_changes.py --rebuild        # recompute every event (back up the database first)
  pythonw detect_changes.py --log snapshot.log
snapshot.py runs this automatically after storing the feeds.
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sentry_events (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),  -- run where the change was seen
    des         TEXT NOT NULL,
    event_type  TEXT NOT NULL,        -- added, updated, rescored, dropped, reappeared, renamed
    field       TEXT,                 -- sentry_snapshot column that changed; "des" for renamed
    old_value,                        -- no declared type, so values keep their REAL/INTEGER/TEXT
    new_value                         -- type and a null<->value flip stays visible
);

CREATE TABLE IF NOT EXISTS scout_events (
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    object_name  TEXT NOT NULL,       -- temporary designation
    event_type   TEXT NOT NULL,       -- added, updated, dropped, reappeared
    field        TEXT,                -- scout_snapshot column that changed
    old_value,
    new_value
);

CREATE TABLE IF NOT EXISTS detected_runs (
    run_id       INTEGER PRIMARY KEY REFERENCES runs(run_id),  -- Sentry or Scout run
    prev_run_id  INTEGER REFERENCES runs(run_id),  -- what it was compared with
    status       TEXT NOT NULL,       -- baseline, compared, or skipped (rows missing, see docstring)
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
EVENT_TYPES = ("added", "reappeared", "renamed", "updated", "rescored", "dropped")


def snapshot_rows(conn, table, key, fields, run_id):
    """One run's rows as {object key: tuple of field values}."""
    columns = ", ".join((key,) + fields)
    cur = conn.execute(f"SELECT {columns} FROM {table} WHERE run_id = ?", (run_id,))
    return {row[0]: row[1:] for row in cur}


def last_seen_rows(conn, table, key, fields, keys, run_id):
    """{key: values} from the latest usable run before run_id that had the key, for keys seen before."""
    columns = ", ".join(fields)
    rows = {}
    for k in keys:
        row = conn.execute(
            f"SELECT {columns} FROM {table} WHERE {key} = ? AND run_id < ? "
            "AND run_id NOT IN (SELECT run_id FROM detected_runs WHERE status = 'skipped') "
            "ORDER BY run_id DESC LIMIT 1",
            (k, run_id),
        ).fetchone()
        if row:
            rows[k] = tuple(row)
    return rows


def field_changes(key, old_row, new_row, fields):
    """(key, event_type, field, old, new) per changed field: rescored if only Palermo moved."""
    # None == None, so a field that stays null is not a change, but a
    # null<->value flip is
    changed = [(f, old, new) for f, old, new in zip(fields, old_row, new_row) if old != new]
    if not changed:
        return []
    event_type = "rescored" if {f for f, _, _ in changed} <= RESCORE_FIELDS else "updated"
    return [(key, event_type, f, old, new) for f, old, new in changed]


def renames(prev, curr, fields):
    """{new key: old key} for Sentry objects that got numbered between the runs.

    "2026 RH79" would come back as "700001" with fullname "700001 (2026 RH79)",
    so a new des whose fullname holds a vanished des in parentheses is the same
    object, not a drop plus an add.
    """
    if "fullname" not in fields:
        return {}
    i = fields.index("fullname")
    gone = prev.keys() - curr.keys()
    found = {}
    for new in curr.keys() - prev.keys():
        for old in gone:
            if f"({old})" in (curr[new][i] or ""):
                found[new] = old
    return found


def diff_runs(prev, curr, last_seen, fields):
    """Compare two runs' rows and return (key, event_type, field, old, new) tuples.

    last_seen holds the latest earlier row of keys that are missing from prev
    but were in some run before it.
    """
    events = []
    renamed = renames(prev, curr, fields)
    for new, old in sorted(renamed.items()):
        events.append((new, "renamed", "des", old, new))
        events.extend(field_changes(new, prev[old], curr[new], fields))
    for key in sorted(curr.keys() - prev.keys() - renamed.keys()):
        if key in last_seen:
            events.append((key, "reappeared", None, None, None))
            events.extend(field_changes(key, last_seen[key], curr[key], fields))
        else:
            events.append((key, "added", None, None, None))
    for key in sorted(prev.keys() - curr.keys() - set(renamed.values())):
        events.append((key, "dropped", None, None, None))
    for key in sorted(prev.keys() & curr.keys()):
        events.extend(field_changes(key, prev[key], curr[key], fields))
    return events


def pending_runs(conn, source):
    """Runs of this source that haven't been compared yet, oldest first."""
    cur = conn.execute(
        "SELECT run_id FROM runs WHERE source = ? "
        "AND run_id NOT IN (SELECT run_id FROM detected_runs) ORDER BY run_id",
        (source,),
    )
    return [row[0] for row in cur]


def row_counts(conn, table, run_id):
    """(rows in the snapshot table, records the feed said it sent) for one run."""
    stored = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)).fetchone()[0]
    reported = conn.execute("SELECT record_count FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    return stored, reported


def previous_run(conn, source, run_id):
    # D7: the previous usable run of the same source. Runs are compared in
    # order, so every earlier run already has its detected_runs row.
    row = conn.execute(
        "SELECT MAX(r.run_id) FROM runs r JOIN detected_runs d ON d.run_id = r.run_id "
        "WHERE r.source = ? AND r.run_id < ? AND d.status != 'skipped'",
        (source, run_id),
    ).fetchone()
    return row[0]


def detect_run(conn, source, run_id):
    """Compare one run with its predecessor and store the events. Returns (status, prev_run_id, events)."""
    table, key, events_table, fields = DETECTORS[source]
    prev_id, events = None, []
    stored, reported = row_counts(conn, table, run_id)
    if stored == 0 or stored != reported:
        status = "skipped"
    else:
        prev_id = previous_run(conn, source, run_id)
        status = "baseline" if prev_id is None else "compared"
    if status == "compared":
        prev = snapshot_rows(conn, table, key, fields, prev_id)
        curr = snapshot_rows(conn, table, key, fields, run_id)
        last_seen = last_seen_rows(conn, table, key, fields, curr.keys() - prev.keys(), run_id)
        events = diff_runs(prev, curr, last_seen, fields)
    detected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # The detected_runs row and the events commit together, so a run is never
    # half-processed. If another process (a manual run during the scheduled
    # one) got here first, the row already exists and nothing is written.
    with conn:
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO detected_runs VALUES (?, ?, ?, ?, ?)",
            (run_id, prev_id, status, len(events), detected_at),
        )
        if conn.total_changes == before:
            return "already compared", prev_id, []
        conn.executemany(
            f"INSERT INTO {events_table} VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id,) + event for event in events],
        )
    return status, prev_id, events


def summarize(events):
    """'1 added, 3 updated, ...' counting objects, not field rows."""
    objects = {(event[0], event[1]) for event in events}
    counts = [(sum(1 for _, t in objects if t == event_type), event_type) for event_type in EVENT_TYPES]
    return ", ".join(f"{n} {event_type}" for n, event_type in counts if n) or "no changes"


def detect_changes(conn, log):
    """Compare every new Sentry and Scout run. Returns the number of problems (0 = all fine)."""
    conn.executescript(SCHEMA)
    problems = 0
    for source, (table, _, _, _) in DETECTORS.items():
        try:
            for run_id in pending_runs(conn, source):
                status, prev_id, events = detect_run(conn, source, run_id)
                if status == "skipped":
                    # Counted as a problem so the exit code tells the scheduler
                    stored, reported = row_counts(conn, table, run_id)
                    problems += 1
                    log(f"{source} events: WARNING run {run_id} skipped, "
                        f"{stored} rows stored but the feed reported {reported}")
                elif status == "baseline":
                    log(f"{source} events: run {run_id} is the baseline")
                elif status == "compared":
                    log(f"{source} events: run {run_id} vs run {prev_id}: {summarize(events)}")
                else:
                    log(f"{source} events: run {run_id} {status} by another process")
        except Exception as exc:  # keep going so a Sentry problem doesn't block Scout
            problems += 1
            log(f"{source} events: FAILED - {exc}")
    return problems


def clear_events(conn):
    """Forget every event and compared run. They are derived, so nothing is lost."""
    conn.executescript(SCHEMA)
    with conn:
        for _, _, events_table, _ in DETECTORS.values():
            conn.execute(f"DELETE FROM {events_table}")
        conn.execute("DELETE FROM detected_runs")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="neo_risk.db")
    parser.add_argument("--log", help="also append output to this file (for scheduled runs)")
    parser.add_argument("--rebuild", action="store_true",
                        help="clear all events and recompute them from the snapshot tables")
    args = parser.parse_args()

    def log(message):
        # print() goes nowhere under pythonw.exe, same as in snapshot.py
        print(message)
        if args.log:
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(message + "\n")

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"===== change detection at {started} UTC =====")
    try:
        conn = sqlite3.connect(args.db)
        if args.rebuild:
            clear_events(conn)
            log("rebuild: cleared all events, recomputing every run")
        problems = detect_changes(conn, log)
        conn.close()
    except Exception as exc:  # so the error reaches the log file under pythonw
        problems = 1
        log(f"change detection: FAILED - {exc}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
