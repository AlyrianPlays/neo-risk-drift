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
loses one object and gains another. day2/sentry_details holds real per-object
responses for the four day2 Sentry objects (Bennu's 157 virtual impactors are
cut to 3).
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
        day2 = os.path.join(FIXTURES, "day2")
        self.run_day("day1")
        self.assertIn("sentry_removed: run 5, 2 rows stored", self.run_snapshot(day2, "--details-limit", "0").stdout)
        self.assertIn("sentry_removed: run 8, 0 rows stored", self.run_snapshot(day2, "--details-limit", "0").stdout)

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

    def day2_copy(self):
        copy = os.path.join(self.tmp, "day2")
        shutil.copytree(os.path.join(FIXTURES, "day2"), copy)
        return copy

    def details_rows(self):
        return self.query("SELECT des FROM sentry_details ORDER BY run_id")

    def test_details_fetched_once_for_new_objects(self):
        self.run_day("day1")
        result = self.run_day("day2")
        self.assertIn("sentry_details: 4 stored", result.stdout)
        # Newest observations first, so the backfill starts with active objects
        self.assertEqual(self.details_rows()[0], ("2026 TC94",))
        bennu = self.query("SELECT method, arc_days, n_del, first_obs FROM sentry_details WHERE des = '101955'")
        self.assertEqual(bennu, [("MC", 7693.3, 58, "1999-09-11.40624")])
        self.run_day("day2")
        self.assertEqual(len(self.details_rows()), 4, "nothing changed, so nothing refetched")

    def test_refit_triggers_new_details(self):
        day2 = self.day2_copy()
        self.run_snapshot(day2)
        path = os.path.join(day2, "sentry_summary.json")
        with open(path, encoding="ascii") as f:
            payload = json.load(f)
        for row in payload["data"]:
            if row["des"] == "2026 RH79":
                row["ip"] = "6e-07"
        with open(path, "w", encoding="ascii") as f:
            json.dump(payload, f)
        result = self.run_snapshot(day2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sentry_details: 1 stored", result.stdout)
        self.assertEqual(self.details_rows()[-1], ("2026 RH79",))

    def test_details_limit(self):
        result = self.run_snapshot(os.path.join(FIXTURES, "day2"), "--details-limit", "2")
        self.assertIn("sentry_details: 2 stored, 0 without details, 0 failed, 2 still to fetch", result.stdout)
        result = self.run_snapshot(os.path.join(FIXTURES, "day2"), "--details-limit", "0")
        self.assertNotIn("sentry_details", result.stdout)
        self.assertEqual(len(self.details_rows()), 2)

    def write_details(self, day2, des, payload):
        with open(os.path.join(day2, "sentry_details", des + ".json"), "w", encoding="ascii") as f:
            json.dump(payload, f)

    def test_no_details_answer_is_stored_and_not_asked_again(self):
        day2 = self.day2_copy()
        self.write_details(day2, "2026 TC94", {"error": "specified object removed", "removed": "2026-10-16 15:00:00"})
        self.write_details(day2, "2010 UE", {"error": "specified object not found"})
        result = self.run_snapshot(day2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sentry_details: 2 stored, 2 without details, 0 failed, 0 still to fetch", result.stdout)
        self.assertIn("sentry_details: 2010 UE: specified object not found", result.stdout)
        errors = self.query("SELECT des, error FROM sentry_details WHERE error IS NOT NULL ORDER BY des")
        self.assertEqual(errors, [("2010 UE", "specified object not found"), ("2026 TC94", "specified object removed")])
        result = self.run_snapshot(day2)
        self.assertIn("sentry_details: 0 stored, 0 without details, 0 failed, 0 still to fetch", result.stdout)

    def test_other_jpl_error_is_a_failure_and_retried(self):
        day2 = self.day2_copy()
        self.write_details(day2, "2026 TC94", {"error": "service temporarily unavailable"})
        result = self.run_snapshot(day2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("2026 TC94 FAILED - service temporarily unavailable", result.stdout)
        self.assertIn("1 failed, 1 still to fetch", result.stdout)

    def test_skipped_sentry_run_does_not_stall_the_details(self):
        self.run_snapshot(os.path.join(FIXTURES, "day1"))
        day2 = self.day2_copy()
        path = os.path.join(day2, "sentry_summary.json")
        with open(path, "w", encoding="ascii") as f:
            json.dump({"count": "0", "data": []}, f)
        # Day 1's five objects are still the current list. Three have saved
        # responses in day2; 2026 TL77 and 2026 TS don't, so they fail
        result = self.run_snapshot(day2)
        self.assertIn("WARNING run 4 skipped", result.stdout)
        self.assertIn("sentry_details: 3 stored, 0 without details, 2 failed, 2 still to fetch", result.stdout)

    def test_object_fetched_by_another_run_meanwhile_is_skipped(self):
        day2 = os.path.join(FIXTURES, "day2")
        self.run_snapshot(day2, "--details-limit", "0")
        sys.path.insert(0, os.path.dirname(SNAPSHOT))
        import snapshot
        conn = sqlite3.connect(self.db)
        lines = []
        saved = snapshot.details_wanted
        try:
            # As if a parallel run stored 2026 TC94 after this one picked its list
            conn.execute("INSERT INTO sentry_details (run_id, des) VALUES (999, '2026 TC94')")
            conn.commit()
            snapshot.details_wanted = lambda c: ["2026 TC94", "2026 RH79"]
            snapshot.fetch_sentry_details(conn, day2, 10, "2026-10-16T00:00:00+00:00", lines.append)
        finally:
            snapshot.details_wanted = saved
            conn.close()
        self.assertIn("sentry_details: 1 stored, 0 without details, 0 failed, 0 still to fetch", lines)
        self.assertEqual(self.details_rows(), [("2026 RH79",), ("2026 TC94",)])

    def test_details_stop_when_out_of_time(self):
        day2 = os.path.join(FIXTURES, "day2")
        self.run_snapshot(day2, "--details-limit", "0")
        sys.path.insert(0, os.path.dirname(SNAPSHOT))
        import snapshot
        saved = snapshot.DETAILS_MAX_SECONDS
        snapshot.DETAILS_MAX_SECONDS = -1  # already over budget before the first request
        conn = sqlite3.connect(self.db)
        lines = []
        try:
            snapshot.fetch_sentry_details(conn, day2, 10, "2026-10-16T00:00:00+00:00", lines.append)
        finally:
            snapshot.DETAILS_MAX_SECONDS = saved
            conn.close()
        self.assertIn("sentry_details: out of time, will continue next run", lines)
        self.assertEqual(self.details_rows(), [])

    def test_details_stop_after_repeated_failures(self):
        day2 = self.day2_copy()
        details = os.path.join(day2, "sentry_details")
        for name in os.listdir(details):
            with open(os.path.join(details, name), "w", encoding="ascii") as f:
                f.write("{not json")
        result = self.run_snapshot(day2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("stopping after repeated failures", result.stdout)
        self.assertEqual(result.stdout.count("FAILED"), 3)
        # The feeds themselves still landed
        self.assertEqual(self.query("SELECT COUNT(*) FROM sentry_snapshot")[0][0], 4)


if __name__ == "__main__":
    unittest.main()
