"""
Daily snapshot of NASA/JPL near-Earth-object risk data.

Each run pulls three feeds and stores them in a local SQLite database:
  - Sentry summary:  objects with a nonzero chance of hitting Earth
  - Sentry removed:  objects later ruled out (and when)
  - Scout summary:   newly spotted, unconfirmed objects

Two layers are stored on purpose:
  1. `runs` keeps the raw JSON exactly as received ("bronze" layer).
     If we parse something wrong later, we can re-parse from here
     instead of losing history we can never re-download.
  2. Snapshot tables hold clean, typed rows tagged with the run they came
     from ("silver" layer). Comparing rows across runs is how we'll
     later see how each object's risk changed.

Usage:
  python snapshot.py                 # pull live data from JPL
  python snapshot.py --db my.db      # use a different database file
  python snapshot.py --from-dir DIR  # load saved JSON files instead (for testing)
  pythonw snapshot.py --log snapshot.log  # scheduled run on Windows, no window
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

API_BASE = "https://ssd-api.jpl.nasa.gov"

# source name -> (endpoint, query params)
FEEDS = {
    "sentry_summary": ("sentry.api", {}),
    "sentry_removed": ("sentry.api", {"removed": "1"}),
    "scout_summary": ("scout.api", {}),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,      -- UTC, ISO 8601
    api_version   TEXT,
    record_count  INTEGER,
    raw_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sentry_snapshot (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    des         TEXT NOT NULL,        -- object designation
    fullname    TEXT,
    ip          REAL,                 -- cumulative impact probability
    ps_cum      REAL,                 -- cumulative Palermo scale rating
    ps_max      REAL,                 -- max Palermo scale rating
    ts_max      INTEGER,              -- max Torino scale rating (0-10)
    n_imp       INTEGER,              -- number of potential impacts
    impact_years TEXT,                -- e.g. "2056-2113"
    h           REAL,                 -- absolute magnitude (brightness)
    diameter_km REAL,
    v_inf_kms   REAL,                 -- relative velocity, km/s
    last_obs    TEXT,                 -- date of last observation
    PRIMARY KEY (run_id, des)
);

CREATE TABLE IF NOT EXISTS sentry_removed (
    des          TEXT NOT NULL,
    removed_at   TEXT NOT NULL,       -- when JPL removed it from Sentry
    first_seen_run INTEGER NOT NULL REFERENCES runs(run_id),
    PRIMARY KEY (des, removed_at)
);

CREATE TABLE IF NOT EXISTS scout_snapshot (
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    object_name  TEXT NOT NULL,       -- temporary designation
    n_obs        INTEGER,
    arc_hours    REAL,                -- time span of observations
    h            REAL,
    rating       INTEGER,             -- Scout impact rating
    moid_au      REAL,                -- closest orbit distance to Earth's orbit
    ca_dist_ld   REAL,                -- close-approach distance, lunar distances
    v_inf_kms    REAL,
    neo_score    INTEGER,             -- 0-100: chance it's a near-Earth object
    pha_score    INTEGER,             -- 0-100: chance it's potentially hazardous
    last_run     TEXT,                -- when Scout last computed it
    PRIMARY KEY (run_id, object_name)
);
"""


def to_float(value):
    """JPL returns numbers as strings, sometimes null or empty. Convert safely."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value):
    f = to_float(value)
    return int(f) if f is not None else None


def fetch(endpoint, params):
    response = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def load_from_dir(directory, source):
    return json.loads((Path(directory) / f"{source}.json").read_text())


def record_run(conn, source, payload, fetched_at):
    cur = conn.execute(
        "INSERT INTO runs (source, fetched_at, api_version, record_count, raw_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            source,
            fetched_at,
            (payload.get("signature") or {}).get("version"),
            to_int(payload.get("count")),
            json.dumps(payload),
        ),
    )
    return cur.lastrowid


def store_sentry_summary(conn, run_id, payload):
    rows = [
        (
            run_id, r.get("des"), r.get("fullname"),
            to_float(r.get("ip")), to_float(r.get("ps_cum")), to_float(r.get("ps_max")),
            to_int(r.get("ts_max")), to_int(r.get("n_imp")), r.get("range"),
            to_float(r.get("h")), to_float(r.get("diameter")), to_float(r.get("v_inf")),
            r.get("last_obs"),
        )
        for r in payload.get("data", [])
    ]
    conn.executemany(
        "INSERT INTO sentry_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    return len(rows)


def store_sentry_removed(conn, run_id, payload):
    # The removed list is cumulative, so we only insert objects we haven't
    # seen before. first_seen_run tells us when *we* first noticed the removal.
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO sentry_removed (des, removed_at, first_seen_run) "
        "VALUES (?, ?, ?)",
        [(r.get("des"), r.get("removed"), run_id) for r in payload.get("data", [])],
    )
    return conn.total_changes - before


def store_scout_summary(conn, run_id, payload):
    rows = [
        (
            run_id, r.get("objectName"), to_int(r.get("nObs")), to_float(r.get("arc")),
            to_float(r.get("H")), to_int(r.get("rating")), to_float(r.get("moid")),
            to_float(r.get("caDist")), to_float(r.get("vInf")),
            to_int(r.get("neoScore")), to_int(r.get("phaScore")), r.get("lastRun"),
        )
        for r in payload.get("data", [])
    ]
    conn.executemany(
        "INSERT INTO scout_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    return len(rows)


STORERS = {
    "sentry_summary": store_sentry_summary,
    "sentry_removed": store_sentry_removed,
    "scout_summary": store_scout_summary,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="neo_risk.db")
    parser.add_argument("--from-dir", help="read <source>.json files instead of calling JPL")
    parser.add_argument("--log", help="also append output to this file (for scheduled runs)")
    args = parser.parse_args()

    def log(message):
        # print() goes nowhere under pythonw.exe (no window), so scheduled
        # runs rely on the log file instead.
        print(message)
        if args.log:
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(message + "\n")

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"===== run at {fetched_at} UTC =====")
    failures = 0

    for source, (endpoint, params) in FEEDS.items():
        try:
            payload = (
                load_from_dir(args.from_dir, source) if args.from_dir
                else fetch(endpoint, params)
            )
            if "error" in payload:
                raise RuntimeError(payload["error"])
            # One transaction per feed: either the raw JSON and its parsed rows
            # are both saved, or neither is. No half-written runs.
            with conn:
                run_id = record_run(conn, source, payload, fetched_at)
                n = STORERS[source](conn, run_id, payload)
            log(f"{source}: run {run_id}, {n} rows stored")
        except Exception as exc:  # keep going so one bad feed doesn't sink the others
            failures += 1
            log(f"{source}: FAILED - {exc}")

    conn.close()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
