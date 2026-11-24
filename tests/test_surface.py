"""brunna surface (bin/surface.py, docs/design/agent-surface.md): what the platform shows and offers inside one coding agent
session, in the session-surface.v1 contract, for a surface adapter (the Claude Code mod). It is decided from the session's
own record, never from the environment it runs in; it reads only (the bus is left exactly as it was: no presence, no
telling, no hold); served: false carries nothing but why_not, except gated; and its lines hold counts and states only,
so a sandboxed project's session sees no other project's text in them.

    bin/run-tests tests   (or brunna test)
"""
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import contracts  # noqa: E402
import names  # noqa: E402

SCHEMA = ROOT / "contracts" / "session-surface.v1.schema.json"
MOD = ROOT / "integrations" / "claude-code" / "hooks" / "brunna.tsx"


def mod_blank() -> set[str]:
    """The variables the Claude Code mod passes blank, read from its source (PLATFORM_VARS in each spelling, OTHER_VARS)."""
    src = MOD.read_text()

    def listed(name):
        return re.findall(r"'([A-Z_]+)'", re.search(rf"const {name} = \[(.*?)\]", src, re.S).group(1))
    return {p + x for x in listed("PLATFORM_VARS") for p in ("AGENT_HUB_", "AGENTHUB_", "BRUNNA_")} | set(listed("OTHER_VARS"))


def tree(*roots) -> dict:
    """Every file and folder under these, with its size and modification time."""
    out = {}
    for root in roots:
        for p in sorted(pathlib.Path(root).rglob("*")) if pathlib.Path(root).exists() else []:
            st = p.lstat()
            out[str(p)] = (st.st_size if p.is_file() else None, st.st_mtime_ns)
    return out


def mine(files: dict) -> dict:
    """Without what other test modules, run at the same time on this same tree, write into its .logs and .state: the
    memory sync's log (test_agent_folder_git) and a test run's own throwaway platform."""
    theirs = (str(ROOT / ".logs" / "sync-all-memory-"), str(ROOT / ".state" / "run-tests-guard-"))
    return {p: v for p, v in files.items() if not p.startswith(theirs)}


SESSION = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-2222-3333-4444-555555555555"

# Runs inside the test instance (its env): tells this session a letter as a hook does, 40 minutes ago
TELL = """
import datetime, importlib.machinery, importlib.util, sys
sys.path.insert(0, sys.argv[1])
loader = importlib.machinery.SourceFileLoader("hb", sys.argv[1] + "/hubbus")
spec = importlib.util.spec_from_loader("hb", loader)
hb = importlib.util.module_from_spec(spec)
loader.exec_module(hb)
then = hb.now() - datetime.timedelta(minutes=int(sys.argv[4]))
hb.now = lambda: then
c = hb.db()
r = hb.get_message(c, sys.argv[2])
c.execute("BEGIN IMMEDIATE")
hb.tell(c, [r], sys.argv[3], "prompt", hb.Live(me=sys.argv[3], c=c))
hb.now = lambda: then + datetime.timedelta(minutes=int(sys.argv[4]))
hb.record_presence(c, sys.argv[3], r["to_project"], sys.argv[5], "claude")   # it runs: its hook was just seen
c.execute("COMMIT")
"""


