"""The scheduler never stops while everything reports healthy: a tick that doesn't complete is told (the record of completed
ticks, the watchdog, the status page), and one broken thing (the state file, schedule.yaml, one job's fields, a project's
before:, the watchdog's own state) stops only what it concerns.

Every clock is a fixed one; no test sleeps. A full disk is a file size limit of 0 in a forked child: opening a file still
truncates it and every write to it fails, as on a full disk.

    bin/py -m unittest tests.test_scheduler_robust
"""
import contextlib
import datetime
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import resource
import shutil
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import alert  # noqa: E402
import hubconf  # noqa: E402
import registry  # noqa: E402
from test_scheduler_tick import Tick  # noqa: E402

DAY = datetime.datetime(2026, 10, 30, 12, 0)   # a Friday
TODAY = "2026-10-30"
MIN = datetime.timedelta(minutes=1)


def fake_clock(now):
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return now() if callable(now) else now
    return FakeDT


def local(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text).astimezone().replace(tzinfo=None)


def stamp(path: pathlib.Path, when: datetime.datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def tag(name: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"refs/tags/{name}"],
                          capture_output=True).returncode == 0


@contextlib.contextmanager
def zone(name: str):
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


def on_full_disk(fn) -> str:
    """fn run in a child whose every write to a file fails; what it returned, or the name of what it raised."""
    r, w = os.pipe()
    pid = os.fork()
    if not pid:
        try:
            os.close(r)
            os.dup2(os.open(os.devnull, os.O_WRONLY), 2)   # what it says there isn't the test's output
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
            try:
                out = repr(fn())
            except BaseException as e:
                out = type(e).__name__
            os.write(w, out.encode())
        finally:
            os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        out = f.read().decode()
    os.waitpid(pid, 0)
    return out


class Sched(Tick):
    """Tick, with the real all_jobs (schedule.yaml's text and a registry index), the alerts recorded, and the files of
    the scheduler and the watchdog in one hub folder."""

    def __init__(self, test: unittest.TestCase):
        super().__init__(test)
        self.st = self.m.STATE_FILE.parent
        (self.tmp / "logs").mkdir(exist_ok=True)
        self.m.STATE_FILE = self.st / "scheduler-last-run.json"
        self.m.HEARTBEAT = self.st / "scheduler-heartbeat"
        self.m.SCHEDULE = self.tmp / "schedule.yaml"
        self.alerts, self.ids = [], {}

    def bounded(self, command, job_log, minutes, env=None):
        """a registry job's command is "cd … && exit 1" """
        self.ran.append({"command": command, "env": env, "minutes": minutes})
        last = command.rsplit("&& ", 1)[-1]
        return int(last.split()[1]) if last.startswith("exit ") else 0

    def main(self, now, jobs=None, schedule="", index=None, refresh=None, **patches):
        if schedule:
            self.m.SCHEDULE.write_text(schedule)
        info = {"alias": {}, "renames": {}, "skipped": [], "errors": {}}
        fake_sp = types.SimpleNamespace(run=self.run, CalledProcessError=subprocess.CalledProcessError,
                                        STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired)
        ps = [mock.patch.object(self.m, "subprocess", fake_sp),
              mock.patch.object(self.m.datetime, "datetime", fake_clock(now)),
              mock.patch.object(hubconf, "projects", lambda: {**(index or {}).get("projects", {})}),
              mock.patch.object(hubconf, "worktree_lookup", lambda: None),
              mock.patch.object(hubconf, "login_shell", lambda: "/bin/sh"),
              mock.patch.object(self.m, "spawn_background", lambda *a: self.spawned.append(a)),
              mock.patch.object(self.m, "run_bounded", self.bounded),
              mock.patch.object(registry, "rules_refused", lambda index: {}),
              mock.patch.object(registry, "refresh", refresh or (lambda: index or {"projects": {}, "modules": {},
                                                                                   "errors": {}})),
              mock.patch.object(alert, "once", lambda key, problems, ok, ids=None, names=None: (
                  self.alerts.append((key, problems)), self.ids.__setitem__(key, ids))),
              mock.patch.object(alert, "open_keys", lambda p: []), mock.patch.object(alert, "failures", dict),
              mock.patch("builtins.print")]
        if jobs is not None:
            ps.append(mock.patch.object(self.m, "all_jobs", lambda log_errors=False: (jobs, info)))
        ps += [mock.patch.object(self.m, k, v, create=True) for k, v in patches.items()]
        with contextlib.ExitStack() as stack:
            for p in ps:
                stack.enter_context(p)
            self.m.main()

    def log(self):
        f = self.m.LOG_DIR / "scheduler.log"
        return f.read_text() if f.exists() else ""

    def told(self):
        return [k for k, problems in self.alerts if problems]

    def record(self):
        return json.loads((self.st / "scheduler-tick.json").read_text())


def job(name, **kw):
    return {"name": name, "time": "06:00", "command": f"echo {name}", **kw}


def boom(*a, **k):
    raise RuntimeError("boom")


class CompletedTicks(unittest.TestCase):
    def setUp(self):
        self.s = Sched(self)

    def test_a_crashing_tick_is_recorded_and_a_completed_one_clears_it(self):
        for i in range(3):
            with self.assertRaises(RuntimeError):
                self.s.main(DAY + 15 * i * MIN, jobs=[], reconcile=boom)
        rec = self.s.record()
        self.assertEqual((rec["unfinished"], local(rec["since"]), rec["running"]), (3, DAY, False))
        self.assertIn("boom", rec["error"])
        self.assertIn("did not complete", self.s.log())
        self.s.main(DAY + 45 * MIN, jobs=[])
        rec = self.s.record()
        self.assertEqual((rec["unfinished"], local(rec["completed"]), rec["running"]), (0, DAY + 45 * MIN, False))

    def test_the_status_page_is_made_after_a_tick_that_crashed_too(self):
        made = []
        with self.assertRaises(RuntimeError):
            self.s.main(DAY, jobs=[], reconcile=boom, status_page=lambda: made.append(1))
        self.assertEqual(made, [1])

    def test_a_tick_that_was_killed_counts_when_the_next_one_begins(self):
        killed = mock.Mock(side_effect=KeyboardInterrupt)   # as a kill: nothing after it in the tick runs
        for i in range(3):
            with self.assertRaises(KeyboardInterrupt):
                self.s.main(DAY + 15 * i * MIN, jobs=[], reconcile=killed)
        self.assertEqual((self.s.record()["unfinished"], self.s.record()["running"]), (2, True))

    def test_a_long_tick_is_one_running_tick(self):
        seen = []

        def slow(command, job_log, minutes, env=None):
            seen.append(self.s.record())
            return 0
        self.s.main(DAY, jobs=[job("a"), job("b")], run_bounded=slow)
        self.assertEqual([(r["running"], r.get("unfinished", 0)) for r in seen], [(True, 0)] * 2)

    def test_the_rule_for_doctor_and_the_status_page(self):
        import tick_record as t
        for rec, n in (({}, None), ({"unfinished": 0, "running": True}, None), ({"unfinished": "x"}, None),
                       ({"unfinished": 1, "running": True}, None),   # a reboot, and the first tick after it
                       ({"unfinished": 1, "running": False}, 1), ({"unfinished": 2, "running": True}, 2)):
            got = t.not_completing(rec)
            self.assertEqual(got and got["n"], n, rec)

    def test_glance_says_it_is_not_alive(self):
        import glance
        (self.s.st / "scheduler-heartbeat").write_text("")
        stamp(self.s.st / "scheduler-heartbeat", DAY)
        with mock.patch.object(glance, "HEARTBEAT", self.s.st / "scheduler-heartbeat"):
            self.assertTrue(glance.scheduler(DAY)["alive"])
            (self.s.st / "scheduler-tick.json").write_text(json.dumps({"unfinished": 2, "running": False}))
            self.assertEqual(glance.scheduler(DAY), {"last_tick": None, "alive": False})


