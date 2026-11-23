"""manifest.update: a project changes its own registry entry by letter (`registry.py apply-updates`, every 15 minutes).

End to end through a scratch instance: the letter goes onto the bus with hubbus, apply-updates writes the entry or
sends the problems back, and closes the letter either way.

    bin/py -m unittest tests.test_manifest_update
"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import names  # noqa: E402

HUB = names.current("project")
ENTRY = {"v": 2, "name": "open", "paths": {"workspace": "~/workspace/open"}, "memory": True,
         "mailbox": {"escalate": True}, "tests": [{"tier": "unit", "run": "make test", "when": ["merge"]}]}


class Instance(unittest.TestCase):
    """One unsandboxed project, `open`, in a scratch instance; the CLIs run as subprocesses against it."""

    def setUp(self):
        t = self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, t, ignore_errors=True)
        for d in ("home", "config/projects", "data", "secrets", "bus", "state/shared/registry", "ws/open"):
            (t / d).mkdir(parents=True)
        cfg = {"owner": {"name": "alex", "tag": "Alex"}, "owner_side": "builtin", "bus_dir": str(t / "bus"),
               "registry_dir": "projects", "workspace_root": str(t / "ws"), "runtime": {"default": "none"},
               "language": {"ui": "en", "content": "en"}}
        (t / "config" / "hub.json").write_text(json.dumps(cfg))
        self.write_entry(ENTRY)
        (t / "state" / "shared" / "registry" / "index.json").write_text(json.dumps({"projects": {"open": ENTRY}}))
        self.env = {**{k: v for k, v in os.environ.items()
                       if not k.startswith(("BRUNNA_", "AGENT_HUB_", "AGENTHUB_", "HUB_"))
                       or k in ("AGENT_HUB_PYTHON", "BRUNNA_PYTHON")},
                    "HOME": str(t / "home"), "AGENT_HUB_CONFIG": str(t / "config"), "AGENT_HUB_DATA": str(t / "data"),
                    "AGENT_HUB_SECRETS": str(t / "secrets"), "AGENT_HUB_STATE": str(t / "state"),
                    "AGENT_HUB_LANG_UI": "en", "AGENT_HUB_LANG_CONTENT": "en", "GIT_CONFIG_GLOBAL": "/dev/null"}
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)

    @property
    def entry_file(self) -> pathlib.Path:
        return self.tmp / "config" / "projects" / "open.yaml"

    def write_entry(self, doc: dict):
        self.entry_file.write_text(yaml.safe_dump(doc, sort_keys=False))

    def entry(self) -> dict:
        return yaml.safe_load(self.entry_file.read_text())

    def run_cli(self, *argv):
        r = subprocess.run([sys.executable, *map(str, argv)], env=self.env, cwd=self.tmp, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def update(self, text: str) -> str:
        """Send one manifest.update as `open`, apply it, and return the platform's reply to it."""
        self.run_cli(BIN / "hubbus", "send", "--from", "open", "--to", HUB, "--type", "manifest.update", "--text", text)
        self.run_cli(BIN / "registry.py", "apply-updates")
        c = sqlite3.connect(self.tmp / "bus" / "bus.db")
        try:
            letters = [json.loads(p) for (p,) in c.execute(
                "SELECT payload FROM events WHERE type='message' AND to_project='open' ORDER BY seq")]
            open_updates = c.execute(
                "SELECT COUNT(*) FROM events m WHERE m.type='message' AND m.to_project=? AND NOT EXISTS("
                "SELECT 1 FROM events x WHERE x.type='message.done' AND x.ref=m.id)", (HUB,)).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(open_updates, 0, "every manifest.update is closed, applied or not")
        self.assertTrue(letters, "the project always gets a reply")
        return letters[-1]["text"]


class WholeEntry(Instance):
    """A letter holding the whole entry (it has v) replaces it, as it always has."""

    def test_a_valid_entry_replaces_the_old_one(self):
        new = {k: v for k, v in ENTRY.items() if k != "tests"} | {"commits": "conventional"}
        reply = self.update(yaml.safe_dump(new))
        self.assertIn("applied", reply)
        self.assertEqual(self.entry(), new, "what the letter left out is gone")

    def test_an_invalid_entry_changes_nothing(self):
        before = self.entry_file.read_text()
        reply = self.update(yaml.safe_dump({**ENTRY, "tests": [{"tier": "unit", "when": ["merge"]}]}))
        self.assertIn("not applied", reply)
        self.assertEqual(self.entry_file.read_text(), before)

    def test_another_projects_entry_is_refused(self):
        before = self.entry_file.read_text()
        reply = self.update(yaml.safe_dump({**ENTRY, "name": "other"}))
        self.assertIn("not applied", reply)
        self.assertEqual(self.entry_file.read_text(), before)

    def test_a_security_field_is_refused(self):
        before = self.entry_file.read_text()
        reply = self.update(yaml.safe_dump({**ENTRY, "enforce": {"sandbox": False}}))
        self.assertIn("security field", reply)
        self.assertEqual(self.entry_file.read_text(), before)


if __name__ == "__main__":
    unittest.main()
