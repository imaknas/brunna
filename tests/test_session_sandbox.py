"""A sandboxed project's sessions and the settings files in its folder that aren't the platform's: what the platform's own
value stands in the place of, what its hooks take of a session's environment, what the owner is told, what is never
carried over, the repos in the project's folder, and a settings file that changes under a live session.

    bin/py -m unittest tests.test_session_sandbox
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import alert  # noqa: E402
import gitdirs  # noqa: E402
import hubconf  # noqa: E402
import isolation  # noqa: E402
import names  # noqa: E402
import policy  # noqa: E402
import project_git  # noqa: E402
import runtime_claude as rc  # noqa: E402
import session_worktree  # noqa: E402

GIT = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "protocol.file.allow=always", *args], env=GIT, capture_output=True, text=True, check=True).stdout


def load(name: str, file: str):
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / file))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(m)
    return m


def shell(command: str, env=None, stdin="", cwd=None):
    return subprocess.run(["/bin/sh", "-c", command], env=env, input=stdin, cwd=cwd, capture_output=True, text=True, timeout=120)


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.dict(os.environ)   # what is told is looked for in English, whatever language the run has
        p.start()
        self.addCleanup(p.stop)
        names.env_unset(os.environ, "LANG_UI")
        names.env_set(os.environ, "LANG_UI", "en")

    def patch(self, *args, **kw):
        p = mock.patch.object(*args, **kw)
        p.start()
        self.addCleanup(p.stop)


class Hooks(Temp):
    """The platform's hooks run as the user with the session's environment, which the project's settings files may set."""

    def setUp(self):
        super().setUp()
        self.hub = self.tmp / "a hub"
        (self.hub / "bin").mkdir(parents=True)
        self.event = self.hub / "bin" / "hub-event"
        self.event.write_text("import json, os, sys\nprint(json.dumps([sys.argv[1:], dict(os.environ), sys.path, sys.stdin.read()]))\n")
        for n in ("ROOT", "MAIN"):
            self.patch(hubconf, n, self.hub)

    def commands(self) -> list[str]:
        return [h["command"] for es in rc.platform_hooks().values() for e in es for h in e["hooks"]]

    def test_nothing_comes_before_the_clean_environment_nor_after_the_script(self):
        got = self.commands()
        self.assertGreater(len(got), 9)
        for c in got:
            self.assertTrue(c.startswith("exec /usr/bin/env -i "), c)
            for word in ("||", "&&", ";;", "~/"):
                self.assertNotIn(word, c.replace("sys;del", ""))
        unsandboxed = [h["command"] for es in rc.install_hooks({})["hooks"].values() for e in es for h in e["hooks"]]
        self.assertEqual([c for c in unsandboxed if not c.startswith("exec /usr/bin/env -i ")], [], "every project's, not only a sandboxed one's")

    def test_the_sessions_environment_runs_nothing_in_them(self):
        evil, mark = self.tmp / "evil", self.tmp / "ran"
        (evil / "bin").mkdir(parents=True)
        for f in ("py", "bin/python3", "bin/env", "bin/hub-event", "prefix"):
            (evil / f).write_text(f"#!/bin/sh\n/usr/bin/touch '{mark}'\n")
            (evil / f).chmod(0o755)
        (evil / "json.py").write_text(f"open({str(mark)!r}, 'w')\n")
        (evil / "be").write_text(f"/usr/bin/touch '{mark}'\n")
        env = {"HOME": str(evil), "PATH": f"{evil / 'bin'}:/usr/bin:/bin", "AGENT_HUB_PYTHON": str(evil / "py"), "BRUNNA_PYTHON": str(evil / "py"),
               "BRUNNA_ROOT": str(evil), "AGENT_HUB_ROOT": str(evil), "PYTHONPATH": str(evil), "PYTHONHOME": str(evil), "PYTHONSTARTUP": str(evil / "json.py"),
               "BASH_ENV": str(evil / "be"), "ENV": str(evil / "be"), "BASH_FUNC_[%%": f"() {{ /usr/bin/touch '{mark}'; }}",
               "BASH_FUNC_true%%": f"() {{ /usr/bin/touch '{mark}'; }}", "HUB_AS_PROJECT": "1", "GIT_SSH_COMMAND": str(evil / "prefix"),
               "GIT_CONFIG_GLOBAL": str(evil / "x"), "BRUNNA_STATE": str(evil), "XDG_CONFIG_HOME": str(evil), "NODE_OPTIONS": "--x"}
        r = shell(rc.hook_command("prompt"), env=env, cwd=evil)
        self.assertFalse(mark.exists())
        argv, seen, path, _stdin = json.loads(r.stdout)
        self.assertEqual(argv, ["--runtime", "claude", "prompt"])
        self.assertEqual([k for k, v in seen.items() if str(evil) in v or k in ("HUB_AS_PROJECT", "GIT_SSH_COMMAND", "NODE_OPTIONS")], [])
        self.assertEqual(seen["HOME"], str(pathlib.Path.home()))
        self.assertNotIn(str(evil), path)

    def test_what_a_hook_needs_of_its_session_reaches_it(self):
        given = {"CLAUDE_PID": "123", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CONFIG_DIR": "/a b/$(x)`y`\"c", "SSH_AUTH_SOCK": "/s", "TMPDIR": "/t",
                 "HTTPS_PROXY": "http://p", "no_proxy": "x", "CLAUDE_CODE_ENTRYPOINT": "cli", "HUB_LLM_POINT": "p", "HUBBUS_LEASE": "1",
                 "BRUNNA_JOB_CHAIN": '["a"]', "AGENT_HUB_JOB_DEPTH": "2", "BRUNNA_CONFINED": "1"}
        for sh in ("/bin/sh", "/bin/bash", "/bin/zsh", "/bin/dash"):
            if not os.access(sh, os.X_OK):
                continue
            r = subprocess.run([sh, "-c", rc.hook_command("prompt")], env={**given, "PATH": "/usr/bin:/bin"}, input='{"session_id": 1}', capture_output=True,
                               text=True, timeout=120)
            _argv, seen, _path, stdin = json.loads(r.stdout)
            self.assertEqual({k: seen.get(k) for k in given}, given, sh)
            self.assertEqual(stdin, '{"session_id": 1}')
            self.assertNotIn("HTTP_PROXY", seen, "one that isn't set isn't set empty")

    def test_the_platform_gone_or_broken_stops_no_session(self):
        self.event.write_text("raise RuntimeError('x')\n")
        self.assertEqual(shell(rc.hook_command("prompt")).returncode, 0)
        self.event.write_text("def (\n")
        self.assertEqual(shell(rc.hook_command("prompt")).returncode, 0)
        self.event.write_text("import sys\nprint('woken', file=sys.stderr)\nsys.exit(2)\n")
        r = shell(rc.wait_command())
        self.assertEqual((r.returncode, r.stderr.strip()), (2, "woken"), "the waiter's wake")
        change = rc.platform_hooks()["ConfigChange"][0]["hooks"][0]["command"]
        self.assertEqual(shell(change).returncode, 2)
        self.event.write_text("raise RuntimeError('x')\n")
        self.assertEqual(shell(change).returncode, 2, "can't tell: the session keeps what it has")
        self.assertNotEqual(shell(rc.worktree_command("worktree-make")).returncode, 0, "never made as the user")
        self.event.write_text("print('ran')\n")
        with mock.patch.object(hubconf, "python_path", lambda: str(self.tmp / "no python")):
            for c in (*self.commands(), rc.worktree_command("worktree-make")):
                r = shell(c)
                self.assertNotIn(r.returncode, (0, 2), "said, and taken for no refusal")
                self.assertEqual(r.stdout, "", "no other interpreter in its place")
        self.event.unlink()
        for c in (rc.hook_command("prompt"), rc.wait_command(), change):
            r = shell(c)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), "a missing script isn't a refusal (Python's own exit for one is 2)")
        self.assertEqual(shell(rc.worktree_command("worktree-make")).returncode, 1)

    def test_they_are_the_same_whoever_writes_them(self):
        with mock.patch.dict(os.environ, {"LANG": "C", "USER": "someone", "PATH": "/x", "NVM_DIR": str(pathlib.Path.home() / ".nvm"), "SSH_AUTH_SOCK": "/a"}):
            one = self.commands()
        with mock.patch.dict(os.environ, {"LANG": "de_DE.UTF-8", "PATH": "/y"}):
            os.environ.pop("NVM_DIR", None)
            os.environ.pop("SSH_AUTH_SOCK", None)
            self.assertEqual(self.commands(), one)

    def test_where_this_platform_is_goes_with_them(self):
        with mock.patch.dict(os.environ, {"BRUNNA_STATE": "/my state"}):
            self.assertIn("BRUNNA_STATE=/my state", shlex.split(rc.hook_command("prompt")))

    def test_an_earlier_releases_are_known_for_the_platforms(self):
        old = {"hooks": {e: [{"hooks": [{"type": "command", "command": "[ -x ~/agent-hub/bin/hub-event ] && exec ~/agent-hub/bin/hub-event --runtime claude "
                                         f"{what}; echo x >&2; exit 1"}]}] for e, what in rc.WORKTREE_EVENTS}}
        self.assertTrue(rc.platform_rendered(old))
        self.assertTrue(rc.platform_rendered({"hooks": rc.platform_hooks()}))
        self.assertFalse(rc.platform_rendered({"hooks": rc.install_hooks({})["hooks"]}), "a project's that isn't sandboxed")
        self.assertEqual(rc.session_hooks_of({"hooks": rc.platform_hooks()}), rc.install_hooks({})["hooks"])
        for e in (*old["hooks"].values(), *rc.platform_hooks().values()):
            self.assertTrue(rc.MANAGED.search(e[0]["hooks"][0]["command"]))