class State(unittest.TestCase):
    def setUp(self):
        self.s = Sched(self)
        self.jobs = [job("a"), job("b", background=True), {"name": "i", "every_minutes": 60, "command": "echo i"},
                     job("later", time="13:00"),
                     job("week", days_of_week=["mon"], catch_up="period")]

    def ran(self):
        return self.s.commands() + [a[0] for a in self.s.spawned]

    def break_state(self, text="", age=MIN):
        self.s.m.STATE_FILE.write_text(text)
        stamp(self.s.m.STATE_FILE, datetime.datetime.now() - age)

    def test_unreadable_state_goes_on_from_the_copy(self):
        for text in ("", '{"a": "2026-', "[1]", "\x00\x00"):
            with self.subTest(text=text):
                s = self.s = Sched(self)
                s.main(DAY, jobs=self.jobs)
                s.m.STATE_FILE.write_text(json.dumps({**s.state(), "b": TODAY}))   # the background run ended well
                s.m.save_state(s.state())
                s.ran.clear(), s.spawned.clear()
                self.break_state(text)
                s.main(DAY + 15 * MIN, jobs=self.jobs)
                self.assertEqual(self.ran(), [], "nothing runs again")
                self.assertEqual(s.state()["a"], TODAY)
                aside = list(s.st.glob("scheduler-last-run.broken-*.json"))
                self.assertEqual([p.read_text() for p in aside], [text])
                self.assertEqual([k for k in s.told() if k.startswith("scheduler-state:")],
                                 [f"scheduler-state:{aside[0].name}"])
                s.main(DAY.replace(hour=13, minute=20), jobs=self.jobs)
                self.assertEqual(self.ran(), ["echo i", "echo later"], "what is due later runs")

    def test_without_a_copy_the_jobs_due_now_count_as_run(self):
        self.break_state()
        self.s.main(DAY, jobs=self.jobs)
        self.assertEqual(self.ran(), [])
        self.s.main(DAY.replace(hour=13), jobs=self.jobs)
        self.assertEqual(self.ran(), ["echo i", "echo later"])
        self.s.main(DAY.replace(day=31, hour=7), jobs=self.jobs)
        self.assertEqual(sorted(self.ran()), sorted(["echo i", "echo later", "echo a", "echo i", "b"]))

    def test_a_copy_older_than_the_state_does_not_bring_back_old_days(self):
        self.s.main(DAY - datetime.timedelta(days=3), jobs=self.jobs)
        self.s.ran.clear(), self.s.spawned.clear()
        stamp(self.s.st / "scheduler-last-run.copy.json", datetime.datetime.now() - datetime.timedelta(days=3))
        self.break_state()
        self.s.main(DAY, jobs=self.jobs)
        self.assertEqual(self.ran(), [])

    def test_a_state_that_vanished_is_not_a_fresh_install(self):
        self.s.main(DAY, jobs=[job("a")])
        self.s.m.STATE_FILE.unlink()
        # the copy was saved now by the real clock; the tick's clock is in the past, so it isn't old
        self.s.main(DAY + 15 * MIN, jobs=[job("a")])
        self.assertEqual(self.s.commands(), ["echo a"])

    def test_a_fresh_install_runs_what_is_due(self):
        self.s.main(DAY, jobs=[job("a")])
        self.assertEqual(self.s.commands(), ["echo a"])
        self.assertEqual(self.s.told(), [])

    def test_a_tick_that_dies_while_replacing_it_finds_it_again(self):
        self.break_state()
        with self.assertRaises(Exception):
            self.s.main(DAY, jobs=self.jobs, save_state=boom)
        self.s.main(DAY + 15 * MIN, jobs=self.jobs)
        self.assertEqual(self.ran(), [], "not a fresh install")

    def broken(self):
        return sorted(p.name for p in self.s.st.glob("scheduler-last-run.broken-*"))

    def test_one_broken_state_is_one_file_and_one_alert(self):
        self.break_state()
        real = self.s.m.tick_record.save

        def full(path, text):
            if path.name.startswith("scheduler-last-run"):
                raise OSError(28, "No space left on device")
            real(path, text)
        with mock.patch.object(self.s.m.tick_record, "save", full):
            for i in range(6):
                with self.assertRaises(self.s.m.StateNotSaved):
                    self.s.main(DAY + 15 * i * MIN, jobs=self.jobs)
        self.assertEqual((self.broken(), self.s.m.STATE_FILE.read_text()), ([], ""), "still broken, still there")
        for i in range(6, 10):
            self.s.main(DAY + 15 * i * MIN, jobs=self.jobs)
        self.assertEqual(len(self.broken()), 1)
        self.assertEqual({k for k in self.s.told() if k.startswith("scheduler-state:")},
                         {f"scheduler-state:{self.broken()[0]}"})

    def test_it_is_said_for_a_day_and_not_as_a_daily_reminder(self):
        self.break_state()
        self.s.main(DAY, jobs=[])
        for hours, said in ((1, True), (21, True), (23, False), (48, False)):   # alert.REMIND is 23 hours
            self.assertEqual(bool(self.s.m.broken_state_lines(DAY + 60 * hours * MIN)), said, hours)
        (self.s.st / "scheduler-last-run.broken-x.json").write_text("")   # a file someone put there
        self.assertEqual(self.s.m.broken_state_lines(DAY + 48 * 60 * MIN), {})

    @unittest.skipIf(os.geteuid() == 0, "root reads every file")
    def test_files_that_may_not_be_read_are_unreadable(self):
        self.s.main(DAY, jobs=self.jobs)
        self.s.m.STATE_FILE.write_text(json.dumps({**self.s.state(), "b": TODAY}))
        self.s.m.save_state(self.s.state())
        self.s.ran.clear(), self.s.spawned.clear()
        self.s.m.STATE_FILE.chmod(0)
        (self.s.m.RUNS_DIR / "x.json").write_text("{}")
        (self.s.m.RUNS_DIR / "x.json").chmod(0)
        self.s.main(DAY + 15 * MIN, jobs=self.jobs)
        self.assertEqual(self.ran(), [])
        self.assertEqual(len(self.broken()), 1)
        self.assertFalse((self.s.m.RUNS_DIR / "x.json").exists())

    def test_files_that_cannot_be_read_are_unreadable(self):
        self.s.main(DAY, jobs=self.jobs)
        self.s.m.STATE_FILE.write_text(json.dumps({**self.s.state(), "b": TODAY}))
        self.s.m.save_state(self.s.state())
        self.s.ran.clear(), self.s.spawned.clear()
        (self.s.m.RUNS_DIR / "x.json").write_text("{}")
        real = pathlib.Path.read_text

        def eio(path, *a, **k):
            if path in (self.s.m.STATE_FILE, self.s.m.RUNS_DIR / "x.json"):
                raise OSError(5, "Input/output error")
            return real(path, *a, **k)
        with mock.patch.object(pathlib.Path, "read_text", eio):
            self.s.main(DAY + 15 * MIN, jobs=self.jobs)
        self.assertEqual(self.ran(), [])

    def test_a_last_run_in_the_future_is_due(self):
        # the clock was set back a year
        self.s.m.STATE_FILE.write_text(json.dumps({"i.last_ok": "2027-10-30T12:00:00"}))
        self.s.main(DAY, jobs=[{"name": "i", "every_minutes": 60, "command": "echo i"}])
        self.assertEqual(self.ran(), ["echo i"])

    def test_daylight_saving_does_not_move_an_interval(self):
        with zone("America/New_York"):   # 2026-03-08 02:00 → 03:00
            spring = datetime.datetime(2026, 3, 8, 3, 5)
            self.s.m.STATE_FILE.write_text(json.dumps({"i.last_ok": "2026-03-08T01:55:00"}))   # 10 minutes ago
            self.s.main(spring, jobs=[{"name": "i", "every_minutes": 60, "command": "echo i"}])
            self.assertEqual(self.ran(), [])
            # 02:00 → 01:00: 01:30 the second time is an hour after 01:30 the first, and 01:10 the second 40 minutes
            for minute, ran in ((10, []), (30, ["echo i"])):
                s = Sched(self)
                s.main(datetime.datetime(2026, 11, 1, 1, 30), jobs=[{"name": "i", "every_minutes": 60, "command": "echo i"}])
                s.ran.clear()
                s.main(datetime.datetime(2026, 11, 1, 1, minute, fold=1), jobs=[
                    {"name": "i", "every_minutes": 60, "command": "echo i"}])
                self.assertEqual(s.commands(), ran, minute)
            # ... and after a run the second time, the next is an hour on, not two
            s = Sched(self)
            s.main(datetime.datetime(2026, 11, 1, 1, 30, fold=1), jobs=[{"name": "i", "every_minutes": 60, "command": "echo i"}])
            s.main(datetime.datetime(2026, 11, 1, 1, 50, fold=1), jobs=[{"name": "i", "every_minutes": 60, "command": "echo i"}])
            self.assertEqual(s.commands(), ["echo i"])
            # another version's tick writes last_ok alone: the time beside it is of another moment
            state = {"i.last_ok": "2026-11-01T05:00:00", "i.last_ok_at": 1.0}
            self.assertFalse(s.m.is_due({"name": "i", "every_minutes": 60}, state, datetime.datetime(2026, 11, 1, 5, 30), "x"))
            since = {"a": {"since": "2026-03-08T01:55:00"}}
            self.assertEqual(self.s.m.failing_problems(since, spring), [], "failing for 10 minutes")

    def test_saved_before_renamed(self):
        for save in (lambda: self.s.m.save_state({"a": 1}), lambda: self.s.m.write_json(self.s.st / "x.json", {"a": 1})):
            order = []
            fsync, replace = os.fsync, os.replace
            path_replace = pathlib.Path.replace
            with mock.patch.object(os, "fsync", lambda fd: (order.append("fsync"), fsync(fd))[1]), \
                    mock.patch.object(os, "replace", lambda a, b: (order.append("rename"), replace(a, b))[1]), \
                    mock.patch.object(pathlib.Path, "replace", lambda a, b: (order.append("rename"), path_replace(a, b))[1]):
                save()
            self.assertEqual(order[:2], ["fsync", "rename"])

    def test_one_jobs_record_stops_only_that_job(self):
        self.s.m.STATE_FILE.write_text(json.dumps({"a.attempts": "x", "i.last_ok": "soon"}))
        self.s.main(DAY, jobs=[job("a"), {"name": "i", "every_minutes": 60, "command": "echo i"}, job("c"),
                               job("d", after=["a"])])
        self.assertEqual(self.s.commands(), ["echo c", "echo d"])
        self.assertEqual(self.s.told(), ["scheduler-broken:(schedule.yaml)"])
        text = "\n".join(dict(self.s.alerts)["scheduler-broken:(schedule.yaml)"])
        self.assertTrue("a: " in text and "i: " in text and "soon" not in text, text)
        self.assertEqual(self.s.ids["scheduler-broken:(schedule.yaml)"], ["a", "i"], "by name: a second one is news")

    def test_a_run_record_cut_short_is_dropped(self):
        for text in ("", "{}", "[]", '{"exit": 0}', '{"started": 5}'):
            (self.s.m.RUNS_DIR / "x.json").write_text(text)
            self.s.main(DAY, jobs=[])
            self.assertFalse((self.s.m.RUNS_DIR / "x.json").exists(), text)

    def test_a_cut_list_of_failing_jobs_does_not_silence_the_next_failure(self):
        self.s.m.FAILING_FILE.write_text("")
        self.s.m.mark_job("a", True, "exit 1")
        self.assertIn("a", json.loads(self.s.m.FAILING_FILE.read_text()))


