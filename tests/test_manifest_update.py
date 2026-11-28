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
         "mailbox": {"escalate": True}, "tests": [{"tier": "unit", "run": "sh test.sh", "when": ["merge"]}]}


class Instance(unittest.TestCase):
    """One unsandboxed project, `open`, in a scratch instance; the CLIs run as subprocesses against it."""

    def setUp(self):
        t = self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, t, ignore_errors=True)
        for d in ("home", "config/projects", "data", "secrets", "bus", "state/shared/registry", "ws/open"):
            (t / d).mkdir(parents=True)
        cfg = {"owner": {"name": "alex", "tag": "Alex"}, "owner_side": "builtin", "bus_dir": str(t / "bus"),
               "registry_dir": "projects", "workspace_root": str(t / "ws"), "services_root": str(t / "services"), "runtime": {"default": "none"},
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


class TheHubsOwnEntry(Instance):
    """The platform's own entry is its file in the repo, which apply-updates never writes: a manifest.update from it is
    refused when sent, and one already on the bus is returned, never written into the registry folder as answered."""

    def send(self, *argv):
        return subprocess.run([sys.executable, str(BIN / "hubbus"), "send", *argv], env=self.env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=60)

    def messages(self):
        c = sqlite3.connect(self.tmp / "bus" / "bus.db")
        try:
            return c.execute("SELECT id, source, to_project, payload FROM events WHERE type='message' ORDER BY seq").fetchall()
        except sqlite3.OperationalError:   # no bus yet: nothing was ever sent
            return []
        finally:
            c.close()

    def test_sent_by_the_hub_or_to_another_project_it_is_refused(self):
        for what, argv in (("from the hub", ("--from", HUB, "--to", HUB)), ("to a project", ("--from", "open", "--to", "open"))):
            with self.subTest(what):
                r = self.send(*argv, "--type", "manifest.update", "--text", "commits: conventional\n")
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("manifest.update", r.stderr)
        self.assertIn("commit", self.send("--from", HUB, "--to", HUB, "--type", "manifest.update", "--text", "a: 1").stderr)
        self.assertEqual(self.messages(), [])

    def test_one_on_the_bus_from_the_hub_is_returned(self):
        self.run_cli(BIN / "hubbus", "send", "--from", "open", "--to", HUB, "--type", "manifest.update", "--text",
                     "commits: conventional\n")
        c = sqlite3.connect(self.tmp / "bus" / "bus.db")
        try:
            with c:
                c.execute("UPDATE events SET source=? WHERE type='message'", (HUB,))
        finally:
            c.close()
        before = self.entry_file.read_text()
        self.run_cli(BIN / "registry.py", "apply-updates")
        self.assertFalse((self.tmp / "config" / "projects" / f"{HUB}.yaml").exists(), "no file the platform never reads")
        self.assertEqual(self.entry_file.read_text(), before)
        replies = [(s, to, json.loads(pl)["text"]) for _i, s, to, pl in self.messages()[1:]]
        self.assertEqual(len(replies), 1, replies)
        self.assertEqual(replies[0][:2], (HUB, HUB))
        self.assertIn("not applied", replies[0][2])
        self.assertIn("commit", replies[0][2])
        c = sqlite3.connect(self.tmp / "bus" / "bus.db")
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE type='message.done'").fetchone()[0], 1)
        finally:
            c.close()


class NothingStopsTheOthers(Instance):
    """A letter that can't be written, or whose reply can't be sent as it is, is returned like any other."""

    def test_each_is_returned_and_the_next_one_applied(self):
        before = self.entry_file.read_text()
        for what, text in (("a date key", "services: [{name: w, run: sh, expose: {2020-01-01: 1}}]"), ("a surrogate", 'purpose: "\\uD800"'),
                           ("a number", f"provides: [0x{'f' * 5000}]"), ("a date key elsewhere", "publishes: [{2020-01-01: 1}]"),
                           ("a NUL in a key", '"a\\0b": 1'), ("a surrogate in a key", '"\\uD800": 1'),
                           ("a long reply", "".join(f"k{i}: 1\n" for i in range(9000)))):
            with self.subTest(what):
                self.assertIn("not applied", self.update(text))
                self.assertEqual(self.entry_file.read_text(), before)
        self.assertIn("applied", self.update("commits: conventional\n"))
        self.assertEqual(self.entry()["commits"], "conventional")


class PartOfTheEntry(Instance):
    """A letter without v is the part that changes: merged into the entry, the rest kept."""

    def test_a_part_changes_only_what_it_names(self):
        tests = [{"tier": "unit", "run": "sh test-all.sh", "when": ["merge"]}]
        reply = self.update(yaml.safe_dump({"tests": tests, "code": ["src/**"]}))
        self.assertIn("applied", reply)
        self.assertEqual(self.entry(), {**ENTRY, "tests": tests, "code": ["src/**"]})
        self.assertIn("sh test-all.sh", reply, "the reply says what the changed fields now are")
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


class ReadBack(Instance):
    """`brunna manifest` (hubbus manifest): the entry of the project the folder belongs to, as a letter is merged onto
    it. What it prints, sent back whole or one field of it, changes nothing: a project copies from it."""

    ENTRIES = {
        "plain": ENTRY,
        "v1": {k: v for k, v in ENTRY.items() if k != "tests"} | {"v": 1, "runtime": {"test": "sh test.sh"}},
        "services": {**ENTRY, "services": [{"name": "web", "run": "sh serve.sh", "keepalive": True,
                                            "expose": {"local_port": 18555}}],
                     "jobs": [{"name": "tidy", "run": "sh tidy.sh", "at": "03:00"}]},
        "sandboxed": {**ENTRY, "enforce": {"sandbox": True}, "privacy": {"reflection": "exclude"},
                      "mailbox": {"escalate": True, "wake": False},
                      "secrets": [{"name": "api", "path": "~/workspace/open/.env", "env_key": "API_KEY",
                                   "expires": "2027-01-31"}]},
    }

    def setUp(self):
        super().setUp()
        self.ws = self.tmp / "home" / "workspace" / "open"
        self.ws.mkdir(parents=True)
        (self.tmp / "services" / "open").mkdir(parents=True)   # a v1 entry with runtime: its folder must be there

    def use(self, doc: dict):
        self.write_entry(doc)
        (self.tmp / "state" / "shared" / "registry" / "index.json").write_text(json.dumps({"projects": {"open": doc}}))

    def manifest(self, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN / "hubbus"), "manifest"], env=self.env, cwd=cwd or self.ws,
                              capture_output=True, text=True, timeout=60)

    # run in the scratch instance (its settings, not this checkout's): each part through registry.manifest_update
    CHECK = """
import json, sys, yaml
sys.path.insert(0, sys.argv[1])
import registry, tiers
doc, printed = json.loads(sys.argv[2]), yaml.load(sys.stdin.read(), Loader=registry.LetterLoader)
out = []
for part in [printed, *({k: v} for k, v in printed.items() if k not in ("v", "name"))]:
    new, errs = registry.manifest_update(part, "open", doc, {"projects": {"open": doc}})
    out.append({"part": sorted(part), "errs": errs, "same": new == tiers.upgrade(doc), "printed": printed == tiers.upgrade(doc)})
print(json.dumps(out))
"""

    def test_what_it_prints_sent_back_changes_nothing(self):
        for what, doc in self.ENTRIES.items():
            with self.subTest(what):
                self.use(doc)
                r = self.manifest()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("manifest.update", r.stderr, "how to change it, beside the entry")
                c = subprocess.run([sys.executable, "-c", self.CHECK, str(BIN), json.dumps(doc)], input=r.stdout,
                                   env=self.env, cwd=self.tmp, capture_output=True, text=True, timeout=60)
                self.assertEqual(c.returncode, 0, c.stderr)
                for got in json.loads(c.stdout):
                    self.assertEqual((got["errs"], got["same"], got["printed"]), ([], True, True), got["part"])

    def test_a_value_shared_by_an_anchor_is_written_out(self):
        self.use(ENTRY)
        self.entry_file.write_text(yaml.safe_dump(ENTRY, sort_keys=False) + (
            "services:\n- {name: a, run: sh a.sh, keepalive: true, env: &e {A: '1'}}\n"
            "- {name: b, run: sh b.sh, keepalive: true, env: *e}\n"))
        r = self.manifest()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("*", r.stdout)
        self.assertIn("changed nothing", self.update(r.stdout))

    def test_through_the_bus_it_changes_nothing(self):
        self.use(self.ENTRIES["sandboxed"])
        before = self.entry_file.read_text()
        self.assertIn("changed nothing", self.update(self.manifest().stdout))
        self.assertEqual(self.entry_file.read_text(), before)

    def test_from_a_folder_inside_the_project(self):
        self.use(ENTRY)
        (self.ws / "src").mkdir()
        r = self.manifest(self.ws / "src")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(yaml.safe_load(r.stdout)["name"], "open")

    def test_outside_a_project_it_is_refused(self):
        r = self.manifest(self.tmp)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not in a project folder", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_no_entry_or_an_unreadable_one_is_said(self):
        self.use(ENTRY)
        self.entry_file.unlink()
        r = self.manifest()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("has no registry entry", r.stderr)
        self.entry_file.write_text("name: open\n  bad: [\n")
        r = self.manifest()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot read", r.stderr)
        self.entry_file.write_text("- a list\n")
        self.assertIn("not a mapping", self.manifest().stderr)

    def test_it_takes_no_other_project(self):
        self.use(ENTRY)
        r = subprocess.run([sys.executable, str(BIN / "hubbus"), "manifest", "--project", "other"], env=self.env,
                           cwd=self.ws, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


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
