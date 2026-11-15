"""Onboarding asks two questions (docs/design/platform-push.md section 2): sandbox it, and which branches the platform may
push for it. bin/onboard-project.sh's flags (--sandbox/--no-sandbox, --push, --push-email) and bin/connect.py's (the
same, plus the questions a person answers).

onboard-project.sh runs for real against a throwaway hub (AGENT_HUB_ROOT, its bin/ links to the real scripts) under a
temp HOME, as tests/test_i18n_golden_shell_projects.py does; git_push_grant.py there is a stand-in that records how it
was called (`extra` still runs the real one), so nothing reaches gh or GitHub (tests/test_git_push_grant.py tests the
step itself). connect.py runs in-process with onboarding replaced (--dry-run, or a recorded call).

    bin/py -m unittest tests.test_onboard_push
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_RUN = subprocess.run

STUBS = {
    "sync-local-setup.sh": r"""#!/bin/bash
[ "${1:-}" = --quiet ] && shift
cd "${1:-.}" || exit 1
echo ".agent-memory/" >> "$(git rev-parse --git-common-dir)/info/exclude"
""",
    "gen-index-roster.sh": "#!/bin/bash\nexit 0\n",
    "commit-hub-files.sh": "#!/bin/bash\nexit 0\n",
    "render-services.py": "print('render-services (stand-in)')\n",
    # what onboarding asks of the grant step, recorded; `extra` is the real merge
    "git_push_grant.py": """import json, os, subprocess, sys
open(os.environ["AGENT_HUB_ROOT"] + "/grant-calls.jsonl", "a").write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["extra"]:
    sys.exit(subprocess.run([sys.executable, os.environ["REAL_GRANT"], *sys.argv[1:]]).returncode)
