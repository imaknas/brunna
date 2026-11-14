"""A project's declared model call served by a local model (11-15; docs/design/llm-providers.md "Projects' calls on a
provider"): the owner pins <project>/<point> in llm.points to one of llm.providers, the project names only a tier.

Configuring a provider for that must move nothing else: llm.providers without llm.use serves only the points pinned to
it, every other completion point (the platform's and other projects') goes where it goes without llm.providers. The
local Ollama is a stub HTTP server here; no real model is called.

    bin/run-tests        (or brunna test)
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(ROOT / "tests"))
import completion  # noqa: E402
import hubconf  # noqa: E402
import provider_ollama  # noqa: E402
from test_completion import FakeServer  # noqa: E402


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / file))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


llm = load("llm_plm", "llm.py")
llm_ready = load("llm_ready_plm", "llm_ready.py")

PROJECTS = {"proj": {"name": "proj", "models": {"reviewer": {"tier": "strong"}, "classify": {"tier": "fast"}},
                     "agents": {"fixer": {"tier": "standard", "allow": ["Read"]}}},
            "other": {"name": "other", "models": {"summarize": {"tier": "standard"}}}}


def setUpModule():
    p = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": "en"})   # the reasons asserted are English
    p.start()
    unittest.addModuleCleanup(p.stop)


def local(url="http://127.0.0.1:11434", **models):
    return {"impl": "ollama", "base_url": url, "location": "local",
            "models": models or {"strong": {"name": "big:35b", "context": 65536}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.usage = self.tmp / "usage.jsonl"
        self.failed = []
        for obj, target, value in ((completion, "USAGE", self.usage), (hubconf, "projects", lambda: PROJECTS),
                                   (llm, "job_failed", lambda p, r: self.failed.append((p, r)))):
            p = mock.patch.object(obj, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.config({})
        provider_ollama._shown.clear()
        self.addCleanup(provider_ollama._shown.clear)

    def config(self, llm_block, **more):
        """The instance's settings: runtime.default claude, this llm block (None = none at all), and more."""
        cfg = {"runtime": {"default": "claude"}, **({"llm": llm_block} if llm_block is not None else {}), **more}
        p = mock.patch.object(hubconf, "_cfg", cfg)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def where(t):
        return (t["provider"], t["impl"], t["model"], t["location"])


