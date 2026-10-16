"""
Change detection: turn consecutive snapshots into events.

Each Sentry run is compared with the previous run of the same source, and what
moved is recorded in sentry_events:
  - added       first time we have seen the object
  - updated     JPL refit the object; one row per changed field, old and new value
  - rescored    only ps_cum/ps_max moved, a calendar effect (see RESCORE_FIELDS)
  - dropped     the object is missing from this run
  - reappeared  the object is back after being dropped

Events are derived data. They can be rebuilt from the snapshot tables at any
time, and those can be rebuilt from runs.raw_json.

Usage:
  python detect_changes.py --db test.db   # create the event tables
"""

import argparse
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS sentry_events (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),  -- run where the change was seen
    des         TEXT NOT NULL,
    event_type  TEXT NOT NULL,        -- added, updated, rescored, dropped, reappeared
    field       TEXT,                 -- sentry_snapshot column that changed (updated/rescored only)
    old_value,                        -- no declared type, so values keep their REAL/INTEGER/TEXT
    new_value                         -- type and a null<->value flip stays visible
);
"""

# JPL re-evaluates the Palermo scale against the current date even when the
# orbit hasn't been refit, so ps_cum/ps_max step up on their own as impact dates
# get closer (T-3.1). A change touching only these fields is not new data.
RESCORE_FIELDS = {"ps_cum", "ps_max"}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="neo_risk.db")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.close()
    print("event tables ready")


if __name__ == "__main__":
    main()