""",
}


def git(cwd, *args, env=None) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=env).stdout


class Onboard(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="onbpush-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hub, self.home = self.tmp / "hub", self.tmp / "home"
        self.ws = self.home / "workspace"
        (self.hub / "bin").mkdir(parents=True)
        (self.hub / "projects").mkdir()
        for f in sorted((ROOT / "bin").iterdir()):
            if f.name not in STUBS:
                (self.hub / "bin" / f.name).symlink_to(f)
        for name, text in STUBS.items():
            (self.hub / "bin" / name).write_text(text)
            (self.hub / "bin" / name).chmod(0o755)
        (self.hub / "templates").symlink_to(ROOT / "templates")
        self.ws.mkdir(parents=True)
        (self.home / "git-origin").mkdir()
        (self.home / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n")
        (self.hub / "hub.json").write_text(json.dumps({
            "v": 1, "registry_dir": "projects", "workspace_root": "~/workspace", "git_origin": "~/git-origin",
            "memory_copies": "~/.memory-copies", "services_root": "~/services", "hosts": {"server": "hubbox"},
            "github": {"owner": "o"}, "language": {"ui": "en", "content": "en"}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BRUNNA_", "AGENT_HUB_", "GIT_", "GH_"))}
        self.env = {**env, "HOME": str(self.home), "AGENT_HUB_ROOT": str(self.hub), "AGENT_HUB_PYTHON": sys.executable,
                    "AGENT_HUB_LANG_UI": "en", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                    "REAL_GRANT": str(ROOT / "bin" / "git_push_grant.py"), "PATH": os.environ["PATH"]}

    def onboard(self, *args, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(ROOT / "bin" / "onboard-project.sh"), *map(str, args)], capture_output=True,
                              text=True, env={**self.env, **(env or {})}, timeout=120, cwd=self.home)

    def grant_calls(self) -> list[list[str]]:
        f = self.hub / "grant-calls.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def reg(self, name: str) -> dict:
        return yaml.safe_load((self.hub / "projects" / f"{name}.yaml").read_text())

    def repo(self, name: str, origin: str | None = None) -> pathlib.Path:
        p = self.ws / name
        p.mkdir()
        (p / "a.txt").write_text("a\n")
        e = {**self.env}
        git(p, "init", "-q", env=e)
        git(p, "add", "a.txt", env=e)
        git(p, "commit", "-q", "-m", "first", env=e)
        if origin:
            git(p, "remote", "add", "origin", origin, env=e)
        return p

    def test_push_other_than_none_needs_the_sandbox_and_is_refused_before_anything(self):
        for args in (["--push", "all"], ["--push", "others"], ["--no-sandbox", "--push", "all"],
                     ["--push", "everything", "--sandbox"], ["--sandbox", "--push-email"]):
            with self.subTest(args=args):
                r = self.onboard("--no-remote", *args, self.ws / "x")
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertFalse((self.ws / "x").exists(), "refused before the folder is made")
        r = self.onboard("--no-remote", "--push", "all", self.ws / "x")
        self.assertIn("--push all is for a sandboxed project", r.stderr)
        # the control: none needs no sandbox (connect_project always passes it)
        r = self.onboard("--no-remote", "--push", "none", self.ws / "x")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("enforce", self.reg("x"))

    def test_sandbox_is_registered_and_merged_with_what_the_caller_registers(self):
        r = self.onboard("--no-remote", "--sandbox", self.ws / "alpha",
                         env={"ONBOARD_REGISTRY_EXTRA": json.dumps({"layout_intent": {"split": True}})})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        doc = self.reg("alpha")
        self.assertEqual(doc["enforce"], {"sandbox": True})
        self.assertEqual(doc["layout_intent"], {"split": True})
        self.assertEqual([c[0] for c in self.grant_calls()], ["extra"], "no remote: no push step")
        # the control: without --sandbox, nothing is sandboxed (the non-interactive default)
        r = self.onboard("--no-remote", self.ws / "beta")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("enforce", self.reg("beta"))

    def test_the_push_step_gets_the_answers(self):
        self.repo("gamma", "git@github.com:o/gamma.git")
        r = self.onboard("--sandbox", "--push", "others", "--push-email", "a@b.example", self.ws / "gamma")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.reg("gamma")["enforce"], {"sandbox": True})
        self.assertEqual(self.grant_calls()[-1], ["onboard", "gamma", "--push-email", "a@b.example", "--push", "others"])
        # no --push: the step makes it none with the to-do (tests/test_git_push_grant.py, Onboard)
        self.repo("delta", "git@github.com:o/delta.git")
        r = self.onboard("--sandbox", self.ws / "delta")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.grant_calls()[-1], ["onboard", "delta"])
        # a person's typed answer (connect's question): the step still asks before the gh login adds the key
        self.repo("zeta", "git@github.com:o/zeta.git")
        r = self.onboard("--sandbox", "--push", "all", "--push-confirm", self.ws / "zeta")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.grant_calls()[-1], ["onboard", "zeta", "--confirm", "--push", "all"])

    def test_from_an_agent_session_the_push_is_not_a_persons_choice(self):
        self.repo("eps", "git@github.com:o/eps.git")
        r = self.onboard("--sandbox", "--push", "none", self.ws / "eps",
                         env={"AGENTHUB_CONNECT_ROOTS": str(self.ws.resolve())})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.grant_calls()[-1], ["onboard", "eps", "--push", "none", "--agent"])


loader = importlib.machinery.SourceFileLoader("connect_push_t", str(ROOT / "bin" / "connect.py"))
spec = importlib.util.spec_from_loader("connect_push_t", loader)
connect = importlib.util.module_from_spec(spec)
loader.exec_module(connect)


class Connect(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="conpush-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.code = self.home / "code"
        self.code.mkdir(parents=True)
        self.app = self.code / "app"
        self.app.mkdir()
        (self.app / "README").write_text("x\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"],
                     ["remote", "add", "origin", "git@github.com:o/app.git"]):
            subprocess.run(["git", "-C", str(self.app), *args], check=True, capture_output=True, env=env)
        self.ran: list[list[str]] = []
        for p in (mock.patch.object(connect.hubconf, "projects", lambda: {}),
                  mock.patch.object(connect.hubconf, "MAIN", self.tmp / "hub"),
                  mock.patch.object(connect.hubconf, "instance_home", lambda: self.tmp / "instance"),
                  mock.patch.object(connect.pathlib.Path, "home", classmethod(lambda cls: self.home)),
                  mock.patch.object(connect, "hidden", lambda p: False),
                  mock.patch("llm_ready.unsupported", lambda point: "no model in this test")):
            p.start()
            self.addCleanup(p.stop)

    def run_connect(self, *argv, tty=False, answers=()):
        out, err = io.StringIO(), io.StringIO()
        it = iter(answers)
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty)), \
                mock.patch("builtins.input", lambda prompt="": next(it)), \
                mock.patch.object(connect.subprocess, "run", self.fake_run):
            rc = connect.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def fake_run(self, argv, **kw):
        if any(str(a).endswith("onboard-project.sh") for a in argv):
            self.ran.append([str(a) for a in argv])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if any(str(a).endswith("first-look.py") for a in argv):   # the first summary isn't this test's
            return subprocess.CompletedProcess(argv, 0, "", "")
        return REAL_RUN(argv, **kw)   # the folder's own git (connect's checks)

    def onboard_flags(self) -> list[str]:
        self.assertEqual(len(self.ran), 1, self.ran)
        return self.ran[0][1:-1]

    def test_flags_pass_through(self):
        rc, out, err = self.run_connect(str(self.app), "--yes", "--sandbox", "--push", "others", "--push-email",
                                        "a@b.example")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.onboard_flags(), ["--no-remote", "--sandbox", "--push", "others", "--push-email",
                                                "a@b.example"])

    def test_without_a_person_nothing_is_sandboxed_and_the_push_is_left_to_onboarding(self):
        rc, out, err = self.run_connect(str(self.app), "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.onboard_flags(), ["--no-remote"])
        self.ran.clear()
        rc, out, err = self.run_connect(str(self.app), "--yes", "--sandbox")
        self.assertEqual(self.onboard_flags(), ["--no-remote", "--sandbox"], "onboarding: none and the to-do")

    def test_bad_flags_show_the_usage(self):
        for args in (["--push", "all"], ["--no-sandbox", "--push", "others"], ["--sandbox", "--push", "most"],
                     ["--sandbox", "--push-email", "nope"]):
            with self.subTest(args=args):
                rc, out, err = self.run_connect(str(self.app), "--yes", *args)
                self.assertEqual(rc, 2)
                self.assertIn("--push", err)
                self.assertEqual(self.ran, [])

    def test_a_person_answers_both_questions(self):
        # "Connect it?", "Sandbox it?", the push
        rc, out, err = self.run_connect(str(self.app), tty=True, answers=["", "y", "others"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.onboard_flags(), ["--no-remote", "--sandbox", "--push", "others", "--push-confirm"])
        self.ran.clear()
        rc, out, err = self.run_connect(str(self.app), tty=True, answers=["", "n"])
        self.assertEqual(self.onboard_flags(), ["--no-remote"], "not sandboxed: no push question")
        # no remote, none created: no push question either
        subprocess.run(["git", "-C", str(self.app), "remote", "remove", "origin"], check=True)
        self.ran.clear()
        rc, out, err = self.run_connect(str(self.app), tty=True, answers=["", "y"])
        self.assertEqual(self.onboard_flags(), ["--no-remote", "--sandbox"])

    def test_the_questions_carry_the_criterion_and_the_warning(self):
        self.assertIn("other people's data", connect.SANDBOX_Q)
        self.assertIn("untrusted code or content", connect.SANDBOX_Q)
        self.assertIn("published as soon as it is pushed", connect.PUSH_Q)
        self.assertIn("gh-pages", connect.PUSH_Q)


if __name__ == "__main__":
    unittest.main()
