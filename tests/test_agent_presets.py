"""init's agent step (docs/design/newcomer-path.md B4): the agents it offers come from the runtime adapters'
install_info() and the modules' runtime_preset, never from init itself; applying one sets hub.json's runtime and turns
the other presets' modules off.

    bin/py -m unittest tests.test_agent_presets
"""
import importlib.machinery
import importlib.util
import pathlib
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import registry  # noqa: E402
import yaml  # noqa: E402

loader = importlib.machinery.SourceFileLoader("agenthub_presets", str(ROOT / "bin" / "agenthub"))
spec = importlib.util.spec_from_loader("agenthub_presets", loader)
ah = importlib.util.module_from_spec(spec)
loader.exec_module(ah)


class Presets(unittest.TestCase):
    def test_mcp_registration_needs_the_owners_yes(self):
        """B5: a user-level change, so --yes or no terminal registers nothing; a yes runs each tool's command with the
        agenthub command put in."""
        presets = {"x": {"label": "X", "find": ["x"], "mcp_register": ["x", "mcp", "add", "{server}", "{cli}", "mcp"]}}
        with mock.patch.object(ah, "agent_found", lambda info: True), \
             mock.patch.object(ah.subprocess, "run") as run, mock.patch.object(ah, "cli_command",
                                                                               lambda: ["/hub/bin/agenthub"]):
            for yes, tty in ((True, True), (False, False)):
                with mock.patch.object(ah.sys.stdin, "isatty", lambda: tty), mock.patch("builtins.input", lambda q: "y"):
                    ah.init_dialogue.use(ah.init_dialogue.Terminal(yes))
                    ah.offer_mcp_registration(presets)
            run.assert_not_called()
            run.return_value.returncode = 0
            with mock.patch.object(ah.sys.stdin, "isatty", lambda: True), mock.patch("builtins.input", lambda q: ""):
                ah.init_dialogue.use(ah.init_dialogue.Terminal(False))
                ah.offer_mcp_registration(presets)
            self.assertEqual(run.call_args.args[0], ["x", "mcp", "add", "brunna", "/hub/bin/agenthub", "mcp"])

    def test_offered_agents(self):
        presets = ah.agent_presets()
        self.assertEqual(list(presets)[:2], ["claude", "codex"], "adapters first, then modules")
        for name, info in presets.items():
            with self.subTest(name=name):
                self.assertTrue(info["label"] and info["gives"] and info["find"])
                self.assertIn("runtime", info["settings"])
                self.assertIn(info["credential"]["kind"], ("own-token", "tool-login"))

    def test_apply(self):
        presets = ah.agent_presets()
        d = {"runtime": {"default": "claude"}, "modules": {"claude": True, "codex": False, "owner": True}}
        ah.apply_agent(d, "codex", presets)
        self.assertEqual((d["runtime"]["default"], d["runtime"]["installed"]), ("none", ["codex"]),
                         "no default until a Codex copy can be promoted (runtime-codex.md, Step 1 r2's step 6)")
        e = {"runtime": {"default": "claude", "installed": ["claude"]}, "modules": {}}
        ah.apply_agent(e, "codex", presets)
        self.assertEqual(e["runtime"]["installed"], ["claude", "codex"], "added to, never replaced")
        self.assertNotIn("command", d["runtime"], "Codex's own runtime replaces the command template")
        self.assertEqual((d["modules"]["claude"], d["modules"]["codex"], d["modules"]["owner"]), (False, True, True))
        ah.apply_agent(d, "none", presets)
        self.assertEqual(d["runtime"]["default"], "none")
        self.assertFalse(d["modules"]["codex"])

    def test_codex_module_validates(self):
        doc = yaml.safe_load((ROOT / "modules" / "codex.yaml").read_text())
        self.assertEqual(registry.validate_module(doc, "codex"), [])
        bad = {**doc, "runtime_preset": {"label": "X"}}
        self.assertTrue(registry.validate_module(bad, "codex"))

    def test_init_never_installs_or_logs_in_without_a_yes(self):
        self.assertFalse(ah.init_dialogue.Terminal(yes=True).consent("x", "anything?"),
                         "--yes does not mean installing for the user")


