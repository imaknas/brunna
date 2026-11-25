"""What a run on the owner's API key may spend (bin/spending.py), which login a run takes (runtime_ports.default_login), and how
the key is handed over (runtime_claude.key_passed)."""
from __future__ import annotations

import concurrent.futures
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import completion  # noqa: E402
import hubconf  # noqa: E402
import llm_ready  # noqa: E402
import runtime_claude as rc  # noqa: E402
import runtime_ports  # noqa: E402
import spending  # noqa: E402
import usage_log  # noqa: E402

KEY = "sk-ant-api03-nobodys"


class Base(unittest.TestCase):
    CONF: dict = {}

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conf = {"runtime.default": "claude", "runtime.login": "key", "llm": {"budget": {"daily_usd": 10}}, **self.CONF}
        self.secrets = self.tmp / "secrets"
        self.secrets.mkdir(mode=0o700)
        self.log = self.tmp / "state" / "llm-usage.jsonl"
        for target, attr, value in ((hubconf, "get", lambda k, d=None: self.conf.get(k, d)), (hubconf, "secrets_dir", lambda: self.secrets),
                                    (hubconf, "STATE", self.tmp / "state"), (completion, "USAGE", self.log),
                                    (spending, "tidied", lambda rows: True)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def key(self, text=KEY + "\n", mode=0o600) -> pathlib.Path:
        f = self.secrets / rc.KEY_FILE
        f.unlink(missing_ok=True)
        f.write_text(text)
        f.chmod(mode)
        return f

    def ran(self, cost, point="p", billing="metered", day=None) -> None:
        usage_log.append(self.log, {"ts": day or usage_log.now(), "point": point, "cost_usd": cost, "billing": billing})


class Login(Base):
    def test_nothing_set_is_never_a_paid_login(self):
        self.key()
        self.conf["runtime.login"] = None
        self.assertEqual(runtime_ports.default_login(), "token")
        self.conf["runtime.login"] = "key"
        self.assertEqual(runtime_ports.default_login(), "key")

    def test_only_the_platform_s_agent_and_only_a_login_it_has(self):
        self.assertEqual((runtime_ports.default_login("claude"), runtime_ports.default_login("codex")), ("key", "token"))
        self.conf["runtime.default"] = "codex"
        self.assertEqual((runtime_ports.default_login(), runtime_ports.default_login("claude")), ("token", "token"))
        self.conf.update({"runtime.default": "claude", "runtime.login": "other"})
        self.assertEqual(runtime_ports.default_login(), "token")

    def test_a_login_that_does_not_exist_is_refused(self):
        for mode in ("Key", "keys", ""):
            self.assertIsNone(rc.auth_env(mode, {})[0])

    def test_the_config_check(self):
        for conf, bad in (({"login": "key"}, True), ({"login": "token"}, False)):
            got = self.check({"runtime": {"default": "claude", **conf}})
            self.assertEqual(any("llm.budget.daily_usd" in e for e in got), bad, got)
        self.assertFalse(any("runtime.login" in e for e in self.check({"runtime": {"login": "key"}, "llm": {"budget": {"daily_usd": 5}}})))
        self.assertTrue(any("no login of" in e for e in self.check({"runtime": {"default": "codex", "login": "key"}})))

    def check(self, doc) -> list[str]:
        d = self.tmp / "cfg"
        d.mkdir(exist_ok=True)
        base = json.loads((ROOT / "brunna.example.json").read_text())
        (d / "brunna.json").write_text(json.dumps({**base, **doc}))
        import names
        env = dict(os.environ)
        for var, value in (("CONFIG", str(d)), ("LANG_UI", "en"), ("STATE", str(self.tmp / "cfg-state"))):
            names.env_set(env, var, value)
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "config-check"), "--child"], capture_output=True, text=True, env=env)
        return (r.stdout + r.stderr).splitlines()