class WorktreeScript(Temp):
    """bin/worktree-memory.sh, which the user's own settings run in any folder: on an install."""

    def setUp(self):
        super().setUp()
        self.hub, self.said = self.tmp / "hub", self.tmp / "said"
        (self.hub / "bin").mkdir(parents=True)
        for f in ("worktree-memory.sh", "hubpaths.sh", "runtime_claude.py"):
            shutil.copy2(BIN / f, self.hub / "bin" / f)
        (self.hub / "bin" / "py").write_text(f"#!/bin/sh\n{{ echo run; /usr/bin/env; }} >> '{self.said}'\nexit 1\n")
        (self.hub / "bin" / "py").chmod(0o755)
        (self.hub / ".hub-home").write_text(str(self.tmp / "instance") + "\n")
        self.evil, self.mark = self.tmp / "evil", self.tmp / "ran"
        (self.evil / "bin").mkdir(parents=True)
        for f in ("bin/py", "bin/bash", "bin/dirname", "bin/git"):
            (self.evil / f).write_text(f"#!/bin/sh\n/usr/bin/touch '{self.mark}'\n")
            (self.evil / f).chmod(0o755)
        (self.evil / "be").write_text(f"/usr/bin/touch '{self.mark}'\n")
        self.env = {"HOME": str(self.tmp), "PATH": f"{self.evil / 'bin'}:/usr/bin:/bin", "AGENT_HUB_ROOT": str(self.evil), "BRUNNA_ROOT": str(self.evil),
                    "AGENT_HUB_PYTHON": str(self.evil / "bin/py"), "PYTHONPATH": str(self.evil), "BASH_ENV": str(self.evil / "be"), "HUB_AS_PROJECT": "1",
                    "BASH_FUNC_dirname%%": f"() {{ /usr/bin/touch '{self.mark}'; }}", "GIT_DIR": str(self.evil), "BRUNNA_STATE": str(self.evil),
                    "CLAUDE_PID": "77", "SSH_AUTH_SOCK": "/a b", "HUB_LLM_POINT": "p", "BRUNNA_JOB_CHAIN": "[]"}

    def run_it(self, *args):
        return subprocess.run([str(self.hub / "bin" / "worktree-memory.sh"), *args], env=self.env, cwd=getattr(self, "at", self.tmp), input='{"x": 1}\n', capture_output=True,
                              text=True, timeout=120)

    def test_the_sessions_environment_runs_nothing_in_it(self):
        self.assertEqual(self.run_it("prompt").returncode, 0)
        self.assertFalse(self.mark.exists())
        got = self.said.read_text().splitlines()
        self.assertIn("run", got, "the hub's own py ran")
        self.assertEqual([ln for ln in got if str(self.evil) in ln or ln.startswith(("HUB_AS_PROJECT", "AGENT_HUB_ROOT", "BRUNNA_ROOT", "GIT_", "PYTHON"))], [])
        for kept in ("CLAUDE_PID=77", "SSH_AUTH_SOCK=/a b", "HUB_LLM_POINT=p", "BRUNNA_JOB_CHAIN=[]"):
            self.assertIn(kept, got)

    def test_its_list_is_the_adapters(self):
        self.env = {"HOME": str(self.tmp), **{n: f"v-{n}" for n in rc.HOOK_PASSES},
                    **{f"{p}{n}": f"v-{p}{n}" for n in rc.HOOK_PASSES_PLATFORM for p in ("AGENT_HUB_", "BRUNNA_")}}
        self.run_it("prompt")
        got = self.said.read_text().splitlines()
        self.assertEqual([k for k in self.env if k != "HOME" and f"{k}=v-{k}" not in got], [])

    def test_a_missing_python_is_seen_and_stops_no_session(self):
        (self.hub / "bin" / "py").write_text("#!/bin/sh\nexit 127\n")
        main, wt = self.tmp / "main", self.tmp / "main" / "wt" / "sub"
        wt.mkdir(parents=True)
        for event in ("start", "prompt", "wait"):
            r = self.run_it(event)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), "no project's worktree: not a word")
        (wt.parent / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
        self.at = wt
        self.assertEqual(self.run_it("start").returncode, 0, "no onboarded project's")
        (main / ".agent-memory").mkdir()
        for event in ("start", "prompt", "nudge", "end", "wait"):
            r = self.run_it(event)
            self.assertEqual(r.returncode, 127, "not the exit code the tool takes for a refusal (2)")
            self.assertIn("Python was not found", r.stderr)
        (self.hub / "bin" / "py").write_text("#!/bin/sh\nexit 1\n")
        self.assertEqual(self.run_it("prompt").returncode, 0, "not a repo, or no project: nothing")

    def test_what_runs_as_the_project_is_said_by_the_script_only(self):
        text = (BIN / "worktree-memory.sh").read_text()
        self.assertTrue(text.startswith("#!/bin/bash -p\n"))
        self.assertIn('worktree-memory.sh" --as-project "$event"', text)
        self.assertEqual(text.count("HUB_AS_PROJECT=1"), 1)


class Rendered(Temp):
    """What the platform's own value stands in the place of."""

    def pol(self, **kw):
        return {"workspace": self.tmp / "ws", "write": [], "other_projects": [self.tmp / "other"], "deny_read": [self.tmp / "secret", self.tmp / "c" / "*" / "memory"],
                "credentials": [], "credential_files": [self.tmp / "key"], "protect": [], "protect_roots": [], "deny_write": [], "deny_write_files": [],
                "platform_state": [self.tmp / "state"], "network": ["a.example"], "platform_commands": [], "local_bind": True, "env": {}, **kw}

    def setUp(self):
        super().setUp()
        (self.tmp / "ws").mkdir()
        for p in ("other", "secret", "state"):
            (self.tmp / p).mkdir()
        (self.tmp / "key").write_text("")
        self.user = self.tmp / "user"
        self.user.mkdir()
        self.patch(rc, "_home", lambda: self.user)

    def mine(self, doc):
        (self.user / "settings.json").write_text(json.dumps(doc))

    def test_the_switches(self):
        sb = rc.session_settings(self.pol())[0]
        self.assertIs(sb["autoAllowBashIfSandboxed"], False)
        self.assertIs(sb["network"]["allowAllUnixSockets"], False)
        self.assertIs(sb["network"]["allowLocalBinding"], True)
        self.assertIs(sb["allowUnsandboxedCommands"], False)
        self.assertEqual(rc.session_fixed(), {"disableAllHooks": False, "apiKeyHelper": ""})

    def test_the_owners_own_are_kept(self):
        self.mine({"sandbox": {"autoAllowBashIfSandboxed": True, "network": {"allowAllUnixSockets": True}}, "apiKeyHelper": "mine",
                   "env": {"BASH_ENV": "/mine", "CLAUDE_CODE_SHELL_PREFIX": "/pre", "BRUNNA_ROOT": "/not"}})
        sb = rc.session_settings(self.pol())[0]
        self.assertIs(sb["autoAllowBashIfSandboxed"], True)
        self.assertIs(sb["network"]["allowAllUnixSockets"], True)
        self.assertEqual(rc.session_fixed(), {"disableAllHooks": False})
        env = rc.session_env(self.pol())
        self.assertEqual((env["BASH_ENV"], env["CLAUDE_CODE_SHELL_PREFIX"], env["ENV"]), ("/mine", "/pre", ""))
        self.assertNotEqual(env["BRUNNA_ROOT"], "/not", "the platform's own places are never a settings file's")

    def test_only_these_variables(self):
        with mock.patch.object(hubconf, "own_pointers", lambda: {}):
            env = rc.session_env(self.pol())
        for n in ("CLAUDE_CODE_SHELL_PREFIX", "BASH_ENV", "ENV", "AGENT_HUB_PYTHON", "BRUNNA_PYTHON", "BRUNNA_ROOT", "HUB_AS_PROJECT"):
            self.assertEqual(env[n], "", n)
        for n in ("PYTHONPATH", "PATH", "HOME", "NODE_OPTIONS"):   # people set these for their own work
            self.assertNotIn(n, env)
        self.assertEqual("LD_PRELOAD" in env, sys.platform.startswith("linux"))

    def test_where_reading_is_denied_no_file_allows_it(self):
        files = rc.session_settings(self.pol())[0]["credentials"]["files"]
        self.assertTrue(all(f["mode"] == "deny" for f in files))
        got = [f["path"] for f in files]
        for p in ("other", "secret", "key", "state"):
            self.assertIn(str(self.tmp / p), got)
        self.assertFalse([p for p in got if "*" in p or p.startswith("~")])

    def test_not_in_what_leases_and_runs_get(self):
        sb = rc.settings(self.pol())[0]
        self.assertNotIn("credentials", sb)
        self.assertNotIn("autoAllowBashIfSandboxed", sb)


class Project(Temp):
    """A sandboxed project rendered for real but for the sandbox its jobs run in."""

    def setUp(self):
        super().setUp()
        self.m = load("rps_sbx", "render-project-settings.py")
        self.m.RENDERED = self.tmp / "state" / "rendered-settings"
        self.ws = self.tmp / "ws" / "proj"
        (self.ws / ".claude").mkdir(parents=True)
        git(self.ws, "init", "-q")
        self.f = self.ws / ".claude" / "settings.local.json"
        self.doc = {"name": "proj", "paths": {"workspace": str(self.ws)}, "enforce": {"sandbox": True}, "allow_write": [str(self.tmp / "also")]}
        self.index = {"projects": {"proj": self.doc}}
        self.alerts, self.jobs = {}, []

        def once(key, problems, ok_text, ids=None):
            self.alerts[key] = problems
            if key.startswith("project-settings-kept"):
                self.told += problems
            return True
        self.told = []

        def data(project, cwd, op, *args, **kw):
            r = subprocess.run([sys.executable, str(BIN / "project-git-job.py"), op, *map(str, args)], cwd=cwd, env=GIT, capture_output=True, text=True, timeout=120)
            self.jobs.append((op, *map(str, args)))
            if r.returncode:
                raise project_git.Unavailable(r.stderr)
            return json.loads(r.stdout)
        for p in (mock.patch.object(hubconf, "projects", lambda: self.index["projects"]),
                  mock.patch.object(isolation, "wrap", lambda *a, **kw: ["/usr/bin/false"]),
                  mock.patch.object(self.m, "push_folder", lambda *a: None),
                  mock.patch.object(self.m.registry, "refresh", lambda *a, **kw: self.index),
                  mock.patch.object(project_git, "data", data),
                  mock.patch.object(project_git, "job", lambda project, cwd, op, *args, **kw: self.job(data, project, cwd, op, *args)),
                  mock.patch.object(alert, "once", once), mock.patch.object(alert, "open_keys", lambda prefix: [])):
            p.start()
            self.addCleanup(p.stop)

    def job(self, data, *a):
        with contextlib.suppress(Exception):
            data(*a)

    def on(self):
        self.err = io.StringIO()
        with contextlib.redirect_stderr(self.err):
            return self.m.write_project("proj", self.doc, self.index, report=False)

    def off(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.m.unapply("proj", {k: v for k, v in self.doc.items() if k != "enforce"})

    def commit(self, rel, doc):
        f = self.ws / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def found(self, folders=()) -> list[str]:
        return self.m.foreign_controls(self.ws, "proj", self.m.reach(self.doc), folders)


OUT = "/usr/local/elsewhere"


class Unwritable(Project):
    def test_a_state_that_cant_note_what_was_written_never_stops_the_sandbox(self):
        """Which sandboxes the platform wrote is a record; a state disk that is full isn't a reason to go without one."""
        real = hubconf.write_atomic

        def full(path, text, *a, **kw):
            if "renders" in pathlib.Path(path).parts:
                raise OSError(28, "No space left on device")
            return real(path, text, *a, **kw)
        self.patch(hubconf, "write_atomic", full)
        self.on()
        self.assertTrue(json.loads(self.f.read_text())["sandbox"]["enabled"])
        self.assertIn("could not be noted", self.err.getvalue())


class Told(Project):
    TELLS = (
        ({"sandbox": {"filesystem": {"allowWrite": [OUT]}}}, OUT),
        ({"sandbox": {"filesystem": {"allowWrite": ["~/"]}}}, "~/"),
        ({"sandbox": {"filesystem": {"allowWrite": ["../../.."]}}}, "../../.."),
        ({"permissions": {"additionalDirectories": [OUT]}}, OUT),
        ({"permissions": {"allow": [f"Edit(/{OUT}/**)"]}}, f"Edit(/{OUT}/**)"),
        ({"permissions": {"allow": ["Edit(~/**)"]}}, "Edit(~/**)"),
        ({"permissions": {"allow": ["Write"]}}, "Write"),
        ({"permissions": {"allow": ["Edit(lnk/**)"]}}, "Edit(lnk/**)"),
        ({"permissions": {"allow": ["Edit(lnk/x)"]}}, "Edit(lnk/x)"),
        ({"sandbox": {"filesystem": {"allowRead": ["~/.ssh/id"]}}}, "~/.ssh/id"),
        ({"sandbox": {"network": {"allowedDomains": ["evil.example"]}}}, "evil.example"),
        ({"permissions": {"allow": ["WebFetch(domain:evil.example)"]}}, "WebFetch(domain:evil.example)"),
        ({"permissions": {"allow": ["WebFetch"]}}, "WebFetch"),
        ({"sandbox": {"network": {"allowUnixSockets": ["/var/run/x.sock"]}}}, "/var/run/x.sock"),
        ({"sandbox": {"network": {"httpProxyPort": 8123}}}, "8123"),
        ({"sandbox": {"network": {"allowMachLookup": ["com.x"]}}}, "com.x"),
        ({"sandbox": {"excludedCommands": ["docker *"]}}, "docker *"),
        ({"sandbox": {"enableWeakerNetworkIsolation": True}}, "enableWeakerNetworkIsolation"),
        ({"permissions": {"defaultMode": "bypassPermissions"}}, "bypassPermissions"),
        ({"hooks": {"Stop": []}}, "hook"),
        ({"awsAuthRefresh": "x"}, "awsAuthRefresh"),
        ({"statusLine": {"type": "command", "command": "x"}}, "statusLine"),
        ({"enableAllProjectMcpServers": True}, "enableAllProjectMcpServers"),
        ({"env": {"PATH": "x", "LANG": "C"}}, "PATH"),
    )
    QUIET = (
        {"permissions": {"allow": ["Edit", "Edit(**)", "Edit(src/**)", "Edit(./src/*)", "Edit(/src/**)", "Edit(src/a.py)", "Edit(/src/a b.py)", "Edit(./x)", "Bash(make:*)", "Bash(echo {a,b} > x)", "mcp__x__*", "Read(~/x)", "WebFetch(domain:github.com)"]}},
        {"permissions": {"deny": [f"Edit(/{OUT}/**)"], "ask": ["Write"], "defaultMode": "plan", "additionalDirectories": ["./sub", "src"]}},
        {"sandbox": {"enabled": False, "allowUnsandboxedCommands": True, "autoAllowBashIfSandboxed": True,
                     "filesystem": {"allowWrite": ["./out", "./out/**", "//WS/x", "ALSO/y"], "denyRead": ["~/x"], "denyWrite": ["/"], "allowRead": ["./in"]},
                     "network": {"allowLocalBinding": True, "allowAllUnixSockets": True, "deniedDomains": ["x"], "allowedDomains": ["api.github.com"]}}},
        {"env": {"LANG": "C", "CI": "1", "BASH_MAX_TIMEOUT_MS": "1", "BASH_ENV": "x", "BRUNNA_ROOT": "x"}},
        {"disableAllHooks": True, "model": "x", "includeCoAuthoredBy": False, "apiKeyHelper": "x"},
    )

    def setUp(self):
        super().setUp()
        (self.ws / "lnk").symlink_to(OUT)
        self.patch(rc, "user_settings", lambda: {})

    def test_each(self):
        for doc, what in self.TELLS:
            with self.subTest(doc):
                self.commit(".claude/settings.json", doc)
                [got] = self.found()
                self.assertIn(what, got)
                self.assertIn(".claude/settings.json", got)

    def test_what_an_ordinary_repo_has_isnt(self):
        for doc in self.QUIET:
            with self.subTest(doc):
                self.commit(".claude/settings.json", json.dumps(doc).replace("//WS", f"/{self.ws}").replace("ALSO", str(self.tmp / "also")))
                self.assertEqual(self.found(), [])

    def test_a_helper_is_told_where_the_owner_has_one(self):
        self.commit(".claude/settings.json", {"apiKeyHelper": "x"})
        with mock.patch.object(rc, "user_settings", lambda: {"apiKeyHelper": "mine"}):
            [got] = self.found()
        self.assertIn("apiKeyHelper", got)

    def test_a_skills_own_commands_where_some_run_outside(self):
        self.commit(".claude/skills/z/SKILL.md", "---\nname: z\n---\nresult: !`docker ps`\n")
        self.assertEqual(self.found(), [])
        self.commit(".claude/settings.json", {"sandbox": {"excludedCommands": ["docker *"]}})
        self.assertTrue(any("SKILL.md" in x for x in self.found()))

    def test_its_other_folders(self):
        wide = {"sandbox": {"filesystem": {"allowWrite": [OUT]}}}
        self.commit(".claude/worktrees/w/.claude/settings.json", wide)
        self.commit("sub/.claude/settings.json", wide)
        self.commit("sub/.claude/settings.local.json", {"sandbox": {"excludedCommands": ["x *"]}})
        self.commit("sub/.mcp.json", "{}")
        got = "\n".join(self.found(["sub"]))
        for part in ("worktrees/w/.claude/settings.json", "sub/.claude/settings.json", "sub/.claude/settings.local.json", "sub/.mcp.json"):
            self.assertIn(part, got)
        self.commit(".claude/settings.json", wide)
        self.assertFalse([x for x in self.found(["sub"]) if "worktrees/w" in x], "what is the same in its folder isn't said twice")

    def test_once_each_and_in_bounds(self):
        self.commit(".claude/settings.json", {"sandbox": {"filesystem": {"allowWrite": [f"{OUT}/{i}/{'x' * 300}" for i in range(500)]}}})
        drift = self.on()
        self.assertEqual(len(self.told), 1)
        self.assertLess(len(self.told[0]), 3000)
        self.assertEqual(len([d for d in drift if "settings.json" in d]), 1)
        self.on()
        self.assertEqual(len(self.told), 1, "the same content: once")
        self.commit(".claude/settings.json", {"sandbox": {"filesystem": {"allowWrite": [f"{OUT}/{i}/{'x' * 300}" for i in range(500)]}}, "model": "x"})
        self.on()
        self.assertEqual(len(self.told), 1, "another file with the same entries")
        self.commit(".claude/settings.json", {"hooks": {"Stop": []}})
        self.on()
        self.assertEqual(len(self.told), 2)

    def test_once_per_content(self):
        """What is told is keyed by the content it was made from, whole, not by the line made of it."""
        many = [f"{OUT}/{i}" for i in range(50)]
        n = 0
        for doc in ({"sandbox": {"filesystem": {"allowWrite": many}}}, {"sandbox": {"filesystem": {"allowWrite": [*many[:-1], f"{OUT}/other"]}}},
                    {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "a"}]}]}}, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "b"}]}]}},
                    {"env": {"PATH": "a"}}, {"env": {"PATH": "b"}}, {"statusLine": {"command": "a"}}, {"statusLine": {"command": "b"}},
                    {"enabledPlugins": {"a@b": True}}, {"enabledPlugins": {"c@b": True}}):
            with self.subTest(doc):
                self.commit(".claude/settings.json", doc)
                self.on()
                n += 1
                self.assertEqual(len(self.told), n)
                names.env_set(os.environ, "LANG_UI", "zh-Hant")   # nor by its language
                self.on()
                names.env_set(os.environ, "LANG_UI", "en")
                self.assertEqual(len(self.told), n)
        self.commit(".claude/settings.json", "{}")
        self.on()
        for i, text in enumerate(('{"mcpServers": {"a": {}}}', '{"mcpServers": {"b": {}}}'), 1):
            self.commit(".mcp.json", text)
            self.on()
            self.assertEqual(len(self.told), n + i)

    def test_where_nothing_of_the_platforms_holds_nothing_is_taken_for_held(self):
        """A submodule's folder, or any other .git that is no real repo's: the platform writes nothing there."""
        (self.ws / "subm").mkdir()
        (self.ws / "subm" / ".git").write_text("gitdir: x\n")
        self.commit("subm/.claude/settings.json", {"disableAllHooks": True, "apiKeyHelper": "x", "env": {"BASH_ENV": "y"},
                                                   "sandbox": {"enabled": False, "autoAllowBashIfSandboxed": True, "network": {"allowAllUnixSockets": True}}})
        self.on()
        got = "\n".join(self.told)
        for what in ("disableAllHooks", "apiKeyHelper", "BASH_ENV", "sandbox.enabled", "autoAllowBashIfSandboxed", "allowAllUnixSockets"):
            self.assertIn(what, got)

    def test_a_file_that_only_looks_like_the_platforms_is_looked_at(self):
        self.on()
        fake = {"hooks": {e: [{"hooks": [{"type": "command", "command": rc.worktree_command(w)}]}] for e, w in rc.WORKTREE_EVENTS},
                "sandbox": {"excludedCommands": ["sh *"]}}
        self.commit("sub/.claude/settings.local.json", fake)
        self.on()
        self.assertTrue(any("sub/.claude/settings.local.json" in t and "sh *" in t for t in self.told), self.told)

    def test_a_note_that_couldnt_be_sent_is_sent_later(self):
        self.commit(".claude/settings.json", {"hooks": {"Stop": []}})
        with mock.patch.object(alert, "once", lambda *a, **kw: False):
            self.on()
        self.assertEqual(self.told, [])
        self.on()
        self.assertEqual(len(self.told), 1)


