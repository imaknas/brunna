"""A check that found something hasn't failed (docs/design/alert-routing.md step 3).

A job that declares found_exit and exits alert.FOUND ran, and what it found is with whoever must hear it: the scheduler
takes it as a completed run (not retried, not failing, no job.failed); the status page shows "problems found". Any
other job's 4 is a failure. alert.once says whether the problems were delivered, which the checks need to answer FOUND.

    bin/py -m unittest tests.test_check_found
"""
import datetime
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import hubconf  # noqa: E402
from test_scheduler_tick import DAY, Tick  # noqa: E402


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CHECK = {"name": "brunna/check", "time": "06:00", "command": "exit 4", "found_exit": True,
         "max_attempts_per_day": 2}


class Scheduler(unittest.TestCase):
    def setUp(self):
        self.t = Tick(self)

    def failing(self) -> dict:
        f = self.t.m.FAILING_FILE
        return json.loads(f.read_text()) if f.exists() else {}

    def job_failed(self) -> list:
        return [b for b in self.t.bus if b["argv"][:2] == ["publish", "job.failed"]]

    def test_found_from_a_declared_check_is_a_completed_run(self):
        for i in range(3):
            self.t.tick([CHECK], DAY + datetime.timedelta(minutes=15 * i))
        self.assertEqual(self.t.commands(), ["exit 4"], "not retried before its next turn")
        self.assertEqual(self.t.state()["brunna/check"], "2026-10-30")
        self.assertEqual((self.failing(), self.job_failed()), ({}, []), "not failing, no job.failed")
        log = (self.t.m.LOG_DIR / "scheduler.log").read_text()
        self.assertIn("job 'brunna/check' finished, problems found and sent (exit 4)", log)

    def test_an_hourly_check_sets_last_ok(self):
        job = {**CHECK, "every_minutes": 60}
        del job["time"]
        self.t.tick([job], DAY)
        self.t.tick([job], DAY + datetime.timedelta(minutes=15))
        self.assertEqual(len(self.t.ran), 1)
        self.assertEqual(self.t.state()["brunna/check.last_ok"], DAY.isoformat(timespec="seconds"))

    def test_four_from_any_other_job_is_a_failure(self):
        job = {k: v for k, v in CHECK.items() if k != "found_exit"}
        for i in range(2):
            self.t.tick([job], DAY + datetime.timedelta(minutes=15 * i))
        self.assertEqual(len(self.t.ran), 2, "retried")
        self.assertIn("brunna/check", self.failing())
        self.assertEqual(len(self.job_failed()), 2)

    def test_a_declared_check_that_broke_is_a_failure(self):
        self.t.tick([{**CHECK, "command": "exit 1"}], DAY)
        self.assertIn("brunna/check", self.failing())
        self.assertEqual(len(self.job_failed()), 1)

    def test_an_open_failure_clears_when_the_check_runs_and_finds(self):
        self.t.tick([{**CHECK, "command": "exit 1"}], DAY)
        self.t.tick([CHECK], DAY + datetime.timedelta(minutes=15))
        self.assertEqual(self.failing(), {}, "its scheduler-job alert recovers and the letter is taken back")

    def test_a_background_run_carries_the_flag(self):
        self.t.tick([{**CHECK, "background": True}], DAY)
        self.assertTrue(self.t.spawned[0][6].get("found_exit"))
        self.t.tick([{**CHECK, "background": True, "found_exit": False}], DAY + datetime.timedelta(days=1))
        self.assertNotIn("found_exit", self.t.spawned[1][6])

    def test_reconcile_reads_the_flag_from_the_run_file(self):
        runs = self.t.m.RUNS_DIR
        (runs / "brunna__check.json").write_text(json.dumps({"date": "2026-10-30", "started": "2026-10-30T11:00:00",
                                                             "pid": 1, "exit": 4, "found_exit": True}))
        (runs / "brunna__other.json").write_text(json.dumps({"date": "2026-10-30", "started": "2026-10-30T11:00:00",
                                                             "pid": 1, "exit": 4}))
        self.t.tick([], DAY)
        self.assertEqual(self.t.state().get("brunna/check"), "2026-10-30")
        self.assertNotIn("brunna/other", self.t.state())

    def test_a_run_by_hand(self):
        m = self.t.m
        with mock.patch.object(m, "all_jobs", lambda log_errors=False: ([CHECK, {"name": "p/x", "command": "x"}], {})):
            m.record_run("brunna/check", 4)
            self.assertEqual(self.failing(), {})
            m.record_run("p/x", 4)
            self.assertIn("p/x", self.failing())
            m.record_run("brunna/check", 1)
            self.assertIn("brunna/check", self.failing())

    def test_the_two_constants_agree(self):
        import alert
        self.assertEqual(self.t.m.FOUND, alert.FOUND)


