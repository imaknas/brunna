"""agenthub connect (docs/design/newcomer-path.md B6): what a folder holds, what is refused, and what would be run.
Nothing is onboarded: --dry-run, or onboarding replaced.

    bin/py -m unittest tests.test_connect
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_LANG = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": "en", "AGENT_HUB_LANG_CONTENT": "en"})


def setUpModule():
    _LANG.start()


def tearDownModule():
    _LANG.stop()


ROOT = pathlib.Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("connect_t", str(ROOT / "bin" / "connect.py"))
spec = importlib.util.spec_from_loader("connect_t", loader)
connect = importlib.util.module_from_spec(spec)
loader.exec_module(connect)


def repo(p: pathlib.Path, files=None) -> pathlib.Path:
    p.mkdir(parents=True, exist_ok=True)
    for name, text in (files or {"README": "x\n"}).items():
        (p / name).parent.mkdir(parents=True, exist_ok=True)
        (p / name).write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(p), *args], check=True, capture_output=True, env=env)
    return p


class Connect(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.code = self.home / "code"
        self.code.mkdir(parents=True)
        self.projects = {}
        patches = [mock.patch.object(connect.hubconf, "projects", lambda: self.projects),
                   mock.patch.object(connect.hubconf, "MAIN", self.tmp / "hub"),
                   mock.patch.object(connect.hubconf, "instance_home", lambda: self.tmp / "instance"),
                   mock.patch.object(connect.pathlib.Path, "home", classmethod(lambda cls: self.home)),
                   # from this fixture down: the test runner's temp folder is under the instance's, which is itself hidden
                   mock.patch.object(connect, "hidden", lambda p: any(x.startswith(".") for x in p.relative_to(self.tmp).parts))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_connect(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = connect.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_layouts(self):
        plain = self.code / "notes"
        plain.mkdir()
        (plain / "a.md").write_text("x")
        self.assertEqual(connect.layout(plain), ("plain", [plain]))
        one = repo(self.code / "app")
        self.assertEqual(connect.layout(one), ("repo", [one]))
        mono = repo(self.code / "mono", {"pnpm-workspace.yaml": "packages: []\n"})
        self.assertEqual(connect.layout(mono)[0], "monorepo")
        parts = repo(self.code / "parts", {"packages/a/package.json": "{}", "packages/b/package.json": "{}"})
        self.assertEqual(connect.layout(parts)[0], "monorepo")
        group = self.code / "group"
        a, b = repo(group / "a"), repo(group / "b")
        self.assertEqual(connect.layout(group), ("repos", [a, b]))

    def test_refusals(self):
        (self.code / ".hidden").mkdir()
        repo(self.code / "inside" / "sub")
        self.projects = {"inside": {"paths": {"workspace": str(self.code / "inside")}}}
        for folder, why in ((self.home, "home"), (self.home.parent, "home"), (self.code / ".hidden", "hidden"),
                            (self.code / "inside" / "sub", "inside")):
            with self.subTest(folder=folder):
                self.assertIsNotNone(connect.refusal(folder), why)
        self.assertIsNone(connect.refusal(self.code))

    def test_a_symlink_is_followed_to_its_real_place(self):
        (self.home / ".dotfiles").mkdir()
        link = self.code / "x"
        link.symlink_to(self.home / ".dotfiles")
        rc, _out, err = self.run_connect(str(link), "--yes", "--dry-run")
        self.assertEqual(rc, 2)
        self.assertIn(".dotfiles", err, "resolved, then refused as hidden")

    def test_a_folder_of_repos_dry_run(self):
        group = self.code / "group"
        repo(group / "a"), repo(group / "b")
        rc, out, _err = self.run_connect(str(group), "--yes", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("onboard-project.sh --no-remote"), 2)
        self.assertNotIn("layout_intent", out, "--yes doesn't say they belong together")

    def test_monorepo_split_intent_is_recorded(self):
        mono = repo(self.code / "mono", {"go.work": "go 1.22\n"})
        with mock.patch.object(connect, "ask", lambda q, yes, default: True):
            rc, out, _err = self.run_connect(str(mono), "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn('layout_intent={"split": true}', out)

    def test_unsaved_changes_are_skipped(self):
        app = repo(self.code / "app")
        (app / "README").write_text("changed\n")
        with mock.patch.object(connect.subprocess, "run", wraps=connect.subprocess.run) as run:
            rc, out, _err = self.run_connect(str(app), "--yes")
        self.assertEqual(rc, 1)
        self.assertIn(str(app), out)
        self.assertFalse(any("onboard-project.sh" in " ".join(map(str, c.args[0])) for c in run.call_args_list))

    def test_again_is_a_no_op(self):
        group = self.code / "group"
        a, b = repo(group / "a"), repo(group / "b")
        self.projects = {n: {"paths": {"workspace": str(p)}} for n, p in (("a", a), ("b", b))}
        with mock.patch.object(connect.subprocess, "run", side_effect=AssertionError("nothing to run")):
            for argv in ((str(a), "--yes"), (str(group), "--yes"), (str(group),)):
                with self.subTest(argv=argv):
                    rc, out, err = self.run_connect(*argv)
                    self.assertEqual((rc, err), (0, ""))
                    self.assertIn("already connected", out)
                    self.assertNotIn("Start a new session", out, "nothing was connected")
                    self.assertNotIn("Each becomes a project", out)

    def test_without_a_terminal_only_yes_connects(self):
        app = repo(self.code / "app")
        with mock.patch.object(connect.sys, "stdin", io.StringIO()), \
                mock.patch.object(connect, "connect_one", side_effect=AssertionError("connected unasked")):
            rc, out, err = self.run_connect(str(app))
        self.assertEqual(rc, 2)
        self.assertIn("--yes", err)
        self.assertIn(str(app), err)

    def test_remote_without_an_owner_is_said(self):
        app = repo(self.code / "app")
        with mock.patch.object(connect.hubconf, "get", lambda k, d=None: d):
            rc, out, _err = self.run_connect(str(app), "--yes", "--remote", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("no GitHub repo is created", out)
        subprocess.run(["git", "-C", str(app), "remote", "add", "origin", "https://example.invalid/x/app.git"], check=True)
        with mock.patch.object(connect.hubconf, "get", lambda k, d=None: d):
            rc, out, _err = self.run_connect(str(app), "--yes", "--remote", "--dry-run")
        self.assertNotIn("GitHub repo", out, "it has a remote of its own: nothing stays on this machine")

    def test_a_repo_added_beside_connected_ones_can_belong_with_them(self):
        group = self.code / "group"
        a, _b = repo(group / "a"), repo(group / "b")
        self.projects = {"a": {"paths": {"workspace": str(a)}}}
        with mock.patch.object(connect, "ask", lambda q, yes, default: True):
            rc, out, _err = self.run_connect(str(group), "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn('layout_intent={"span": ["a", "b"]}', out)

    def test_folders_left_out_are_named(self):
        group = self.code / "group"
        repo(group / "a")
        (group / "notes").mkdir()
        (group / ".cache").mkdir()
        rc, out, _err = self.run_connect(str(group), "--yes", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("Left out", out)
        self.assertIn("notes", out)
        self.assertNotIn(".cache", out)

    def test_the_steps_go_to_a_log_and_what_comes_next_fits_the_agents(self):
        app = repo(self.code / "app")
        calls = []

        def run(argv, **kw):
            calls.append([str(x) for x in argv])
            return subprocess.CompletedProcess(argv, 0, "", "")
        for installed, caps, said, unsaid in ((["claude"], {"lifecycle"}, "Start a new session", "No AI agent"),
                                              (["none"], set(), "No AI agent is set up", "Start a new session")):
            with self.subTest(installed=installed), mock.patch.object(connect.subprocess, "run", run), \
                    mock.patch.object(connect, "sgit", lambda *a: subprocess.CompletedProcess(a, 0, "", "")), \
                    mock.patch.object(connect.hubconf, "LOGS", self.tmp / "logs"), \
                    mock.patch("runtime_ports.installed", lambda: installed), \
                    mock.patch("runtime_ports.capabilities", lambda n=None: caps), \
                    mock.patch("llm_ready.unsupported", lambda p: "no model"):
                rc, out, _err = self.run_connect(str(app), "--yes")
            self.assertEqual(rc, 0)
            self.assertIn(said, out)
            self.assertNotIn(unsaid, out)
            onboard = next(c for c in calls if "onboard-project.sh" in c[0])
            self.assertIn("connect-", onboard[onboard.index("--log") + 1])
            calls.clear()

    def test_in_roots(self):
        """B5: what an agent may ask to connect: an existing folder strictly inside a root, named without symlinks."""
        inside = self.code / "app"
        inside.mkdir()
        (self.home / "private").mkdir()
        (self.code / "link").symlink_to(self.home / "private")
        roots = [self.code]
        self.assertIsNone(connect.in_roots(inside, roots))
        for given in (self.code, self.home / "private", self.code / "link", self.code / "missing",
                      pathlib.Path("code/app")):
            with self.subTest(given=given):
                self.assertIsNotNone(connect.in_roots(given, roots))
        self.assertIsNotNone(connect.in_roots(inside, []), "no roots: nothing is inside")

    def test_add_root(self):
        """A folder of projects: not home or above it, not hidden, not the hub or inside it, not inside a project; it
        may hold the hub. Recorded in hub.json connect.roots."""
        (self.tmp / "instance" / "config").mkdir(parents=True)
        hub_json = self.tmp / "instance" / "config" / "hub.json"
        hub_json.write_text("{}")
        (self.home / ".ssh").mkdir()
        (self.tmp / "hub" / "bin").mkdir(parents=True)
        repo(self.code / "inside")
        self.projects = {"inside": {"paths": {"workspace": str(self.code / "inside")}}}
        with mock.patch.object(connect.hubconf, "config_path", lambda: self.tmp / "instance" / "config" / "hub.json"), \
             mock.patch.object(connect.subprocess, "run") as run:
            for folder in (self.home, self.home / ".ssh", self.tmp / "hub", self.tmp / "hub" / "bin",
                           self.code / "inside", self.code / "missing"):
                with self.subTest(folder=folder):
                    with redirect_stderr(io.StringIO()):
                        self.assertEqual(connect.add_root(str(folder)), 2)
            self.assertEqual(json.loads(hub_json.read_text()), {}, "nothing recorded")
            run.assert_not_called()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(connect.add_root(str(self.code)), 0)
                (self.home / "src" / "agent-hub").mkdir(parents=True)
                with mock.patch.object(connect.hubconf, "MAIN", self.home / "src" / "agent-hub"):
                    self.assertEqual(connect.add_root(str(self.home / "src")), 0, "may hold the hub")
        self.assertEqual(json.loads(hub_json.read_text())["connect"]["roots"], [str(self.code), str(self.home / "src")])

    def test_untrusted_git_runs_without_hooks(self):
        app = repo(self.code / "app")
        hook = app / ".git" / "hooks" / "fsmonitor"
        hook.write_text("#!/bin/sh\ntouch " + str(self.tmp / "ran") + "\n")
        hook.chmod(0o755)
        subprocess.run(["git", "-C", str(app), "config", "core.fsmonitor", str(hook)], check=True)
        connect.sgit(app, "status", "--porcelain")
        self.assertFalse((self.tmp / "ran").exists(), "the folder's fsmonitor did not run")


if __name__ == "__main__":
    unittest.main()