class LoginUse(unittest.TestCase):
    """brunna's decisions made with another agent's own login (llm setup): only on the owner's own yes."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg = {"runtime": {"default": "none", "installed": ["codex"]}, "modules": {"codex": True},
                    "llm": {"providers": {"local": {"impl": "ollama"}}, "points": {"p/x": {"provider": "local", "model": "m"}}}}
        self.addCleanup(ah.init_dialogue.use, ah.init_dialogue.Terminal())
        self.written = []
        self.present, self.note, self.logged_in = True, None, 0
        cred = lambda: [mock.Mock(present=self.present, note=self.note)]   # noqa: E731
        import runtime_codex
        for p in (mock.patch.object(ah, "read_hub_json", lambda: json.loads(json.dumps(self.cfg))),
                  mock.patch.object(ah, "write_hub_json", self.written.append),
                  mock.patch.object(ah, "install_blocker", lambda n: None), mock.patch.object(ah, "agent_found", lambda *a: pathlib.Path("/x")),
                  mock.patch.object(runtime_codex, "credentials", cred),
                  mock.patch("runtime_ports.installed", lambda: ["codex"]), mock.patch("runtime_ports.default_name", lambda: "none"),
                  mock.patch.object(ah.subprocess, "run", lambda *a, **k: mock.Mock(returncode=self.logged_in))):
            p.start()
            self.addCleanup(p.stop)
        s = mock.patch.object(ah, "store_install", return_value=True)
        self.store = s.start()
        self.addCleanup(s.stop)

    def setup_llm(self, answer=None, yes=False, **args):
        """llm setup choosing codex; answer: what is typed at the question (None: nobody can answer)."""
        asked = []

        def typed(q):
            asked.append(q)
            return answer
        ah.init_dialogue.use(ah.init_dialogue.Terminal(yes))
        with mock.patch.object(ah.sys.stdin, "isatty", lambda: answer is not None), mock.patch("builtins.input", typed), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out):
            rc = ah.llm_setup({"llm": "codex", **args})
        return rc, asked, out.getvalue()

    def test_a_yes_makes_it_the_provider_of_every_group_and_installs_the_copy(self):
        rc, asked, _out = self.setup_llm("y")
        self.assertEqual((rc, len(asked)), (0, 1))
        ask = yaml.safe_load((ROOT / "modules" / "codex.yaml").read_text())["runtime_preset"]["login_use"]
        for what in ("login", "OpenAI", "380 MB", "every day", "raises", "billed per use", "To stop"):
            self.assertIn(what, ask)
        llm = self.written[-1]["llm"]
        self.assertEqual(llm["providers"], {"local": {"impl": "ollama"}, "codex": {"impl": "codex"}})
        self.assertEqual(set(llm["use"].values()), {"codex"})
        self.assertEqual(self.written[-1]["runtime"], self.cfg["runtime"], "the platform starts nothing more than before")
        self.store.assert_called_once_with("codex")

    def test_anything_else_changes_nothing(self):
        for answer, yes in (("", False), ("n", False), (None, False), ("y", True)):
            rc, _asked, out = self.setup_llm(answer, yes)
            self.assertEqual((rc, self.written, self.store.call_count), (1, [], 0), (answer, yes))
            self.assertIn("llm setup", out)

    def test_not_asked_where_it_cant_work(self):
        for name, value in (("install_blocker", lambda n: "no npm"), ("agent_found", lambda *a: None)):
            with mock.patch.object(ah, name, value):
                rc, asked, _out = self.setup_llm("y")
            self.assertEqual((rc, asked, self.written), (1, [], []), name)
        self.note = "the login is kept elsewhere"
        self.assertEqual(self.setup_llm("y")[:2], (1, []))

    def test_no_login_yet_is_said_and_nothing_is_installed(self):
        self.present = False
        rc, _asked, out = self.setup_llm("y")
        self.assertEqual((rc, self.store.call_count), (1, 0))
        self.assertIn("codex login", out)

    def test_offered_only_where_its_sessions_are_served_and_it_isnt_the_one_started(self):
        self.assertEqual(list(ah.llm_logins("none")), ["codex"])
        self.assertEqual(list(ah.llm_logins("codex")), [])
        self.assertEqual(list(ah.llm_logins("none", ["claude"])), [])
        with mock.patch("runtime_ports.installed", lambda: ["claude"]):
            with contextlib.redirect_stderr(io.StringIO()):
                ah.init_dialogue.use(ah.init_dialogue.Terminal())
                self.assertEqual(ah.llm_setup({"llm": "codex"}), 2)

    def test_an_agents_questions_list_it_and_answer_it_for_nobody(self):
        rec = ah.init_dialogue.Recorder({})
        ah.init_dialogue.use(rec)
        self.assertEqual(ah.llm_setup({"llm": "codex"}), 0)
        self.assertIn(("llm_login", "dialog"), [(q["id"], q["via"]) for q in rec.questions])
        self.assertEqual(self.written, [])
        self.assertIsNotNone(ah.answers_problem({"llm_login": "yes"}))
        self.assertIsNone(ah.answers_problem({"llm": "codex"}))

    def test_what_is_the_owners_under_that_name_stays(self):
        self.cfg["llm"]["providers"]["codex"] = {"impl": "openai", "base_url": "http://x"}
        self.assertEqual(self.setup_llm("y")[:2], (1, []))
        self.cfg["llm"]["providers"]["codex"] = {"impl": "codex", "models": {"fast": "a", "standard": "a", "strong": "a"}}
        self.assertEqual(self.setup_llm("y")[0], 0)
        self.assertEqual(self.written[-1]["llm"]["providers"]["codex"]["models"]["fast"], "a")

    def test_a_yes_given_isnt_asked_again(self):
        import completion
        self.cfg["llm"]["providers"]["codex"] = {"impl": "codex"}
        self.cfg["llm"]["use"] = {g: "codex" for g in completion.GROUPS}
        self.assertEqual(self.setup_llm(None)[:2], (0, []))
        self.assertEqual(self.written, [])

    def test_not_offered_where_the_agent_the_platform_starts_makes_them(self):
        self.assertEqual(ah.llm_logins("claude", ["claude", "codex"]), {})

    def test_leaving_codex_ends_it(self):
        import completion

        def d():
            return {"runtime": {"default": "none"}, "modules": {}, "llm": {
                "providers": {"codex": {"impl": "codex"}, "local": {"impl": "ollama"}}, "use": {g: "codex" for g in completion.GROUPS},
                "points": {"p/x": {"provider": "local"}}}}
        for kw, gone in (({"keep": "claude"}, True), ({"keep": "none"}, True), ({"only": "codex"}, True), ({"keep": "codex"}, False),
                         ({"only": "claude"}, False)):
            x = d()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                ah.end_logins(x, **kw)
            self.assertEqual("codex" not in x["llm"]["providers"], gone, kw)
            self.assertEqual("use" not in x["llm"], gone, kw)
            self.assertIn("local", x["llm"]["providers"])
            self.assertEqual(bool(out.getvalue()), gone)
        only = {"llm": {"providers": {"codex": {"impl": "codex"}}, "use": {"fast": "codex"}}}
        with contextlib.redirect_stdout(io.StringIO()):
            ah.end_logins(only)
        self.assertEqual(only, {})
        pinned = d()
        pinned["llm"]["points"]["reflect-week"] = {"provider": "codex"}
        with contextlib.redirect_stdout(io.StringIO()):
            ah.end_logins(pinned)
        self.assertIn("codex", pinned["llm"]["providers"], "a pin of theirs keeps it")

    def test_another_choice_takes_it_out(self):
        llm = {"providers": {"codex": {"impl": "codex"}, "claude": {"impl": "claude"}, "local": {"impl": "ollama"}},
               "use": {"fast": "local"}, "points": {"reflect-week": {"provider": "claude"}}}
        ah.drop_unused_logins(llm)
        self.assertEqual(sorted(llm["providers"]), ["claude", "local"], "what a pin names stays, and what isn't a runtime's")

    def test_a_runtimes_provider_the_owner_wrote_is_left_alone(self):
        def d():
            return {"runtime": {"default": "none"}, "llm": {"providers": {"mine": {"impl": "claude"}}, "use": {"fast": "mine"}}}
        for kw in ({"keep": "codex"}, {"only": "claude"}, {"keep": "none"}):
            x = d()
            self.assertEqual((ah.end_logins(x, **kw), x), ([], d()), kw)

    def test_not_asked_when_it_would_be_refused_anyway(self):
        import completion
        self.cfg["llm"]["providers"]["codex"] = {"impl": "codex", "models": {"standard": "m"}}
        self.assertEqual(self.setup_llm("y")[:2], (1, []), "fast and strong have no model")
        del self.cfg["llm"]["providers"]["codex"]
        self.cfg["llm"]["budget"] = {"daily_usd": 5}
        with mock.patch.object(completion, "login_metered", lambda t: True):
            self.assertEqual(self.setup_llm("y")[:2], (1, []))
        self.assertEqual(self.written, [])

    def test_a_dry_run_on_an_instance_does_nothing(self):
        with mock.patch.object(ah.hubconf, "config_path", lambda: ROOT / "README.md"), mock.patch.object(ah, "set_agent") as agent, \
                mock.patch.object(ah, "set_language") as lang, mock.patch.object(ah, "offer_mcp_registration") as mcp:
            for argv in (["--agent", "codex"], ["--lang", "en"], ["--register-mcp"]):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(ah.cmd_init([*argv, "--dry-run"]), 2, argv)
        self.assertEqual((agent.call_count, lang.call_count, mcp.call_count), (0, 0, 0))

    def test_the_presets_text_is_checked(self):
        doc = yaml.safe_load((ROOT / "modules" / "codex.yaml").read_text())
        for bad in ("", "  ", 1, ["x"], {}):
            self.assertTrue(registry.validate_module({**doc, "runtime_preset": {**doc["runtime_preset"], "login_use": bad}}, "codex"), bad)


class Provider(unittest.TestCase):
    def test_a_runtimes_own_models_unless_the_provider_names_some(self):
        import completion
        import runtime_codex
        self.assertEqual(completion.models_of({"impl": "codex"}), runtime_codex.MODELS)
        self.assertEqual(completion.models_of({"impl": "codex", "models": {"fast": "m"}}), {"fast": "m"})
        self.assertEqual(completion.models_of({"impl": "ollama"}), {})
        use = {g: "codex" for g in completion.GROUPS}
        self.assertEqual(completion.config_errors({"providers": {"codex": {"impl": "codex"}}, "use": use}), [])

    def test_an_api_key_login_is_pay_per_use(self):
        import completion
        import runtime_codex
        for mode, want in (("apikey", "metered"), ("chatgpt", "subscription"), (None, "subscription")):
            with mock.patch.object(runtime_codex, "_auth_fields", lambda m=mode: {"auth_mode": m} if m else {}), \
                    mock.patch.dict(os.environ, {"OPENAI_API_KEY": "x"}):
                for target in ({"impl": "codex", "conf": {"impl": "codex"}, "location": "cloud"}, completion.runtime_target("codex", None, "token")):
                    self.assertEqual(completion.billing(target), want, (mode, target))
        with mock.patch.object(runtime_codex, "_auth_fields", lambda: {"auth_mode": "apikey"}), \
                mock.patch.object(completion, "settings", lambda: {"budget": {"daily_usd": 5}}), \
                mock.patch.object(completion, "today_cost", lambda: 0.0):
            with self.assertRaises(completion.Refused):
                completion.run("x", {}, "p", None, target=completion.runtime_target("codex", "m", "token"))
        rows = []
        with mock.patch.object(completion.usage_log, "append", lambda log, row: rows.append(row)):
            res = mock.Mock(cost_usd=None, input_tokens=1, output_tokens=1, stop="end", started=True)
            for mode in ("apikey", "chatgpt"):
                with mock.patch.object(runtime_codex, "_auth_fields", lambda m=mode: {"auth_mode": m}):
                    completion.record("x", completion.runtime_target("codex", "m", "token"), res)
        self.assertEqual([r.get("cost_unknown") for r in rows], [True, None])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):   # its runs get none of it
            self.assertEqual(completion.billing(completion.runtime_target("claude", None, "token")), "subscription", "as before")


if __name__ == "__main__":
    unittest.main()