class FullDisk(unittest.TestCase):
    def setUp(self):
        self.s = Sched(self)

    def test_the_tick_fails_visibly_and_nothing_is_cut(self):
        s = self.s
        s.main(DAY, jobs=[job("a")])
        s.m.mark_job("x", True, "exit 1")
        before = {p.name: p.read_text() for p in s.st.iterdir() if p.is_file()}
        old = DAY - datetime.timedelta(hours=1)
        stamp(s.m.HEARTBEAT, old)
        out = on_full_disk(lambda: s.main(DAY + 15 * MIN, jobs=[job("a"), job("z")]))
        self.assertNotEqual(out, "None", "the tick must not look like one that went well")
        self.assertEqual({p.name: p.read_text() for p in s.st.iterdir() if p.is_file()}, before)
        self.assertEqual(s.m.HEARTBEAT.stat().st_mtime, old.timestamp(), "a heartbeat that wasn't written is not newer")

    def test_a_disk_that_fills_up_midway_runs_nothing_unrecorded(self):
        s = self.s
        s.main(DAY, jobs=[])
        s.m.mark_job("x", True, "exit 1")

        def fails():
            with self.assertRaises(s.m.StateNotSaved):
                s.m.save_state({"a": TODAY})
            s.m.mark_job("y", True)
            s.m.log("a line")
            with contextlib.suppress(OSError):
                s.m.heartbeat()
            return "ok"
        before = {p.name: p.read_text() for p in s.st.iterdir() if p.is_file()}
        self.assertEqual(on_full_disk(fails), "'ok'")
        self.assertEqual({p.name: p.read_text() for p in s.st.iterdir() if p.is_file()}, before)

    def test_a_state_that_cannot_be_saved_ends_the_tick_before_the_job(self):
        calls = []

        def save(state):
            calls.append(1)
            if len(calls) > 1:
                raise self.s.m.StateNotSaved("full")
        with self.assertRaises(self.s.m.StateNotSaved):
            self.s.main(DAY, jobs=[job("a"), job("b")], save_state=save)
        self.assertEqual(self.s.commands(), [])
        self.assertEqual(self.s.record()["unfinished"], 1)


class OneBrokenThing(unittest.TestCase):
    INDEX = {"projects": {"p": {"name": "p", "paths": {"workspace": "/tmp"}, "jobs": [
        {"name": "fine", "at": "06:00", "run": "echo fine", "cwd": "workspace"}]}}, "modules": {}, "errors": {}}

    def setUp(self):
        self.s = Sched(self)

    def ran(self):
        return [c.rsplit("&& ", 1)[-1] for c in self.s.commands()]

    def test_a_typo_in_schedule_yaml(self):
        for text in ("jobs:\n- name: a\n  time: '06:00\n", "jobs: [", "- a\n", "jobs: {a: 1}\n",
                     "jobs:\n- {name: a, time: '06:00', command: x, end_date: 2026-13-45}\n"):
            with self.subTest(text=text):
                s = self.s = Sched(self)
                s.main(DAY, schedule=text, index=self.INDEX)
                self.assertEqual(self.ran(), ["echo fine"])
                self.assertEqual(s.told(), ["scheduler-broken:(schedule.yaml)"])
                self.assertIn("schedule.yaml", s.log())
        s.alerts.clear()
        s.main(DAY + 15 * MIN, schedule="jobs:\n- {name: a, time: '06:00', command: echo a}\n", index=self.INDEX)
        self.assertEqual(s.told(), [])
        self.assertEqual(self.ran(), ["echo fine", "echo a"])

    def test_a_typo_does_not_start_the_registry_twin_of_one_of_its_jobs(self):
        index = {"projects": {"p": {"name": "p", "paths": {"workspace": "/tmp"}, "jobs": [
            {"name": "new", "at": "06:00", "run": "echo new", "cwd": "workspace", "legacy_name": "old"}]}},
            "modules": {}, "errors": {}}
        self.s.main(DAY, schedule="jobs:\n- name: old\n  time: '06:00\n  command: echo old\n", index=index)
        self.assertEqual(self.ran(), [])

    def test_one_entry_of_schedule_yaml(self):
        for field in ("time: 13:00", "time: noon", "every_minutes: 15\n  between: 8", "time: '06:00'\n  days_of_month: 1",
                      "time: '06:00'\n  end_date: soon", "time: '06:00'\n  max_attempts_per_day: two",
                      "time: '06:00'\n  timeout_minutes: long", "every_minutes: often", "every_minutes: 15\n  between: '8'",
                      "time: '06:00'\n  after: 5", "time: '06:00'\n  months: 5"):
            with self.subTest(field=field):
                s = self.s = Sched(self)
                s.main(DAY, schedule=f"jobs:\n- name: bad\n  command: echo bad\n  {field}\n"
                                     "- {name: good, time: '06:00', command: echo good}\n", index=self.INDEX)
                self.assertEqual(sorted(self.ran()), ["echo fine", "echo good"])
                self.assertEqual(s.told(), ["scheduler-broken:(schedule.yaml)"])
                self.assertIn("'bad'", s.log())

    def test_one_registry_job(self):
        for field, value in (("until", "soon"), ("days_of_month", 1), ("between", "8"), ("months", "x"),
                             ("max_attempts_per_day", "two"), ("between", 5)):
            with self.subTest(field=field, value=value):
                s = self.s = Sched(self)
                bad = {"name": "bad", "at": "06:00", "run": "echo bad", "cwd": "workspace", field: value}
                index = {"projects": {"a": {"name": "a", "paths": {"workspace": "/tmp"}, "jobs": [bad]},
                                      **self.INDEX["projects"]}, "modules": {}, "errors": {}}
                if field == "between":
                    bad["every"] = "15m"
                    del bad["at"]
                s.main(DAY, index=index)
                self.assertEqual(self.ran(), ["echo fine"])
                self.assertEqual(s.told(), ["scheduler-broken:a"])
                self.assertEqual(s.ids["scheduler-broken:a"], ["a"])
                self.assertNotIn(str(value), dict(s.alerts)["scheduler-broken:a"][0].replace("a/bad", ""),
                                 "the rule, not the project's text")

    def test_one_registry_entry_the_registry_cannot_turn_into_jobs(self):
        index = {"projects": {"a": {"name": "a", "paths": {"workspace": "/tmp"}, "jobs": [
            {"name": "bad", "every": 5.5, "run": "x", "cwd": "workspace"}]}, **self.INDEX["projects"]},
            "modules": {}, "errors": {}}
        self.s.main(DAY, index=index)
        self.assertEqual(self.ran(), ["echo fine"])
        self.assertEqual(self.s.told(), ["scheduler-broken:a"])
        # the registry may say it itself (its refused list); when it raises, this is told
        def raises(index, taken):
            if "a" in index["projects"]:
                raise TypeError("a project's text")
            return real(index, taken)
        real = registry.scheduler_jobs
        s = self.s = Sched(self)
        with mock.patch.object(registry, "scheduler_jobs", raises):
            s.main(DAY, index=index)
        self.assertEqual(self.ran(), ["echo fine"])
        self.assertEqual(s.told(), ["scheduler-broken:a"])
        self.assertNotIn("text", dict(s.alerts)["scheduler-broken:a"][0])

    def test_a_registry_that_cannot_be_read_goes_on_from_its_last_index(self):
        f = self.s.tmp / "index.json"
        f.write_text(json.dumps(self.INDEX))
        with mock.patch.object(hubconf, "REGISTRY_INDEX", f):
            self.s.main(DAY, refresh=boom)
            self.assertEqual(self.ran(), ["echo fine"])
            self.assertEqual(self.s.told(), ["scheduler-broken:(registry)"])
            f.write_text("")
            self.s.main(DAY, schedule="jobs:\n- {name: a, time: '06:00', command: echo a}\n", refresh=boom)
        self.assertEqual(self.ran(), ["echo fine", "echo a"], "with none, schedule.yaml's still run")

    def test_job_failed_cannot_end_the_tick(self):
        def run(cmd, **kw):
            if "hubbus" in str(cmd[1]):
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(self.s, "run", run):
            self.s.main(DAY, jobs=[job("f", command="exit 3"), job("g")])
        self.assertEqual(self.s.commands(), ["exit 3", "echo g"])
        self.assertIn("f", json.loads(self.s.m.FAILING_FILE.read_text()))

    def test_a_run_that_cannot_be_started_is_that_jobs_failure(self):
        def bounded(command, job_log, minutes, env=None):
            if command == "echo a":
                raise OSError("no shell")
            return self.s.bounded(command, job_log, minutes, env)
        self.s.main(DAY, jobs=[job("a"), job("b")], run_bounded=bounded)
        self.assertEqual(self.s.commands(), ["echo b"])
        self.assertIn("a", json.loads(self.s.m.FAILING_FILE.read_text()))