class StatusPage(unittest.TestCase):
    def test_the_found_line_reads_as_problems_found(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sched = load("sched_found", ROOT / "bin" / "scheduler.py")
        page = load("gsp_found", ROOT / "bin" / "gen-status-page.py")
        log = tmp / "scheduler.log"
        log.write_text("\n".join([
            "[2026-10-30 06:00:01] running job 'brunna/check': x",
            f"[2026-10-30 06:00:02] {sched.finished_line('brunna/check', 4)}",
            "[2026-10-30 06:00:03] running job 'brunna/ok': x",
            f"[2026-10-30 06:00:04] {sched.finished_line('brunna/ok', 0)}",
            "[2026-10-30 06:00:05] running job 'brunna/bad': x",
            "[2026-10-30 06:00:06] job 'brunna/bad' FAILED (exit 4), will retry next tick"]) + "\n")
        with mock.patch.object(page, "SCHED_LOG", log):
            runs, _tick = page.scheduler_runs()
        self.assertEqual({k: v["result"] for k, v in runs.items()},
                         {"brunna/check": "found", "brunna/ok": "ok", "brunna/bad": "failed (exit 4)"})


class Registry(unittest.TestCase):
    def errs(self, doc: dict, stem: str) -> list[str]:
        import registry
        return [e for e in registry.validate_runs(doc, stem, registry.dirs(doc)) if "found_exit" in e]

    def test_only_the_platform_and_modules_may_declare_it(self):
        job = {"name": "check", "every": "1h", "run": "true", "found_exit": True}
        self.assertEqual(self.errs({"name": hubconf.hub_project(), "jobs": [job]}, hubconf.hub_project()), [])
        self.assertEqual(self.errs({"name": "mod", "type": "module", "jobs": [job]}, "mod"), [])
        self.assertTrue(self.errs({"name": "proj", "paths": {"workspace": "/nonexistent"}, "jobs": [job]}, "proj"),
                        "a project's job would hide its failures behind it")

    def test_not_on_an_event_job_an_agent_job_or_with_another_value(self):
        hub = hubconf.hub_project()
        for job in ({"name": "e", "trigger": {"events": ["merge.done"]}, "run": "true", "found_exit": True},
                    {"name": "a", "every": "1h", "kind": "agent", "prompt": "jobs/a.md", "found_exit": True},
                    {"name": "c", "every": "1h", "run": "true", "found_exit": "yes"}):
            with self.subTest(job=job["name"]):
                self.assertTrue(self.errs({"name": hub, "jobs": [job]}, hub))

    def test_the_scheduler_gets_it(self):
        import registry
        hub = hubconf.hub_project()
        doc = {"name": hub, "jobs": [{"name": "check", "every": "1h", "run": "true", "found_exit": True}]}
        proj = {"name": "proj", "paths": {"workspace": "/nonexistent"},
                "jobs": [{"name": "check", "every": "1h", "run": "true", "found_exit": True}]}
        jobs, _s, _r = registry.scheduler_jobs({"projects": {hub: doc, "proj": proj}}, set())
        flags = {j["name"]: j.get("found_exit") for j in jobs}
        self.assertEqual(flags, {f"{hub}/check": True, "proj/check": None}, "never from an entry that may not have it")

    def test_the_checks_declare_it(self):
        import yaml
        want = {"modules/claude.yaml": "claude-login", "modules/codex.yaml": "codex-login",
                "modules/assistant.yaml": "push-reach"}
        for f, name in want.items():
            jobs = {j["name"]: j for j in yaml.safe_load((ROOT / f).read_text())["jobs"]}
            self.assertIs(jobs[name].get("found_exit"), True, f)


class Checks(unittest.TestCase):
    """What check-secrets and check-runtime-login answer: FOUND only when what they found was delivered and the check
    itself worked."""

    def secrets(self, argv: list[str], delivered: bool) -> int:
        m = load("check_secrets_found", ROOT / "bin" / "check-secrets.py")
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        index = {"projects": {"demo": {"name": "demo", "paths": {"workspace": str(tmp)},
                                       "secrets": [{"name": "gone", "path": "gone.key"}]}}, "modules": {}}
        with mock.patch.object(m.registry, "refresh", lambda: index), \
                mock.patch.object(m.alert, "probe", lambda: True), \
                mock.patch.object(m.alert, "clear_failure", lambda k: None), \
                mock.patch.object(m.alert, "once", lambda *a, **k: delivered), \
                mock.patch("builtins.print"):
            return m.main(argv)

    def test_secrets(self):
        import alert
        self.assertEqual(self.secrets([], True), alert.FOUND)
        self.assertEqual(self.secrets([], False), 1, "not delivered")
        self.assertEqual(self.secrets(["demo"], True), 1, "projects named: no alert is sent, so nothing was delivered")

    def runtime_login(self, problems, delivered: bool = True):
        m = load("check_runtime_login_found", ROOT / "bin" / "check-runtime-login.py")
        import alert

        def check():
            if isinstance(problems, Exception):
                raise problems
            return problems
        with mock.patch.object(m.runtime_ports, "RUNTIMES", {"tool": None}), \
                mock.patch.object(m.runtime_ports, "platform_work", lambda: {"tool"}), \
                mock.patch.object(m.runtime_ports, "_module", lambda n: mock.Mock(login_check=check)), \
                mock.patch.object(alert, "once", lambda *a, **k: delivered), \
                mock.patch("builtins.print"):
            return m.main(["--runtime", "tool"])

    def test_runtime_login(self):
        import alert
        self.assertEqual(self.runtime_login([]), 0)
        self.assertEqual(self.runtime_login([("status", "the login doesn't work")]), alert.FOUND)
        self.assertEqual(self.runtime_login([("status", "the login doesn't work")], delivered=False), 1)
        self.assertEqual(self.runtime_login(RuntimeError("x")), 1, "the check raised")
        self.assertEqual(self.runtime_login([("broke", "codex login status could not run: TimeoutExpired")]), 1)


class Once(unittest.TestCase):
    """alert.once's answer: whether the problems are with whoever must hear them."""

    def setUp(self):
        self.a = load("alert_found", ROOT / "bin" / "alert.py")
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in ("SENT", "FAILED", "LOG"):
            p = mock.patch.object(self.a, name, tmp / name)
            p.start()
            self.addCleanup(p.stop)
        self.ok = True
        for p in (mock.patch.object(self.a, "send", lambda *a, **k: self.ok),
                  mock.patch.object(self.a, "route", lambda key: "owner"),
                  mock.patch.object(self.a, "configured", lambda: ["ntfy"])):
            p.start()
            self.addCleanup(p.stop)

    def test_sent_and_told_before(self):
        self.assertTrue(self.a.once("k", ["x"], "fine"))
        self.ok = False
        self.assertTrue(self.a.once("k", ["x"], "fine"), "open and unchanged: told before")
        with mock.patch.object(self.a, "REMIND", datetime.timedelta(0)):
            self.assertTrue(self.a.once("k", ["x"], "fine"), "a reminder that didn't go: told before")

    def test_not_delivered(self):
        self.ok = False
        self.assertFalse(self.a.once("k", ["x"], "fine"))
        self.assertFalse(self.a.once("k", ["x", "y"], "fine"), "a new problem that didn't go")

    def test_a_test_held_from_the_live_state_delivers_nothing(self):
        with mock.patch.object(self.a, "_held", lambda: True):
            self.assertFalse(self.a.once("k", ["x"], "fine"))

    def test_a_letter_to_the_platforms_agents_counts(self):
        with mock.patch.object(self.a, "route", lambda key: "brunna"), \
                mock.patch.object(self.a, "_agent", lambda *a: True):
            self.assertTrue(self.a.once("k", ["x"], "fine"))

    def test_a_daily_check_starting_earlier_still_reminds(self):
        self.assertTrue(self.a.once("k", ["x"], "fine"))
        sent = json.loads(self.a.SENT.read_text())
        sent["k"]["at"] = (datetime.datetime.now() - datetime.timedelta(hours=23, minutes=50)).isoformat()
        self.a.SENT.write_text(json.dumps(sent))
        reminded = []
        with mock.patch.object(self.a, "send", lambda text, *a, **k: reminded.append(text) or True):
            self.a.once("k", ["x"], "fine")
        self.assertEqual(len(reminded), 1)


if __name__ == "__main__":
    unittest.main()