class Surface(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for d in ("home", "config/projects", "data", "secrets/sessions", "bus", "state", "ws/app", "ws/box", "ws/cage"):
            (self.tmp / d).mkdir(parents=True)
        self.env = {**names.env_strip(os.environ), "HOME": str(self.tmp / "home"),
                    "AGENT_HUB_CONFIG": str(self.tmp / "config"), "AGENT_HUB_DATA": str(self.tmp / "data"),
                    "AGENT_HUB_SECRETS": str(self.tmp / "secrets"), "AGENT_HUB_STATE": str(self.tmp / "state"),
                    "AGENT_HUB_LANG_UI": "en"}

    def set_up(self):
        (self.tmp / "config" / "hub.json").write_text(json.dumps({"v": 1, "owner": {"tag": "Alex"},
                                                                  "bus_dir": str(self.tmp / "bus"),
                                                                  "registry_dir": "projects"}))
        for name in ("app", "box"):
            (self.tmp / "config" / "projects" / f"{name}.yaml").write_text(
                f"v: 1\nname: {name}\npaths:\n  workspace: {self.tmp / 'ws' / name}\n")
        (self.tmp / "config" / "projects" / "cage.yaml").write_text(
            f"v: 1\nname: cage\npaths:\n  workspace: {self.tmp / 'ws' / 'cage'}\nenforce:\n  sandbox: true\n")

    def record(self, session=SESSION, runtime="claude", project="app", version=None):
        rec = {"runtime": runtime, "pid": 4242, "start": "Mon Nov 16 09:00:00 2026", "cwd": str(self.tmp / "ws" / project),
               "project": project, **({"version": version} if version else {})}
        (self.tmp / "secrets" / "sessions" / f"{session}.live").write_text(json.dumps(rec))

    def run_(self, *args, env=None):
        return subprocess.run([sys.executable, *args], env=env or self.env, cwd=self.tmp, capture_output=True,
                              text=True, timeout=120)

    def surface(self, project="app", session=SESSION, runtime="claude", env=None):
        r = self.run_(str(ROOT / "bin" / "surface.py"), "--runtime", runtime, "--session", session,
                      "--cwd", str(self.tmp / "ws" / project), "--json", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(contracts.check_json(SCHEMA, out), [], out)
        return out

    def send(self, frm, to, kind, text):
        r = self.run_(str(ROOT / "bin" / "hubbus"), "send", "--from", frm, "--to", to, "--type", kind, "--text", text)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.strip().split()[-1]

    def letter_ids(self):
        c = sqlite3.connect(self.tmp / "bus" / "bus.db")
        try:
            return [r[0] for r in c.execute("SELECT id FROM events WHERE type='message' ORDER BY seq")]
        finally:
            c.close()

    def tell(self, mid, session=SESSION, minutes_ago=40):
        r = self.run_("-c", TELL, str(ROOT / "bin"), mid, session, str(minutes_ago), str(self.tmp / "ws"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def bus_state(self):
        c = sqlite3.connect(self.tmp / "bus" / "bus.db")
        try:
            return (c.execute("SELECT COUNT(*), MAX(seq) FROM events").fetchone(),
                    c.execute("SELECT * FROM presence").fetchall())
        finally:
            c.close()

    def tick(self, needs, alerts=()):
        """The status page's part of a tick, as write_glance leaves it."""
        f = self.tmp / "state" / "status" / "glance.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"v": 1, "as_of": "2026-11-18T09:00:00+08:00",
                                 "alerts": [{"level": lv, "text": t} for lv, t in alerts], "needs_you": needs}))
        (self.tmp / "state" / "scheduler-heartbeat").touch()   # a live scheduler: health is the alerts' alone

    # ---- the contract

    def test_the_example_is_valid(self):
        example = json.loads((ROOT / "contracts" / "fixtures" / "session-surface.v1.example.json").read_text())
        self.assertEqual(contracts.check_json(SCHEMA, example), [])
        self.assertTrue(contracts.check_json(SCHEMA, {**example, "why_not": "because"}))

    # ---- not served: why_not and nothing else

    def test_not_set_up_touches_nothing(self):
        self.assertEqual(self.surface(), {"v": 1, "set_up": False})
        self.assertEqual(list((self.tmp / "bus").iterdir()), [], "no bus made")

    def test_not_a_project(self):
        self.set_up()
        r = self.run_(str(ROOT / "bin" / "surface.py"), "--runtime", "claude", "--session", SESSION, "--cwd",
                      str(self.tmp / "home"), "--json")
        self.assertEqual(json.loads(r.stdout), {"v": 1, "set_up": True, "served": False, "why_not": "not_a_project"})

    def test_a_session_with_no_record_is_not_served(self):
        """A headless session, the platform's own run, or one whose hooks never ran: no record, nothing shown. The
        environment it runs in doesn't make it one (the platform's own-run variables count for nothing here)."""
        self.set_up()
        self.send("box", "app", "request", "please look")
        bare = {"v": 1, "set_up": True, "served": False, "why_not": "not_recorded"}
        self.assertEqual(self.surface(), bare)
        self.record(runtime="codex")
        self.assertEqual(self.surface(), bare, "a record of another runtime is not this session's")
        self.record()
        self.assertEqual(self.surface(runtime="no-such-runtime"), bare)
        self.assertTrue(self.surface(env={**self.env, "HUB_LLM_POINT": "x"})["served"], "decided by the record")

    def test_a_runtime_that_cant_be_confined_is_not_served_in_a_sandboxed_project(self):
        self.set_up()
        self.record(session=f"codex:{SESSION}", runtime="codex", project="cage")
        self.assertEqual(self.surface(project="cage", runtime="codex"),
                         {"v": 1, "set_up": True, "served": False, "why_not": "not_served"})

    @unittest.skipIf(os.geteuid() == 0, "root reads a folder whatever its mode")
    def test_a_bus_that_cant_be_read_is_said_not_taken_for_empty(self):
        self.set_up()
        self.record()
        self.send("box", "app", "request", "please look")
        bus = self.tmp / "bus"
        bus.chmod(0)
        self.addCleanup(bus.chmod, 0o700)
        self.assertEqual(self.surface(), {"v": 1, "set_up": True, "served": False, "why_not": "no_bus_access"})

    def test_no_bus_yet_is_no_letters(self):
        self.set_up()
        self.record()
        out = self.surface()
        self.assertTrue(out["served"])
        self.assertEqual(out["mail"], {"held": [], "waiting": 0})

    # ---- served

    def test_letters_held_and_waiting_and_nothing_written(self):
        self.set_up()
        self.record()
        self.send("box", "app", "request", "first")
        self.send("box", "app", "question", "second")
        first, second = self.letter_ids()
        out = self.surface()
        self.assertEqual((out["project"], out["mail"]["held"], out["mail"]["waiting"]), ("app", [], 2))
        self.assertEqual([c["name"] for c in out["commands"]], ["inbox", "brunna"])
        self.assertIn("brunna inbox", out["commands"][0]["prompt"])
        self.assertEqual(out["lines"], [], "nothing needs the owner: the band stays empty")
        self.tell(first)
        before = self.bus_state()   # the test's own read is before the snapshot and after the second one
        files = mine(tree(self.tmp, ROOT / ".logs", ROOT / ".state"))
        out = self.surface()
        after = mine(tree(self.tmp, ROOT / ".logs", ROOT / ".state"))
        self.assertEqual(self.bus_state(), before, "read-only: no event, no presence")
        # a reader of a WAL database (glance's too) may leave SQLite's companion files -wal and -shm beside it, which
        # the platform's own connections keep there anyway; the database and everything else stay as they were
        sqlite_own = (str(self.tmp / "bus"), "bus.db-wal", "bus.db-shm")
        self.assertEqual(sorted(k for k in {*files, *after} if files.get(k) != after.get(k)
                                and not (k == sqlite_own[0] or k.endswith(sqlite_own[1:]))), [],
                         "nothing written: the instance's state, data, secrets, bus and the hub's logs as they were")
        self.assertEqual([(h["id"], h["from"], h["type"]) for h in out["mail"]["held"]], [(first, "box", "request")])
        self.assertEqual(out["mail"]["waiting"], 1, "the second is still to be told")
        self.assertEqual(out["lines"], ["brunna · 1 letter held here for over 30 min, not handled yet"])
        self.record(session=OTHER)
        other = self.surface(session=OTHER)
        self.assertEqual((other["mail"]["held"], other["mail"]["waiting"]), ([], 1), "held by another session")

    def test_a_fresh_hold_is_not_in_the_band(self):
        self.set_up()
        self.record()
        self.send("box", "app", "request", "first")
        self.tell(self.letter_ids()[0], minutes_ago=5)
        out = self.surface()
        self.assertEqual(len(out["mail"]["held"]), 1)
        self.assertEqual(out["lines"], [])

    def test_the_owners_part_is_counts_and_states(self):
        self.set_up()
        self.record()
        self.tick([{"kind": "todo", "project": "box", "id": "task-box-1", "text": "Renew box's domain", "since": ""},
                   {"kind": "question", "project": "app", "id": "m1", "text": "Which host?", "since": ""}],
                  alerts=[("bad", "box/nightly failed")])
        out = self.surface()
        self.assertEqual(out["owner"], {"needs_you": 2, "side": "builtin", "health": "problem"})
        self.assertEqual(out["lines"], ["brunna · 2 need you", "the platform has problems"])

    def test_a_sandboxed_sessions_lines_hold_no_other_projects_text(self):
        self.set_up()
        self.record(project="cage")
        self.send("box", "cage", "request", "box asks cage something secret-ish")
        self.tell(self.letter_ids()[0])
        self.tick([{"kind": "todo", "project": "box", "id": "task-box-1", "text": "Renew box's domain", "since": ""}],
                  alerts=[("bad", "box/nightly failed")])
        out = self.surface(project="cage")
        self.assertTrue(out["served"])
        self.assertEqual(len(out["lines"]), 3, out["lines"])
        for line in out["lines"]:
            for word in ("box", "Renew", "nightly", "secret"):
                self.assertNotIn(word, line)

    def test_gated_keeps_the_owners_part_and_no_letters(self):
        """A sandboxed project's session on a version that failed a safety check: no letters are told to it, so it gets
        no mail and no /inbox, and its band says letters wait for another session."""
        self.set_up()
        self.record(project="cage", version="2.0.0")
        result = self.tmp / "state" / "runtime-conformance" / "claude" / "2.0.0.session.json"
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({"checks": {"sandbox-holds": {"state": "fail", "kind": "safety"}}}))
        self.send("box", "cage", "request", "from box")
        out = self.surface(project="cage")
        self.assertEqual((out["served"], out["why_not"], out["project"]), (False, "gated", "cage"))
        self.assertNotIn("mail", out)
        self.assertEqual([c["name"] for c in out["commands"]], ["brunna"])
        self.assertEqual(out["lines"], ["brunna · 1 letter waits for another session: this session's version failed the "
                                        "platform's sandbox check"])
        self.assertEqual(set(out), {"v", "set_up", "served", "why_not", "project", "owner", "labels", "lines",
                                    "commands"})
        self.record(project="app", version="2.0.0")
        self.assertTrue(self.surface()["served"], "the gate is for sandboxed projects only")

    def blanked(self) -> tuple[dict, dict]:
        """(bare, blank): the test instance named by the current spellings alone, and the same with every variable the
        Claude Code mod passes blank (its PLATFORM_VARS in every spelling, its OTHER_VARS) set to "", but for the
        current spellings that name the instance (they stand for the instance a real session's brunna finds itself)."""
        bare = {k: v for k, v in self.env.items() if not k.startswith(("AGENT_HUB_", "AGENTHUB_", "BRUNNA_"))}
        for k, v in self.env.items():
            if k.startswith("AGENT_HUB_"):
                bare["BRUNNA_" + k[len("AGENT_HUB_"):]] = v
        blank = {**bare, **{name: "" for name in mod_blank() if name not in bare}}
        return bare, blank

    def test_the_mods_blank_variables_are_as_absent(self):
        """The mod passes blank every variable the platform reads, and Python's and the loader's own: a project's
        settings `env` reaches its children (it can only set, not unset, theirs). Blank reads as unset everywhere."""
        self.set_up()
        self.record()
        self.send("box", "app", "request", "first")
        bare, blank = self.blanked()
        self.assertIn("AGENT_HUB_HOME", blank)
        self.assertIn("PYTHONPATH", blank)
        out = self.surface(env=blank)
        self.assertTrue(out["served"], out)
        self.assertEqual(out, self.surface(env=bare))

    def test_the_mod_blanks_every_variable_the_platform_reads(self):
        names_read = set()
        for f in [*(ROOT / "bin").glob("*.py"), *(ROOT / "bin").glob("hub*"), ROOT / "bin" / "agenthub"]:
            if f.is_file():
                names_read |= set(re.findall(r'names\.env\("([A-Z_]+)"', f.read_text(errors="replace")))
        import event_jobs  # its variables are named by constants
        names_read |= {event_jobs.CHAIN_VAR, event_jobs.DEPTH_VAR, event_jobs.JOB_VAR, event_jobs.EVENTS_VAR}
        blank = mod_blank()
        for x in sorted(names_read):
            for spelling in names.env_names(x):
                self.assertIn(spelling, blank, f"the mod doesn't blank {spelling}")

    def test_a_sitecustomize_on_pythonpath_doesnt_run(self):
        """Through the whole chain the mod starts (the brunna command, bin/py, the CLI, surface.py): PYTHONPATH as a
        project's settings would set it runs a sitecustomize; blank, as the mod passes it, nothing runs."""
        self.set_up()
        self.record()
        pp = self.tmp / "pp"
        pp.mkdir()
        marker = self.tmp / "sitecustomize-ran"
        (pp / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'a').write('x')\n")
        py = self.tmp / "home" / ".local" / "share" / names.current("share_dir") / "python" / "bin" / "python3"
        py.parent.mkdir(parents=True)
        py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')   # the platform's Python, where bin/py looks
        py.chmod(0o755)
        bare, blank = self.blanked()

        def run(env):
            r = subprocess.run([str(ROOT / "bin" / "brunna"), "surface", "--json", "--runtime", "claude", "--session",
                                SESSION, "--cwd", str(self.tmp / "ws" / "app")], env=env, cwd=self.tmp / "home",
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return json.loads(r.stdout)

        run({**bare, "PYTHONPATH": str(pp)})
        self.assertTrue(marker.exists(), "the probe works: a PYTHONPATH sitecustomize runs")
        marker.unlink()
        out = run({**blank, "PYTHONPATH": ""})
        self.assertFalse(marker.exists(), "blank, it doesn't")
        self.assertTrue(out["served"])

    def test_a_wrong_language_variable_gives_way_to_the_settings(self):
        self.set_up()
        self.record()
        self.tick([{"kind": "todo", "project": "box", "id": "task-box-1", "text": "x", "since": ""}])
        out = self.surface(env={**self.env, "AGENT_HUB_LANG_UI": "klingon", "AGENT_HUB_LANG_CONTENT": "xx"})
        self.assertEqual(out["lines"], ["brunna · 1 needs you"])

    def test_lines_in_the_interface_language(self):
        """The words come from the catalog of the instance's interface language (their goldens:
        tests/test_i18n_golden_surface.py)."""
        import i18n
        self.set_up()
        self.record()
        self.tick([{"kind": "todo", "project": "box", "id": "task-box-1", "text": "x", "since": ""}])
        out = self.surface(env={**self.env, "AGENT_HUB_LANG_UI": "zh-Hant"})
        need = i18n.translate("surface", "{n} {n:needs|need} you", {"n": 1}, "zh-Hant")
        self.assertNotEqual(need, "1 needs you")
        self.assertEqual(out["lines"], [i18n.translate("surface", "brunna · {line}", {"line": need}, "zh-Hant")])
        self.assertEqual(out["labels"]["open"], i18n.translate("surface", "needs you", {}, "zh-Hant"))

    def test_text_form_and_usage(self):
        self.set_up()
        r = self.run_(str(ROOT / "bin" / "surface.py"), "--runtime", "claude")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: brunna surface", r.stderr)


if __name__ == "__main__":
    unittest.main()
