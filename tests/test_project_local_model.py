"""A project's declared model call served by a local model (11-15; docs/design/llm-providers.md "Projects' calls on a
provider"): the owner pins <project>/<point> in llm.points to one of llm.providers, the project names only a tier.

Configuring a provider for that must move nothing else: llm.providers without llm.use serves only the points pinned to
it, every other completion point (the platform's and other projects') goes where it goes without llm.providers. The
local Ollama is a stub HTTP server here; no real model is called.

    bin/run-tests tests/test_project_local_model.py
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

PROJECTS = {"proj": {"name": "proj", "models": {"reviewer": {"tier": "strong"}, "classify": {"tier": "fast"}}},
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
        provider_ollama._show.cache_clear()
        self.addCleanup(provider_ollama._show.cache_clear)

    def config(self, llm_block):
        """The instance's settings: runtime.default claude, and this llm block (None = none at all)."""
        cfg = {"runtime": {"default": "claude"}, **({"llm": llm_block} if llm_block is not None else {})}
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

    def test_a_pinned_name_the_provider_lists_keeps_its_context(self):
        self.config({"providers": {"local": local()}, "points": {"proj/reviewer": {"provider": "local", "model": "big:35b"}}})
        t = completion.resolve("proj/reviewer", {"model_group": "strong"})
        self.assertEqual((t["model"], t["context"]), ("big:35b", 65536))

    def test_doctor_and_schedulectl_see_where_each_call_goes(self):
        self.config({"providers": {"local": local()},
                     "points": {"proj/reviewer": {"provider": "local"}, "proj/gone": {"provider": "local"}}})
        calls, stale = llm.project_calls()
        got = {key: (t["provider"], t["group"]) for key, _tier, t, _why in calls}
        self.assertEqual(got["proj/reviewer"], ("local", "strong"))
        self.assertIsNone(got["other/summarize"][1], "the runtime's own completion interface")
        self.assertEqual(stale, ["proj/gone"])


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
        # words, not one 900k-character run: redact's lookahead scans a run to its end at each position
        req.write_text(json.dumps({"prompt": "word " * 180_000}))
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


if __name__ == "__main__":
    unittest.main()
