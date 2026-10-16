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

After the feeds are stored, detect_changes.py compares every Sentry and Scout
run it hasn't seen yet with the previous one and records what moved as events.
Run against an existing database for the first time, it works through all of
the earlier runs too.

Usage:
  python snapshot.py                 # pull live data from JPL
  python snapshot.py --db my.db      # use a different database file
  python snapshot.py --from-dir DIR  # load saved JSON files instead (for testing)
  pythonw snapshot.py --log snapshot.log  # scheduled run on Windows, no window
  python snapshot.py --details-limit 0    # skip the per-object Sentry details
"""

import argparse
import json
import sqlite3
import sys
import time
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

# Sentry's per-object mode (sentry.api?des=...) adds what the summary lacks:
# the observation arc, observation counts and when JPL last ran the impact analysis.
# That's one request per object, so each run only fetches objects that are new,
# were refit, or have no details yet, at most --details-limit of them, one
# every DETAILS_PAUSE_S seconds. The first backfill of ~2,200 objects is spread
# over about a dozen daily runs.
DETAILS_PER_RUN = 200
DETAILS_PAUSE_S = 1.0
DETAILS_MAX_FAILURES = 3   # in a row: JPL is probably down, so stop and retry next run
DETAILS_MAX_SECONDS = 600  # a slow API shouldn't keep the daily job running for hours
DETAILS_TRIGGERS = ("added", "reappeared", "updated")
# JPL answers "specified object removed" or "... not found" (with HTTP 200) when
# it has no details for an object. That is an answer about the object, not
# an outage, so it is stored instead of counted as a failure.
DETAILS_NO_DATA = "specified object"

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

CREATE TABLE IF NOT EXISTS sentry_details (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    des         TEXT NOT NULL,
    method      TEXT,                 -- impact search: IOBS, LOV or MC (Monte Carlo)
    cdate       TEXT,                 -- when JPL ran the impact analysis, US Pacific local time (not UTC)
    pdate       TEXT,                 -- UTC date the Palermo values were computed for
    first_obs   TEXT,                 -- first observation used, UTC; may carry a fractional day
    last_obs    TEXT,                 -- last observation used, UTC
    arc_days    REAL,                 -- days spanned by the observations used
    n_obs       INTEGER,              -- all observations used
    n_del       INTEGER,              -- radar delay (range) observations
    n_dop       INTEGER,              -- radar Doppler observations
    n_sat       INTEGER,              -- optical observations from satellites
    mass_kg     REAL,                 -- rough: assumes a sphere of density 2.6 g/cm3
    energy_mt   REAL,                 -- impact energy, megatons of TNT
    v_imp_kms   REAL,                 -- speed at atmospheric entry, km/s
    error       TEXT,                 -- JPL's answer when it had no details (object removed or not found)
    PRIMARY KEY (run_id, des)
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


def to_days(value):
    """darc comes as "32.543 days", "27983. days" or a bare "7693.3", always in days."""
    parts = str(value or "").split()
    return to_float(parts[0]) if parts else None


def fetch(endpoint, params):
    response = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def load_from_dir(directory, source):
    return json.loads((Path(directory) / f"{source}.json").read_text())


def load_details_from_dir(directory, des):
    return json.loads((Path(directory) / "sentry_details" / f"{des}.json").read_text())


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


def store_sentry_details(conn, run_id, des, payload):
    # Keyed by the designation we asked for, which is how sentry_snapshot knows
    # the object, even if JPL answers under another name
    s = payload.get("summary") or {}
    conn.execute(
        "INSERT INTO sentry_details VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, des, s.get("method"), s.get("cdate"), s.get("pdate"),
            s.get("first_obs"), s.get("last_obs"), to_days(s.get("darc")),
            to_int(s.get("nobs")), to_int(s.get("ndel")), to_int(s.get("ndop")),
            to_int(s.get("nsat")), to_float(s.get("mass")), to_float(s.get("energy")),
            to_float(s.get("v_imp")), payload.get("error"),
        ),
    )
    return 1


def details_wanted(conn):
    """Objects in the latest Sentry run with no details since they were added or last refit."""
    # Pass over a run change detection skipped as incomplete (an empty or
    # cut-off response), or the backfill would stall on it
    latest = conn.execute(
        "SELECT des, last_obs FROM sentry_snapshot WHERE run_id = "
        "(SELECT MAX(r.run_id) FROM runs r LEFT JOIN detected_runs d ON d.run_id = r.run_id "
        " WHERE r.source = 'sentry_summary' AND IFNULL(d.status, '') != 'skipped')"
    ).fetchall()
    fetched = dict(conn.execute("SELECT des, MAX(run_id) FROM sentry_details GROUP BY des"))
    marks = ", ".join("?" * len(DETAILS_TRIGGERS))
    changed = dict(conn.execute(
        f"SELECT des, MAX(run_id) FROM sentry_events WHERE event_type IN ({marks}) GROUP BY des",
        DETAILS_TRIGGERS,
    ))
    wanted = [(last_obs or "", des) for des, last_obs in latest
              if fetched.get(des, -1) < changed.get(des, 0)]
    # Recently observed objects first: they're still changing, and they're the
    # likeliest to be removed before the backfill gets to them
    return [des for _, des in sorted(wanted, reverse=True)]


def fetch_sentry_details(conn, from_dir, limit, fetched_at, log):
    """Fetch and store details for up to `limit` objects. Returns 1 if any request failed, else 0."""
    if from_dir and not (Path(from_dir) / "sentry_details").is_dir():
        log("sentry_details: skipped, no saved responses")
        return 0
    wanted = details_wanted(conn)
    newest_run = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    stored = no_data = elsewhere = failed = in_a_row = 0
    started = time.monotonic()
    for i, des in enumerate(wanted[:limit]):
        if time.monotonic() - started > DETAILS_MAX_SECONDS:
            log("sentry_details: out of time, will continue next run")
            break
        # A manual run alongside the scheduled one may have just fetched it
        if conn.execute("SELECT 1 FROM sentry_details WHERE des = ? AND run_id > ?",
                        (des, newest_run)).fetchone():
            elsewhere += 1
            continue
        try:
            if from_dir:
                payload = load_details_from_dir(from_dir, des)
            else:
                if i:
                    time.sleep(DETAILS_PAUSE_S)  # one request at a time, spaced out
                payload = fetch("sentry.api", {"des": des})
            error = payload.get("error")
            if error and not error.startswith(DETAILS_NO_DATA):
                raise RuntimeError(error)
            # Same rule as the feeds: raw JSON and parsed row land together.
            # JPL's "no details" answers are stored too, so the object isn't
            # asked for again until it changes.
            with conn:
                run_id = record_run(conn, "sentry_details", payload, fetched_at)
                store_sentry_details(conn, run_id, des, payload)
            if error:
                no_data += 1
                log(f"sentry_details: {des}: {error}")
            else:
                stored += 1
            in_a_row = 0
        except Exception as exc:  # network or server trouble; one bad object shouldn't stop the rest
            failed += 1
            in_a_row += 1
            log(f"sentry_details: {des} FAILED - {exc}")
            if in_a_row >= DETAILS_MAX_FAILURES:
                log("sentry_details: stopping after repeated failures, will retry next run")
                break
    left = len(wanted) - stored - no_data - elsewhere
    log(f"sentry_details: {stored} stored, {no_data} without details, {failed} failed, {left} still to fetch")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="neo_risk.db")
    parser.add_argument("--from-dir", help="read <source>.json files instead of calling JPL")
    parser.add_argument("--log", help="also append output to this file (for scheduled runs)")
    parser.add_argument("--details-limit", type=int, default=DETAILS_PER_RUN,
                        help="most Sentry objects to fetch details for (0 to skip)")
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

    # Change detection reads the snapshot tables and writes only its own
    # tables, in its own transactions, after every feed above has committed,
    # so a bug there can never cost a snapshot. It's imported here rather than
    # at the top so that even a detect_changes.py that won't import can't stop
    # the feeds from landing.
    try:
        import detect_changes
        failures += detect_changes.detect_changes(conn, log)
    except Exception as exc:
        failures += 1
        log(f"change detection: FAILED - {exc}")

    # Details come last: they pick objects using the events found above, and
    # they're the slowest step, so nothing else waits on them.
    if args.details_limit > 0:
        try:
            failures += fetch_sentry_details(conn, args.from_dir, args.details_limit, fetched_at, log)
        except Exception as exc:
            failures += 1
            log(f"sentry_details: FAILED - {exc}")

    conn.close()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
