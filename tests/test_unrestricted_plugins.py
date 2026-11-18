"""A platform run without --restricted never starts where a skills folder holds a plugin (the owner's decision of 11-18,
docs/design/agent-surface.md "Platform runs").

Measured 11-18 on 2.1.327: such a run (a lease's shape) loads a trusted project's `.claude/skills/<x>` plugin, whose
hooks run as the user outside the project's sandbox; no switch turns plugins off and keeps the rules file. So the Claude
adapter looks through the skills folders of the run's folder (and its repository's top, and a linked worktree's main
checkout) for a plugin's manifest or hooks list at any depth, in any case, a link of the name counted, before starting
one, and refuses on a find or on a folder it can't look through (fail closed). A --restricted run isn't looked at: it
loads no plugin (conformance_claude's platform-run-plugins holds that per version).

The tool is a stand-in from a temp store that notes it was started: a refused run never starts it.

    bin/run-tests        (running this module directly in a worktree reads the real config through .hub-home)
"""
import http.server
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import runtime_claude  # noqa: E402
import runtime_ports  # noqa: E402
import store_fixture  # noqa: E402

_LANG = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": "en", "AGENT_HUB_LANG_CONTENT": "en"})   # the assertions read the English texts

# the agent points whose run carries no --restricted (runtime_claude._flags: neither settings: platform nor restricted:
# true). A new one fails here until it is listed, and so looked at by the refusal below
AFFECTED = {"librarian-index", "agent-job", "login-check", "lease-session", "lease-work", "lease-handover"}
SID = "bbbbbbbb-0000-4000-8000-000000000001"


def setUpModule():
    _LANG.start()


def tearDownModule():
    _LANG.stop()


def points() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "contracts" / "llm-points.yaml").read_text())["points"]


def env_for(home) -> dict:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "MAILBOX_PROJECT": "x", "AGENT_JOB_PROJECT": "x",
            "AGENT_JOB_ALLOW": "[]", "LEASE_MODE": "discuss", "LEASE_WS": str(home), "LEASE_PROJECT": "x"}


def plugin(skills: pathlib.Path, how: str) -> str:
    """Plants one plugin marker of the given form under a skills folder; the name the refusal must give."""
    x = skills / "x"
    x.mkdir(parents=True, exist_ok=True)
    if how == "manifest":
        (x / ".claude-plugin").mkdir()
        (x / ".claude-plugin" / "plugin.json").write_text('{"name": "x"}')
        return ".claude-plugin"
    if how == "hooks":
        (x / "hooks.json").write_text('{"hooks": {}}')
        return "hooks.json"
    if how == "nested":
        (x / "deep" / "er").mkdir(parents=True)
        (x / "deep" / "er" / "HOOKS.JSON").write_text("{}")
        return "HOOKS.JSON"
    if how == "link":
        target = skills.parent.parent.parent / "elsewhere-plugin"
        target.mkdir(exist_ok=True)
        (x / ".claude-plugin").symlink_to(target, target_is_directory=True)
        return ".claude-plugin"
    raise ValueError(how)


class Affected(unittest.TestCase):
    def test_the_points_without_restricted_are_these(self):
        tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        lacking = set()
        for point, spec in points().items():
            if not isinstance(spec, dict) or spec.get("shape") != "agent":
                continue
            if spec.get("settings") == "platform":
                # _flags always adds --restricted there (it needs the project's registry entry to build the rest)
                continue
            task = runtime_ports.Task(point=point, prompt="p", spec=spec, cwd=str(tmp), env=env_for(tmp))
            if "--restricted" not in runtime_claude._command(task, "claude"):
                lacking.add(point)
        self.assertEqual(lacking, AFFECTED)


