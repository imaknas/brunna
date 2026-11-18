"""A scheduled job that keeps failing raises an alert (10-27: contracts-check failed two nights running, only the
status page was red, nobody knew).

Failure → recorded in scheduler-failing.json (the start time kept); success → removed; only a failure lasting longer
than ALERT_AFTER goes on the alert list (one that the next tick's retry fixes makes no noise); the alert wording for
the same job must stay stable (alert.once deduplicates by wording).

    bin/py -m unittest discover -s tests
"""
import datetime
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


def load():
    spec = importlib.util.spec_from_file_location("scheduler_al", ROOT / "bin" / "scheduler.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FailingJobs(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.m.FAILING_FILE = pathlib.Path(tempfile.mkdtemp()) / "failing.json"
        self.addCleanup(shutil.rmtree, self.m.FAILING_FILE.parent, ignore_errors=True)

    def failing(self):
        return json.loads(self.m.FAILING_FILE.read_text()) if self.m.FAILING_FILE.exists() else {}

    def test_failure_is_recorded_and_success_clears_it(self):
        self.m.mark_job("agent-hub/contracts-check", True, "exit 1")
        first = self.failing()["agent-hub/contracts-check"]["since"]
        self.m.mark_job("agent-hub/contracts-check", True, "exit 1")   # fails once more: start time unchanged
        self.assertEqual(self.failing()["agent-hub/contracts-check"]["since"], first)
        self.m.mark_job("agent-hub/contracts-check", False)
        self.assertEqual(self.failing(), {})

    def test_only_long_failures_are_alerted_and_wording_is_stable(self):
        now = datetime.datetime(2026, 10, 27, 9, 0)
        failing = {"a/old": {"since": "2026-10-27T01:30:00", "detail": "exit 1"},
                   "b/new": {"since": "2026-10-27T08:50:00", "detail": "exit 2"}}
        probs = self.m.failing_problems(failing, now)
        self.assertEqual(len(probs), 1)
        self.assertIn("a/old", probs[0])
        self.assertEqual(probs, self.m.failing_problems(failing, now + datetime.timedelta(hours=3))[:1])

    def test_one_alert_per_job_a_changing_detail_is_not_new_and_each_recovers_alone(self):
        """11-18: one key for every failing job sent the whole list again whenever one line changed (a job's line names
        its log, a new file each day), so one failure came back as new for two weeks."""
        import alert
        tmp = self.m.FAILING_FILE.parent
        for attr, name in (("SENT", "sent.json"), ("FAILED", "failed.json"), ("LOG", "log.jsonl")):
            orig = getattr(alert, attr)
            self.addCleanup(setattr, alert, attr, orig)
            setattr(alert, attr, tmp / name)
        sent = []
        orig_send = alert.send
        self.addCleanup(setattr, alert, "send", orig_send)
        alert.send = lambda text, notice=None, plain=True: sent.append((notice["key"], notice["state"], plain)) or True
        old = "2020-01-01T00:00:00"
        self.m.FAILING_FILE.write_text(json.dumps({"p/a": {"since": old, "detail": "log a-11-08"},
                                                   "p/b": {"since": old, "detail": "log b-11-08"}}))
        self.m.alert_failing()
        self.assertEqual(sorted(k for k, _s, _p in sent), ["scheduler-job:p/a", "scheduler-job:p/b"])
        sent.clear()
        self.m.FAILING_FILE.write_text(json.dumps({"p/a": {"since": old, "detail": "log a-11-09"},
                                                   "p/b": {"since": old, "detail": "log b-11-09"}}))
        self.m.alert_failing()   # the next day: the same failures, new log names
        self.assertEqual(sent, [])
        self.m.FAILING_FILE.write_text(json.dumps({"p/b": {"since": old, "detail": "log b-11-09"}}))
        self.m.alert_failing()   # p/a works again: its recovery only, kept off the plain channels
        self.assertEqual(sent, [("scheduler-job:p/a", "resolved", False)])
        self.assertEqual([json.loads(l)["key"] for l in (tmp / "log.jsonl").read_text().splitlines()],
                         ["scheduler-job:p/a", "scheduler-job:p/b", "scheduler-job:p/a"])

    def test_a_broken_alert_does_not_stop_the_scheduler(self):
        import alert
        orig = alert.once
        self.addCleanup(setattr, alert, "once", orig)
        alert.once = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("discord down"))
        self.m.FAILING_FILE.write_text(json.dumps({"x/y": {"since": "2020-01-01T00:00:00", "detail": "exit 1"}}))
        self.m.log = lambda s: None
        self.m.alert_failing()

if __name__ == "__main__":
    unittest.main()
