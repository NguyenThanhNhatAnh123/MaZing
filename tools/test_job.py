"""Offline regression checks for bounded job logs and independent job cursors."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import dashboard


class JobLogTests(unittest.TestCase):
    def test_cursor_continues_after_retained_log_reaches_its_limit(self):
        job = dashboard.Job()
        for index in range(2000):
            job.say(f"event {index}")
        previous = job.as_json()
        job.say("new event after the buffer limit")
        current = job.as_json(previous["total_lines"], previous["job_id"])
        self.assertEqual(current["total_lines"], 2001)
        self.assertEqual(current["first_line"], 1)
        self.assertEqual(len(current["lines"]), 1)
        self.assertIn("new event after the buffer limit", current["lines"][0])
        self.assertEqual(job.as_json(current["total_lines"], current["job_id"])["lines"], [])

    def test_client_that_misses_the_window_gets_all_retained_events(self):
        job = dashboard.Job()
        for index in range(2500):
            job.say(f"event {index}")
        current = job.as_json(10, job.id)
        self.assertEqual((current["first_line"], current["total_lines"]), (500, 2500))
        self.assertEqual(len(current["lines"]), 2000)
        self.assertTrue(current["lines"][0].endswith("event 500"))
        self.assertTrue(current["lines"][-1].endswith("event 2499"))

    def test_multiline_message_advances_by_actual_number_of_lines(self):
        job = dashboard.Job()
        job.say("first\nsecond\nthird")
        self.assertEqual(job.as_json()["total_lines"], 3)
        self.assertEqual(len(job.as_json(1)["lines"]), 2)

    def start_and_finish(self, job, count):
        def run():
            for index in range(count):
                job.say(f"new job event {index}")
        # Run the captured worker inline; no browser, shop, or real data is touched.
        with patch.object(dashboard.threading, "Thread") as worker:
            self.assertTrue(job.start("test", run))
        worker.call_args.kwargs["target"]()
        self.assertFalse(job.running)

    def test_replacement_job_resets_old_cursor_even_with_same_or_more_lines(self):
        with tempfile.TemporaryDirectory(prefix="mayzing_job_test_") as folder, \
             patch.object(config, "DRAIN_FILE", Path(folder) / "drain"), \
             patch.object(dashboard.time, "time", return_value=123.0):
            for new_count in (4, 8):
                with self.subTest(new_count=new_count):
                    job = dashboard.Job()
                    self.start_and_finish(job, 4)
                    previous = job.as_json()
                    self.start_and_finish(job, new_count)
                    current = job.as_json(previous["total_lines"], previous["job_id"])
                    self.assertNotEqual(current["job_id"], previous["job_id"])
                    self.assertEqual(current["started"], previous["started"])
                    self.assertEqual(len(current["lines"]), new_count + 1)
                    self.assertIn("new job event 0", current["lines"][0])
                    self.assertIn("kết thúc", current["lines"][-1])

    def test_http_endpoint_resets_a_cursor_belonging_to_another_job(self):
        job = dashboard.Job()
        job.say("first event")
        with patch.object(dashboard, "JOB", job):
            response = dashboard.app.test_client().get("/api/job?since=100&job_id=old-job")
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["job_id"], job.id)
        self.assertEqual(len(data["lines"]), 1)
        self.assertIn("first event", data["lines"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