class OneAlertPerProject(unittest.TestCase):
    """A project's broken jobs are one alert, to the project first as its failing jobs are, whatever it calls them."""

    def broken(self, n, prefix="j", group="p"):
        return {f"{group}/{prefix}{i}": {"group": group, "why": "time can't be used"} for i in range(n)}

    def told(self, broken=None, failing=None, projects=("p", "q")):
        hub, s, got = hubconf.hub_project(), Sched(self), []
        docs = {hub: {"name": hub, "type": "platform"}, **{p: {"name": p} for p in projects}}
        with mock.patch.object(alert, "once", lambda k, p, ok, ids=None, names=None: p and got.append((k, p, ids))), \
                mock.patch.object(alert, "open_keys", lambda p: []), mock.patch.object(alert, "failures", dict), \
                mock.patch.object(hubconf, "projects", lambda: docs):
            if broken is not None:
                s.m.alert_broken(broken)
            else:
                s.m.FAILING_FILE.write_text(json.dumps(failing))
                s.m.alert_failing()
        return got

    def test_thirty_jobs_are_one_alert_and_a_rename_is_not_new(self):
        a, b = self.told(self.broken(30)), self.told(self.broken(30, prefix="x" * 500))
        self.assertEqual([(k, ids) for k, _p, ids in a], [("scheduler-broken:p", ["p"])])
        self.assertEqual([(k, ids) for k, _p, ids in b], [(k, ids) for k, _p, ids in a])
        for _key, problems, _ids in a + b:
            self.assertEqual(len(problems), 1)
            self.assertLess(len(problems[0]), 1500)

    def test_who_is_told(self):
        docs = {"p": {"name": "p"}, hubconf.hub_project(): {"type": "platform"}}
        with mock.patch.object(hubconf, "projects", lambda: docs):
            for key, to in (("p", "p"), (hubconf.hub_project(), hubconf.hub_project()), ("backup", hubconf.hub_project()),
                            ("(registry)", "owner"), ("(schedule.yaml)", "owner"),
                            (f"{hubconf.hub_project()}/hubbus-bridge", "owner")):
                self.assertEqual(alert.route("scheduler-broken:" + key), to, key)

    def test_the_job_the_mailbox_needs_is_its_own(self):
        hub = hubconf.hub_project()
        got = self.told({f"{hub}/hubbus-bridge": {"group": hub, "why": "x"}, f"{hub}/todo": {"group": hub, "why": "x"}})
        self.assertEqual([k for k, _p, _i in got], [f"scheduler-broken:{hub}", f"scheduler-broken:{hub}/hubbus-bridge"])

    def test_the_platforms_and_the_owners_are_told_by_name(self):
        hub = hubconf.hub_project()
        got = self.told({f"{hub}/a": {"group": hub, "why": "x"}, f"{hub}/b": {"group": hub, "why": "x"},
                         "backup/n": {"group": "backup", "why": "x"}, "old": {"group": "(schedule.yaml)", "why": "x"}})
        self.assertEqual([(k, ids) for k, _p, ids in got], [
            ("scheduler-broken:(schedule.yaml)", ["old"]), ("scheduler-broken:backup", ["backup/n"]),
            (f"scheduler-broken:{hub}", [f"{hub}/a", f"{hub}/b"])])

    def test_a_projects_failing_jobs_are_one_letter_the_platforms_each_their_own(self):
        hub, old = hubconf.hub_project(), {"since": "2020-01-01T00:00:00", "detail": "exit 1" * 100}
        failing = {**{f"p/j{i}": old for i in range(30)}, "q/x": old, f"{hub}/todo": old, "backup/n": old, "own": old}
        got = self.told(failing=failing)
        self.assertEqual([(k, ids) for k, _p, ids in got], [
            ("scheduler-job:backup/n", ["backup/n"]), (f"scheduler-job:{hub}/todo", [f"{hub}/todo"]),
            ("scheduler-job:own", ["own"]), ("scheduler-job:p", ["p"]), ("scheduler-job:q", ["q"])])
        text = dict((k, p) for k, p, _i in got)["scheduler-job:p"]
        self.assertEqual(len(text), 1)
        self.assertLess(len(text[0]), 1500)
        self.assertEqual(alert.JOB_KEY + "p", "scheduler-job:p")
        with mock.patch.object(hubconf, "projects", lambda: {"p": {"name": "p"}}):
            self.assertEqual(alert.route("scheduler-job:p"), "p", "the letter goes to the project")

    def test_an_alert_opened_per_job_before_stays_that_until_it_is_over(self):
        old, s, got = {"since": "2020-01-01T00:00:00", "detail": "exit 1"}, Sched(self), []
        s.m.FAILING_FILE.write_text(json.dumps({"p/told": old, "p/new": old}))
        with mock.patch.object(alert, "once", lambda k, p, ok, ids=None, names=None: got.append((k, bool(p), ids))), \
                mock.patch.object(alert, "open_keys", lambda p: [p + "p/over", p + "p/told"]), \
                mock.patch.object(alert, "failures", dict), mock.patch.object(hubconf, "projects", lambda: {"p": {"name": "p"}}):
            s.m.alert_failing()
        self.assertEqual(got[:-1], [("scheduler-job:p", True, ["p"]), ("scheduler-job:p/over", False, []),
                                    ("scheduler-job:p/told", True, ["p/told"])])

    def test_a_registry_that_cannot_be_read_is_told(self):
        def none():
            raise RuntimeError("no index")
        for projects in (none, lambda: {}):   # no index; a good one
            with mock.patch.object(hubconf, "projects", projects), mock.patch.object(hubconf, "hub_project", none):
                self.assertEqual(alert.route("scheduler-broken:(registry)"), "owner")
        with mock.patch.object(hubconf, "projects", none):
            for key in ("scheduler-job:p/x", "scheduler-broken:p", "scheduler-job:p"):
                self.assertEqual(alert.route(key), "owner", key)
        with mock.patch.object(hubconf, "REGISTRY_INDEX", self_none := pathlib.Path("/nonexistent/index.json")):
            self.assertFalse(self_none.exists())
            s = Sched(self)
            s.main(DAY, refresh=boom)
        self.assertEqual(s.told(), ["scheduler-broken:(registry)"])


class TickOrder(unittest.TestCase):
    def test_the_state_not_saved_after_a_run_ends_the_tick(self):
        s, calls = Sched(self), []

        def save(state):
            calls.append(1)
            if len(calls) > 2:   # the start's, the attempt's, then the success's
                raise s.m.StateNotSaved("full")
        with self.assertRaises(s.m.StateNotSaved):
            s.main(DAY, jobs=[job("a"), job("b")], save_state=save)
        self.assertEqual(s.commands(), ["echo a"])
        self.assertFalse(s.m.FAILING_FILE.exists(), "the job succeeded: it is the tick that failed")

    def test_the_heartbeat_is_fresh_when_a_job_starts(self):
        s, events = Sched(self), []
        real = s.m.heartbeat

        def bounded(command, job_log, minutes, env=None):
            events.append("run")
            return 0
        s.main(DAY, jobs=[job("a"), job("b")], run_bounded=bounded,
               reconcile=lambda *a: events.append("start") or set(), heartbeat=lambda: (events.append("beat"), real())[1])
        self.assertEqual(events[events.index("start"):], ["start", "beat", "run", "beat", "beat", "run", "beat"])

    def test_the_jobs_run_as_listed_with_their_own_limits(self):
        # the projects between the platform and the modules, each with the time limit it declares (as at v0.21.0)
        hub = hubconf.hub_project()
        index = {"projects": {p: {"name": p, "paths": {"workspace": "/tmp"}, **kw, "jobs": [
            {"name": "x", "at": "06:00", "run": f"echo {p}", "cwd": "workspace", "timeout": "9h"}]}
            for p, kw in (("a", {}), (hub, {"type": "platform"}), ("z", {}))}, "modules": {}, "errors": {}}
        s = Sched(self)
        s.main(DAY, index=index)
        self.assertEqual([c.rsplit("echo ", 1)[-1] for c in s.commands()], ["a", hub, "z"])
        self.assertEqual([r["minutes"] for r in s.ran], [540] * 3)