class NothingElseMoves(Base):
    def targets(self):
        out = {}
        for name, spec in llm.points()["points"].items():
            if spec.get("shape") == "completion":
                out[name] = self.where(llm.completion_target(name, spec))
                out[name + " ready"] = llm_ready.unsupported(name) is None
        for proj, doc in PROJECTS.items():
            for point, decl in doc["models"].items():
                spec = {"shape": "completion", "model_group": decl["tier"]}
                out[f"{proj}/{point}"] = self.where(llm.completion_target(f"{proj}/{point}", spec))
        return out

    def test_a_local_provider_without_use_leaves_every_unpinned_point_where_it_was(self):
        self.config(None)
        before = self.targets()
        self.assertTrue(before, "there are completion points to compare")
        self.config({"providers": {"local": local()}})
        self.assertEqual(self.targets(), before)
        self.config({"providers": {"local": local()}, "points": {"proj/reviewer": {"provider": "local"}}})
        after = self.targets()
        self.assertEqual(after.pop("proj/reviewer"), ("local", "ollama", "big:35b", "local"))
        before.pop("proj/reviewer")
        self.assertEqual(after, before, "only the pinned call moved")

    def test_use_still_moves_every_group_it_names(self):
        self.config({"providers": {"local": local(fast="small:8b")}, "use": {"fast": "local"}})
        spec = {"shape": "completion", "model_group": "fast"}
        self.assertEqual(self.where(llm.completion_target("proj/classify", spec))[:3], ("local", "ollama", "small:8b"))

    def test_config_check_takes_providers_without_use_and_project_pins(self):
        ok = {"providers": {"local": local()}, "points": {"proj/reviewer": {"provider": "local", "model": "big:35b"}}}
        self.assertEqual(completion.config_errors(ok), [])
        for bad in ({"proj/reviewer": {"runtime": "claude"}}, {"proj/Bad Name": {"provider": "local"}},
                    {"proj/reviewer": {"provider": "cloud"}}):
            self.assertTrue(completion.config_errors({**ok, "points": bad}), bad)
        keyed = json.loads(json.dumps(ok))
        keyed["providers"]["local"]["key"] = {"env": "X"}
        self.assertTrue(any("location local" in e for e in completion.config_errors(keyed)), "local and a key: refused")

    def test_a_provider_pin_on_an_agent_call_is_flagged(self):
        """an agent call runs on the runtime: a provider pin there would be ignored (config check, doctor, schedulectl
        say so; llm.py refuses the call, tests/test_project_agent.py)"""
        pins = {"providers": {"local": local()}, "points": {"proj/fixer": {"provider": "local"}}}
        self.assertEqual(completion.config_errors(pins), [], "without the registry only the form is checked")
        [err] = completion.config_errors(pins, PROJECTS, "claude")
        self.assertIn("proj/fixer", err)
        self.assertEqual(completion.config_errors({**pins, "points": {"proj/fixer": {"model": "m"}}}, PROJECTS, "claude"),
                         [], "a model pin on an agent call is fine")
        self.config(pins)
        self.assertEqual(llm.project_calls()[2], ["proj/fixer"])

    def test_the_feature_table_counts_providers_only_with_use(self):
        import runtime_ports
        self.config({"providers": {"local": local()}})
        self.assertEqual(runtime_ports._feature_settings()[0], False, "providers without use serve no judgment point")
        self.config({"providers": {"local": local(fast="s")}, "use": {"fast": "local"}})
        self.assertEqual(runtime_ports._feature_settings()[0], True)

    def test_why_a_point_has_nowhere_to_go_names_use(self):
        self.config({"providers": {"local": local()}})
        self.assertIn("llm.use", completion.nowhere("none", {"model_group": "fast"}))
        self.config(None)
        self.assertNotIn("llm.use", completion.nowhere("none"))

    def test_a_pinned_name_the_provider_lists_keeps_its_context(self):
        self.config({"providers": {"local": local()}, "points": {"proj/reviewer": {"provider": "local", "model": "big:35b"}}})
        t = completion.resolve("proj/reviewer", {"model_group": "strong"})
        self.assertEqual((t["model"], t["context"]), ("big:35b", 65536))

    def test_doctor_and_schedulectl_see_where_each_call_goes(self):
        self.config({"providers": {"local": local()},
                     "points": {"proj/reviewer": {"provider": "local"}, "proj/gone": {"provider": "local"}}})
        calls, stale, _agent_pins = llm.project_calls()
        got = {key: (t["provider"], t["group"]) for key, _tier, t, _why in calls}
        self.assertEqual(got["proj/reviewer"], ("local", "strong"))
        self.assertIsNone(got["other/summarize"][1], "the runtime's own completion interface")
        self.assertEqual(stale, ["proj/gone"])

    def test_doctor_lines(self):
        hub = load("agenthub_plm", "agenthub")
        self.config({"providers": {"local": local()},
                     "points": {"proj/reviewer": {"provider": "local"}, "proj/classify": {"provider": "nope"},
                                "proj/gone": {"provider": "local"}}})
        rows = []
        hub.doctor_project_calls(lambda level, text, fix="": rows.append((level, text)))
        by = {level: [t for lv, t in rows if lv == level] for level in ("ok", "bad", "warn")}
        self.assertEqual(by["ok"], ["project model call proj/reviewer → local (local) big:35b"])
        [bad] = by["bad"]
        self.assertIn("proj/classify", bad)
        self.assertIn("nope", bad)
        [warn] = by["warn"]
        self.assertIn("proj/gone", warn)
        self.assertEqual(len(rows), 3, "calls on the runtime have no line of their own")


