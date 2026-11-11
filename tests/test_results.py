# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Each job's Result: exit status, why there is none, the last lines of its output."""

import unittest

from udevmon import actions as A
from udevmon import config as C
from udevmon.engine import Job
from udevmon.events import MOUNTED, Endpoint, Event

EVENT = Event(MOUNTED, Endpoint("udisks", "k", {"mountpoint": "/m"}))


def job(run, name="a", **kw):
    return Job(C.Action(name, ("d",), (), ("mounted",), tuple(run), kw.pop("timeout", 10),
                        **kw), "d", None)


class ResultTests(unittest.TestCase):
    def run_jobs(self, *jobs, **kw):
        runner = A.ActionRunner(2, **kw)
        self.addCleanup(runner.shutdown, True)
        with self.assertLogs("udevmon.actions", "INFO"):
            return runner.submit(EVENT, list(jobs)).result(20)

    def test_the_last_lines_of_the_output_are_kept(self):
        script = 'for i in $(seq 1 15); do echo "line $i"; done; echo oops >&2; exit 3'
        (r,) = self.run_jobs(job(["sh", "-c", script]))
        self.assertEqual((r.action, r.status, r.failure, r.event, r.device),
                         ("a", 3, None, "mounted", "d"))
        self.assertEqual(len(r.output), A.OUTPUT_LINES)
        self.assertEqual(r.output[-2:], ("line 15", "oops"))        # stderr too
        self.assertGreater(r.time, 0)

    def test_a_long_line_is_cut(self):
        (r,) = self.run_jobs(job(["sh", "-c", "printf '%0500d\\n' 0"]))
        self.assertEqual(len(r.output[0]), A.OUTPUT_WIDTH)

    def test_why_there_is_no_status(self):
        timeout, missing, cannot = self.run_jobs(
            job(["sh", "-c", "echo started; sleep 5"], name="slow", timeout=0.3),
            job(["echo", "{fs_label}"], name="skip"),
            job(["/nonexistent/program"], name="gone"))
        self.assertEqual((timeout.status, timeout.failure, timeout.output),
                         (None, "timed out after 0.3 s", ("started",)))
        self.assertIn("unavailable", missing.failure)
        self.assertTrue(cannot.failure.startswith("could not start"))

    def test_the_last_attempt_counts(self):
        (r,) = self.run_jobs(job(["sh", "-c", "echo try; exit 1"], retries=1,
                                 retry_delay=0.01))
        self.assertEqual((r.status, r.attempts, r.output), (1, 2, ("try",)))

    def test_a_dry_run_and_a_detached_one(self):
        (r,) = self.run_jobs(job(["true"]), dry_run=True)
        self.assertEqual((r.status, r.failure), (None, "dry run"))
        (r,) = self.run_jobs(job(["true"], detach=True))
        self.assertEqual((r.status, r.failure, r.output), (None, "detached", ()))

    def test_every_result_is_reported(self):
        seen = []
        self.run_jobs(job(["true"], name="one"), job(["false"], name="two"),
                      on_result=seen.append)
        self.assertEqual([(r.action, r.status) for r in seen], [("one", 0), ("two", 1)])


if __name__ == "__main__":
    unittest.main()
