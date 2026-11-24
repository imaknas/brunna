"""What a stranger meets on a fresh install (the 11-25 walkthrough in a container): a feature with no model says it is
off instead of passing for a success, a wrong answer is asked again, and connecting twice is harmless.

    bin/py -m unittest tests.test_newcomer_friction
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402
import init_dialogue  # noqa: E402
import job_off  # noqa: E402
import llm_ready  # noqa: E402

_LANG = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": "en", "AGENT_HUB_LANG_CONTENT": "en"})


def setUpModule():
    _LANG.start()


def tearDownModule():
    _LANG.stop()


def load(name: str, file: str):
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / file))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def patch(self, *args, **kw):
        p = mock.patch.object(*args, **kw)
        p.start()
        self.addCleanup(p.stop)


class JobOff(Tmp):
    """A job that is off: completed for the scheduler, told apart by everyone who reads it."""

    def setUp(self):
        super().setUp()
        self.patch(hubconf, "STATE", self.tmp / ".state")

    def off(self, name, reason):
        with mock.patch.dict(os.environ, {"BRUNNA_JOB": name}), contextlib.redirect_stdout(io.StringIO()) as out:
            job_off.off(reason)
        return out.getvalue()

    def test_the_reason_is_printed_and_recorded_until_the_job_runs(self):
        self.assertIn("no model", self.off("brunna/librarian", "no model"), "in the run's own log, not only elsewhere")
        self.assertEqual(job_off.last("brunna/librarian")["reason"], "no model")
        with mock.patch.dict(os.environ, {"BRUNNA_JOB": "brunna/librarian"}):
            job_off.clear()
        self.assertIsNone(job_off.last("brunna/librarian"))

    def test_outside_a_run_nothing_is_recorded(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("BRUNNA_JOB", None)
            os.environ.pop("AGENTHUB_JOB", None)
            with contextlib.redirect_stdout(io.StringIO()):
                job_off.off("no model")
        self.assertFalse((self.tmp / ".state" / "jobs-off").exists())

    def manual(self, manager, off):
        """brunna run p/tidy with that service manager; what it started instead of running it"""
        sc = load("schedulectl_off", "schedulectl")
        (self.tmp / ".state").mkdir(exist_ok=True)
        state = self.tmp / ".state" / "scheduler-last-run.json"
        state.write_text(json.dumps({"p/tidy": "2026-11-01"}))
        started = []
        with mock.patch.object(sc, "HUB", self.tmp), \
                mock.patch.object(sc, "all_jobs", lambda: [{"name": "p/tidy", "command": "true"}]), \
                mock.patch.object(sc.hubconf, "service_manager", lambda: manager), \
                mock.patch.object(sc, "run_here", lambda *a: started.append("here") or 0), \
                mock.patch.object(sc, "start", lambda *a: started.append("service manager")), \
                mock.patch.object(llm_ready, "job", lambda name: off), contextlib.redirect_stdout(io.StringIO()) as out:
            code = sc.cmd_run("p/tidy") or 0
        return code, out.getvalue(), json.loads(state.read_text())["p/tidy"], started

    def test_a_manual_run_of_a_job_that_is_off_is_not_started_and_keeps_the_days_run(self):
        for manager in (None, "launchd", "systemd"):
            with self.subTest(manager=manager):
                code, out, mark, started = self.manual(manager, "no model (brunna llm setup)")
                self.assertEqual((code, mark, started), (1, "2026-11-01", []), "said here, whatever runs the jobs")
                self.assertIn("is off here", out)
                self.assertIn("brunna llm setup", out, "with what to do about it")
                self.assertNotIn("won't run it again", out)

    def test_a_manual_run_of_any_other_job_is_as_before(self):
        for manager, how in ((None, "here"), ("launchd", "service manager")):
            code, _out, mark, started = self.manual(manager, None)
            self.assertEqual((code, started), (0, [how]))
            self.assertNotEqual(mark, "2026-11-01", "recorded as run today")

    def test_which_jobs_do_nothing_without_a_model(self):
        with mock.patch.object(llm_ready, "unsupported", lambda point: f"no model for {point}"):
            self.assertIn("no model", llm_ready.job(f"{hubconf.hub_project()}/librarian"))
            self.assertIn("no model", llm_ready.job("consolidate/consolidate-memory"))
            self.assertIsNone(llm_ready.job(f"{hubconf.hub_project()}/todo"))
            self.assertIsNone(llm_ready.job("shop/librarian"), "a project's own job of that name")


class Librarian(Tmp):
    def test_off_only_when_neither_version_can_run(self):
        for conf, unsupported, want in (({}, {}, None),
                                        ({}, {"librarian-state": "no agent"}, None),
                                        ({}, {"librarian-state": "no agent", "librarian-state-edits": "no model"}, "no model"),
                                        ({"completion": {"state": True}}, {"librarian-state-edits": "no model"}, "no model"),
                                        ({"completion": {"state": False}}, {"librarian-state-edits": "no model"}, None)):
            with self.subTest(conf=conf, unsupported=unsupported), \
                    mock.patch.object(hubconf, "get", lambda k, d=None: conf if k == "librarian" else d), \
                    mock.patch.object(llm_ready, "unsupported", unsupported.get):
                self.assertEqual(llm_ready.librarian(), want)

    def test_the_run_leaves_its_reason_in_its_own_output(self):
        """librarian.py as the scheduler starts it, on an instance with no model: it exits 0 and says on stdout why it
        did nothing (its own log is another file), and the record the status page reads is there."""
        hub, cfg = self.tmp / "hub", self.tmp / "cfg"
        hub.mkdir(), cfg.mkdir()
        for name in ("bin", "contracts", "locales", "templates", "modules", "projects"):
            (hub / name).symlink_to(ROOT / name)
        (cfg / "brunna.json").write_text(json.dumps({"runtime": {"default": "none"}, "owner": {"tag": "t"}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_HUB", "AGENTHUB", "BRUNNA"))}
        env.update(HOME=str(self.tmp), BRUNNA_ROOT=str(hub), BRUNNA_CONFIG=str(cfg), BRUNNA_DATA=str(self.tmp / "data"),
                   BRUNNA_LANG_UI="en", BRUNNA_JOB="brunna/librarian", BRUNNA_PYTHON=sys.executable)
        r = subprocess.run([sys.executable, str(hub / "bin" / "librarian.py")], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the librarian is off", r.stdout)
        self.assertIn("brunna llm setup", r.stdout, "with what to do about it")
        self.assertIn("off", (hub / ".state" / "jobs-off" / "brunna__librarian.json").read_text())


class Answers(unittest.TestCase):
    def ask(self, *typed):
        t = init_dialogue.Terminal()
        answers = iter(typed)
        with mock.patch.object(init_dialogue.Terminal, "can_ask", property(lambda self: True)), \
                mock.patch("builtins.input", lambda prompt="": next(answers)), contextlib.redirect_stdout(io.StringIO()) as out:
            got = t.menu("llm", "Which?", [("anthropic", "A"), ("none", "N")], "none")
        return got, out.getvalue()

    def test_a_wrong_answer_is_said_and_asked_again(self):
        got, out = self.ask("gpt", "7", "1")
        self.assertEqual(got, "anthropic")
        self.assertIn("gpt: not one of the choices", out)
        self.assertIn("anthropic, none", out, "every valid choice is listed")

    def test_a_choice_can_be_typed_by_name(self):
        self.assertEqual(self.ask("Anthropic")[0], "anthropic")
        self.assertEqual(self.ask("")[0], "none")

    def test_every_value_the_question_lists_can_be_typed(self):
        values = [("en", "English"), ("zh-Hant", "Traditional Chinese")]   # the first question: a value with capitals
        for typed, want in (("zh-Hant", "zh-Hant"), ("ZH-HANT", "zh-Hant"), ("en", "en")):
            with mock.patch.object(init_dialogue.Terminal, "can_ask", property(lambda self: True)), \
                    mock.patch("builtins.input", side_effect=[typed, ""]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(init_dialogue.Terminal().menu("language", "Language", values, "en"), want)

    def test_the_model_question_lists_every_choice_and_asks_again(self):
        ah = load("agenthub_llm_again", "agenthub")
        answers = iter(("gpt", "none"))
        with tempfile.TemporaryDirectory() as d:
            cfg = pathlib.Path(d) / "brunna.json"
            cfg.write_text(json.dumps({"runtime": {"default": "none"}}))
            with mock.patch.object(hubconf, "config_path", lambda: cfg), mock.patch.object(hubconf, "_cfg", None), \
                    mock.patch.object(init_dialogue.Terminal, "can_ask", property(lambda self: True)), \
                    mock.patch.object(ah, "default_llm_choice", lambda rt=None: "none"), \
                    mock.patch("runtime_ports.default_name", lambda: "none"), \
                    mock.patch("builtins.input", lambda prompt="": next(answers)), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                init_dialogue.use(init_dialogue.Terminal())
                self.assertEqual(ah.llm_setup({}), 0)
        self.assertIsNone(next(answers, None), "asked twice")
        for choice in ("anthropic:", "openai:", "ollama:", "none:"):
            self.assertIn(choice, out.getvalue())

    def test_an_unknown_feature_is_refused_before_anything_is_written(self):
        ah = load("agenthub_modules", "agenthub")
        self.assertIn("reflecct", ah.init_argv_problem(["--modules", "owner,reflecct"]))
        self.assertIsNone(ah.init_argv_problem(["--modules", "owner,reflect"]))
        self.assertIn("reflecct", ah.answers_problem({"modules": "reflecct"}))
        self.assertIsNone(ah.init_argv_problem(["--modules", "none"]), "none of them is an answer")
        self.assertEqual(ah.module_names("none"), set())
        self.assertEqual(ah.module_names("owner, reflect"), {"owner", "reflect"})


class Help(unittest.TestCase):
    def run_(self, *argv):
        ah = load("agenthub_help", "agenthub")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = ah.main(list(argv))
        return code, out.getvalue()

    def test_help_is_for_the_user(self):
        for argv in (("--help",), ("help", "all"), ("init", "--help")):
            code, out = self.run_(*argv)
            with self.subTest(argv=argv):
                self.assertEqual(code, 0)
                for internal in ("new-user.md", "dispatch layer", "2026-"):
                    self.assertNotIn(internal, out)
        out = self.run_("--help")[1]
        for c in ("brunna init", "brunna llm setup", "brunna connect", "brunna doctor"):
            self.assertIn(c, out)
        self.assertLess(len(out.splitlines()), 25)

    def test_one_commands_help_is_only_its_own(self):
        out = self.run_("init", "--help")[1]
        self.assertIn("--no-launchd", out)
        self.assertNotIn("brunna connect [", out)
        self.assertIn("brunna doctor  ", self.run_("doctor", "--help")[1])

    def test_help_anywhere_runs_nothing(self):
        ah = load("agenthub_help_anywhere", "agenthub")
        for argv in (["llm", "setup", "--help"], ["push", "--help"], ["release", "-h"], ["config", "apply", "--help"]):
            with self.subTest(argv=argv), mock.patch.dict(ah.COMMANDS, {argv[0]: mock.Mock(side_effect=AssertionError("ran"))}), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ah.main(argv), 0)
            self.assertIn(f"brunna {argv[0]}", out.getvalue())


class LeftOff(Tmp):
    """init says which features it left off for want of a model, after the answers, and only those it did leave off."""

    def run_init(self, modules, llm="none"):
        ah = load("agenthub_left_off", "agenthub")
        args = {"owner": "t", "timezone": "UTC", "workspace": str(self.tmp / "w"), "agent": "none", "llm": llm}
        with mock.patch.object(init_dialogue.Terminal, "can_ask", property(lambda self: True)), \
                mock.patch.object(init_dialogue.Terminal, "ask", lambda self, qid, q, default: modules or default), \
                mock.patch("shutil.which", lambda *a, **k: None), contextlib.redirect_stdout(io.StringIO()) as out:
            init_dialogue.use(init_dialogue.Terminal())
            d = ah.new_hub_json(args)
        return args["_held"], d["modules"], out.getvalue()

    def test_what_was_typed_in_anyway_is_not_left_off(self):
        held, modules, out = self.run_init("")
        self.assertEqual(held, ["consolidate", "reflect"])
        self.assertNotIn("left off", out, "not before the questions are answered")
        held, modules, _out = self.run_init("consolidate,owner")
        self.assertEqual(held, ["reflect"])
        self.assertTrue(modules["consolidate"])
        self.assertEqual(self.run_init("none")[1]["owner"], False)


if __name__ == "__main__":
    unittest.main()