class NotCarried(Project):
    def test_what_gives_more_stays_in_the_copy_and_is_told(self):
        mine = {"permissions": {"allow": ["Bash(make:*)", f"Edit(/{OUT}/**)", "Write", "WebFetch(domain:evil.example)", "Edit(src/a.py)"],
                                "additionalDirectories": [OUT, "./sub"]}, "enableAllProjectMcpServers": True}
        self.f.write_text(json.dumps(mine))
        self.on()
        now = json.loads(self.f.read_text())
        self.assertTrue({"Bash(make:*)", "Edit(src/a.py)"} <= set(now["permissions"]["allow"]))
        self.assertFalse({f"Edit(/{OUT}/**)", "Write", "WebFetch(domain:evil.example)"} & set(now["permissions"]["allow"]))
        self.assertEqual(now["permissions"]["additionalDirectories"], ["./sub"])
        self.assertNotIn("enableAllProjectMcpServers", now)
        self.assertTrue(any("enableAllProjectMcpServers" in t for t in self.told), self.told)
        self.assertTrue(any(f.read_text() == json.dumps(mine) for f in self.m.saved_dir("proj").glob("*.json")))
        self.off()
        back = json.loads(self.f.read_text())
        self.assertEqual(sorted(back["permissions"]["allow"]), sorted(mine["permissions"]["allow"]))
        self.assertEqual(sorted(back["permissions"]["additionalDirectories"]), sorted(mine["permissions"]["additionalDirectories"]))
        self.assertIs(back["enableAllProjectMcpServers"], True)

    def test_what_only_takes_away_stays(self):
        self.f.write_text(json.dumps({"enableAllProjectMcpServers": False, "permissions": {"deny": [f"Edit(/{OUT}/**)"]}}))
        self.on()
        now = json.loads(self.f.read_text())
        self.assertIs(now["enableAllProjectMcpServers"], False)
        self.assertIn(f"Edit(/{OUT}/**)", now["permissions"]["deny"])


