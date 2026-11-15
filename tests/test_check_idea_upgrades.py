"""check-idea-upgrades.py: which idea sandboxes are listed for the owner (registry facts only), --keep holding one until
a new signal appears, the todo.md block, and the rules text an idea sandbox's sessions get (prompts.listed_rules).

    bin/py -m unittest tests.test_check_idea_upgrades
"""
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import prompts  # noqa: E402


def load():
    spec = importlib.util.spec_from_file_location("check_idea_upgrades", ROOT / "bin" / "check-idea-upgrades.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


IDEA = {"type": "idea", "purpose": "explore", "enforce": {"sandbox": True}}


class Signals(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_a_bare_exploration_has_none(self):
        self.assertEqual(self.m.signals(IDEA), [])
        self.assertEqual(self.m.signals({**IDEA, "network": ["pypi.org", "files.pythonhosted.org", "huggingface.co"]}), [])

    def test_each_signal(self):
        doc = {**IDEA, "secrets": [{"name": "api-key"}], "tests": [{"tier": "fast"}], "metered_models": ["x"],
               "services": [{"name": "web"}], "network": ["pypi.org", "api.example.com"]}
        self.assertEqual(self.m.signals(doc), ["secrets", "tests", "metered", "runs", "network"])
        self.assertIn("api-key", self.m.describe("secrets", doc))
        self.assertIn("api.example.com", self.m.describe("network", doc))
        self.assertNotIn("pypi.org", self.m.describe("network", doc))
        self.assertEqual(self.m.signals({**IDEA, "jobs": [{"name": "nightly"}]}), ["runs"])


class Scan(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.m.KEPT = self.tmp / "kept.json"
        self.projects = {
            "garden": {**IDEA, "secrets": [{"name": "k"}]},
            "quiet": dict(IDEA),
            "real": {"type": "project", "secrets": [{"name": "k"}]},
        }
        patcher = mock.patch.object(self.m.hubconf, "projects", lambda: self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_ideas_with_signals_are_listed(self):
        lines = self.m.scan(None)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("**garden**", lines[0])
        self.assertTrue(lines[0].startswith("- [ ] "))

    def test_keep_holds_until_a_new_signal(self):
        self.assertEqual(self.m.keep("garden"), 0)
        self.assertEqual(self.m.scan(None), [])
        self.projects["garden"]["tests"] = [{"tier": "fast"}]
        self.assertEqual(len(self.m.scan(None)), 1)

    def test_keep_refuses_a_project(self):
        self.assertEqual(self.m.keep("real"), 1)
        self.assertFalse(self.m.KEPT.exists())

    def test_todo_block_put_and_removed(self):
        todo = self.tmp / "todo.md"
        todo.write_text("# todo\n\n- [ ] something\n")
        self.m.write_todo_block(todo, self.m.scan(None))
        text = todo.read_text()
        self.assertIn("<!-- idea-upgrades:begin -->", text)
        self.assertIn("**garden**", text)
        self.m.write_todo_block(todo, self.m.scan(None))   # rewritten in place, never twice
        self.assertEqual(todo.read_text().count("idea-upgrades:begin"), 1)
        self.m.write_todo_block(todo, [])
        self.assertEqual(todo.read_text(), "# todo\n\n- [ ] something\n")


class Rules(unittest.TestCase):
    def test_an_idea_sandbox_gets_its_rule(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rules = tmp / "RULES"
        self.assertIn("idea-sandbox", prompts.listed_rules(rules, {"type": "idea"}))
        self.assertNotIn("idea-sandbox", prompts.listed_rules(rules, {"type": "project"}))
        rules.write_text("idea-sandbox\n")
        self.assertEqual(prompts.listed_rules(rules, {"type": "idea"}).count("idea-sandbox"), 1)
        text, missing = prompts.agent_rules(["idea-sandbox"])
        self.assertEqual(missing, [])
        self.assertIn("brunna send --to brunna --type request", text)
        self.assertNotIn("{{", text)


if __name__ == "__main__":
    unittest.main()
