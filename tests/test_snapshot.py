"""Black-box checks for snapshot.py using saved JPL responses as fixtures.

Each test runs snapshot.py in a subprocess against a throwaway database in a
temp folder, the same way the scheduled task runs it, so the real
neo_risk.db is never touched.

Run from the project folder:
    .venv\\Scripts\\python.exe -m unittest discover tests -v

The fixtures are trimmed copies of real responses. day1 is 2026-10-15 and day2
is 2026-10-16 (runs 28-33). Between them Sentry adds 2026 TC94 and drops
2026 TL77 and 2026 TS (both show up in the removed list), 2026 RH79 gets a new
orbit fit, 2010 UE only has its Palermo values re-scored, and the Scout list
loses one object and gains another.
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
SNAPSHOT = os.path.join(os.path.dirname(HERE), "snapshot.py")
FIXTURES = os.path.join(HERE, "fixtures")
SOURCES = ("sentry_summary", "sentry_removed", "scout_summary")
SNAPSHOT_TABLES = {"sentry_summary": "sentry_snapshot", "scout_summary": "scout_snapshot"}


def fixture(day, source):
    with open(os.path.join(FIXTURES, day, source + ".json"), encoding="ascii") as f:
        return json.load(f)


class SnapshotTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_snapshot(self, from_dir, *extra):
        cmd = [sys.executable, SNAPSHOT, "--db", self.db, "--from-dir", from_dir] + list(extra)
        return subprocess.run(cmd, capture_output=True, text=True)

    def run_day(self, day):
        result = self.run_snapshot(os.path.join(FIXTURES, day))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def query(self, sql, params=()):
        # Close every connection so Windows can delete the temp folder afterwards
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def broken_copy(self, day, source, text):
        broken = os.path.join(self.tmp, "broken")
        shutil.copytree(os.path.join(FIXTURES, day), broken)
        with open(os.path.join(broken, source + ".json"), "w", encoding="ascii") as f:
            f.write(text)
        return broken

    def test_every_feed_lands_in_one_run(self):
        self.run_day("day1")
        runs = self.query("SELECT run_id, source, fetched_at, record_count FROM runs")
        self.assertEqual(sorted(r[1] for r in runs), sorted(SOURCES))
        self.assertEqual(len({r[2] for r in runs}), 1, "feeds in one run share fetched_at")
        for run_id, source, _, record_count in runs:
            self.assertEqual(record_count, len(fixture("day1", source)["data"]), source)
            if source in SNAPSHOT_TABLES:
                rows = self.query("SELECT COUNT(*) FROM %s WHERE run_id = ?" % SNAPSHOT_TABLES[source], (run_id,))
                self.assertEqual(rows[0][0], record_count, source)
        self.assertEqual(self.query("SELECT COUNT(*) FROM sentry_removed")[0][0], 2)

    def test_raw_layer_is_verbatim(self):
        self.run_day("day1")
        for source, raw in self.query("SELECT source, raw_json FROM runs"):
            self.assertEqual(json.loads(raw), fixture("day1", source), source)

    def test_numbers_are_typed_and_nulls_kept(self):
        self.run_day("day1")
        bennu = self.query("SELECT typeof(ip), typeof(n_imp), ts_max FROM sentry_snapshot WHERE des = '101955'")
        self.assertEqual(bennu, [("real", "integer", None)])
        ue = self.query("SELECT ps_cum, ts_max, typeof(ts_max) FROM sentry_snapshot WHERE des = '2010 UE'")
        self.assertEqual(ue, [(-7.97, 0, "integer")])
        scout = self.query("SELECT typeof(neo_score), typeof(arc_hours) FROM scout_snapshot WHERE object_name = 'P12rhre'")
        self.assertEqual(scout, [("integer", "real")])

    def test_removed_list_is_cumulative(self):
        self.run_day("day1")
        self.run_day("day2")
        self.run_day("day2")
        self.assertEqual(self.query("SELECT COUNT(*) FROM sentry_removed")[0][0], 4)
        first_day2_run = self.query(
            "SELECT MIN(run_id) FROM runs WHERE source = 'sentry_removed' AND run_id > "
            "(SELECT MIN(run_id) FROM runs WHERE source = 'sentry_removed')")[0][0]
        seen = self.query("SELECT first_seen_run FROM sentry_removed WHERE des = '2026 TS'")
        self.assertEqual(seen, [(first_day2_run,)])

    def test_removed_logs_only_new_rows(self):
        self.run_day("day1")
        self.assertIn("sentry_removed: run 5, 2 rows stored", self.run_day("day2").stdout)
        self.assertIn("sentry_removed: run 8, 0 rows stored", self.run_day("day2").stdout)

    def test_one_bad_feed_does_not_sink_the_others(self):
        result = self.run_snapshot(self.broken_copy("day1", "scout_summary", "{not json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED", result.stdout + result.stderr)
        sources = sorted(r[0] for r in self.query("SELECT source FROM runs"))
        self.assertEqual(sources, ["sentry_removed", "sentry_summary"])

    def test_failed_feed_rolls_back_its_raw_row(self):
        # Valid JSON, but a record with no objectName fails inside the storer,
        # after the raw row was written, so the whole feed must roll back
        payload = fixture("day1", "scout_summary")
        del payload["data"][0]["objectName"]
        result = self.run_snapshot(self.broken_copy("day1", "scout_summary", json.dumps(payload)))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM runs WHERE source = 'scout_summary'")[0][0], 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM scout_snapshot")[0][0], 0)

    def test_log_option_appends_to_file(self):
        log_path = os.path.join(self.tmp, "snapshot.log")
        for _ in range(2):
            result = self.run_snapshot(os.path.join(FIXTURES, "day1"), "--log", log_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(log_path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("sentry_summary: run"), 2)

    def test_change_detection_runs_after_the_feeds(self):
        self.run_day("day1")
        result = self.run_day("day2")
        self.assertIn("sentry_summary events: run 4 vs run 1", result.stdout)
        self.assertEqual(self.query("SELECT COUNT(*) FROM detected_runs")[0][0], 4)
        self.assertGreater(self.query("SELECT COUNT(*) FROM sentry_events")[0][0], 0)

    def test_broken_detector_cannot_cost_a_snapshot(self):
        # A copy of snapshot.py beside a detect_changes.py that won't even import
        project = os.path.join(self.tmp, "project")
        os.mkdir(project)
        shutil.copy(SNAPSHOT, project)
        with open(os.path.join(project, "detect_changes.py"), "w", encoding="ascii") as f:
            f.write("this is not python\n")
        cmd = [sys.executable, os.path.join(project, "snapshot.py"),
               "--db", self.db, "--from-dir", os.path.join(FIXTURES, "day1")]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("change detection: FAILED", result.stdout)
        self.assertEqual(sorted(r[0] for r in self.query("SELECT source FROM runs")), sorted(SOURCES))


if __name__ == "__main__":
    unittest.main()