class OneReading(Project):
    """Only a path that can be read one way only is known to be inside: the tool's reading of any other isn't the platform's
    to guess."""

    ODD = ("{src,%s}/x" % OUT, "src/../../../x", "../x", "../**", "src/../**", "src/**/../x", "/usr/../**", "[s]rc/x", "[s]/**", "{a,b}/**", "src /**", " src/**",
           "src/** ", "src\\x", "'src'/x", '"src"/x', " src/x", "src/x ", "!src/x", "src/$HOME", "~root/x", "a/~/x", "src\tx", "src\nx", "src?x", "", "src|x", "`x`",
           "src/\u2215x", "src/\u202ex", "src/ /x", "src/\u00a0x")
    AWAY = ("//**", "//*", "~/**", "~/x", "//usr/x", "/*" if False else "//usr/**/x")
    PLAIN = ("src/a.py", "./src/a", "src", ".", "/src/a", "a b/c-d_e.f", "\u5c08\u6848/\u7b46\u8a18.md", "a@b/c+d,e=f(g)#h", "#src", "(src)", "cafe\u0301/x", "\u00fc")
    FOLDERS = ("src/**", "./src/*", "**", "*", "/src/**", "/**", "./**", "a b/**", "*.md", "docs/*.md", "src/**/*.ts", "**/x", "src/*/x", "src**", "\u5c08\u6848/**/*.md")

    def test_a_rule(self):
        reach = self.m.reach(self.doc)
        for x in self.ODD:
            with self.subTest(x):
                self.assertTrue(rc.widens(f"Edit({x})", reach))
                self.assertTrue(rc.unclear(f"Edit({x})"))
                self.assertTrue(rc.outside(x, reach, glob=True))
        for x in self.AWAY:
            self.assertTrue(rc.widens(f"Edit({x})", reach), x)
            self.assertFalse(rc.unclear(f"Edit({x})"), x)
        for x in (*self.PLAIN, *self.FOLDERS, f"/{self.ws}/**", f"/{self.ws}/src/*", f"/{self.tmp}/also/**"):
            self.assertFalse(rc.widens(f"Edit({x})", reach), x)
        for x in (f"/{self.tmp}/**", f"/{OUT}/**", f"/{self.ws}x/**"):
            self.assertTrue(rc.widens(f"Edit({x})", reach), x)
        (self.ws / "lnk").symlink_to(OUT)
        self.assertTrue(rc.widens("Edit(lnk/**)", reach))
        for x in self.FOLDERS:
            self.assertTrue(rc.outside(x, reach), f"{x}: a folder of additionalDirectories is literal")
        self.assertTrue(rc.outside("/**", reach, glob=True), "from the root where it isn't a rule")
        for x in ("edit(src/a)", "EDIT", "write(src/a)", " Edit(src/a)", "Edit (src/a)", "Edit(src/a", "*", "Wri*", "", "mcp*", "WebFetch(domain:github.com,evil.example)",
                  "WebFetch( domain:github.com)", "webfetch(domain:github.com)", "WebFetch(domain:{github.com,evil.example})", "\u0395dit(src/a)"):
            self.assertTrue(rc.widens(x, reach), x)
        for x in ("Read(~/x)", "Bash(a{b,c} *)", "Skill", "mcp__a__b", "mcp__a__*", "WebFetch(domain:github.com)"):
            self.assertFalse(rc.widens(x, reach), x)

    def test_not_carried_told_and_not_taken_in(self):
        odd, folders = [f"Edit({x})" for x in self.ODD], [f"Edit({x})" for x in self.FOLDERS]
        self.f.write_text(json.dumps({"permissions": {"allow": ["Edit(src/a.py)", *folders, *odd], "additionalDirectories": ["./s", "{s,%s}" % OUT, "s*", "s/**"]}}))
        self.on()
        now = json.loads(self.f.read_text())["permissions"]
        self.assertFalse(set(odd) & set(now["allow"]))
        self.assertTrue({"Edit(src/a.py)", *folders} <= set(now["allow"]))
        self.assertEqual(now["additionalDirectories"], ["./s"])
        self.assertTrue(any("permissions.allow" in t for t in self.told), self.told)
        good = json.loads(self.f.read_text())
        for x in odd:
            self.assertFalse(self.m.ours({**good, "permissions": {**good["permissions"], "allow": [*good["permissions"]["allow"], x]}},
                                         self.m.last_record("proj", self.doc), self.doc), x)
        self.commit(".claude/settings.json", {"permissions": {"allow": ["Edit({src,/x}/y)"]}})
        [got] = self.found()
        self.assertIn("Edit({src,/x}/y)", got)
        self.assertIn("can't be told", got)
        self.assertNotIn("write outside", got)
        self.commit(".claude/settings.json", {"sandbox": {"filesystem": {"allowWrite": ["//usr/x"]}}, "permissions": {"allow": ["Edit(docs/*.md)", "Edit(//usr/**)"]}})
        [got] = self.found()
        self.assertIn("write outside", got)

    def test_a_folder_of_any_name_is_ordinary(self):
        """The project's own path, in characters no rule of the user's is asked to have."""
        for n in ("\u5c08\u6848", "a@b (c)", "x[1]", "q'{y}"):
            with self.subTest(n):
                self.ws = self.tmp / "ws" / n
                w = self.ws / ".claude" / "worktrees" / "w"
                w.mkdir(parents=True)
                git(self.ws, "init", "-q")
                (self.ws / ".agent-memory").mkdir()
                self.doc["paths"] = {"workspace": str(self.ws)}
                self.f = self.ws / ".claude" / "settings.local.json"
                self.f.write_text(json.dumps({"permissions": {"allow": ["Edit(src/a.py)"]}}))
                self.on()
                for at in (self.f, w / ".claude" / "settings.local.json"):
                    got = json.loads(at.read_text())
                    self.assertTrue(self.m.ours(got, self.m.last_record("proj", self.doc), self.doc), at)
                    self.assertFalse(self.m.ours({**got, "permissions": {**got["permissions"], "additionalDirectories": [OUT]}},
                                                 self.m.last_record("proj", self.doc), self.doc))


