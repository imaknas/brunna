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

os.environ["AGENT_HUB_LANG_UI"] = os.environ["BRUNNA_LANG_UI"] = "en"   # the messages these tests read are the English ones
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
                    "AGENT_HUB_LANG_UI": "en", "BRUNNA_LANG_UI": "en", "AGENT_HUB_LANG_CONTENT": "en",
                    "BRUNNA_LANG_CONTENT": "en", "GIT_CONFIG_GLOBAL": "/dev/null"}
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


class PartOfTheEntry(Instance):
    """A letter without v is the part that changes: merged into the entry, the rest kept."""

    def test_a_part_changes_only_what_it_names(self):
        tests = [{"tier": "unit", "run": "uv run pytest -q", "when": ["merge"]}]
        reply = self.update(yaml.safe_dump({"tests": tests, "code": ["src/**"]}))
        self.assertIn("applied", reply)
        self.assertEqual(self.entry(), {**ENTRY, "tests": tests, "code": ["src/**"]})
        self.assertIn("uv run pytest -q", reply, "the reply says what the changed fields now are")
        self.assertIn("src/**", reply)

    def test_null_removes_a_field(self):
        reply = self.update("tests: null\n")
        self.assertIn("Removed: tests", reply)
        self.assertNotIn("tests", self.entry())

    def test_a_mapping_merges_key_by_key(self):
        self.write_entry({**ENTRY, "mailbox": {"escalate": True, "wake": False}})
        reply = self.update("mailbox:\n  escalate: false\n")
        self.assertIn("applied", reply)
        self.assertEqual(self.entry()["mailbox"], {"escalate": False, "wake": False}, "wake stays the owner's")

    def test_a_part_that_changes_nothing_writes_nothing(self):
        before = self.entry_file.read_text()
        reply = self.update(yaml.safe_dump({"tests": ENTRY["tests"]}))
        self.assertIn("changed nothing", reply)
        self.assertEqual(self.entry_file.read_text(), before)

    def test_a_v1_entry_takes_v2_fields(self):
        self.write_entry({k: v for k, v in ENTRY.items() if k != "tests"} | {"v": 1})
        reply = self.update(yaml.safe_dump({"tests": ENTRY["tests"]}))
        self.assertIn("applied", reply)
        self.assertEqual(self.entry(), ENTRY)

    def test_the_reply_shows_only_the_keys_sent(self):
        self.write_entry({**ENTRY, "mailbox": {"escalate": True, "wake": False}})
        reply = self.update("mailbox:\n  escalate: false\n")
        self.assertIn("escalate: false", reply)
        self.assertNotIn("wake", reply, "the owner's key beside it isn't echoed")

    def test_an_alias_is_returned_and_the_next_letter_still_applies(self):
        reply = self.update("models: &m {a: *m}\n")
        self.assertIn("not applied", reply)
        self.assertIn("alias", reply)
        reply = self.update("commits: conventional\n")
        self.assertIn("applied", reply)
        self.assertEqual(self.entry()["commits"], "conventional")

    def test_an_unreadable_entry_returns_the_letter(self):
        self.entry_file.write_text("name: open\n  bad: [\n")
        reply = self.update("commits: conventional\n")
        self.assertIn("not applied", reply)

    def test_an_invalid_result_changes_nothing(self):
        before = self.entry_file.read_text()
        reply = self.update(yaml.safe_dump({"tests": [{"tier": "unit", "when": ["merge"]}]}))
        self.assertIn("not applied", reply)
        self.assertEqual(self.entry_file.read_text(), before)


class PartsCantDoMore(unittest.TestCase):
    """A part is checked as the whole entry it makes: whatever a whole entry may not change, a part may not either
    (registry.manifest_update, no bus)."""

    CUR = {**ENTRY, "enforce": {"sandbox": True, "capabilities": ["pdf"]}, "privacy": {"reflection": "exclude"},
           "mailbox": {"escalate": True, "wake": False},
           "jobs": [{"name": "brief", "kind": "agent", "at": "07:00", "prompt": "jobs/brief.md", "connectors": True,
                     "allow": ["Read"], "legacy_name": "old-brief"}]}

    def refused(self, part: dict, cur: dict | None = None, sender: str = "open") -> list[str]:
        import registry
        _new, errs = registry.manifest_update(part, sender, self.CUR if cur is None else cur)
        return errs

    def test_security_fields(self):
        for part in ({"enforce": {"sandbox": False}}, {"enforce": None}, {"enforce": {"capabilities": ["pdf", "gpu"]}},
                     {"paths": {"workspace": "~"}}, {"paths": None}, {"privacy": None}, {"privacy": {"reflection": "include"}},
                     {"network": ["example.com"]}, {"allow_write": ["~"]}, {"metered_models": True}, {"type": "idea"},
                     {"mailbox": {"wake": True}}, {"mailbox": {"wake": None}}, {"mailbox": None}):
            with self.subTest(part=part):
                self.assertTrue(any("security field" in e for e in self.refused(part)), self.refused(part))

    def test_a_connector_job(self):
        job = self.CUR["jobs"][0]
        for part in ({"jobs": None}, {"jobs": []}, {"jobs": [{**job, "allow": ["Read", "Bash"]}]},
                     {"jobs": [{**job, "legacy_name": "other"}]}):
            with self.subTest(part=part):
                self.assertTrue(any("security field" in e for e in self.refused(part)), self.refused(part))

    def test_another_name(self):
        for part in ({"name": "other"}, {"name": None}):
            with self.subTest(part=part):
                self.assertTrue(any("its own registry entry" in e for e in self.refused(part)), self.refused(part))

    def test_v_null_is_a_whole_entry(self):
        self.assertTrue(self.refused({"v": None, "tests": []}))

    def test_no_aliases(self):
        import registry
        laughs = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
            f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join(['*' + chr(97 + i)] * 10)}]\n" for i in range(9))
        for text in ("models: &m {a: *m}\n", laughs):
            with self.subTest(text=text[:20]), self.assertRaises(yaml.YAMLError):
                yaml.load(text, Loader=registry.LetterLoader)

    def test_no_entry_to_change(self):
        import registry
        new, errs = registry.manifest_update({"tests": []}, "ghost", None)
        self.assertIsNone(new)
        self.assertTrue(errs)

    def test_the_fields_a_project_owns(self):
        for part in ({"tests": None}, {"commits": "conventional"}, {"models": {"sum": {"tier": "fast"}}},
                     {"mailbox": {"escalate": False}}, {"name": "open"}):
            with self.subTest(part=part):   # (the scratch platform has no pdf and no prompt file: only who may change what)
                self.assertEqual([e for e in self.refused(part) if "security field" in e or "own registry entry" in e], [])


if __name__ == "__main__":
    unittest.main()