class Refused(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ran = self.tmp / "ran"
        store_fixture.stand_in(self, body=f"#!{sys.executable}\nimport json, sys\nopen({str(self.ran)!r}, 'a').write('x')\n"
                               "print(json.dumps({'type': 'result', 'result': 'ok', 'is_error': False, 'session_id': 's', "
                               "'num_turns': 1}))\n")
        self.project = self.tmp / "project"
        (self.project / ".git").mkdir(parents=True)   # its own repository's top: nothing above it is looked at

    def task(self, point, cwd=None):
        return runtime_ports.Task(point=point, prompt="p", spec=points()[point], cwd=str(cwd or self.project),
                                  env=env_for(self.tmp))

    def test_each_affected_point_is_refused_before_the_tool_starts(self):
        for how in ("manifest", "hooks", "nested", "link"):
            skills = self.project / ".claude" / "skills"
            marker = plugin(skills, how)
            for point in sorted(AFFECTED):
                with self.subTest(how=how, point=point):
                    self.ran.unlink(missing_ok=True)
                    if points()[point].get("stream"):
                        with mock.patch.object(runtime_claude.os, "execvpe") as ex:
                            with self.assertRaises(ValueError) as cm:
                                runtime_claude.exec_stream(self.task(point))
                        ex.assert_not_called()
                        why = str(cm.exception)
                    else:
                        res = runtime_claude.run(self.task(point))
                        self.assertFalse(res.ok)
                        why = res.error
                    self.assertFalse(self.ran.exists(), "the tool never started")
                    self.assertIn("not started", why)
                    self.assertIn(marker, why)
                    self.assertIn(str(skills / "x"), why)
            shutil.rmtree(self.project / ".claude")

    def test_the_same_runs_start_in_a_clean_folder(self):
        (self.project / ".claude" / "skills" / "plain").mkdir(parents=True)
        (self.project / ".claude" / "skills" / "plain" / "SKILL.md").write_text("---\nname: plain\n---\n")
        for point in sorted(AFFECTED):
            with self.subTest(point=point):
                self.ran.unlink(missing_ok=True)
                if points()[point].get("stream"):
                    with mock.patch.object(runtime_claude.os, "execvpe") as ex:
                        runtime_claude.exec_stream(self.task(point))
                    ex.assert_called_once()
                else:
                    res = runtime_claude.run(self.task(point))
                    self.assertTrue(res.ok, res.error)
                    self.assertTrue(self.ran.exists())

    def test_a_folder_that_cant_be_looked_through_is_refused(self):
        outside = self.tmp / "outside-skills"
        outside.mkdir()
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "skills").symlink_to(outside, target_is_directory=True)
        res = runtime_claude.run(self.task("agent-job"))
        self.assertFalse(res.ok)
        self.assertIn("can't be looked through", res.error)
        self.assertIn("link to a folder", res.error)
        self.assertFalse(self.ran.exists())
        if os.geteuid() == 0:
            return   # root reads a folder whatever its mode
        (self.project / ".claude" / "skills").unlink()
        locked = self.project / ".claude" / "skills"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        res = runtime_claude.run(self.task("agent-job"))
        self.assertFalse(res.ok)
        self.assertIn("can't be looked through", res.error)
        self.assertFalse(self.ran.exists())

    def test_a_run_in_a_subfolder_looks_at_the_repository_top(self):
        plugin(self.project / ".claude" / "skills", "hooks")
        sub = self.project / "sub"
        sub.mkdir()
        res = runtime_claude.run(self.task("agent-job", cwd=sub))
        self.assertFalse(res.ok)
        self.assertIn("hooks.json", res.error)
        self.assertFalse(self.ran.exists())

    def test_a_linked_worktree_looks_at_the_main_checkout(self):
        # a worktree as git lays one out: its .git file names <main>/.git/worktrees/w, whose commondir leads back
        admin = self.project / ".git" / "worktrees" / "w"
        admin.mkdir(parents=True)
        wt = self.project / ".claude" / "worktrees" / "w"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        (admin / "commondir").write_text("../..\n")
        (admin / "gitdir").write_text(f"{wt / '.git'}\n")
        self.assertEqual({os.path.realpath(p) for p in runtime_claude.plugin_folders(wt)},
                         {os.path.realpath(wt), os.path.realpath(self.project)})
        plugin(self.project / ".claude" / "skills", "manifest")
        res = runtime_claude.run(self.task("lease-handover", cwd=wt))
        self.assertFalse(res.ok)
        self.assertIn(".claude-plugin", res.error)
        self.assertFalse(self.ran.exists())

    def test_a_restricted_run_is_not_looked_at(self):
        # mailbox-propose says restricted: true: its argv has --restricted, which loads no plugin, so it starts
        plugin(self.project / ".claude" / "skills", "manifest")
        task = self.task("mailbox-propose")
        self.assertIn("--restricted", runtime_claude._command(task, "claude"))
        res = runtime_claude.run(task)
        self.assertTrue(res.ok, res.error)
        self.assertTrue(self.ran.exists())

    def test_the_tool_server_listing_is_refused_too(self):
        # an agent-job with connectors lists the folder's tool servers with `claude mcp list` (no --restricted, and it
        # starts a plugin's servers) before its run: refused the same way, the tool never started
        plugin(self.project / ".claude" / "skills", "hooks")
        spec = {k: v for k, v in points()["agent-job"].items() if k != "extra_args"}
        task = runtime_ports.Task(point="agent-job", prompt="p", spec=spec, cwd=str(self.project), env=env_for(self.tmp))
        with self.assertRaises(ValueError) as cm:
            runtime_claude.ambient_servers(task)
        self.assertIn("hooks.json", str(cm.exception))
        self.assertFalse(self.ran.exists())