class BackgroundRuns(unittest.TestCase):
    def setUp(self):
        self.s = Sched(self)

    def run_record(self, **kw):
        (self.s.m.RUNS_DIR / "x.json").write_text(json.dumps(
            {"date": TODAY, "started": (DAY - 60 * MIN).isoformat(), "pid": os.getpid(), **kw}))

    def running(self, mark, **kw):
        self.run_record(**kw)
        with mock.patch.object(self.s.m, "process_mark", lambda pid: mark, create=True):
            self.s.main(DAY + 120 * MIN, jobs=[job("x", background=True)])
        return not (self.s.m.FAILING_FILE.exists() and "x" in json.loads(self.s.m.FAILING_FILE.read_text()))

    def test_a_process_that_got_the_number_after_a_reboot_is_not_the_run(self):
        self.assertTrue(self.running("boot1 5", proc="boot1 5"))
        self.assertTrue(self.running(None, proc="boot1 5"), "can't be told")
        self.assertTrue(self.running("boot1 5"), "a record from before")
        self.assertFalse(self.running("boot2 5", proc="boot1 5"))

    def test_a_process_mark_is_of_the_process_and_not_of_the_clock(self):
        mark = self.s.m.process_mark(os.getpid())
        self.assertTrue(mark)
        with zone("America/New_York"):
            self.assertEqual(self.s.m.process_mark(os.getpid()), mark)
        self.assertIsNone(self.s.m.process_mark(2 ** 22 + 7))
        child = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
        try:
            self.assertTrue(self.s.m.process_mark(child.pid))
            if sys.platform.startswith("linux"):
                self.assertNotEqual(self.s.m.process_mark(child.pid), mark)
        finally:
            child.communicate(b"\n")

    def test_a_run_is_recorded_with_its_process(self):
        # the real fork, its command replaced; the record is the grandchild's, which has ended when the pipe has no writer left
        r, w = os.pipe()
        with mock.patch.object(self.s.m, "run_bounded", lambda *a: 0), mock.patch.object(self.s.m, "log", lambda m: None):
            self.s.m.spawn_background("x", "true", self.s.tmp / "log", DAY, TODAY)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            f.read()
        rec = json.loads((self.s.m.RUNS_DIR / "x.json").read_text())
        self.assertEqual((rec.get("exit"), bool(rec.get("proc")), rec.get("started_at")), (0, True, DAY.timestamp()))


class AsBefore(unittest.TestCase):
    """What ran as it was keeps running as it did (bf0de81c)."""

    def test_what_the_registry_put_into_after_is_waited_for(self):
        s = Sched(self)
        s.main(DAY, index={"projects": {"p": {"name": "p", "paths": {"workspace": "/tmp"}, "jobs": [
            {"name": "fails", "at": "06:00", "run": "exit 1", "cwd": "workspace"}]},
            "q": {"name": "q", "paths": {"workspace": "/tmp"}, "jobs": [
                {"name": "k", "at": "06:00", "run": "echo k", "cwd": "workspace", "after": ["p/fails"]}]}},
            "modules": {}, "errors": {}})
        self.assertEqual(len(s.ran), 1)
        self.assertFalse(hasattr(s.m, "ignore_foreign_before"), "whose before: counts is the registry's")

    def test_only_what_would_raise_is_refused(self):
        m, base = Sched(self).m, {"name": "x", "time": "06:00", "command": "echo x"}
        friday = DAY.date()
        for days, ran in (("mon", False), ("fri", True), (["mon", "wednesday"], False), (["fri", "wednesday"], True), ([], False)):
            got = {**base, "days_of_week": days}
            self.assertIsNone(m.unsound(got), days)
            self.assertEqual(m.scheduled_on(got, friday), ran, days)
        for over in ({"days_of_month": []}, {"months": [99]}, {"timeout_minutes": 1e9}, {"max_attempts_per_day": True},
                     {"after": []}, {"command": ""}, {"between": 8}):
            self.assertIsNone(m.unsound({**base, **over}), over)
        for over in ({"time": 780}, {"days_of_week": 5}, {"days_of_month": "1"}, {"months": None}, {"after": None}, {"after": [5]},
                     {"end_date": "soon"}, {"command": 5}):
            self.assertIsNotNone(m.unsound({**base, **over}), over)
            self.assertIsNone(m.unsound({**base, **over, "enabled": False}), over)
        s = Sched(self)
        s.main(DAY, schedule="jobs:\n- {name: paused, enabled: false, time: 13:00}\n- {name: a, time: '06:00', command: echo a, "
                             "days_of_week: fri}\n")
        self.assertEqual((s.commands(), s.told()), (["echo a"], []))

    def test_an_interval_does_not_drift_by_a_ticks_fraction(self):
        for every in (16, 31, 61):
            s, jobs = Sched(self), [{"name": "i", "every_minutes": every, "command": "echo i"}]
            s.main(DAY + datetime.timedelta(seconds=0.9), jobs=jobs)
            s.main(DAY + (every - 1) * MIN + datetime.timedelta(seconds=0.1), jobs=jobs)
            self.assertEqual(len(s.ran), 2, every)

    def test_the_page_does_not_break_on_them(self):
        loader = importlib.machinery.SourceFileLoader("gsp_robust", str(ROOT / "bin" / "gen-status-page.py"))
        spec = importlib.util.spec_from_loader("gsp_robust", loader)
        g = importlib.util.module_from_spec(spec)
        loader.exec_module(g)
        for over in ({"days_of_week": "mon"}, {"days_of_week": ["mon", "wednesday"]}, {"days_of_month": []}, {"time": None}):
            j = {"name": "x", "time": "06:00", "command": "x", **over}
            self.assertIsInstance(g.said_or_dash(g.describe_when, j), str)
            self.assertIsInstance(g.said_or_dash(g.next_run, j, {}, DAY), str)


