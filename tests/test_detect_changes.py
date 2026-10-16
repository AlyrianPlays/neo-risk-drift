"""Checks for detect_changes.py: the pure diff, then whole runs over the fixtures.

Run from the project folder:
    .venv\\Scripts\\python.exe -m unittest discover tests -v

See test_snapshot.py for what changes between fixtures day1 and day2.
snapshot.py runs detection itself, so tests that need detect_changes.py to do
real work either use --rebuild or block detection with a trigger first.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, PROJECT)

import detect_changes  # noqa: E402  (needs the project folder on sys.path first)
import snapshot  # noqa: E402

FIELDS = ("ip", "ps_cum", "ps_max", "last_obs")
DAY2_SENTRY_EVENTS = {
    ("2026 TC94", "added", None),
    ("2026 TL77", "dropped", None),
    ("2026 TS", "dropped", None),
    ("2010 UE", "rescored", "ps_cum"),
    ("2010 UE", "rescored", "ps_max"),
    ("2026 RH79", "updated", "ip"),
    ("2026 RH79", "updated", "ps_cum"),
    ("2026 RH79", "updated", "ps_max"),
    ("2026 RH79", "updated", "h"),
    ("2026 RH79", "updated", "last_obs"),
}


class DiffRunsTest(unittest.TestCase):

    def test_added_dropped_and_reappeared(self):
        row = (1.0, -5.0, -5.0, "d1")
        prev = {"A": row, "B": row}
        curr = {"A": row, "C": row, "D": row}
        events = detect_changes.diff_runs(prev, curr, {"D": row}, FIELDS)
        self.assertEqual(events, [
            ("C", "added", None, None, None),
            ("D", "reappeared", None, None, None),
            ("B", "dropped", None, None, None),
        ])

    def test_reappeared_object_is_compared_with_its_last_seen_row(self):
        # 2026 TB8 came back with ip 100x lower; that refit must not be lost
        events = detect_changes.diff_runs({}, {"A": (5e-07, -6.1, -6.1, "d2")},
                                          {"A": (5e-05, -3.9, -4.1, "d1")}, FIELDS)
        self.assertEqual(events[0], ("A", "reappeared", None, None, None))
        self.assertEqual([e[1:3] for e in events[1:]],
                         [("updated", "ip"), ("updated", "ps_cum"), ("updated", "ps_max"), ("updated", "last_obs")])

    def test_palermo_only_change_is_rescored(self):
        prev = {"A": (1.0, -5.0, -5.1, "d1")}
        curr = {"A": (1.0, -4.99, -5.1, "d1")}
        events = detect_changes.diff_runs(prev, curr, {}, FIELDS)
        self.assertEqual(events, [("A", "rescored", "ps_cum", -5.0, -4.99)])

    def test_refit_logs_every_changed_field_as_updated(self):
        prev = {"A": (1.0, -5.0, -5.1, "d1")}
        curr = {"A": (2.0, -4.0, -5.1, "d2")}
        events = detect_changes.diff_runs(prev, curr, {}, FIELDS)
        self.assertEqual(events, [
            ("A", "updated", "ip", 1.0, 2.0),
            ("A", "updated", "ps_cum", -5.0, -4.0),
            ("A", "updated", "last_obs", "d1", "d2"),
        ])

    def test_null_flip_is_a_change_but_null_to_null_is_not(self):
        prev = {"A": (None, -5.0, -5.0, None), "B": (None, -5.0, -5.0, None)}
        curr = {"A": (0.5, -5.0, -5.0, None), "B": (None, -5.0, -5.0, None)}
        events = detect_changes.diff_runs(prev, curr, {}, FIELDS)
        self.assertEqual(events, [("A", "updated", "ip", None, 0.5)])

    def test_numbered_object_is_renamed_not_dropped(self):
        fields = ("fullname", "ip")
        prev = {"2026 RH79": ("(2026 RH79)", 1e-06), "2026 TS": ("(2026 TS)", 1e-08)}
        curr = {"700001": ("700001 (2026 RH79)", 1e-06)}
        events = detect_changes.diff_runs(prev, curr, {}, fields)
        self.assertEqual(events, [
            ("700001", "renamed", "des", "2026 RH79", "700001"),
            ("700001", "updated", "fullname", "(2026 RH79)", "700001 (2026 RH79)"),
            ("2026 TS", "dropped", None, None, None),
        ])


class FieldListTest(unittest.TestCase):

    def columns(self, table):
        conn = sqlite3.connect(":memory:")
        conn.executescript(snapshot.SCHEMA)
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]

    def test_every_sentry_column_is_compared(self):
        expected = [c for c in self.columns("sentry_snapshot") if c not in ("run_id", "des")]
        self.assertEqual(list(detect_changes.SENTRY_FIELDS), expected)

    def test_scout_compares_only_the_risk_fields(self):
        # Decided in T-3.1/T-3.3; the other Scout columns are noise
        self.assertEqual(set(detect_changes.SCOUT_FIELDS), {"rating", "neo_score", "pha_score", "moid_au", "h"})
        self.assertLessEqual(set(detect_changes.SCOUT_FIELDS), set(self.columns("scout_snapshot")))


class RemovalCheckTest(unittest.TestCase):
    """The T-3.6 views on a hand-built history with controlled timestamps.

    Mirrors 2026 TB8: listed, removed, back a day later, removed again. X
    vanishes with no removal on record. Y was removed in 2020 and has been
    back in Sentry since before our first run.
    """

    RUNS = [
        ("2026-10-13T01:00:00+00:00", ["2026 TB8", "X", "Y"]),
        ("2026-10-13T18:00:00+00:00", ["X", "Y"]),
        ("2026-10-14T23:00:00+00:00", ["2026 TB8", "Y"]),
        ("2026-10-16T14:00:00+00:00", ["Y"]),
    ]
    REMOVED = [("2026 TB8", "2026-10-13 17:29:15"), ("2026 TB8", "2026-10-15 20:08:52"), ("Y", "2020-01-01 00:00:00")]

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(snapshot.SCHEMA)
        with self.conn:
            for fetched_at, objects in self.RUNS:
                run_id = self.conn.execute(
                    "INSERT INTO runs (source, fetched_at, record_count, raw_json) VALUES ('sentry_summary', ?, ?, '{}')",
                    (fetched_at, len(objects))).lastrowid
                self.conn.executemany("INSERT INTO sentry_snapshot (run_id, des) VALUES (?, ?)",
                                      [(run_id, des) for des in objects])
            self.conn.executemany("INSERT INTO sentry_removed VALUES (?, ?, 1)", self.REMOVED)
        detect_changes.detect_changes(self.conn, lambda message: None)

    def tearDown(self):
        self.conn.close()

    def test_each_drop_is_matched_to_its_own_removal(self):
        rows = self.conn.execute("SELECT run_id, des, removed_at FROM sentry_drop_check ORDER BY run_id").fetchall()
        self.assertEqual(rows, [
            (2, "2026 TB8", "2026-10-13 17:29:15"),
            (3, "X", None),
            (4, "2026 TB8", "2026-10-15 20:08:52"),
        ])

    def test_removal_status_shows_when_an_object_came_back(self):
        rows = self.conn.execute(
            "SELECT des, removed_at, back_in_sentry_run FROM sentry_removed_status ORDER BY des, removed_at").fetchall()
        self.assertEqual(rows, [
            ("2026 TB8", "2026-10-13 17:29:15", 3),
            ("2026 TB8", "2026-10-15 20:08:52", None),
            ("Y", "2020-01-01 00:00:00", 1),
        ])


class DetectChangesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_script(self, *args, expect=0):
        result = subprocess.run([sys.executable] + list(args), capture_output=True, text=True, cwd=PROJECT)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result.stdout

    def snapshot(self, day, expect=0):
        from_dir = day if os.path.isabs(day) else os.path.join(FIXTURES, day)
        return self.run_script("snapshot.py", "--db", self.db, "--from-dir", from_dir, expect=expect)

    def detect(self, *extra):
        return self.run_script("detect_changes.py", "--db", self.db, *extra)

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def execute(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(sql)
        finally:
            conn.close()

    def event_count(self):
        return self.query("SELECT (SELECT COUNT(*) FROM sentry_events) + (SELECT COUNT(*) FROM scout_events)")[0][0]

    def sentry_events(self, run_id=None):
        sql = "SELECT des, event_type, field FROM sentry_events"
        if run_id is None:
            return set(self.query(sql))
        return set(self.query(sql + " WHERE run_id = ?", (run_id,)))

    def short_day2(self, edit):
        """A copy of day2 whose sentry_summary.json was changed by edit(payload)."""
        copy = os.path.join(self.tmp, "short")
        shutil.copytree(os.path.join(FIXTURES, "day2"), copy)
        path = os.path.join(copy, "sentry_summary.json")
        with open(path, encoding="ascii") as f:
            payload = json.load(f)
        edit(payload)
        with open(path, "w", encoding="ascii") as f:
            json.dump(payload, f)
        return copy

    def test_detected_runs_records_each_comparison(self):
        self.snapshot("day1")
        self.snapshot("day2")
        runs = self.query("SELECT run_id, prev_run_id, status FROM detected_runs ORDER BY run_id")
        self.assertEqual(runs, [(1, None, "baseline"), (3, None, "baseline"), (4, 1, "compared"), (6, 3, "compared")])
        counts = self.query(
            "SELECT d.run_id, d.event_count, "
            "(SELECT COUNT(*) FROM sentry_events e WHERE e.run_id = d.run_id) + "
            "(SELECT COUNT(*) FROM scout_events e WHERE e.run_id = d.run_id) FROM detected_runs d")
        for run_id, recorded, actual in counts:
            self.assertEqual(recorded, actual, run_id)
        self.assertEqual(self.event_count(), sum(c[1] for c in counts))

    def test_sentry_events_between_two_days(self):
        self.snapshot("day1")
        self.snapshot("day2")
        self.assertEqual(self.sentry_events(), DAY2_SENTRY_EVENTS)
        ip = self.query("SELECT old_value, new_value FROM sentry_events WHERE des = '2026 RH79' AND field = 'ip'")
        self.assertEqual(ip, [(5.28e-07, 5.792e-07)])

    def test_scout_events_between_two_days(self):
        self.snapshot("day1")
        self.snapshot("day2")
        objects = set(self.query("SELECT object_name, event_type FROM scout_events WHERE field IS NULL"))
        self.assertEqual(objects, {("8NIQ001", "added"), ("8NIP003", "dropped")})
        score = self.query("SELECT old_value, new_value FROM scout_events WHERE object_name = 'P12rhre' AND field = 'neo_score'")
        self.assertEqual(score, [(63, 0)])
        noise = self.query("SELECT COUNT(*) FROM scout_events WHERE field IN ('n_obs', 'arc_hours', 'last_run')")
        self.assertEqual(noise, [(0,)])

    def test_log_summary_counts_objects_not_field_rows(self):
        self.snapshot("day1")
        out = self.snapshot("day2")
        self.assertIn("sentry_summary events: run 4 vs run 1: 1 added, 1 updated, 1 rescored, 2 dropped", out)

    def test_rebuild_recomputes_the_same_events_and_reruns_add_nothing(self):
        self.snapshot("day1")
        self.snapshot("day2")
        first = self.event_count()
        self.assertGreater(first, 0)
        out = self.detect("--rebuild")
        self.assertIn("sentry_summary events: run 4 vs run 1", out)
        self.assertEqual(self.event_count(), first)
        self.assertEqual(len(self.detect().splitlines()), 1, "only the header, nothing left to compare")
        self.assertEqual(self.event_count(), first)
        # An identical pull adds a compared run but no events
        self.snapshot("day2")
        self.assertEqual(self.event_count(), first)
        self.assertEqual(self.query("SELECT COUNT(*) FROM detected_runs")[0][0], 6)

    def test_second_pass_over_a_run_writes_nothing(self):
        # What a second process sees if it picked the same run (manual run during the scheduled one)
        self.snapshot("day1")
        self.snapshot("day2")
        before = self.event_count()
        conn = sqlite3.connect(self.db)
        try:
            status, _, events = detect_changes.detect_run(conn, "sentry_summary", 4)
        finally:
            conn.close()
        self.assertEqual((status, events), ("already compared", []))
        self.assertEqual(self.event_count(), before)

    def test_object_back_after_a_drop_is_reappeared(self):
        self.snapshot("day1")
        self.snapshot("day2")
        self.snapshot("day1")
        back = set(self.query("SELECT des, event_type FROM sentry_events WHERE run_id = 7 AND field IS NULL"))
        self.assertEqual(back, {("2026 TL77", "reappeared"), ("2026 TS", "reappeared"), ("2026 TC94", "dropped")})
        # day1 again, so the reappeared objects look exactly as when last seen: no field rows
        self.assertEqual(self.query("SELECT COUNT(*) FROM sentry_events WHERE run_id = 7 AND des = '2026 TS'"), [(1,)])
        scout_back = self.query("SELECT event_type FROM scout_events WHERE object_name = '8NIP003' ORDER BY run_id")
        self.assertEqual(scout_back, [("dropped",), ("reappeared",)])

    def test_run_missing_rows_is_skipped_not_diffed(self):
        self.snapshot("day1")

        def claim_more(payload):
            payload["count"] = "9"
        out = self.snapshot(self.short_day2(claim_more), expect=1)
        self.assertIn("sentry_summary events: WARNING run 4 skipped, 4 rows stored but the feed reported 9", out)
        self.assertEqual(self.query("SELECT status FROM detected_runs WHERE run_id = 4"), [("skipped",)])
        self.assertEqual(self.sentry_events(4), set())
        # The next good run is compared with the last usable one, run 1
        out = self.snapshot("day2")
        self.assertIn("sentry_summary events: run 7 vs run 1", out)
        self.assertEqual(self.sentry_events(7), DAY2_SENTRY_EVENTS)

    def test_empty_run_is_skipped(self):
        self.snapshot("day1")

        def empty(payload):
            payload["data"], payload["count"] = [], "0"
        out = self.snapshot(self.short_day2(empty), expect=1)
        self.assertIn("WARNING run 4 skipped, 0 rows stored", out)
        self.assertEqual(self.query("SELECT COUNT(*) FROM sentry_events"), [(0,)])

    def test_events_and_their_run_marker_commit_together(self):
        self.snapshot("day1")
        # Make the event insert fail after the detected_runs row went in
        self.execute("CREATE TRIGGER boom BEFORE INSERT ON sentry_events BEGIN SELECT RAISE(ABORT, 'boom'); END;")
        out = self.snapshot("day2", expect=1)
        self.assertIn("sentry_summary events: FAILED - boom", out)
        self.assertEqual(self.query("SELECT COUNT(*) FROM detected_runs WHERE run_id = 4"), [(0,)])
        # Scout is compared even though Sentry failed
        self.assertEqual(self.query("SELECT status FROM detected_runs WHERE run_id = 6"), [("compared",)])
        self.execute("DROP TRIGGER boom")
        self.assertIn("sentry_summary events: run 4 vs run 1", self.detect())
        self.assertEqual(self.sentry_events(), DAY2_SENTRY_EVENTS)

    def test_failed_run_marker_leaves_no_events(self):
        self.snapshot("day1")
        self.execute("CREATE TRIGGER boom BEFORE INSERT ON detected_runs WHEN NEW.run_id = 4 "
                     "BEGIN SELECT RAISE(ABORT, 'boom'); END;")
        self.snapshot("day2", expect=1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM sentry_events"), [(0,)])


if __name__ == "__main__":
    unittest.main()
