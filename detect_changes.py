"""
Change detection: turn consecutive snapshots into events.

Each Sentry and Scout run is compared with the previous run of the same
source, and what moved is recorded in sentry_events and scout_events:
  - added       first time we have seen the object
  - updated     JPL refit the object; one row per changed field, old and new value
  - rescored    Sentry only: just ps_cum/ps_max moved, a calendar effect (see RESCORE_FIELDS)
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

CREATE TABLE IF NOT EXISTS scout_events (
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    object_name  TEXT NOT NULL,       -- temporary designation
    event_type   TEXT NOT NULL,       -- added, updated, dropped, reappeared
    field        TEXT,                -- scout_snapshot column that changed (updated only)
    old_value,
    new_value
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