class Lease(unittest.TestCase):
    """The lease runner asks before it opens, resumes or switches a lease, and says why (409 folder_refused)."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("lease_runner_refused", ROOT / "bin" / "lease-runner.py")
        self.m = m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        m.DIR = self.tmp / "state"
        m.token = lambda: "t"
        self.ws = self.tmp / "proj"
        (self.ws / ".git").mkdir(parents=True)
        self.started = []
        for target, attr, value in [
                (m.hubconf, "workspace", lambda name: self.ws if name == "proj" else None),
                (m.hubconf, "projects", lambda: {"proj": {}}),
                (m, "start_proc", self.fake_start),
                (m, "stop_proc", lambda lease, st: setattr(lease, "state", st))]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        reg = types.ModuleType("registry")
        reg.refresh = lambda: {"projects": {}}
        p = mock.patch.dict(sys.modules, {"registry": reg})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(m.LEASES.clear)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), m.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.server_close)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def fake_start(self, lease, resume):
        self.m.refuse_folder(lease.cwd)   # what the real start_proc asks first
        self.started.append(lease.id)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer t"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_opening_a_lease_says_why_it_was_refused(self):
        plugin(self.ws / ".claude" / "skills", "manifest")
        code, body = self.post("/v1/leases", {"v": 2, "project": "proj", "session_id": SID, "thread": "t"})
        self.assertEqual((code, body["error"]), (409, "folder_refused"))
        self.assertIn(".claude-plugin", body["detail"])
        self.assertIn(str(self.ws / ".claude" / "skills" / "x"), body["detail"])
        self.assertEqual((self.started, dict(self.m.LEASES)), ([], {}), "nothing started, no lease recorded")

    def test_a_clean_project_opens(self):
        code, body = self.post("/v1/leases", {"v": 2, "project": "proj", "session_id": SID, "thread": "t"})
        self.assertEqual(code, 201, body)
        self.assertEqual(self.started, [SID])

    def test_a_plugin_that_appears_mid_lease_stops_the_next_start(self):
        code, _ = self.post("/v1/leases", {"v": 2, "project": "proj", "session_id": SID, "thread": "t"})
        self.assertEqual(code, 201)
        lease = self.m.LEASES[SID]
        lease.proc, lease.state = None, "idle_stopped"
        plugin(self.ws / ".claude" / "skills", "hooks")
        code, body = self.post(f"/v1/leases/{SID}/messages", {"v": 2, "client_msg_id": "m1", "text": "hi"})
        self.assertEqual((code, body["error"]), (409, "folder_refused"))
        self.assertEqual(lease.state, "idle_stopped", "the lease stays as it was")
        self.assertNotIn("m1", lease.seen_msgs, "the same message can be sent again once the folder is fixed")
        with mock.patch.object(self.m, "work_ready", lambda project: (True, "")):
            code, body = self.post(f"/v1/leases/{SID}/mode", {"v": 2, "mode": "work"})
        self.assertEqual((code, body["error"]), (409, "folder_refused"))
        self.assertEqual((lease.mode, lease.state), ("discuss", "idle_stopped"), "nothing was stopped or switched")
        self.assertEqual(self.started, [SID])


if __name__ == "__main__":
    unittest.main()
