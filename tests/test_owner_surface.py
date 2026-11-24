"""The owner's surface stays small (the owner's rule, 11-16: the system may be complex, but the complexity lands on the
platform's agents, never on the owner). Two mechanical checks:

- what the platform writes into todo.md's fixed blocks for the owner asks a question or states a fact; it never tells
  the owner to run one of the platform's scripts (an agent runs those when the owner answers);
- `brunna --help` lists only the owner's few commands; everything is under `brunna help all`.

    bin/py -m unittest tests.test_owner_surface
"""
import json
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = str(ROOT / "bin" / "py")
# the scripts that put lines for the owner into todo.md's fixed blocks
TODO_BLOCK_WRITERS = ["check-memory-gaps", "check-state-anchors", "check-idea-upgrades"]
SCRIPT = re.compile(r"`(?:bin/|bin/py )[\w./-]+")


class TodoBlocks(unittest.TestCase):
    def test_no_script_for_the_owner_to_run(self):
        for name in TODO_BLOCK_WRITERS:
            src = (ROOT / "bin" / f"{name}.py").read_text()
            body = src.split('"""', 2)[-1]   # the header describes the script to agents; the lines come after it
            texts = [body]
            cat = ROOT / "locales" / "zh-Hant" / f"{name}.json"
            if cat.exists():
                texts += list(json.loads(cat.read_text()).values())
            for text in texts:
                with self.subTest(name):
                    self.assertIsNone(SCRIPT.search(text), f"{name}: an owner-facing line names a script to run")


class Help(unittest.TestCase):
    def run_(self, *argv):
        return subprocess.run([PY, str(ROOT / "bin" / "agenthub"), *argv], capture_output=True, text=True,
                              timeout=60).stdout

    def test_help_is_the_owners_and_all_is_everything(self):
        short, full = self.run_("--help"), self.run_("help", "all")
        commands = set(re.findall(r"^  brunna ([a-z-]+)", short, re.M))
        # with the two a newcomer looks for (connect, llm setup: the 11-25 walkthrough)
        self.assertLessEqual(len(commands), 9, commands)
        self.assertIn("help all", short)
        self.assertGreater(len(full.splitlines()), 3 * len(short.splitlines()))
        for c in ("merge-branch", "config", "send"):
            self.assertIn(f"brunna {c}", full)
            self.assertNotIn(f"brunna {c}", short)


if __name__ == "__main__":
    unittest.main()