class Merge(Temp):
    def test_any_spelling_at_any_depth(self):
        held = [".claude/settings.local.json", "sub/.claude/settings.local.json", ".Claude/settings.local.json", "a/b/.CLAUDE/Settings.Local.json",
                ".claude/ſettings.local.json", "a b/.claude/settings.json"]
        self.assertEqual(policy.protected_changes([*held, "src/settings.local.json", "docs/claude/settings.json", ".claude/worktrees/x/a.py"]), held)

    def test_a_name_git_would_quote(self):
        """merge-branch takes the names as they are (-z): one git quotes is the same file."""
        repo = self.tmp / "r"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "commit", "-q", "--allow-empty", "-m", "c")
        f = repo / "ü x" / ".claude" / "settings.local.json"
        f.parent.mkdir(parents=True)
        f.write_text("{}")
        git(repo, "checkout", "-q", "-b", "b")
        git(repo, "add", "-f", ".")
        git(repo, "commit", "-q", "-m", "x")
        text = (BIN / "merge-branch").read_text()
        lines = [ln for ln in text.splitlines() if '"--name-only", f"' in ln and ('"main":' in ln or '"worktree":' in ln)]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn('"-z"', line)
            self.assertIn('.split("\\0")', line)
        got = git(repo, "diff", "-z", "--name-only", "main...b").split("\0")
        self.assertEqual(len(policy.protected_changes(got)), 1)


