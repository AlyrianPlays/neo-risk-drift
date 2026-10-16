"""Checks for detect_changes.py: the pure diff, then whole runs over the fixtures.

Run from the project folder:
    .venv\\Scripts\\python.exe -m unittest discover tests -v

See test_snapshot.py for what changes between fixtures day1 and day2.
"""
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

FIELDS = ("ip", "ps_cum", "ps_max", "last_obs")


class DiffRunsTest(unittest.TestCase):

    def test_added_dropped_and_reappeared(self):
        prev = {"A": (1.0, -5.0, -5.0, "d1"), "B": (1.0, -5.0, -5.0, "d1")}
        curr = {"A": (1.0, -5.0, -5.0, "d1"), "C": (1.0, -5.0, -5.0, "d1"), "D": (1.0, -5.0, -5.0, "d1")}
        events = detect_changes.diff_runs(prev, curr, {"A", "B", "D"}, FIELDS)
        self.assertEqual(events, [
            ("C", "added", None, None, None),
            ("D", "reappeared", None, None, None),
            ("B", "dropped", None, None, None),
        ])

    def test_palermo_only_change_is_rescored(self):
        prev = {"A": (1.0, -5.0, -5.1, "d1")}
        curr = {"A": (1.0, -4.99, -5.1, "d1")}
        events = detect_changes.diff_runs(prev, curr, set(), FIELDS)
        self.assertEqual(events, [("A", "rescored", "ps_cum", -5.0, -4.99)])

    def test_refit_logs_every_changed_field_as_updated(self):
        prev = {"A": (1.0, -5.0, -5.1, "d1")}
        curr = {"A": (2.0, -4.0, -5.1, "d2")}
        events = detect_changes.diff_runs(prev, curr, set(), FIELDS)
        self.assertEqual(events, [
            ("A", "updated", "ip", 1.0, 2.0),
            ("A", "updated", "ps_cum", -5.0, -4.0),
            ("A", "updated", "last_obs", "d1", "d2"),
        ])

    def test_null_flip_is_a_change_but_null_to_null_is_not(self):
        prev = {"A": (None, -5.0, -5.0, None), "B": (None, -5.0, -5.0, None)}
        curr = {"A": (0.5, -5.0, -5.0, None), "B": (None, -5.0, -5.0, None)}
        events = detect_changes.diff_runs(prev, curr, set(), FIELDS)
        self.assertEqual(events, [("A", "updated", "ip", None, 0.5)])


class DetectChangesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_script(self, *args):
        result = subprocess.run([sys.executable] + list(args), capture_output=True, text=True, cwd=PROJECT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def snapshot(self, day):
        return self.run_script("snapshot.py", "--db", self.db, "--from-dir", os.path.join(FIXTURES, day))

    def detect(self):
        return self.run_script("detect_changes.py", "--db", self.db)

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def event_count(self):
        return self.query("SELECT (SELECT COUNT(*) FROM sentry_events) + (SELECT COUNT(*) FROM scout_events)")[0][0]

    def test_first_run_is_a_baseline(self):
        self.snapshot("day1")
        self.detect()
        self.assertEqual(self.event_count(), 0)
        baselines = self.query("SELECT COUNT(*) FROM detected_runs WHERE prev_run_id IS NULL")
        self.assertEqual(baselines, [(2,)])

    def test_sentry_events_between_two_days(self):
        self.snapshot("day1")
        self.snapshot("day2")
        self.detect()
        events = set(self.query("SELECT des, event_type, field FROM sentry_events"))
        self.assertEqual(events, {
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
        })
        ip = self.query("SELECT old_value, new_value FROM sentry_events WHERE des = '2026 RH79' AND field = 'ip'")
        self.assertEqual(ip, [(5.28e-07, 5.792e-07)])

    def test_scout_events_between_two_days(self):
        self.snapshot("day1")
        self.snapshot("day2")
        self.detect()
        objects = set(self.query("SELECT object_name, event_type FROM scout_events WHERE field IS NULL"))
        self.assertEqual(objects, {("8NIQ001", "added"), ("8NIP003", "dropped")})
        score = self.query("SELECT old_value, new_value FROM scout_events WHERE object_name = 'P12rhre' AND field = 'neo_score'")
        self.assertEqual(score, [(63, 0)])
        noise = self.query("SELECT COUNT(*) FROM scout_events WHERE field IN ('n_obs', 'arc_hours', 'last_run')")
        self.assertEqual(noise, [(0,)])

    def test_rerunning_never_duplicates_events(self):
        self.snapshot("day1")
        self.snapshot("day2")
        self.detect()
        first = self.event_count()
        self.assertGreater(first, 0)
        self.assertEqual(len(self.detect().splitlines()), 1, "only the header, nothing left to compare")
        self.assertEqual(self.event_count(), first)
        # An identical pull adds a compared run but no events
        self.snapshot("day2")
        self.detect()
        self.assertEqual(self.event_count(), first)
        self.assertEqual(self.query("SELECT COUNT(*) FROM detected_runs")[0][0], 6)

    def test_object_back_after_a_drop_is_reappeared(self):
        self.snapshot("day1")
        self.snapshot("day2")
        self.snapshot("day1")
        self.detect()
        last_sentry = self.query("SELECT MAX(run_id) FROM runs WHERE source = 'sentry_summary'")[0][0]
        back = self.query(
            "SELECT des, event_type FROM sentry_events WHERE run_id = ? AND field IS NULL", (last_sentry,))
        self.assertEqual(set(back), {("2026 TL77", "reappeared"), ("2026 TS", "reappeared"), ("2026 TC94", "dropped")})
        scout_back = self.query("SELECT event_type FROM scout_events WHERE object_name = '8NIP003' ORDER BY run_id")
        self.assertEqual(scout_back, [("dropped",), ("reappeared",)])


if __name__ == "__main__":
    unittest.main()