class Ollama(Base):
    """The whole path of `brunna complete reviewer` past cap-runner, against a stub Ollama."""

    def serve(self, answer='{"verdict": "ok"}', caps=("completion", "thinking"), chat_status=200):
        def h(method, path, body):
            if path == "/api/show":
                return 200, {"capabilities": list(caps), "model_info": {"fam.context_length": 262144}}
            if path == "/api/chat":
                if "format" in body:
                    return 501, {"error": "structured output is unavailable"}
                return chat_status, {"message": {"content": answer}, "done_reason": "stop", "prompt_eval_count": 11,
                                     "eval_count": 4}
            return 404, {}
        s = FakeServer(h)
        self.addCleanup(s.close)
        return s

    def call(self, schema=None, point="reviewer"):
        req = self.tmp / "req.json"
        req.write_text(json.dumps({"prompt": "Review this change.", "schema": schema}))
        with redirect_stdout(io.StringIO()) as out:
            rc = llm.project_call("proj", point, req)
        return rc, json.loads(out.getvalue())

    def rows(self):
        return [json.loads(x) for x in self.usage.read_text().splitlines()] if self.usage.exists() else []

    def chats(self, s):
        return [b for _m, p, _h, b in s.requests if p == "/api/chat"]

    def test_a_pinned_call_goes_to_the_local_model_with_a_window_sized_to_it(self):
        s = self.serve()
        self.config({"providers": {"local": {**local(s.url), "models": {}}},
                     "points": {"proj/reviewer": {"provider": "local", "model": "qwen:35b"}}})
        schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}
        rc, out = self.call(schema)
        self.assertEqual((rc, out["structured_output"], out["provider"], out["model"]),
                         (0, {"verdict": "ok"}, "local", "qwen:35b"))
        first, second = self.chats(s)
        self.assertIn("format", first)
        self.assertNotIn("format", second)
        self.assertEqual(first["options"]["num_ctx"], 16384, "8192 output + a short prompt, not the model's 262144")
        self.assertIs(first["think"], False)
        [row] = self.rows()   # one usage row: the 501 is the provider's own fallback, one completion
        self.assertEqual((row["project"], row["point"], row["billing"], row["provider"]),
                         ("proj", "proj/reviewer", "local", "local"))

    def test_other_projects_calls_stay_on_the_runtime(self):
        self.config({"providers": {"local": local()}, "points": {"proj/reviewer": {"provider": "local"}}})
        t = llm.completion_target("proj/classify", {"shape": "completion", "model_group": "fast"})
        self.assertEqual((t["provider"], t["group"]), ("claude", None))

    def test_too_long_for_the_models_context_is_not_sent(self):
        s = self.serve()
        self.config({"providers": {"local": {**local(s.url), "models": {}}},
                     "points": {"proj/reviewer": {"provider": "local", "model": "qwen:35b"}}})
        req = self.tmp / "req.json"
        req.write_text(json.dumps({"prompt": "x" * 900_000}))   # one run: redact is linear
        with redirect_stdout(io.StringIO()) as out:
            rc = llm.project_call("proj", "reviewer", req)
        self.assertEqual(rc, 1)
        self.assertIn("262144", json.loads(out.getvalue())["llm_error"])
        self.assertEqual(self.chats(s), [])

    def test_a_model_without_thinking_gets_no_think_field_and_think_is_stripped(self):
        s = self.serve(answer="<think>hmm</think>\nLooks fine.", caps=("completion",))
        self.config({"providers": {"local": {**local(s.url), "models": {}}},
                     "points": {"proj/reviewer": {"provider": "local", "model": "g:12b"}}})
        rc, out = self.call()
        self.assertEqual((rc, out["result"]), (0, "Looks fine."))
        self.assertNotIn("think", self.chats(s)[0])

    def test_failures_are_said_and_nothing_else_is_tried(self):
        s = self.serve(answer="")
        self.config({"providers": {"local": {**local(s.url), "models": {}}},
                     "points": {"proj/reviewer": {"provider": "local", "model": "qwen:35b"}}})
        rc, out = self.call()
        self.assertEqual(rc, 1)
        self.assertIn("empty", out["llm_error"])
        self.config({"providers": {"local": {**local("http://127.0.0.1:9"), "models": {}}},
                     "points": {"proj/reviewer": {"provider": "local", "model": "qwen:35b"}}})
        rc, out = self.call()
        self.assertEqual(rc, 1)
        self.assertIn("127.0.0.1:9", out["llm_error"])
        self.assertEqual({r["provider"] for r in self.rows()}, {"local"}, "never another provider")
        self.assertEqual(len(self.failed), 2, "each failure is published (job.failed)")

    def test_digit_heavy_text_gets_a_window_that_holds_it(self):
        """a token per digit: 30k digits need a 64k window (bytes / 3 said 16k, and Ollama cut the prompt and answered)"""
        s = self.serve()
        self.config({"providers": {"local": {**local(s.url), "models": {}}},
                     "points": {"proj/reviewer": {"provider": "local", "model": "qwen:35b"}}})
        req = self.tmp / "req.json"
        req.write_text(json.dumps({"prompt": " ".join(str(10 ** 9 + i) for i in range(3000))}))   # 30k digits
        with redirect_stdout(io.StringIO()):
            self.assertEqual(llm.project_call("proj", "reviewer", req), 0)
        [chat] = self.chats(s)
        self.assertEqual(chat["options"]["num_ctx"], 65536)
        self.assertEqual((chat["truncate"], chat["shift"]), (False, False))

    def test_a_metered_provider_pinned_still_needs_the_owners_opt_in(self):
        self.config({"providers": {"paid": {"impl": "openai", "base_url": "http://127.0.0.1:9", "key": {"env": "PLM_KEY"},
                                            "models": {"strong": "big"}}},
                     "points": {"proj/reviewer": {"provider": "paid"}}})
        with mock.patch.dict(os.environ, {"PLM_KEY": "k"}):
            rc, out = self.call()
        self.assertEqual(rc, 1)
        self.assertIn("metered_models", out["llm_error"])
        self.assertEqual(self.rows(), [], "refused before anything was sent")

    def test_the_call_ends_before_cap_runners_deadline(self):
        seen = []
        self.config({"providers": {"local": local()}, "points": {"proj/reviewer": {"provider": "local"}}},
                    cap_runner={"timeout_s": 300})
        with mock.patch.object(completion, "run", lambda point, spec, *a, **k: seen.append(spec["timeout_s"]) or {}):
            self.call()
        self.assertEqual(seen, [300 - llm.PROJECT_CALL_MARGIN_S])

    def test_a_slow_answer_is_said_to_be_slow_and_a_failed_show_is_asked_again(self):
        import threading
        import time as _time
        from runtime_ports import CompletionRequest
        shows = []

        def h(method, path, body):
            if path == "/api/show":
                shows.append(1)
                return 500, {"error": "busy"}
            _time.sleep(2)
            return 200, {"message": {"content": "late"}, "done_reason": "stop"}
        s = FakeServer(h)
        self.addCleanup(s.close)
        req = CompletionRequest(point="p", prompt="hi", model="m", schema=None, max_tokens=10, timeout_s=1)
        self.assertIsNone(provider_ollama.model_context({"base_url": s.url}, "m"))
        r = provider_ollama.complete(req, {"base_url": s.url})
        self.assertFalse(r.ok)
        self.assertIn("timed out", r.error)
        self.assertNotIn("could not be reached", r.error)
        self.assertGreaterEqual(len(shows), 2, "a failed /api/show isn't kept")
        threading.Event().wait(1.5)   # let the stub finish its sleeping request before it is shut down


if __name__ == "__main__":
    unittest.main()