class Nested(Project):
    """Repos in the project's folder: a session opened in one loads its settings files only."""

    def repo(self, rel, commit=False):
        d = self.ws / rel
        d.mkdir(parents=True)
        git(d, "init", "-q")
        if commit:
            git(d, "commit", "-q", "--allow-empty", "-m", "c")
        return d

    def settings(self, d) -> dict:
        return json.loads((d / ".claude" / "settings.local.json").read_text())

    def assertRendered(self, d):
        now = self.settings(d)
        self.assertTrue(now["sandbox"]["enabled"])
        self.assertEqual(now["hooks"], rc.platform_hooks())

    def test_a_real_repo(self):
        dep = self.repo("vendor/dep", commit=True)
        self.repo("node_modules/x")
        self.on()
        for d in ("vendor/dep", "node_modules/x"):
            self.assertRendered(self.ws / d)
        self.assertEqual(git(dep, "status", "--porcelain", "--untracked-files=all", "--", ".claude/settings.local.json"), "", "its own status stays clean")
        self.assertTrue(any("vendor/dep" in t for t in self.told), self.told)
        n = len(self.told)
        before = (self.f.read_bytes(), (dep / ".claude" / "settings.local.json").read_bytes())
        self.on()
        self.assertEqual(len(self.told), n, "told once")
        self.assertEqual((self.f.read_bytes(), (dep / ".claude" / "settings.local.json").read_bytes()), before, "the next run changes nothing")

    def test_any_other_git_is_told_once_and_written_nothing_into(self):
        """What a session could remove before, it still can."""
        dep = self.repo("dep", commit=True)
        git(dep, "worktree", "add", "-q", str(self.ws / "dep-tree"), "-b", "b")
        git(dep, "worktree", "add", "-q", ".claude/worktrees/w", "-b", "w")
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q")
        git(other, "commit", "-q", "--allow-empty", "-m", "c")
        git(self.ws, "submodule", "add", "-q", str(other), "subm")
        for d in ("e1", "e2", "e3/.git", "e4"):
            (self.ws / d).mkdir(parents=True)
        (self.ws / "e1" / ".git").write_text("")
        (self.ws / "e2" / ".git").symlink_to(other / ".git")
        (self.ws / "e4" / ".git" / "objects").mkdir(parents=True)
        kinds = ("dep-tree", "dep/.claude/worktrees/w", "subm", "e1", "e2", "e3", "e4")
        self.on()
        for d in kinds:
            with self.subTest(d):
                self.assertFalse((self.ws / d / ".claude" / "settings.local.json").exists())
                self.assertTrue(any("no sandbox among them" in t and d in t for t in self.told), self.told)
        self.assertRendered(dep)
        n = len(self.told)
        shutil.rmtree(self.ws / "e1")
        self.on()
        (self.ws / "e1").mkdir()
        (self.ws / "e1" / ".git").write_text("")
        self.assertEqual(self.on(), [])
        self.assertEqual(len(self.told), n, "once a path")
        self.commit("subm/.claude/settings.json", {"hooks": {"Stop": []}})
        self.on()
        self.assertTrue(any("subm/.claude/settings.json" in t for t in self.told[n:]), "its own files are looked at all the same")
        git(self.ws / "e3", "init", "-q")
        self.on()
        self.assertRendered(self.ws / "e3")

    def test_what_isnt_one(self):
        (self.ws / "plain").mkdir()
        away = self.tmp / "away"
        away.mkdir()
        git(away, "init", "-q")
        (self.ws / "lnk").symlink_to(away)
        mem = self.ws / ".agent-memory"
        mem.mkdir()
        (mem / ".git").write_text("gitdir: /x")
        self.on()
        for d in (self.ws / "plain", away, mem):
            self.assertFalse((d / ".claude").exists(), d)

    def test_no_rule_names_them(self):
        """A session moves and removes them as before: only the rules at any depth, which were there."""
        before = self.render()
        self.repo("node_modules/x")
        self.repo("odd (name)")
        self.on()
        self.on()
        self.assertRendered(self.ws / "odd (name)")
        now = json.loads(self.f.read_text())
        self.assertEqual((now["permissions"]["deny"], now["sandbox"]), (before["permissions"]["deny"], before["sandbox"]))

    def render(self) -> dict:
        self.on()
        return json.loads(self.f.read_text())

    def test_one_removed_is_rendered_again_and_one_that_comes_and_goes_is_told_once(self):
        d = self.repo("tmp-repo")
        self.on()
        n = len(self.told)
        shutil.rmtree(d / ".claude")
        self.assertEqual(self.on(), [])
        self.assertRendered(d)
        shutil.rmtree(d)
        self.assertEqual(self.on(), [])
        self.assertFalse(d.exists())
        self.assertEqual(self.m.known("proj", self.doc)["nested"], [])
        self.repo("tmp-repo")
        self.assertEqual(self.on(), [])
        self.assertRendered(d)
        self.assertEqual(len(self.told), n)

    def test_what_stood_in_one_that_is_gone_is_never_put_into_another(self):
        d = self.repo("a")
        (d / ".claude").mkdir()
        (d / ".claude" / "settings.local.json").write_text('{"model": "old"}')
        self.on()
        shutil.rmtree(d)
        self.on()
        self.repo("a")
        self.on()
        self.off()
        self.assertFalse((d / ".claude").exists())

    def test_off_removes_them_and_puts_back_what_was_there(self):
        a, b, c = self.repo("a"), self.repo("b"), self.repo("c")
        mine = '{"permissions": {"allow": ["Bash(theirs:*)"]}, "model":   "x"}'
        (b / ".claude").mkdir()
        (b / ".claude" / "settings.local.json").write_text(mine)
        (c / ".claude").mkdir()
        (c / ".claude" / "settings.local.json").write_text("not json")
        self.f.write_text(json.dumps({"permissions": {"allow": ["Bash(make:*)"]}}))
        self.on()
        for d in (a, b, c):
            self.assertRendered(d)
        self.off()
        self.assertFalse((a / ".claude").exists())
        self.assertEqual((b / ".claude" / "settings.local.json").read_text(), mine)
        self.assertEqual((c / ".claude" / "settings.local.json").read_text(), "not json")
        self.assertEqual(json.loads(self.f.read_text())["permissions"]["allow"], ["Bash(make:*)"])

    def test_a_folder_swapped_for_a_link_is_written_nowhere_else(self):
        d = self.repo("a")
        self.on()
        away = self.tmp / "away"
        away.mkdir()
        shutil.rmtree(d)
        d.symlink_to(away)
        with mock.patch.object(self.m, "look", lambda *a: ([], [], [])):
            drift = self.on()
        self.assertEqual(list(away.iterdir()), [])
        self.assertTrue(any("could not be written" in x for x in drift), drift)

    def test_more_than_is_looked_through_is_said_and_nothing_known_is_dropped(self):
        self.repo("a")
        self.on()
        self.patch(self.m, "WALK_FOLDERS", 1)
        n = len(self.told)
        self.assertTrue(any("could be looked through" in x for x in self.on()))
        self.assertTrue(any("could be looked through" in t for t in self.told[n:]))
        self.assertEqual(self.m.known("proj", self.doc)["nested"], ["a"])
        self.assertTrue(json.loads(self.f.read_text())["sandbox"]["enabled"])
        n = len(self.told)
        self.assertEqual(self.on(), [], "a folder that is too big fails no run but the first")
        self.assertEqual(len(self.told), n, "once")

    def test_more_repos_than_are_covered(self):
        """A repo that has the settings keeps them, whatever others there are by name; how many aren't covered is told."""
        self.patch(self.m, "NESTED_MAX", 2)
        self.repo("m")
        self.repo("z")
        self.on()
        for n in ("a", "b", "c"):
            self.repo(n)
        n = len(self.told)
        self.assertTrue(any("3 repos" in x for x in self.on()))
        for d in ("m", "z"):
            self.assertRendered(self.ws / d)
        self.assertFalse((self.ws / "a" / ".claude").exists())
        self.assertTrue(any("3 repos" in t for t in self.told[n:]), self.told)
        self.assertEqual(self.m.known("proj", self.doc)["nested"], ["m", "z"])
        n = len(self.told)
        self.on()
        self.assertEqual(len(self.told), n, "once")
        shutil.rmtree(self.ws / "z")
        self.on()
        self.assertRendered(self.ws / "a")

    def test_one_the_walk_doesnt_list_is_left_only_when_it_is_no_repo(self):
        d = self.repo("a")
        self.on()
        for got in ({"repos": [], "settings": [], "others": [], "over": None}, {"repos": [f"x{i}" for i in range(300)], "settings": [], "others": [], "over": "more"}):
            with mock.patch.object(project_git, "data", lambda *a, got=got, **kw: got):
                self.on()
            self.assertRendered(d)
            self.assertIn("a", self.m.known("proj", self.doc)["nested"])
        with mock.patch.object(project_git, "data", lambda *a, **kw: {"repos": [], "settings": [], "others": [], "over": None}), \
                mock.patch.object(self.m, "real_repo", lambda *a: None):
            self.on()
        self.assertRendered(d)

    def test_each_reason_is_told(self):
        n = len(self.told)
        for over, told in (("it took too long", 1), ("it took too long", 1), ("x: 12 errors", 2), ("x: 13 errors", 2)):
            with mock.patch.object(project_git, "data", lambda *a, over=over, **kw: {"repos": [], "settings": [], "others": [], "over": over}):
                self.on()
            self.assertEqual(len(self.told) - n, told, over)

    def test_an_earlier_render_is_never_taken_for_the_users(self):
        d = self.repo("a")
        self.on()
        old = (d / ".claude" / "settings.local.json").read_bytes()
        self.doc["network"] = ["later.example"]
        self.on()
        self.assertNotEqual((d / ".claude" / "settings.local.json").read_bytes(), old)
        away = self.tmp / "away"
        shutil.move(d, away)
        self.on()
        (away / ".claude" / "settings.local.json").write_bytes(old)
        shutil.move(away, d)
        e = self.repo("b")   # a copy of an old one, where nothing was rendered before
        (e / ".claude").mkdir()
        (e / ".claude" / "settings.local.json").write_bytes(old)
        self.on()
        for x in (d, e):
            self.assertRendered(x)
            self.assertFalse(self.m.nested_before("proj", x.name).exists())
        (d / ".claude" / "settings.local.json").write_bytes(old)   # put back after the last render
        self.off()
        for x in (d, e):
            self.assertFalse((x / ".claude").exists(), "no old sandbox comes back")

    def test_a_walk_that_cant_run_is_said(self):
        def no(*a, **kw):
            raise project_git.Unavailable("no sandbox here")
        with mock.patch.object(project_git, "data", no):
            self.assertTrue(any("no sandbox here" in x for x in self.on()))
        self.assertTrue(json.loads(self.f.read_text())["sandbox"]["enabled"])

    def test_the_walk_takes_only_names_in_the_folder(self):
        with mock.patch.object(project_git, "data", lambda *a, **kw: {"repos": ["../x", "/etc", "a\nb", 5, "ok", ".agent-memory"], "settings": "x", "over": None}):
            self.m.look("proj", self.doc)
        self.assertEqual(self.m.known("proj", self.doc)["nested"], ["a\nb", "ok"])
        for odd in ("../x", "a\ud800b"):
            with mock.patch.object(project_git, "data", lambda *a, odd=odd, **kw: {"repos": [odd], "settings": [], "over": None}):
                [said] = self.m.look("proj", self.doc)[2]
                self.assertIn("can't name", said, "said, not passed over")

    def test_names_in_any_characters(self):
        for n in ("a\nb", "\u00fc \u4e2d", "x\u202ey", "q'\"w", "$(x)"):
            with self.subTest(ascii(n)):
                d = self.repo(n)
                drift = self.on()
                self.assertRendered(d)
                self.assertFalse([x for x in drift if "could not" in x], drift)
        if sys.platform.startswith("linux"):
            self.repo(os.fsdecode(b"not \xff text"))
            self.on()
            self.assertRendered(self.ws / os.fsdecode(b"not \xff text"))
        self.assertTrue(any("$(x)" in t for t in self.told))
        self.assertFalse([t for t in self.told if "\n" in t])
        self.off()
        self.assertFalse((self.ws / "a\nb" / ".claude").exists())

    def test_off_is_tried_again_until_it_is_done(self):
        a, b = self.repo("a"), self.repo("b")
        (a / ".claude").mkdir()
        (a / ".claude" / "settings.local.json").write_text('{"model": "mine"}')
        (b / ".claude").mkdir()
        (b / ".claude" / "settings.local.json").write_text('{"model": "other"}')
        self.on()
        rendered = (a / ".claude" / "settings.local.json").read_bytes()
        (a / ".claude" / "settings.local.json").write_text("{ broken")
        put = self.m.put

        def fails(ws, rel, data):
            if "b" in rel.parts:
                raise OSError("full")
            put(ws, rel, data)
        with mock.patch.object(self.m, "put", fails):
            self.off()
        self.assertEqual(sorted(self.m.known("proj", self.doc)["nested"]), ["a", "b"])
        self.assertTrue(self.m.nested_before("proj", "a").exists() and self.m.nested_before("proj", "b").exists())
        self.doc["network"] = ["changed.example"]   # the record it goes by is the one it was written with
        (a / ".claude" / "settings.local.json").write_bytes(rendered)
        self.off()
        self.assertEqual((a / ".claude" / "settings.local.json").read_text(), '{"model": "mine"}')
        self.assertEqual((b / ".claude" / "settings.local.json").read_text(), '{"model": "other"}')
        self.assertEqual(self.m.known("proj", self.doc)["nested"], [])

    def test_a_repo_that_becomes_a_git_file_is_left_as_it_was(self):
        a, b = self.repo("a"), self.repo("b")
        (a / ".claude").mkdir()
        (a / ".claude" / "settings.local.json").write_text('{"model": "mine"}')
        self.on()
        for d in (a, b):
            self.assertRendered(d)
            shutil.rmtree(d / ".git")
        (a / ".git").write_text("gitdir: x\n")
        self.on()
        self.assertEqual((a / ".claude" / "settings.local.json").read_text(), '{"model": "mine"}')
        self.assertFalse((b / ".claude").exists())
        self.assertEqual(self.m.known("proj", self.doc)["leaving"], [])

    def test_one_folders_copies_are_never_anothers(self):
        for d in ("x/same", "y/same"):
            (self.repo(d) / ".claude").mkdir()
        (self.ws / "y/same/.claude/settings.local.json").write_text('{"statusLine": "keep me"}')
        self.on()
        for i in range(self.m.KEEP_MAX + 5):
            (self.ws / "x/same/.claude/settings.local.json").write_text(json.dumps({"statusLine": i}))
            self.on()
            for f in self.m.saved_dir("proj").rglob("*.json"):
                os.utime(f, (f.stat().st_mtime - 10,) * 2)
        self.assertTrue([f for f in self.m.saved_dir("proj").rglob("*.json") if "keep me" in f.read_text()])

    def test_another_case_of_the_name(self):
        """`.GIT` is a repo's only where the volume opens it as `.git`; elsewhere it is a folder like any other."""
        (self.ws / "d" / ".GIT").mkdir(parents=True)
        (self.ws / "d" / ".GIT" / "HEAD").write_text("ref: x\n")
        self.repo("d/.GIT/inner")
        folds = (self.ws / "d" / ".git").exists()
        self.on()
        self.assertEqual(self.m.known("proj", self.doc)["nested"], ["d"] if folds else ["d/.GIT/inner"])

    def test_the_walk_in_time_and_number(self):
        def walk(*args):
            r = subprocess.run([sys.executable, str(BIN / "project-git-job.py"), "session-folders", str(self.ws), *args, *rc.SETTINGS_FILES], capture_output=True,
                               text=True, timeout=120)
            return json.loads(r.stdout)
        for i in range(4):
            self.repo(f"r{i}")
        self.commit("sub/.claude/settings.json", {})
        got = walk("1000", "60", "100")
        self.assertEqual((sorted(got["repos"]), got["settings"], got["over"]), (["r0", "r1", "r2", "r3"], ["sub"], None))
        self.assertEqual(got["others"], [])
        self.assertTrue(walk("1000", "0", "100")["over"])
        self.assertTrue(walk("3", "60", "100")["over"])
        self.assertTrue(walk("1000", "60", "2")["over"])

    def test_its_worktree_mode_is_refused_there(self):
        d = self.repo("a", commit=True)
        git(self.ws, "commit", "-q", "--allow-empty", "-m", "c")
        git(self.ws, "worktree", "add", "-q", ".claude/worktrees/w", "-b", "w")
        self.assertEqual(session_worktree.other_repo(d, self.ws), d)
        for mine in (self.ws, self.ws / ".claude", self.ws / ".claude" / "worktrees" / "w"):
            self.assertIsNone(session_worktree.other_repo(mine, self.ws), mine)
        with mock.patch.object(session_worktree, "_project", lambda cwd: ("proj", self.doc, self.ws)), self.assertRaises(session_worktree.Refused):
            session_worktree.make(d, "x", rc)
        self.assertTrue(gitdirs.dirs(d))

    def test_a_worktree_with_one_in_it_can_be_removed(self):
        git(self.ws, "commit", "-q", "--allow-empty", "-m", "c")
        git(self.ws, "worktree", "add", "-q", ".claude/worktrees/w", "-b", "w")
        w = self.ws / ".claude" / "worktrees" / "w"
        d = self.repo(".claude/worktrees/w/dep")
        own = self.repo(".claude/worktrees/w/own")
        self.on()
        self.assertRendered(d)
        (own / ".claude" / "settings.local.json").write_text('{"model": "x"}')
        with mock.patch.object(session_worktree, "_renderer", lambda: self.m):
            session_worktree.clear_nested("proj", self.doc, self.ws, w)
        self.assertFalse((d / ".claude").exists())
        self.assertTrue((own / ".claude" / "settings.local.json").exists(), "not one the platform wrote")
        self.assertTrue((self.ws / ".claude" / "settings.local.json").exists())
        shutil.rmtree(d)
        with mock.patch.object(session_worktree, "_renderer", lambda: self.m):
            session_worktree.clear_nested("proj", self.doc, self.ws, w)   # gone already


