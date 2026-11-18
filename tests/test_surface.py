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
        before = self.bus_state()
        out = self.surface()
        self.assertEqual(self.bus_state(), before, "read-only: no event, no presence")
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

    def test_blank_root_and_python_variables_are_as_absent(self):
        """The mod passes the platform's root and Python variables blank (it can only set, not unset, a child's)."""
        self.set_up()
        self.record()
        self.send("box", "app", "request", "first")
        bare = {k: v for k, v in self.env.items() if not k.endswith(("_ROOT", "_PYTHON"))}
        blank = {**bare, **{f"{p}{x}": "" for p in ("AGENT_HUB_", "BRUNNA_") for x in ("ROOT", "PYTHON")}}
        self.assertEqual(self.surface(env=blank), self.surface(env=bare))

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