class Watchdog(unittest.TestCase):
    """The watchdog over the files one Sched leaves, on a clock of its own."""

    def setUp(self):
        self.s = Sched(self)
        hub = self.hub = self.s.tmp / "hub"
        (hub / ".logs").mkdir(parents=True)
        (hub / ".state").symlink_to(self.s.st)
        cfg = self.s.tmp / "config"
        cfg.mkdir()
        (self.s.tmp / "secrets").mkdir(mode=0o700)
        (cfg / "brunna.json").write_text(json.dumps({"alerts": {"ntfy": {"topic": "t"}}}))
        env = {"AGENT_HUB_ROOT": str(hub), "AGENT_HUB_CONFIG": str(cfg), "AGENT_HUB_SECRETS": str(self.s.tmp / "secrets"),
               "AGENT_HUB_LANG_UI": "en"}
        with mock.patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location("watchdog_robust", ROOT / "bin" / "scheduler-watchdog.py")
            self.w = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.w)
        self.sent = []

    def check(self, now, send=None, real_resumed=False):
        with mock.patch.object(self.w.datetime, "datetime", fake_clock(now)), \
                mock.patch.object(self.w, "send", send or (lambda text: self.sent.append(text) or True)), \
                mock.patch.object(self.w, "diagnose", lambda: "<diag>"), \
                mock.patch.object(self.w, "resumed", self.w.resumed if real_resumed else lambda st, now: None), \
                mock.patch.object(self.w.sys, "argv", ["x"]):
            return self.w.main()

    def tick(self, now, **kw):
        try:
            self.s.main(now, jobs=[], **kw)
        except RuntimeError:
            pass
        if self.s.m.HEARTBEAT.exists():
            stamp(self.s.m.HEARTBEAT, now)

    def record(self, **rec):
        (self.s.st / "scheduler-tick.json").write_text(json.dumps(rec))

    def beat(self, when):
        self.s.m.HEARTBEAT.write_text("")
        stamp(self.s.m.HEARTBEAT, when)

    def test_ticks_that_crash_every_time_are_alerted_within_the_hour_and_recovery_is_told(self):
        self.tick(DAY)
        first = None
        for m in range(5, 120, 5):
            if m % 15 == 0:
                self.tick(DAY + m * MIN, reconcile=boom)
            self.assertEqual(self.check(DAY + m * MIN), 0)
            if self.sent and first is None:
                first = m
        self.assertIsNotNone(first, "a scheduler whose ticks all crash was never alerted")
        self.assertLessEqual(first, 60)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("ticks don't complete", self.sent[0])
        self.tick(DAY + 120 * MIN)
        self.check(DAY + 121 * MIN)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("is back", self.sent[1])

    def test_it_is_back_only_when_a_tick_has_completed(self):
        self.beat(DAY - 60 * MIN)
        self.record(started=(DAY - 60 * MIN).isoformat(), running=True, unfinished=0, completed=(DAY - 75 * MIN).isoformat())
        self.check(DAY), self.check(DAY + 5 * MIN)
        self.assertEqual(len(self.sent), 1)
        for rec in ({"running": True, "unfinished": 1}, {"running": False, "unfinished": 2}, {"running": True, "unfinished": 0}):
            self.record(completed=(DAY - 75 * MIN).isoformat(), since=(DAY - 60 * MIN).isoformat(), **rec)
            self.beat(DAY + 10 * MIN)
            self.check(DAY + 10 * MIN)
            self.assertEqual(len(self.sent), 1, rec)
        self.record(running=True, unfinished=0, completed=(DAY + 11 * MIN).isoformat())
        self.check(DAY + 12 * MIN)
        self.assertIn("is back", self.sent[-1])
        self.assertEqual(len(self.sent), 2)

    def test_killed_ticks_are_alerted(self):
        self.tick(DAY)
        for m in range(5, 90, 5):
            if m % 15 == 0:
                with contextlib.suppress(KeyboardInterrupt):
                    self.tick(DAY + m * MIN, reconcile=mock.Mock(side_effect=KeyboardInterrupt))
                stamp(self.s.m.HEARTBEAT, DAY + m * MIN)
            self.check(DAY + m * MIN)
        self.assertEqual(len(self.sent), 1)

    def test_a_long_tick_is_no_alarm(self):
        # four foreground jobs of 30 minutes: the last completed tick is two hours old and more
        self.record(started=DAY.isoformat(), running=True, unfinished=0, completed=(DAY - 15 * MIN).isoformat())
        for m in range(0, 135, 5):
            self.beat(DAY + (m - m % 30) * MIN)
            self.assertEqual(self.check(DAY + m * MIN), 0)
        self.assertEqual(self.sent, [])

    def test_a_reboot_in_a_tick_and_a_long_one_after_it_is_no_alarm(self):
        self.record(started=DAY.isoformat(), running=True, unfinished=1, since=(DAY - 600 * MIN).isoformat(),
                    completed=(DAY - 615 * MIN).isoformat())
        for m in range(0, 60, 5):
            self.beat(DAY + (m - m % 30) * MIN)
            self.check(DAY + m * MIN)
        self.assertEqual(self.sent, [])

    def test_an_upgrade_with_no_record_yet_is_no_alarm(self):
        for rec in (None, "", "{}", "[]", '{"unfinished": "x"}', '{"unfinished": 5, "since": 3}'):
            if rec is not None:
                (self.s.st / "scheduler-tick.json").write_text(rec)
            for m in (0, 5, 10):
                self.beat(DAY + m * MIN)
                self.assertEqual(self.check(DAY + m * MIN), 0)
        self.assertEqual(self.sent, [])

    def test_one_tick_that_died_or_three_within_half_an_hour_are_no_alarm(self):
        for n, ago, alarm in ((1, 120, False), (3, 20, False), (2, 36, True), (3, 34, False)):
            self.sent.clear()
            (self.s.st / "scheduler-watchdog.json").unlink(missing_ok=True)
            self.beat(DAY)
            self.record(running=False, unfinished=n, completed=(DAY - ago * MIN).isoformat())
            for _i in range(2):
                self.check(DAY)
            self.assertEqual(bool(self.sent), alarm, (n, ago))

    def test_one_that_stopped_after_such_ticks_is_said_to_have_stopped(self):
        self.beat(DAY - 60 * MIN)
        self.record(running=False, unfinished=3, completed=(DAY - 200 * MIN).isoformat())
        self.check(DAY), self.check(DAY + 5 * MIN)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("has not ticked", self.sent[0])

    def test_daylight_saving_is_no_alarm_and_no_silence(self):
        with zone("America/New_York"):
            spring = datetime.datetime(2026, 3, 8, 3, 5)   # 10 minutes after 01:55
            before = datetime.datetime(2026, 3, 8, 1, 55)
            self.beat(before)
            self.record(running=False, unfinished=3, completed=before.astimezone().isoformat())
            self.check(spring), self.check(spring)
            self.assertEqual(self.sent, [])
            # 02:00 → 01:00: 01:10 the second time is 50 minutes after 01:20 the first time
            autumn = datetime.datetime(2026, 11, 1, 1, 10, fold=1)
            self.beat(datetime.datetime(2026, 11, 1, 1, 20))
            self.check(autumn), self.check(autumn)
            self.assertEqual(len(self.sent), 1)

    def test_asleep_is_told_from_stopped_whatever_the_last_check_found(self):
        clocks = {7: 1000.0, self.w.time.CLOCK_MONOTONIC: 500.0}
        with mock.patch.object(self.w.time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(self.w.time, "clock_gettime", side_effect=lambda c: clocks[c]):
            for start, beat in ((DAY, DAY), (DAY + 600 * MIN, DAY + 590 * MIN)):
                self.beat(beat)
                self.check(start, real_resumed=True)   # healthy: what it knows of sleep stays
                clocks[7] += 8 * 3600   # eight hours asleep; the check runs before the scheduler's first tick
                for m in (0, 5, 10):
                    self.check(start + (480 + m) * MIN, real_resumed=True)
                self.assertEqual(self.sent, [])
                self.beat(start + 495 * MIN)
                self.check(start + 496 * MIN, real_resumed=True)
            self.assertNotIn("resumed_at", json.loads((self.s.st / "scheduler-watchdog.json").read_text()))
            # a short sleep noticed while the last tick is still fresh covers the minutes after it
            at = DAY + 2000 * MIN
            self.beat(at)
            self.check(at, real_resumed=True)
            clocks[7] += 1800
            for m in (31, 36, 41):
                self.check(at + m * MIN, real_resumed=True)
            self.assertEqual(self.sent, [])

    def test_a_full_disk_that_holds_its_output_too(self):
        """The real script in its own process: log, state and stderr on the full disk (stderr is a file in .logs)."""
        self.beat(datetime.datetime.now() - 60 * MIN)
        (self.s.st / "scheduler-watchdog.json").write_text("{}")
        (self.hub / ".logs" / "scheduler-watchdog.log").write_text("")
        code = ("import importlib.util, os, resource, sys\n"
                f"s = importlib.util.spec_from_file_location('w', {str(ROOT / 'bin' / 'scheduler-watchdog.py')!r})\n"
                "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                "m.send_ntfy = lambda conf, text: os.write(1, b'SENT\\n') and False\n"
                "m.send_email = lambda conf, text: True\n"
                "m.diagnose = lambda: ''\n"
                "resource.setrlimit(resource.RLIMIT_FSIZE, (0, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))\n"
                "sys.argv = ['x']; sys.exit(m.main())\n")
        cfg = self.s.tmp / "config"
        (cfg / "brunna.json").write_text(json.dumps({"alerts": {"ntfy": {"topic": "t"}, "email": {"host": "h", "to": "x"}}}))
        env = {**os.environ, "AGENT_HUB_ROOT": str(self.hub), "AGENT_HUB_CONFIG": str(cfg), "AGENT_HUB_LANG_UI": "en",
               "AGENT_HUB_SECRETS": str(self.s.tmp / "secrets")}
        with open(self.hub / ".logs" / "watchdog-stderr.log", "a") as err:
            r = subprocess.run([sys.executable, "-B", "-c", code], env=env, stdout=subprocess.PIPE, stderr=err, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, "SENT\n"))
        self.assertEqual([p.name for p in self.s.st.iterdir() if "tmp" in p.name or "probe" in p.name], [])

    @unittest.skipIf(os.geteuid() == 0, "root writes everywhere")
    def test_a_state_folder_that_cannot_be_written_is_told_once_in_six_hours(self):
        self.beat(DAY)
        self.s.st.chmod(0o555)
        self.addCleanup(self.s.st.chmod, 0o755)
        at = []
        for m in range(2, 24 * 60, 5):
            self.check(DAY + m * MIN, lambda text, m=m: at.append(m) or True)
        self.assertEqual(len(at), 4, at)
        self.assertLessEqual(at[0], 45, "the first one at once")
        # nor is its end told every five minutes
        self.s.st.chmod(0o755)
        state = self.s.st / "scheduler-watchdog.json"
        state.write_text('{"alerting": true, "last_alert": "2026-10-30T11:00:00"}')
        stamp(state, DAY)
        self.beat(DAY + 2000 * MIN)
        self.s.st.chmod(0o555)
        back = []
        for m in range(0, 30, 5):
            self.check(DAY + (2000 + m) * MIN, lambda text, m=m: back.append(m) or True)
        self.assertEqual(len(back), 1, back)
        again = []   # and a stop after that is news
        for m in range(0, 60, 5):
            self.check(DAY + (2060 + m) * MIN, lambda text, m=m: again.append(m) or True)
        self.assertEqual(len(again), 1, again)
        self.s.st.chmod(0o755)
        self.check(DAY + 2125 * MIN)
        self.assertEqual([p.name for p in (*self.s.st.iterdir(), *(self.s.tmp / "secrets" / "tmp").iterdir())
                          if p.name.startswith("scheduler-watchdog") and p != state], [])

    def test_nothing_can_be_remembered_anywhere(self):
        self.beat(DAY)
        at = []
        with mock.patch.object(self.w, "marks", lambda name, make=False: []), mock.patch.object(self.w, "save_state", lambda st: False):
            for m in range(2, 24 * 60, 5):
                self.check(DAY + m * MIN, lambda text, m=m: at.append(m) or True)
            self.assertEqual(len(at), 4, at)
            self.assertLessEqual(at[0], 45, "the first one at once")
            # a check up to a second late is still the one; a minute late is the next
            for past, yes in ((0, True), (5.03, True), (5.2, False), (359, False), (360, True), (-1, False)):
                with mock.patch.object(self.w.datetime, "datetime", fake_clock(DAY + (35 + past) * MIN)):
                    self.assertEqual(self.w.in_slot(DAY), yes, past)
            # no channel could send in the slot: this process remembers, and tries again every minute
            tries, slept = [], []

            def send(text):
                tries.append(1)
                return len(tries) == 4
            with mock.patch.object(self.w.time, "sleep", slept.append):
                self.assertEqual(self.check(DAY + 37 * MIN, send), 0)
                self.assertEqual((len(tries), slept), (4, [60] * 3))
                tries.clear(), slept.clear()
                self.check(DAY + 37 * MIN, lambda text: tries.append(1) and False)
                self.assertTrue(300 < len(tries) < 360, "until the next slot at most")
                # the scheduler is back meanwhile: nothing to tell
                tries.clear()
                with mock.patch.object(self.w.time, "sleep", lambda s: self.beat(DAY + 38 * MIN)):
                    self.check(DAY + 37 * MIN, lambda text: tries.append(1) and False)
                self.assertEqual(len(tries), 1)

    def test_the_second_place_is_not_one_somebody_planted(self):
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.s.tmp), "XDG_RUNTIME_DIR": str(self.s.tmp)}):
            self.assertIsNone(self.w.second_place(False), "looking makes none")
            d = self.w.second_place(True)
        self.assertEqual(d, self.s.tmp / "secrets" / "tmp" / "scheduler-watchdog", "where no sandboxed project writes")
        self.assertEqual((d.stat().st_mode & 0o777, d.parent.stat().st_mode & 0o777), (0o700, 0o700))
        with mock.patch.object(hubconf, "secrets_dir", lambda: self.s.tmp / "secrets"):
            self.assertEqual(hubconf._private_tmp_path(), d.parent, "the platform's private temp area")
        d.chmod(0o755)   # others may look in
        self.assertIsNone(self.w.second_place(True))
        d.chmod(0o700)
        with mock.patch.object(self.w.os, "getuid", lambda: os.geteuid() + 1):
            self.assertIsNone(self.w.second_place(True), "another user's")
        self.w.clear_marks()
        self.assertFalse(d.exists(), "an empty one is removed")
        (self.s.tmp / "elsewhere").mkdir(mode=0o700)
        d.symlink_to(self.s.tmp / "elsewhere")
        self.assertIsNone(self.w.second_place(True), "a link")
        d.unlink()
        shutil.rmtree(self.s.tmp / "secrets")
        self.assertIsNone(self.w.second_place(True), "no secrets directory: none is made")

    def test_a_sleep_that_cannot_be_noted_is_no_grace_for_ever(self):
        (self.s.st / "scheduler-watchdog.json").write_text(json.dumps({"sleep_gap": 0.0}))
        self.beat(DAY - 60 * MIN)
        clocks = {7: 9000.0, self.w.time.CLOCK_MONOTONIC: 500.0}
        with mock.patch.object(self.w.time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(self.w.time, "clock_gettime", side_effect=lambda c: clocks[c]), \
                mock.patch.object(self.w, "save_state", lambda st: False):
            for m in range(0, 120, 5):
                self.check(DAY + m * MIN, real_resumed=True)
        self.assertEqual(len(self.sent), 1)

    @unittest.skipIf(os.geteuid() == 0, "root looks everywhere")
    def test_a_state_folder_that_cannot_be_looked_into_is_told(self):
        self.beat(DAY)
        (self.s.st / "scheduler-watchdog.json").write_text("{}")
        self.s.st.chmod(0)
        self.addCleanup(self.s.st.chmod, 0o755)
        for m in range(0, 60, 5):
            self.assertEqual(self.check(DAY + m * MIN), 0)
        self.assertEqual(len(self.sent), 1)

    def test_nothing_it_runs_can_hold_the_alert_up(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd[0], kw.get("timeout")))
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
        self.beat(DAY - 60 * MIN)
        for platform in ("darwin", "linux"):
            with mock.patch.object(self.w.subprocess, "run", run), mock.patch.object(self.w.sys, "platform", platform):
                self.assertIn("Timeout", self.w.details())
                for fn, conf in ((self.w.send_ntfy, {"ntfy": {"topic": "t"}}), (self.w.send_macos, {})):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        fn(conf, "x")
        self.assertEqual([c for c in calls if not c[1]], [])
        self.assertEqual({pathlib.Path(c[0]).name for c in calls}, {"launchctl", "systemctl", "curl", "osascript"})

    def test_an_alert_in_the_future_does_not_silence_it(self):
        self.beat(DAY - 600 * MIN)
        (self.s.st / "scheduler-watchdog.json").write_text(json.dumps(
            {"alerting": True, "last_alert": (DAY + 99999 * MIN).isoformat()}))   # the clock was set back
        self.check(DAY), self.check(DAY + 5 * MIN)
        self.assertEqual(len(self.sent), 1)

    def test_a_recovery_on_a_full_disk_is_told_once(self):
        state = self.s.st / "scheduler-watchdog.json"
        state.write_text(json.dumps({"alerting": True, "last_alert": (DAY - 60 * MIN).isoformat(), "sleep_gap": 5.0}))
        stamp(state, DAY - 60 * MIN)
        self.beat(DAY)
        r, w = os.pipe()
        out = on_full_disk(lambda: [self.check(DAY + m * MIN, lambda text: os.write(w, b"!") and True) for m in (0, 5, 10)])
        os.close(w)
        with os.fdopen(r, "rb") as f:
            self.assertEqual((out, f.read()), ("[0, 0, 0]", b"!"))

    def test_when_it_last_told_is_read_whoever_wrote_it(self):
        state = self.s.st / "scheduler-watchdog.json"
        self.beat(DAY - 600 * MIN)
        # another version's reminder ten minutes ago: the time beside the reading is of an earlier one
        state.write_text(json.dumps({"alerting": True, "last_alert": (DAY - 10 * MIN).isoformat(),
                                     "last_alert_at": (DAY - 590 * MIN).timestamp()}))
        self.check(DAY)
        self.assertEqual(self.sent, [])
        # a recovery noted in a place that is gone by now, and an alert since
        mark = self.w.second_place(True)
        (mark / self.w.RECOVERED).write_text("")
        stamp(mark / self.w.RECOVERED, DAY - 60 * MIN)
        stamp(state, DAY - 10 * MIN)
        self.check(DAY + MIN), self.check(DAY + 6 * MIN)
        self.assertEqual(self.sent, [], "it was told")

    def test_the_hour_that_comes_twice_does_not_bring_the_reminder_forward(self):
        with zone("America/New_York"):
            told = datetime.datetime(2026, 11, 1, 1, 10, fold=1)
            self.beat(datetime.datetime(2026, 11, 1, 0, 0))
            self.check(told), self.check(told)
            self.assertEqual(len(self.sent), 1)
            self.check(datetime.datetime(2026, 11, 1, 6, 50))   # 5 h 40 on
            self.assertEqual(len(self.sent), 1)
            self.check(datetime.datetime(2026, 11, 1, 7, 15))
            self.assertEqual(len(self.sent), 2)

    def test_what_it_knows_of_sleep_outlives_a_recovery(self):
        state = self.s.st / "scheduler-watchdog.json"
        state.write_text(json.dumps({"alerting": True, "last_alert": DAY.isoformat(), "sleep_gap": 5.0,
                                     "resumed_at": (DAY + 5 * MIN).isoformat()}))
        self.beat(DAY)
        self.check(DAY)
        self.assertEqual(sorted(json.loads(state.read_text())), ["resumed_at", "sleep_gap"])

    @unittest.skipUnless(tag("v0.21.0"), "v0.21.0 is not in this checkout")
    def test_the_last_releases_watchdog_reads_what_this_one_wrote(self):
        old = self.s.tmp / "old-watchdog.py"
        old.write_text(subprocess.run(["git", "-C", str(ROOT), "show", "v0.21.0:bin/scheduler-watchdog.py"],
                                      capture_output=True, text=True, check=True).stdout)
        spec = importlib.util.spec_from_file_location("watchdog_old", old)
        m = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"AGENT_HUB_ROOT": str(self.hub), "AGENT_HUB_CONFIG": str(self.s.tmp / "config"),
                                          "AGENT_HUB_SECRETS": str(self.s.tmp / "secrets"), "AGENT_HUB_LANG_UI": "en"}):
            spec.loader.exec_module(m)
        self.beat(DAY - 60 * MIN)
        clocks = {7: 1000.0, self.w.time.CLOCK_MONOTONIC: 500.0}
        with mock.patch.object(self.w.time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(self.w.time, "clock_gettime", side_effect=lambda c: clocks[c]):
            self.check(DAY, real_resumed=True)
            clocks[7] += 3600
            self.check(DAY + 5 * MIN, real_resumed=True)   # asleep: resumed_at
            self.check(DAY + 30 * MIN, real_resumed=True), self.check(DAY + 35 * MIN, real_resumed=True)
            self.assertEqual(len(self.sent), 1)
            state = self.s.st / "scheduler-watchdog.json"
            state.write_text(json.dumps({**json.loads(state.read_text()), "resumed_at": DAY.isoformat()}))
            told = []
            for minute in (40, 500):   # an alert it was told of, and the reminder six hours on
                with mock.patch.object(m.datetime, "datetime", fake_clock(DAY + minute * MIN)), \
                        mock.patch.object(m, "send", lambda text: told.append(minute) or True), \
                        mock.patch.object(m, "diagnose", lambda: ""), mock.patch.object(m.sys, "argv", ["x"]):
                    self.assertEqual(m.main(), 0)
            self.assertEqual(told, [500])
            # and back: this one reads what that one wrote
            self.check(DAY + 505 * MIN)
            self.assertEqual(len(self.sent), 1, "told at 500")

    def test_a_tick_that_is_stuck_is_still_alerted(self):
        self.record(started=DAY.isoformat(), running=True, unfinished=0, completed=(DAY - 15 * MIN).isoformat())
        self.beat(DAY)
        for m in (30, 40, 45):
            self.check(DAY + m * MIN)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("has not ticked", self.sent[0])

    def test_its_own_state_broken_is_not_the_end_of_it(self):
        self.beat(DAY - 60 * MIN)
        for text in ("", "{", "[]", "null", '{"stale_checks": "x", "alerting": true, "last_alert": 5}',
                     '{"alerting": true}', '{"resumed_at": "soon", "sleep_gap": "x"}', "\x00"):
            with self.subTest(text=text):
                (self.s.st / "scheduler-watchdog.json").write_text(text)
                self.assertEqual(self.check(DAY), 0)
                self.assertEqual(self.check(DAY + 5 * MIN), 0)
                self.assertTrue(self.sent, "a stopped scheduler is alerted")
                json.loads((self.s.st / "scheduler-watchdog.json").read_text())
                self.sent.clear()

    def test_on_a_full_disk_it_tells_once(self):
        self.beat(DAY - 60 * MIN)
        state = self.s.st / "scheduler-watchdog.json"
        state.write_text("{}")
        (self.hub / ".logs" / "scheduler-watchdog.log").write_text("")
        r, w = os.pipe()

        def run():
            def send(text):
                os.write(w, b"!")
                return True
            return [self.check(DAY + m * MIN, send) for m in (0, 5, 10)]
        out = on_full_disk(run)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            told = f.read()
        self.assertEqual((out, told), ("[0, 0, 0]", b"!"))
        self.assertEqual(state.read_text(), "{}", "not cut")
        # space again, and the scheduler ticks: the recovery is told, and nothing stays behind
        self.beat(DAY + 20 * MIN)
        self.check(DAY + 20 * MIN)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("is back", self.sent[0])
        self.beat(DAY + 25 * MIN)
        self.check(DAY + 25 * MIN)
        self.assertEqual(len(self.sent), 1)

    def test_one_channel_that_raises_does_not_silence_the_others(self):
        conf = {"alerts": {"ntfy": {"topic": "t"}, "email": {"host": "h", "to": "x@y"}}}
        (self.s.tmp / "config" / "brunna.json").write_text(json.dumps(conf))
        with mock.patch.object(self.w, "send_ntfy", boom), mock.patch.object(self.w, "send_email", lambda c, t: True):
            self.assertTrue(self.w.send("x"))


class Schedulectl(unittest.TestCase):
    """schedulectl reads and saves the state as a tick does."""

    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("schedulectl_robust", str(ROOT / "bin" / "schedulectl"))
        spec = importlib.util.spec_from_loader("schedulectl_robust", loader)
        self.c = importlib.util.module_from_spec(spec)
        loader.exec_module(self.c)
        self.s = Sched(self)
        self.hub = self.s.tmp / "ctl"
        (self.hub / ".state").mkdir(parents=True)
        self.state = self.hub / ".state" / "scheduler-last-run.json"
        self.s.m.LOG_DIR = self.s.tmp / "logs"
        for p in (mock.patch.object(self.c, "HUB", self.hub), mock.patch.object(self.c, "all_jobs", lambda: [job("a"), job("b")]),
                  mock.patch.object(self.c, "scheduler_module", lambda: self.s.m), mock.patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)

    def mark(self):
        self.c._mark_ran_today("b")
        return json.loads(self.state.read_text())

    def test_a_state_that_vanished_is_not_a_fresh_install(self):
        self.s.m.save_state({"a": "2026-10-30", "p/x.event_seq": 5}, self.state)
        self.state.unlink()
        got = self.mark()
        self.assertEqual((got["a"], got["p/x.event_seq"], "b" in got), ("2026-10-30", 5, True))

    def test_a_state_that_cannot_be_read_is_moved_aside(self):
        self.s.m.save_state({"a": "2026-10-30"}, self.state)
        self.state.write_text("")
        self.assertEqual(self.mark()["a"], "2026-10-30")
        self.assertEqual(len(list(self.state.parent.glob("*.broken-*"))), 1)
        self.state.write_text("")
        self.assertEqual(self.s.m.peek_state(self.state)["a"], "2026-10-30", "only looking changes nothing")
        self.assertEqual(self.state.read_text(), "")

    def test_without_a_copy_the_due_jobs_are_held(self):
        self.state.write_text("{")
        now = datetime.datetime.combine(datetime.date.today(), datetime.time(23, 0))
        with mock.patch.object(self.c.datetime, "datetime", fake_clock(now)):
            self.assertEqual(sorted(self.mark()), ["a", "b"])

    def test_the_copy_is_written_too_and_a_full_disk_changes_nothing(self):
        self.mark()
        self.assertEqual(self.s.m.read_state(self.s.m.state_copy(self.state)), self.s.m.read_state(self.state))
        before = self.state.read_text()
        self.assertEqual(on_full_disk(lambda: self.c.edit_state(lambda st: st.update(x=1))), "SystemExit")
        self.assertEqual(self.state.read_text(), before)


@unittest.skipUnless(tag("v0.21.0"), "v0.21.0 is not in this checkout")
class Rollback(unittest.TestCase):
    def test_the_last_releases_scheduler_reads_the_state(self):
        s = Sched(self)
        s.main(DAY, jobs=[{"name": "i", "every_minutes": 60, "command": "echo i"}, job("d")])
        tree = s.tmp / "old"
        tree.mkdir()
        out = subprocess.run(["git", "-C", str(ROOT), "archive", "v0.21.0", "bin", "locales"], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(tree)], input=out.stdout, check=True)
        code = ("import datetime, json, sys\nsys.path.insert(0, sys.argv[1] + '/bin')\nimport scheduler\n"
                "st = json.loads(open(sys.argv[2]).read()); d = datetime.datetime(2026, 10, 30, 12, 30)\n"
                "print([scheduler.is_due(j, st, t, t.date().isoformat()) for j in ({'name': 'i', 'every_minutes': 60}, "
                "{'name': 'd', 'time': '06:00'}) for t in (d, d + datetime.timedelta(days=1))])\n")
        env = {**os.environ, "AGENT_HUB_ROOT": str(tree), "BRUNNA_ROOT": str(tree)}
        r = subprocess.run([sys.executable, "-B", "-c", code, str(tree), str(s.m.STATE_FILE)], env=env, capture_output=True,
                           text=True)
        self.assertEqual(r.stdout, "[False, True, False, True]\n", r.stderr)


if __name__ == "__main__":
    unittest.main()
