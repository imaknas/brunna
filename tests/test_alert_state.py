"""bin/alert.py's state files never decide whether an alert goes out: one that can't be written (a full disk) loses no alert
and repeats none, one that can't be read doesn't tell everything again, and nothing of them is ever cut.

    bin/py -m unittest tests.test_alert_state
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_scheduler_robust import DAY, MIN, fake_clock, on_full_disk  # noqa: E402

HOUR = 60 * MIN


class State(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("alert_state", ROOT / "bin" / "alert.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        r, self.w = os.pipe()
        self.r = os.fdopen(r, "rb", buffering=0)
        os.set_blocking(r, False)
        self.addCleanup(os.close, self.w)
        self.addCleanup(self.r.close)
        self.letters = []
        quiet = open(os.devnull, "w")
        self.addCleanup(quiet.close)
        for p in (mock.patch.object(self.m, "SENT", self.tmp / "alerts-sent.json"),
                  mock.patch.object(self.m, "FAILED", self.tmp / "alerts-failed.json"),
                  mock.patch.object(self.m, "LOG", self.tmp / "alerts-log.jsonl"),
                  mock.patch.dict(self.m.CHANNELS, {"c": (self.send, lambda: True)}, clear=True),
                  mock.patch.dict(self.m.KEYED, {"k": (lambda notice: self.send(f"{notice['state']}:{notice['key']}"),
                                                       lambda: True)}, clear=True),
                  mock.patch.object(self.m, "route", lambda key: "p" if key.startswith("job:") else "owner"),
                  mock.patch.object(self.m, "_hubbus", self.hubbus), mock.patch.object(sys, "stderr", quiet)):
            p.start()
            self.addCleanup(p.stop)

    def send(self, text):   # over a pipe: a forked child's sends are seen too
        os.write(self.w, text.splitlines()[0].encode() + b"\n")
        return True

    def hubbus(self, *args, out=None):
        os.write(self.w, " ".join(("hubbus", args[0], *(a for a in args if a.startswith("alert-")))).encode() + b"\n")
        return True

    def sent(self):
        return (self.r.read() or b"").decode().splitlines()

    def once(self, key, problems, now=DAY, **kw):
        with mock.patch.object(self.m.datetime, "datetime", fake_clock(now)):
            return self.m.once(key, problems, "ok", **kw)

    def files(self):
        return sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(self.m.MARK))

    def test_on_a_full_disk_an_alert_goes_out_once_and_is_remembered(self):
        self.once("old", ["x"])
        before = self.m.SENT.read_text()
        self.sent()
        out = on_full_disk(lambda: [self.once("k/1", ["broken"], DAY + m * MIN) for m in (0, 15, 30)])
        self.assertEqual((out, self.sent()), ("[True, True, True]", ["⚠️ broken", "open:k/1"]))
        self.assertEqual(self.m.SENT.read_text(), before, "not cut")
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.m.open_keys("k/"), ["k/1"], "known as open, so its end is told")
        out = on_full_disk(lambda: self.once("k/1", ["worse"], DAY + HOUR))
        self.assertEqual((out, self.sent()), ("True", ["⚠️ worse", "open:k/1"]), "a change is news")
        out = on_full_disk(lambda: self.once("k/1", ["worse"], DAY + 25 * HOUR))
        self.assertEqual(len(self.sent()), 2, "and its daily reminder")
        out = on_full_disk(lambda: [self.once("k/1", ["worse"], DAY + 26 * HOUR), self.once("old", ["x"], DAY + HOUR)])
        self.assertEqual((out, self.sent()), ("[True, True]", []))
        # space again: nothing is told again, and what stood for the record goes
        self.assertTrue(self.once("k/1", ["worse"], DAY + 27 * HOUR))
        self.assertEqual((self.sent(), self.files()), ([], []))
        self.assertEqual(json.loads(self.m.SENT.read_text())["k/1"]["problems"], ["worse"])

    def test_a_recovery_on_a_full_disk_is_told_once(self):
        self.once("k", ["broken"])
        self.sent()
        out = on_full_disk(lambda: [self.once("k", [], DAY + m * MIN) for m in (0, 15, 30)])
        self.assertEqual((out, self.sent()), ("[True, True, True]", ["resolved:k"]))
        self.assertEqual(self.m.open_keys(""), [])
        self.once("k", [], DAY + HOUR)
        self.assertEqual((self.sent(), self.files()), ([], []))
        self.assertEqual(json.loads(self.m.SENT.read_text())["k"]["problems"], [])

    def test_a_letter_sent_on_a_full_disk_is_the_one_taken_back(self):
        out = on_full_disk(lambda: [self.once("job:a", ["broken"], DAY + m * MIN, ids=["a"]) for m in (0, 15)])
        letter = f"alert-job-a-{DAY:%Y%m%d%H%M%S}"
        self.assertEqual((out, self.sent()), ("[True, True]", [f"hubbus send {letter}"]))
        self.once("job:a", ["broken"], DAY + 25 * HOUR, ids=["a"])
        self.assertEqual(len(self.sent()), 2, "a day unsettled: the owner is told")
        self.once("job:a", [], DAY + 26 * HOUR, ids=[])
        self.assertIn(f"hubbus done {letter}", self.sent())

    def test_a_failure_that_cannot_be_recorded_is_tried_again(self):
        with mock.patch.dict(self.m.CHANNELS, {"c": (lambda text: False, lambda: True)}):
            self.assertEqual(on_full_disk(lambda: self.once("k", ["broken"])), "False")
        self.sent()
        self.assertTrue(self.once("k", ["broken"], DAY + 15 * MIN))
        self.assertEqual(self.sent(), ["⚠️ broken", "open:k"])

    def test_an_unreadable_state_goes_on_from_its_copy(self):
        for text in ("", "{", "[]", "\x00"):
            for key in ("a", "job:b"):
                self.once(key, ["broken"])
            self.sent()
            self.m.SENT.write_text(text)
            self.m.LOG.unlink(missing_ok=True)   # the copy alone
            for key in ("a", "job:b"):
                self.assertTrue(self.once(key, ["broken"], DAY + MIN))
            self.assertEqual(self.sent(), [], text)

    def test_without_a_copy_what_the_log_says_was_told_is_not_told_again(self):
        for key in ("a", "b", "job:c", "gone"):
            self.once(key, [f"{key} broken"])
        self.once("gone", [], DAY + MIN)
        self.sent()
        self.m.SENT.write_text("")
        (self.tmp / "alerts-sent.copy.json").unlink()
        with (self.tmp / "alerts-log.jsonl").open("a") as f:
            f.write('cut\n[]\n{"key": "x"}\n')
        self.assertEqual(self.m.open_keys(""), ["a", "b", "job:c"])
        for key in ("a", "job:c"):
            self.assertTrue(self.once(key, [f"{key} broken"], DAY + 2 * MIN))
        self.assertEqual(self.sent(), [])
        self.once("job:c", [], DAY + 3 * MIN)
        self.assertEqual(self.sent(), [f"hubbus done alert-job-c-{DAY:%Y%m%d%H%M%S}"], "the letter it was is taken back")
        self.once("b", [], DAY + 2 * MIN)
        self.assertEqual(self.sent(), ["resolved:b"], "a key saved first doesn't make the others forgotten")
        self.once("a", ["a broken"], DAY + 24 * HOUR)
        self.assertEqual(len(self.sent()), 2, "its daily reminder goes on")
        self.once("a", ["a worse"], DAY + 24 * HOUR)
        self.assertEqual(len(self.sent()), 2, "a change is news again")

    def test_a_copy_of_another_save_is_not_gone_on_from(self):
        # a rollback's alert.py wrote SENT alone (b's end, c's start), and that was cut
        for key in ("a", "b"):
            self.once(key, ["broken"])
        self.once("b", [], DAY + MIN)
        copy = self.tmp / "alerts-sent.copy.json"
        copy.write_text(json.dumps({"b": {"problems": ["broken"], "at": DAY.isoformat()}}))
        old = copy.stat().st_mtime - 3600
        os.utime(copy, (old, old))
        self.m.SENT.write_text("{")
        self.sent()
        self.assertEqual(self.m.open_keys(""), ["a"], "what the log says, not the old copy")
        self.once("b", ["broken"], DAY + 2 * MIN)
        self.assertEqual(self.sent(), ["⚠️ broken", "open:b"], "news again")

    def test_a_marker_older_than_what_another_version_wrote_is_void(self):
        on_full_disk(lambda: self.once("k", ["broken"]))
        self.assertEqual(len(self.files()), 1)
        self.sent()
        # a rollback's alert.py, which doesn't know markers, told it again and then its end
        self.m.SENT.write_text(json.dumps({"k": {"problems": [], "at": (DAY + HOUR).isoformat()}}))
        self.once("k", ["broken"], DAY + 2 * HOUR)
        self.assertEqual(self.sent(), ["⚠️ broken", "open:k"], "not taken as told")
        self.assertEqual(self.files(), [])

    def test_an_escalated_alert_on_a_full_disk_stays_the_owners(self):
        self.once("job:a", ["broken"], ids=["a"])
        self.sent()
        out = on_full_disk(lambda: [self.once("job:a", ["broken"], DAY + 25 * HOUR + m * MIN, ids=["a"]) for m in (0, 15, 30)])
        self.assertEqual((out, len(self.sent())), ("[True, True, True]", 2), "the owner once")
        self.assertTrue(self.files()[0].endswith(".escalated"), self.files())
        out = on_full_disk(lambda: self.once("job:a", ["more"], DAY + 26 * HOUR, ids=["b"]))
        self.assertFalse([x for x in self.sent() if x.startswith("hubbus")], "no letter again")

    @unittest.skipIf(os.geteuid() == 0, "root writes everywhere")
    def test_a_void_marker_that_cannot_be_removed_loses_no_alert(self):
        on_full_disk(lambda: self.once("k", ["broken"]))
        self.m.SENT.write_text(json.dumps({"k": {"problems": [], "at": (DAY + HOUR).isoformat()}}))
        self.sent()
        for mode in (0o555, 0):
            self.tmp.chmod(mode)
            try:
                self.assertTrue(self.once("k", ["broken"], DAY + 2 * HOUR), oct(mode))
            finally:
                self.tmp.chmod(0o755)
            self.assertEqual(self.sent(), ["⚠️ broken", "open:k"])

    def test_a_letter_known_from_the_log_is_as_old_as_its_first_line(self):
        self.once("job:a", ["x"], ids=["x"])
        self.once("job:a", ["x", "y"], DAY + 20 * HOUR, ids=["x", "y"])   # an update in its thread
        self.m.SENT.write_text("")
        (self.tmp / "alerts-sent.copy.json").unlink()
        self.sent()
        self.once("job:a", ["x", "y"], DAY + 23 * HOUR, ids=["x", "y"])
        self.assertEqual(self.sent(), [])
        self.once("job:a", ["x", "y"], DAY + 25 * HOUR, ids=["x", "y"])
        self.assertEqual(len(self.sent()), 2, "a day unsettled since it was opened: the owner")

    def test_a_projects_letter_is_told_what_its_list_became_a_few_times(self):
        def jobs(names, minute):
            return self.once("job:p", [f"{', '.join(names)} failing"], DAY + minute * MIN, ids=["p"], names=names)
        texts = []
        with mock.patch.object(self.m, "_hubbus", lambda *a, out=None: texts.append(a) or True):
            jobs(["p/a"], 0), jobs(["p/a"], 15)
            self.assertEqual([a[a.index("--type") + 1] for a in texts], ["request"])
            jobs(["p/a", "p/b"], 30), jobs(["p/a", "p/b"], 45)
            jobs(["p/b"], 60)
            self.assertEqual([a[a.index("--type") + 1] for a in texts], ["request", "info", "info"])
            letter = texts[0][texts[0].index("--id") + 1]
            for a in texts[1:]:
                self.assertEqual(a[a.index("--reply-to") + 1], letter, "the same thread, and no run")
            grown, shrunk = (a[a.index("--text") + 1] for a in texts[1:])
            self.assertIn("p/b", grown)
            self.assertNotIn("p/a", shrunk.splitlines()[-1], "what is fixed isn't said to fail")
            self.assertIn("p/a", shrunk, "and is said to be gone")
            for i in range(50):
                jobs([f"p/{i}-{j}" + "x" * 300 for j in range(40)], 75 + i)   # renamed at every tick
            self.assertEqual(len(texts), 1 + self.m.MAX_UPDATES)
            self.assertLess(len(texts[-1][texts[-1].index("--text") + 1]) - len("failing") - 40 * 310, 3000)
        self.assertEqual([x for x in self.sent() if not x.startswith("hubbus")], [], "the owner isn't told of each")
        self.once("job:p", [f"{i}" for i in "x"], DAY + 25 * HOUR, ids=["p"], names=["p/z"])
        self.assertEqual(len([x for x in self.sent() if x.startswith("⚠️")]), 1, "a day unsettled, once")

    def test_a_file_that_is_no_marker_is_left_alone(self):
        for name in ("alerts-told.", "alerts-told.x", "alerts-told.a.b"):
            (self.tmp / name).write_text("")
        self.assertTrue(self.once("a", ["broken"]))
        self.assertEqual(self.sent(), ["⚠️ broken", "open:a"])
        self.assertEqual(self.m.open_keys(""), ["a"])
        self.assertEqual(len(self.files()), 3)

    def test_with_nothing_to_go_on_the_problems_are_told(self):
        self.m.SENT.write_text("")
        self.assertTrue(self.once("a", ["broken"]))
        self.assertEqual(self.sent(), ["⚠️ broken", "open:a"])

    def test_a_record_that_cannot_be_used_loses_no_alert(self):
        for rec in ("x", 5, None, {"problems": 5}, {"problems": ["broken"], "at": "soon"}, {"problems": [1]},
                    {"problems": ["z"], "ids": "z", "at": 5}, {"problems": ["z"], "at": "2026-10-30T12:00:00+08:00"}):
            self.m.SENT.write_text(json.dumps({"a": rec}))
            self.m.FAILED.write_text(json.dumps({"a": rec, "b": {"notice": {}}}))
            self.assertTrue(self.once("a", ["broken"], ids=["z"] if rec and isinstance(rec, dict) and "ids" in rec else None))
            self.assertTrue(self.sent(), rec)
            self.m.failures()

    def test_what_another_run_wrote_meanwhile_stays(self):
        def send(text):
            self.m.SENT.write_text(json.dumps({"other": {"problems": ["x"], "at": DAY.isoformat()}}))
            return True
        with mock.patch.dict(self.m.CHANNELS, {"c": (send, lambda: True)}):
            self.once("a", ["broken"])
        self.assertEqual(sorted(json.loads(self.m.SENT.read_text())), ["a", "other"])

    def test_saved_before_renamed(self):
        order = []
        fsync, replace = os.fsync, os.replace
        with mock.patch.object(os, "fsync", lambda fd: (order.append("fsync"), fsync(fd))[1]), \
                mock.patch.object(os, "replace", lambda a, b: (order.append("rename"), replace(a, b))[1]):
            self.once("a", ["broken"])
        self.assertEqual(order[:2], ["fsync", "rename"])

    def test_a_key_too_long_for_a_file_name(self):
        key = "scheduler-broken:" + "\u00e9" * 100
        on_full_disk(lambda: [self.once(key, ["broken"], DAY + m * MIN) for m in (0, 15)])
        self.assertEqual(len(self.sent()), 2, "told once")


class OneAlertNeverSilencesAnother(unittest.TestCase):
    def test_alert_each(self):
        from test_scheduler_robust import Sched
        import alert
        s, told = Sched(self), []

        def once(key, problems, ok, ids=None, names=None):
            if key.endswith("a"):
                raise RuntimeError("boom")
            told.append(key)
        with mock.patch.object(alert, "once", once), mock.patch.object(alert, "open_keys", lambda p: []), \
                mock.patch.object(alert, "failures", dict), mock.patch("builtins.print"):
            s.m.alert_broken({g: {"group": g, "why": "x"} for g in "ab"})
        self.assertEqual(told, ["scheduler-broken:b"])


if __name__ == "__main__":
    unittest.main()