class Handover(Base):
    def test_only_where_it_is_the_login_chosen(self):
        self.key()
        for chosen in (None, "token"):
            self.conf["runtime.login"] = chosen
            self.assertIsNone(rc.auth_env("key", {})[0])

    def test_the_environment_of_a_run_on_the_key(self):
        self.key()
        env, why = rc.auth_env("key", {"PATH": "/bin", rc.TOKEN_VAR: "t", "ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "http://x"})
        self.assertEqual(why, "")
        self.assertTrue(rc.on_key(env))
        self.assertEqual((env["PATH"], env[rc.MANAGED_VAR]), ("/bin", "1"))
        self.assertEqual({rc.TOKEN_VAR, "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"} & set(env), set())
        self.assertNotIn(KEY, json.dumps(env))
        self.assertFalse(rc.on_key(rc.clean_env(env)))

    def test_a_key_file_only_the_owner_reads(self):
        for make in (lambda: None, lambda: self.key(mode=0o644), lambda: self.key("two words"), lambda: self.key("a\nb"), lambda: self.key(""),
                     lambda: self.key("ké")):
            (self.secrets / rc.KEY_FILE).unlink(missing_ok=True)
            make()
            env, why = rc.auth_env("key", {})
            self.assertIsNone(env)
            self.assertTrue(why)
            self.assertNotIn(KEY, why)
        (self.secrets / rc.KEY_FILE).unlink(missing_ok=True)
        (self.tmp / "elsewhere").write_text(KEY)
        (self.secrets / rc.KEY_FILE).symlink_to(self.tmp / "elsewhere")
        self.assertIsNone(rc.auth_env("key", {})[0])
        (self.secrets / rc.KEY_FILE).unlink()
        os.mkfifo(self.secrets / rc.KEY_FILE, 0o600)
        self.assertIsNone(rc.auth_env("key", {})[0])

    def test_it_goes_on_a_pipe_read_once(self):
        self.key()
        env, _why = rc.auth_env("key", {"PATH": os.environ["PATH"]})
        with rc.key_passed(env) as (env, fds):
            self.assertEqual(str(fds[0]), env[rc.KEY_FD_VAR])
            r = subprocess.run([sys.executable, "-c", "import os,sys; fd=int(os.environ[sys.argv[1]]); print(os.read(fd, 999).decode()); print(os.read(fd, 9))",
                                rc.KEY_FD_VAR], env=env, pass_fds=fds, capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), [KEY, "b''"])
        with self.assertRaises(OSError):
            os.fstat(fds[0])

    def test_no_other_run_gets_one(self):
        self.key()
        with rc.key_passed({"PATH": "/bin"}) as (env, fds):
            self.assertEqual((env, fds), ({"PATH": "/bin"}, ()))

    def test_a_key_gone_since(self):
        self.key()
        env, _why = rc.auth_env("key", {})
        (self.secrets / rc.KEY_FILE).unlink()
        with self.assertRaises(ValueError):
            with rc.key_passed(env):
                pass

    def test_how_it_is_paid_for(self):
        self.assertEqual({rc.billing("key", {}, r) for r in (True, False)}, {"metered"})
        self.assertTrue(spending.held("claude", "key"))
        self.assertFalse(spending.held("claude", "token"))
        self.assertFalse(spending.held("claude", "subscription"))
        self.assertFalse(spending.held("codex", "token"))


class Budget(Base):
    SPEC = {"shape": "agent", "model_group": "standard"}

    def test_none_set_starts_nothing(self):
        self.conf["llm"] = None
        rid, why = spending.reserve("p", "claude", "m", 0.1)
        self.assertIsNone(rid)
        self.assertIn("daily_usd", why)

    def test_what_is_held_and_what_was_spent_count(self):
        self.ran(4.0)
        self.ran(50.0, billing="subscription")
        self.ran(50.0, day="2000-01-01T00:00:00+00:00")
        a, _w = spending.reserve("p", "claude", "m", 4.0)
        self.assertIsNotNone(a)
        b, why = spending.reserve("p", "claude", "m", 4.0)
        self.assertIsNone(b)
        self.assertIn("2.00", why)
        completion.record_run("p", "claude", "m", "metered", 0.5, "end", None, None, None, a, reserved=4.0)
        c, _w = spending.reserve("p", "claude", "m", 4.0)
        self.assertIsNotNone(c, "its real cost replaced what was held")

    def test_one_with_no_cost_counts_as_its_cap(self):
        a, _w = spending.reserve("p", "claude", "m", 4.0)
        completion.record_run("p", "claude", "m", "metered", None, "", None, None, None, a, reserved=4.0)
        self.assertEqual(completion.today_cost(), 4.0)
        self.assertFalse(completion.unknown_cost_today())

    def test_one_that_never_started_gives_it_back(self):
        a, _w = spending.reserve("p", "claude", "m", 9.0)
        usage_log.drop(self.log, a)
        self.assertIsNotNone(spending.reserve("p", "claude", "m", 9.0)[0])

    def test_one_being_settled_is_never_not_counted(self):
        a, _w = spending.reserve("p", "claude", "m", 9.0)
        usage_log._claim(self.log, a)
        self.assertIsNone(spending.reserve("p", "claude", "m", 9.0)[0])

    def test_a_log_that_cannot_be_read_starts_nothing(self):
        self.ran(1.0)
        self.log.chmod(0)
        self.addCleanup(self.log.chmod, 0o600)
        if os.access(self.log, os.R_OK):
            self.skipTest("root reads anything")
        rid, why = spending.reserve("p", "claude", "m", 0.1)
        self.assertIsNone(rid)
        self.assertTrue(why)

    def test_nor_one_that_cannot_be_written(self):
        with mock.patch.object(usage_log, "begin", lambda *a, **k: None):
            self.assertIsNone(spending.reserve("p", "claude", "m", 0.1)[0])

    def test_many_at_once_never_pass_the_budget(self):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            got = [r for r, _w in pool.map(lambda i: spending.reserve("p", "claude", "m", 1.0), range(24))]
        self.assertEqual(sum(r is not None for r in got), 10)

    def test_the_tidy_up_s_share(self):
        with mock.patch.object(spending, "tidied", lambda rows: False):
            self.assertIsNone(spending.reserve("consolidate", "claude", "m", 7.0)[0])
            self.assertIsNotNone(spending.reserve("librarian-state", "claude", "m", 7.0)[0])
        self.assertIsNone(spending.room("consolidate", 3.0))

    def test_caps(self):
        cap = lambda point, spec=self.SPEC, **k: spending.cap(point, spec, "claude", **k)   # noqa: E731
        self.assertEqual((cap("p"), cap("p", group="strong"), cap("p", {"model_group": "default"})), (1.0, 4.0, 1.0))
        self.assertEqual(cap("p", {"shape": "completion", "model_group": "fast"}), 0.10)
        strong = runtime_ports.models("claude")["strong"]
        self.assertEqual(cap("p", {"model_group": "fast"}, model=strong), 4.0, "a model the owner pinned: its own group's")
        self.conf["llm"] = {"budget": {"daily_usd": 10, "caps": {"p": 7, "q": "x", "r": -1}}}
        self.assertEqual((cap("p"), cap("q"), cap("r")), (7.0, 1.0, 1.0))


class Blocked(Base):
    def test_a_login_that_is_not_held_is_never_blocked(self):
        self.conf.update({"runtime.login": None, "llm": None})
        for point in ("librarian-state", "agent-job", "mailbox-escalation", "reflect-week"):
            self.assertIsNone(spending.blocked(point, "p"))

    def test_the_connectors_jobs_are_the_subscription_s(self):
        self.assertIsNone(spending.blocked("agent-job", "p", auth="subscription"))

    def test_why(self):
        self.assertIn("API key", spending.blocked("librarian-state"))
        self.key()
        self.assertIsNone(spending.blocked("librarian-state"))
        self.assertIsNotNone(spending.blocked("librarian-state", more=9.9), "what the caller has just started counts")
        self.ran(9.9)
        self.assertIn("librarian-state", spending.blocked("librarian-state"))
        self.conf["llm"] = None
        self.assertIn("daily_usd", spending.blocked("librarian-state"))

    def test_a_project_s_own_work_needs_the_owner_s_leave(self):
        self.key()
        import registry
        doc = {}
        with mock.patch.object(registry, "refresh", lambda: {"projects": {"p": doc}}):
            for point in spending.PROJECT_WORK:
                self.assertIn("metered_models", spending.blocked(point, "p"))
            self.assertIsNone(spending.blocked("mailbox-escalation", "p"), "the platform's own work for it doesn't")
            self.assertIsNone(spending.blocked("agent-job", None), "nor the owner's own schedule")
            doc["metered_models"] = True
            self.assertIsNone(spending.blocked("agent-job", "p"))

    def test_stopped_at_its_cap(self):
        self.key()
        self.conf["llm"] = {"budget": {"daily_usd": 10, "caps": {"librarian-state": 0.3}}}
        spending.note_stop("librarian-state", "p", 0.3)
        spending.note_stop("agent-job", "p", 1.0)
        self.assertIn("caps.librarian-state", spending.blocked("librarian-state", "p"))
        self.assertIsNone(spending.blocked("librarian-state", "q"))
        self.assertIsNone(spending.stopped("agent-job", "p", 1.0), "only work that would start over")
        self.conf["llm"]["budget"]["caps"]["librarian-state"] = 0.6
        self.assertIsNone(spending.blocked("librarian-state", "p"), "its cap was raised")
        self.conf["llm"]["budget"]["caps"]["librarian-state"] = 0.3
        old = time.time() - spending.BACKOFF_DAYS * 86400 - 60
        os.utime(spending._stop_file("librarian-state", "p"), (old, old))
        self.assertIsNone(spending.blocked("librarian-state", "p"))

    def test_the_tidy_up_s_day(self):
        with mock.patch.object(llm_ready, "librarian", lambda: None):
            for state, want in ((None, True), ({}, True), ({f"{hubconf.hub_project()}/librarian": "2000-01-01"}, False),
                                ({f"{hubconf.hub_project()}/librarian": datetime.date.today().isoformat()}, True)):
                f = hubconf.STATE / "scheduler-last-run.json"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.unlink(missing_ok=True)
                if state is not None:
                    f.write_text(json.dumps(state))
                with mock.patch.object(spending, "tidied", REAL_TIDIED):
                    self.assertEqual(spending.tidied([]), want, state)
        with mock.patch.object(llm_ready, "librarian", lambda: "off"):
            self.assertTrue(REAL_TIDIED([]))


REAL_TIDIED = spending.tidied


class Requests(Base):
    def target(self):
        return completion.runtime_target("claude", "m", "key")

    def run_it(self, answer, **spec):
        seen = []

        class Impl:
            @staticmethod
            def complete(req, conf):
                seen.append(req.max_cost_usd)
                return answer(req) if callable(answer) else answer
        with mock.patch.object(runtime_ports, "load_completion", lambda n: Impl):
            return completion.run("reflect-week", {"model_group": "standard", **spec}, "p", None, target=self.target()), seen

    def test_held_and_settled(self):
        self.key()
        res = runtime_ports.CompletionResult(True, text="ok", stop="end", cost_usd=0.02)
        out, seen = self.run_it(res)
        self.assertEqual((out["result"], seen), ("ok", [0.5]))
        self.assertEqual(completion.today_cost(), 0.02)

    def test_not_sent_when_it_does_not_fit(self):
        self.key()
        self.ran(9.8)
        with self.assertRaises(completion.Refused):
            self.run_it(runtime_ports.CompletionResult(True, text="ok"))

    def test_one_with_no_answer_counts_as_its_cap(self):
        self.key()
        with self.assertRaises(completion.Refused):
            self.run_it(runtime_ports.CompletionResult(False, stop="error", error="timed out"))
        self.assertEqual(completion.today_cost(), 0.5)

    def test_one_that_never_started_costs_nothing(self):
        with self.assertRaises(completion.Refused):
            self.run_it(runtime_ports.CompletionResult(False, stop="error", error="no key", started=False))
        self.assertEqual(completion.today_cost(), 0.0)

    def test_a_plan_s_requests_are_as_before(self):
        self.conf["runtime.login"] = None
        with mock.patch.object(completion, "settings", lambda: None):
            seen = []

            class Impl:
                @staticmethod
                def complete(req, conf):
                    seen.append(req.max_cost_usd)
                    return runtime_ports.CompletionResult(True, text="ok", stop="end", cost_usd=99.0)
            with mock.patch.object(runtime_ports, "load_completion", lambda n: Impl):
                completion.run("reflect-week", {"model_group": "standard"}, "p", None, target=completion.runtime_target("claude", "m", "token"))
        self.assertEqual((seen, completion.today_cost()), ([None], 0.0))


class Flags(Base):
    def test_the_cap_is_the_tool_s_to_hold(self):
        with mock.patch.object(rc, "_described_exe", lambda: "claude"):
            cmd = rc._flags(runtime_ports.Task(point="x", prompt="p", spec={"restricted": True, "tools": ""}, env={"PATH": "/bin"}, max_cost_usd=0.25))
            self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.2500")
            self.assertNotIn("--max-budget-usd", rc._flags(runtime_ports.Task(point="x", prompt="p", spec={"restricted": True}, env={"PATH": "/bin"})))
        req = runtime_ports.CompletionRequest(point="x", prompt="p", max_cost_usd=0.1)
        cmd = rc.completion_command(req, "claude")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.1000")
        self.assertNotIn("--max-budget-usd", rc.completion_command(req._replace(max_cost_usd=None), "claude"))


class Scheduled(Base):
    def setUp(self):
        super().setUp()
        import importlib.util
        spec = importlib.util.spec_from_file_location("scheduler_spending", ROOT / "bin" / "scheduler.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_a_job_that_cannot_start_keeps_its_turn(self):
        asked = []
        with mock.patch.object(spending, "blocked", lambda *a, **k: asked.append(a) or "no budget"), mock.patch.object(self.m, "log", lambda *_: None):
            job = {"name": "p/j", "project": "p", "command": "AGENT_JOB_TIER=strong AGENT_JOB_ALLOW='[\"Read\"]' AGENT_JOB_PROJECT=p /x/run-agent-job.sh p__j /w /p.md",
                   "every_minutes": 1}
            with mock.patch.object(self.m, "runs_today", lambda *a: True), mock.patch.object(self.m, "is_due", lambda *a: True), \
                    mock.patch.object(self.m, "waiting_on", lambda *a: None):
                self.assertIsNone(self.m.turn(job, {}, {}, set(), datetime.datetime.now(), "2026-01-01"))
            self.assertEqual(asked, [("agent-job-confined", "p", "strong", None)])
            self.assertEqual(list(spending.waiting()), ["p/j"], "and is shown")
            self.assertIsNone(self.m.run_blocked({"name": "x", "command": "bin/py bin/librarian.py"}), "only an agent job's run")
        with mock.patch.object(spending, "blocked", lambda *a, **k: None):
            with mock.patch.object(self.m, "runs_today", lambda *a: True), mock.patch.object(self.m, "is_due", lambda *a: True), \
                    mock.patch.object(self.m, "waiting_on", lambda *a: None):
                self.assertIsNotNone(self.m.turn(job, {}, {}, set(), datetime.datetime.now(), "2026-01-01"))


    def test_a_job_that_waits_holds_nobody(self):
        job = {"name": "p/j", "project": "p", "command": "/x/run-agent-job.sh p__j /w /p.md", "time": "00:00"}
        now = datetime.datetime.now()
        with mock.patch.object(self.m, "runs_today", lambda *a: True):
            for why, want in (("not allowed", False), (None, True)):
                with mock.patch.object(spending, "lasting", lambda *a, _w=why, **k: bool(_w)):
                    self.assertEqual(self.m.unfinished_today("p/j", job, {}, set(), now, now.date().isoformat()), want)

    def test_what_the_tidy_up_waits_for(self):
        hub = hubconf.hub_project()
        by = {f"{hub}/librarian": {"name": f"{hub}/librarian", "after": ["a"]}, "a": {"name": "a", "after": ["b"]}, "b": {"name": "b", "after": ["a"]},
              "c": {"name": "c", "after": ["a"]}}
        self.assertEqual([n for n in by if self.m.before_tidy_up(by[n], by)], [f"{hub}/librarian", "a", "b"])

    def test_what_one_tick_started_counts_for_the_next_job(self):
        self.key()
        self.conf["llm"] = {"budget": {"daily_usd": 2}}
        job = {"name": "x", "project": None, "command": "/x/run-agent-job.sh x /w /p.md"}
        got = []
        for _i in range(4):
            got.append(self.m.run_blocked(job) is None)
            if got[-1]:
                self.m.run_started(job)
        self.assertEqual(got, [True, True, False, False])


class Lasting(Base):
    def test_only_what_does_not_pass_by_itself(self):
        self.key()
        self.conf["llm"] = {"budget": {"daily_usd": 2}}
        rid, _w = spending.reserve("x", "claude", "m", 2.0)
        self.assertIsNotNone(spending.blocked("agent-job"))
        self.assertFalse(spending.lasting("agent-job"), "a run under way holds it just now")
        completion.record_run("x", "claude", "m", "metered", 1.5, "end", None, None, None, rid, reserved=2.0)
        self.assertTrue(spending.lasting("agent-job"), "spent is spent")
        self.conf["llm"] = {"budget": {"daily_usd": 0.5}}
        self.assertTrue(spending.lasting("agent-job"), "it never fits")
        self.conf["llm"] = {"budget": {"daily_usd": 3.0}}
        with mock.patch.object(spending, "tidied", lambda rows: False):
            self.assertTrue(spending.lasting("agent-job", tidy=True), "it waits for the tidy-up itself")
            self.assertFalse(spending.lasting("agent-job"), "for any other job it passes tonight")
        self.assertFalse(spending.lasting("agent-job"))
        self.conf["runtime.login"] = None
        self.assertFalse(spending.lasting("agent-job"))

    def test_settings_that_make_no_sense_block_nobody(self):
        self.conf["llm"] = {"points": {"agent-job": "oops"}, "budget": "x"}
        self.conf["runtime.login"] = None
        self.assertIsNone(spending.blocked("agent-job"))

    def test_a_link_in_the_open_folder_hangs_nothing(self):
        usage_log.open_dir(self.log).mkdir(parents=True)
        (usage_log.open_dir(self.log) / "x.json").symlink_to(self.tmp / "nothing")
        self.assertEqual(spending.spent()[1], 0.0)


class TidyUp(Base):
    def test_an_answer_for_a_project_is_that_project_s(self):
        """Its cap and its backoff: one project's doesn't hold the others."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("librarian_spending", ROOT / "bin" / "librarian.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        asked = []
        with mock.patch.object(m, "llm", lambda point, prompt, cwd, project=None: asked.append((point, project)) or (1, None, "")), \
                mock.patch.object(m, "LOG", self.tmp / "log"), mock.patch.object(m, "SHADOW_DIR", self.tmp / "shadow"):
            try:
                m._edits_call("librarian-state-edits", "p", "before", {"project": "proj"})
            except Exception:   # noqa: BLE001 (what it does with no answer isn't this test's)
                pass
        self.assertEqual(asked, [("librarian-state-edits", "proj")])


class Letters(Base):
    @classmethod
    def setUpClass(cls):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("hubbus_spending", str(ROOT / "bin" / "hubbus"))
        spec = importlib.util.spec_from_loader("hubbus_spending", loader)
        cls.m = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.m)

    def test_nothing_is_held_on_a_plan(self):
        self.conf["runtime.login"] = None
        held = self.m.held_runs()
        self.m.one_a_tick(held)
        self.assertEqual((held, self.m.keep_letter(held, "triage")), ({}, False))

    def test_one_start_a_tick_and_only_a_kept_letter_is_said(self):
        self.key()
        held = self.m.held_runs()
        self.assertEqual((self.m.keep_letter(held, "triage"), held["kept"]), (False, None))
        self.m.one_a_tick(held)
        self.assertTrue(self.m.keep_letter(held, "fork"))
        self.assertTrue(held["kept"])

    def test_no_room(self):
        self.key()
        self.ran(9.9)
        held = self.m.held_runs()
        self.assertIsNone(held["kept"], "no letter was held back yet")
        self.assertTrue(self.m.keep_letter(held, "triage"))
        self.assertIn("daily_usd", held["kept"])


class Refused(unittest.TestCase):
    def test_only_the_service_s_own_answer(self):
        for out, want in (({"is_error": True, "api_error_status": 401, "result": "x"}, "HTTP 401"),
                          ({"is_error": True, "api_error_status": 400, "result": "Credit balance is too low"}, "HTTP 400, no credit"),
                          ({"is_error": True, "api_error_status": 400, "result": "bad request"}, ""), ({"is_error": True, "api_error_status": 529}, ""),
                          ({"is_error": True, "result": "I could not open the login page: 401"}, ""), ({"is_error": False, "api_error_status": 401}, ""), (None, "")):
            self.assertEqual(rc._refused(out), want, out)


class Settling(Base):
    def test_a_run_settling_is_never_in_neither(self):
        """What is open is looked at before the rows and after: a run that settles in between is looked at again."""
        rid, _w = spending.reserve("p", "claude", "m", 4.0)
        real, settled = usage_log.rows, []

        def rows(log, since=""):
            got = real(log, since)
            if not settled:   # it settles after the rows were read
                settled.append(1)
                completion.record_run("p", "claude", "m", "metered", 4.0, "end", None, None, None, rid, reserved=4.0)
            return got
        with mock.patch.object(usage_log, "rows", rows):
            done, reserved, _rows = spending.spent()
        self.assertEqual(done + reserved, 4.0)

    def test_a_reservation_from_before_the_machine_started(self):
        rid, _w = spending.reserve("p", "claude", "m", 4.0)
        f = usage_log.open_dir(self.log) / f"{rid}.json"
        old = usage_log.booted() - 3600
        os.utime(f, (old, old))
        self.assertEqual(spending.spent()[1], 0.0, "its process is gone, whoever has its number now")

    def test_one_cut_short(self):
        usage_log.open_dir(self.log).mkdir(parents=True)
        f = usage_log.open_dir(self.log) / "x.json"
        f.write_text("")
        old = time.time() - usage_log.CLAIM_S - 5
        os.utime(f, (old, old))
        usage_log.sweep(self.log)
        self.assertFalse(f.exists())


class Never(Base):
    def test_what_never_fits_is_named(self):
        self.key()
        import registry
        with mock.patch.object(registry, "enabled_modules", lambda: ["reflect"]):
            self.assertNotIn("consolidate", [p for p, _least in spending.never(5)], "its module is off")
        with mock.patch.object(registry, "enabled_modules", lambda: ["consolidate"]):
            self.assertIn("consolidate", [p for p, _least in spending.never(5)])
            self.assertEqual(spending.never(50), [])
            self.conf["runtime.login"] = None
            self.assertEqual(spending.never(0.01), [], "a plan's runs have no budget")


class Asked(Base):
    """brunna init / llm set-key --agent: a paid login is the owner's own answer in that run."""
    INFO = {"label": "X", "credential": {"kind": "api-key", "secret": rc.KEY_FILE, "login": "key", "name": "API key", "where": "w", "note": "n", "limit": "l"}}

    @classmethod
    def setUpClass(cls):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("agenthub_spending", str(ROOT / "bin" / "agenthub"))
        spec = importlib.util.spec_from_loader("agenthub_spending", loader)
        cls.ah = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.ah)

    class UI:
        def __init__(self, can_ask=True, acts=True, secret=None, said="3", yes=True):
            self.can_ask, self.acts, self.terminal, self.value, self.said, self.yes, self.asked = can_ask, acts, can_ask, secret, said, yes, []

        def secret(self, qid, prompt):
            self.asked.append(qid)
            return self.value

        def ask(self, qid, question, default):
            self.asked.append(qid)
            return self.said

        def needs_user(self, qid, what):
            self.asked.append(qid)

        def consent(self, qid, question, enter=True, later=None):
            self.asked.append(qid)
            return self.yes

    def setUp(self):
        super().setUp()
        self.doc = {"runtime": {"default": "claude"}}
        self.conf.update({"runtime.login": None, "llm": None})
        self.written = []
        for target, attr, value in ((self.ah, "read_hub_json", lambda: json.loads(json.dumps(self.doc))), (self.ah, "write_hub_json", self.written.append),
                                    (self.ah, "without_login", lambda: "off"), (rc, "check_key", lambda k: None),
                                    (rc, "_token_file", lambda: self.tmp / "no-token")):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def ask(self, ui, **kw):
        import contextlib
        import io
        with mock.patch.object(self.ah, "UI", lambda: ui), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out):
            got = self.ah.agent_key("claude", self.INFO, **kw)
        return got, out.getvalue()

    def test_a_key_and_a_budget(self):
        got, _out = self.ask(self.UI(secret=KEY))
        self.assertTrue(got)
        self.assertEqual((self.written[0]["runtime"]["login"], self.written[0]["llm"]["budget"]["daily_usd"]), ("key", 3.0))

    def test_enter_writes_nothing(self):
        got, out = self.ask(self.UI())
        self.assertEqual((got, self.written), (False, []))
        self.assertIn("off", out)

    def test_a_saved_key_is_not_a_choice(self):
        self.key()
        for ui in (self.UI(yes=False), self.UI(can_ask=False, yes=False), self.UI(can_ask=False, acts=False, yes=False)):
            self.assertEqual(self.ask(ui)[0], False)
        self.assertEqual(self.written, [])
        self.assertTrue(self.ask(self.UI(yes=True))[0])

    def test_questions_write_nothing(self):
        self.key()
        ui = self.UI(can_ask=False, acts=False, secret=KEY)
        self.assertEqual((self.ask(ui)[0], self.written, ui.asked), (False, [], ["agent_key", "agent_budget"]))

    def test_relayed(self):
        """The answers' budget, and the owner's own yes to it."""
        self.assertFalse(self.ask(self.UI(can_ask=False, secret=KEY, yes=True))[0], "no budget was said: no default is taken")
        self.assertFalse(self.ask(self.UI(can_ask=False, secret=KEY, yes=False), budget_said="2")[0])
        self.assertEqual(self.written, [])
        self.assertTrue(self.ask(self.UI(can_ask=False, secret=KEY, yes=True), budget_said="2")[0])
        self.assertEqual(self.written[0]["llm"]["budget"]["daily_usd"], 2.0)

    def test_a_key_that_does_not_work(self):
        with mock.patch.object(rc, "check_key", lambda k: "HTTP 401:invalid x-api-key"):
            got, out = self.ask(self.UI(secret=KEY))
        self.assertEqual((got, self.written), (False, []))
        self.assertFalse((self.secrets / rc.KEY_FILE).exists())
        self.assertNotIn(KEY, out)
        for bad in ("two words", "k\u00e9"):
            self.assertFalse(self.ask(self.UI(secret=bad))[0])

    def test_a_bad_budget(self):
        for said in ("lots", "0", "-1", "nan", "1e9", ""):
            self.assertFalse(self.ask(self.UI(secret=KEY, said=said))[0], said)
        self.assertEqual(self.written, [])

    def test_an_install_with_a_login_keeps_it(self):
        self.conf["runtime.login"] = "token"
        ui = self.UI(secret=KEY)
        self.assertFalse(self.ask(ui)[0])
        self.assertEqual(ui.asked, [])
        self.assertFalse(self.ask(self.UI(secret=KEY, yes=False), asked=True)[0])
        self.assertEqual(self.written, [])
        self.assertTrue(self.ask(self.UI(secret=KEY, yes=True), asked=True)[0], "its owner's own yes")

    def test_changing_the_budget(self):
        self.key()
        self.conf["runtime.login"] = "key"
        self.doc = {"runtime": {"login": "key"}, "llm": {"budget": {"daily_usd": 5, "caps": {"consolidate": 2}}}}
        ui = self.UI(said="8")
        self.assertTrue(self.ask(ui, asked=True)[0])
        self.assertEqual(self.written[0]["llm"]["budget"], {"daily_usd": 8.0, "caps": {"consolidate": 2}})

    def test_no_agent_has_no_login(self):
        d = {"runtime": {"default": "claude", "login": "key"}}
        self.ah.apply_agent(d, "none", self.ah.agent_presets())
        self.assertEqual(d["runtime"], {"default": "none"})

    def test_an_install_that_logs_in_is_written_down(self):
        (self.tmp / "no-token").write_text("t")
        d = {"runtime": {"default": "claude"}}
        self.assertTrue(self.ah.keep_login(d))
        self.assertEqual(d["runtime"]["login"], "token")
        self.assertIsNone(self.ah.keep_login(d))
        (self.tmp / "no-token").unlink()
        self.assertIsNone(self.ah.keep_login({"runtime": {"default": "claude"}}), "nothing to keep")
        self.assertIsNone(self.ah.keep_login({"runtime": {"default": "codex"}}))


if __name__ == "__main__":
    unittest.main()