class Upgrade(Project):
    def test_from_files_an_earlier_release_wrote(self):
        """v0.21.0's hooks and record: rewritten without being called a change of anyone's, and off takes them out."""
        def old(what):
            return {"hooks": [{"type": "command", "command": f"[ -x ~/brunna/bin/hub-event ] && ~/brunna/bin/hub-event --runtime claude {what} || true"}]}
        hooks = {"SessionStart": [old("session_start")],
                 **{e: [{"hooks": [{"type": "command", "command": f"[ -x ~/brunna/bin/hub-event ] && exec ~/brunna/bin/hub-event --runtime claude {w}; exit 1"}]}]
                    for e, w in rc.WORKTREE_EVENTS}}
        sandbox = {"enabled": True, "filesystem": {"allowWrite": [], "denyRead": []}, "network": {"allowedDomains": []}}
        self.f.write_text(json.dumps({"sandbox": sandbox, "hooks": hooks, "permissions": {"deny": ["Read(//x/**)"], "allow": ["Bash(mine)"]}, "env": {"A": "1"}}))
        rec = self.m.RENDERED / "proj.json"
        rec.parent.mkdir(parents=True)
        rec.write_text(json.dumps({"sandbox": sandbox, "deny": ["Read(//x/**)"], "allow": [], "env": {}, "hooks": hooks}))
        self.assertEqual(self.on(), [])
        now = json.loads(self.f.read_text())
        self.assertEqual(now["hooks"], rc.platform_hooks())
        self.assertIn("Bash(mine)", now["permissions"]["allow"])
        self.off()
        back = json.loads(self.f.read_text())
        self.assertEqual(back["hooks"], rc.install_hooks({})["hooks"], "the session hooks any project has")
        self.assertEqual((back["permissions"]["allow"], back["env"]), (["Bash(mine)"], {"A": "1"}))
        self.assertNotIn("sandbox", back)
        self.assertNotIn("apiKeyHelper", back)


class Changed(Project):
    """A settings file that changes under a live session: taken in only while it is the platform's."""

    def ask(self, file=None, source="local_settings", cwd=None):
        return self.m.config_changed(json.dumps({"session_id": "s", "cwd": str(cwd or self.ws), "hook_event_name": "ConfigChange", "source": source,
                                                 "file_path": str(file or self.f)}))

    def test_the_platforms_is_taken_in(self):
        w = self.ws / ".claude" / "worktrees" / "w"
        w.mkdir(parents=True)
        d = self.ws / "dep"
        d.mkdir()
        git(d, "init", "-q")
        self.on()
        self.assertIsNone(self.ask())
        for at in (w, d):
            self.assertIsNone(self.ask(at / ".claude" / "settings.local.json", cwd=at))

    def test_with_a_rule_the_tool_was_told_to_remember(self):
        self.on()
        now = json.loads(self.f.read_text())
        now["permissions"]["allow"].append("Bash(always:*)")
        self.f.write_text(json.dumps(now))
        self.assertIsNone(self.ask())

    def test_anything_else_isnt(self):
        self.on()
        good = json.loads(self.f.read_text())

        def changed(**kw):
            return json.dumps({**good, **kw})
        less = {**good, "permissions": {**good["permissions"], "deny": good["permissions"]["deny"][1:]}}
        for text in ('{"permissions": {"allow": ["Bash(*)"]}}', "{}", "not json", "", changed(sandbox={**good["sandbox"], "enabled": False}),
                     changed(hooks={}), changed(disableAllHooks=True), changed(statusLine={"type": "command", "command": "x"}), json.dumps(less),
                     changed(env={}), changed(apiKeyHelper="x"),
                     changed(permissions={**good["permissions"], "allow": [*good["permissions"]["allow"], f"Edit(/{OUT}/**)"]}),
                     changed(permissions={**good["permissions"], "additionalDirectories": [OUT]})):
            with self.subTest(text[:80]):
                self.f.write_text(text)
                self.assertTrue(self.ask())
        self.f.unlink()
        self.assertTrue(self.ask())
        self.f.symlink_to(self.tmp / "x")
        self.assertTrue(self.ask())

    def test_the_platforms_own_next_one_while_it_is_written(self):
        self.on()
        write, asked = self.m.write_settings, []

        def while_writing(*a, **kw):
            write(*a, **kw)
            asked.append(self.ask())
        self.doc["network"] = ["new.example"]
        with mock.patch.object(self.m, "write_settings", while_writing):
            self.on()
        self.assertEqual(asked, [None])
        self.assertIsNone(self.ask())
        self.assertFalse(list((self.m.RENDERED / "coming").glob("*")))

    def test_whose_it_isnt(self):
        self.on()
        self.f.write_text("{}")
        self.assertIsNone(self.ask(source="project_settings"))
        self.doc["enforce"] = {}
        self.assertIsNone(self.ask(), "its sandbox is off")

    def test_the_entry_point(self):
        he = load("hub_event_sbx", "hub-event")
        for got, code in ((None, 0), ("because", 2)):
            with mock.patch.object(he, "_load", lambda *a, got=got: mock.Mock(config_changed=lambda raw: got)), mock.patch.object(he, "_log", lambda *a: None), \
                    mock.patch.object(sys, "stdin", io.StringIO("{}")), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(he.main(["--runtime", "claude", "config-changed"]), code)
            self.assertEqual(err.getvalue().strip(), got or "")
        with mock.patch.object(he, "_load", mock.Mock(side_effect=OSError("x"))), mock.patch.object(he, "_log", lambda *a: None), \
                mock.patch.object(sys, "stdin", io.StringIO("{}")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(he.main(["config-changed"]), 2)

    def test_whose_file_it_is_goes_by_the_file(self):
        self.on()
        self.f.write_text("{}")
        self.assertTrue(self.ask(cwd=self.tmp), "not by where the session is")
        self.assertIsNone(self.ask(self.tmp / "settings.local.json", cwd=self.ws))

    def test_only_a_sandboxed_projects_have_the_hook(self):
        self.assertEqual(rc.platform_hooks()["ConfigChange"][0]["matcher"], "local_settings")
        self.assertNotIn("ConfigChange", rc.install_hooks({})["hooks"])
        self.assertEqual(rc.without_worktree_maker({"hooks": rc.platform_hooks()})["hooks"], rc.install_hooks({})["hooks"])


if __name__ == "__main__":
    unittest.main()
