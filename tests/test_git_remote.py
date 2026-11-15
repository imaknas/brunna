"""brunna push (bin/git_remote.py; docs/design/git-broker.md, "Owner-run push, built first (11-15)"): the owner pushes a
sandboxed project's branch from a bare repo the platform owns, never running git in the project's repo.

Throwaway everything: a registry of one stand-in project (hubconf.projects patched), its "remote" a bare repo in the
test's temp folder, the platform's state there too. The owner's git runs under a temp global config holding
safe.bareRepository=explicit (never the user's). The project's job goes through isolation.wrap: a stand-in that runs it
marked as the project (any OS), and on macOS once under the real Seatbelt profile. Every owner-side git call is recorded
at git_remote.run_git, the one place it starts. Each refusal has a control: plain git pushes the same thing.

    bin/py -m unittest tests.test_git_remote
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import git_remote  # noqa: E402
import hubconf  # noqa: E402
import isolation  # noqa: E402
import seatbelt  # noqa: E402

IDENT = {"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.org", "GIT_COMMITTER_NAME": "Dev",
         "GIT_COMMITTER_EMAIL": "dev@example.org"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.gitconfig = self.tmp / "owner-gitconfig"
        self.gitconfig.write_text("[safe]\n\tbareRepository = explicit\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C",
                   AGENT_HUB_LANG_UI="en", **IDENT)   # the texts asserted are the English msgids (and git's own)
        self.calls: list[tuple[list[str], str]] = []
        self.wrapped: list[list[str]] = []
        self.docs: dict = {}
        run_git = git_remote.run_git
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(hubconf, "STATE", self.tmp / "state"),
                  mock.patch.object(hubconf, "projects", lambda: self.docs),
                  mock.patch.object(isolation, "wrap", self.as_project),
                  mock.patch.object(git_remote, "run_git", lambda argv, cwd, env, **kw: (
                      self.calls.append((list(argv), str(cwd))), run_git(argv, cwd, env, **kw))[1])):
            p.start()
            self.addCleanup(p.stop)
        self.remote = self.tmp / "remote.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = self.tmp / "seed"
        self.git(self.tmp, "init", "-q", "-b", "main", str(seed))
        self.commit(seed, "README", "hello\n", "init")
        self.git(seed, "push", "-q", str(self.remote), "main")
        self.ws = self.tmp / "ws" / "proj"
        self.git(self.tmp, "clone", "-q", str(self.remote), str(self.ws))
        self.docs["proj"] = {"name": "proj", "paths": {"workspace": str(self.ws), "origin": str(self.remote)},
                             "enforce": {"sandbox": True}}

    def as_project(self, project, cwd, argv, port=None):
        """Stands in for bin/sandboxed-run: runs the job marked as the project (the canary says whose run it was)."""
        self.wrapped.append(list(argv))
        return ["env", "CANARY_SIDE=project", *argv]

    def git(self, cwd, *args, check=True, env=None) -> str:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
        if check and r.returncode != 0:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r.stdout

    def bare(self, repo, *args) -> str:
        return subprocess.run(["git", "--git-dir", str(repo), *args], capture_output=True, text=True).stdout.strip()

    def commit(self, repo, path, text, msg):
        f = pathlib.Path(repo) / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        self.git(repo, "add", "--", path)
        self.git(repo, "commit", "-q", "-m", msg)
        return self.git(repo, "rev-parse", "HEAD").strip()

    def tip(self, ref="refs/heads/main") -> str:
        return self.bare(self.remote, "rev-parse", "--verify", "-q", ref)

    def push(self, *args, stdin_tty=False, answer=None):
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.Mock(isatty=lambda: stdin_tty)
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), mock.patch("sys.stdin", stdin), \
                mock.patch("builtins.input", lambda prompt="": answer if answer is not None else ""):
            code = git_remote.main(["push", "proj", *args])
        return code, out.getvalue(), err.getvalue()

    def owned(self) -> pathlib.Path:
        return self.tmp / "state" / "git-broker" / "proj" / "remote.git"

    def assert_owner_git_only_in_owned_repos(self):
        """Every owner-side git names a repo the platform owns with --git-dir, from a folder outside the workspace."""
        self.assertTrue(self.calls)
        ws = self.ws.resolve()
        for argv, cwd in self.calls:
            self.assertEqual(argv[:2], ["git", "--git-dir"], argv)
            repo = pathlib.Path(argv[2]).resolve()
            self.assertTrue(repo == self.owned().resolve() or repo.name == "stage.git", argv)
            for p in (repo, pathlib.Path(cwd).resolve()):
                self.assertFalse(p == ws or p.is_relative_to(ws), (argv, cwd))
            self.assertNotIn("-C", argv[:4])

    def assert_untouched(self, before: str, ref="refs/heads/main"):
        self.assertEqual(self.tip(ref), before, "the remote moved")


class Pushes(Base):
    def test_a_fast_forward_lands_and_the_project_repo_is_not_written(self):
        before_tracking = self.git(self.ws, "rev-parse", "refs/remotes/origin/main").strip()
        new = self.commit(self.ws, "a.txt", "a\n", "add a")
        code, out, err = self.push("main", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tip(), new)
        self.assertIn("add a", out)
        self.assertIn("dev@example.org", out)
        self.assertIn("A a.txt", out)
        self.assertEqual(self.git(self.ws, "rev-parse", "refs/remotes/origin/main").strip(), before_tracking,
                         "the project's remote-tracking ref is the project's own fetch's to move")
        self.assertEqual(len(self.wrapped), 1)
        self.assertIn("bundle-branch", self.wrapped[0])
        self.assert_owner_git_only_in_owned_repos()
        log = [json.loads(x) for x in (self.owned().parent / "pushes.jsonl").read_text().splitlines()]
        self.assertEqual((log[-1]["branch"], log[-1]["new"], log[-1]["ok"]), ("main", new, True))
        self.assertEqual(self.bare(self.owned(), "for-each-ref", "refs/brunna", "refs/agent-hub"), "", "incoming ref gone")

    def test_the_checked_out_branch_by_default_and_a_new_branch(self):
        self.git(self.ws, "checkout", "-q", "-b", "feature")
        new = self.commit(self.ws, "f.txt", "f\n", "feature work")
        code, out, err = self.push("--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tip("refs/heads/feature"), new)
        self.assertIn("a new branch", out)

    def test_nothing_to_push(self):
        before = self.tip()
        code, out, _err = self.push("main", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("nothing to push", out)
        self.assert_untouched(before)

    def test_a_ref_that_moves_to_commits_the_remote_has(self):
        """An empty bundle is no "nothing to push": a new branch at a known commit, or an old branch fast-forwarded to
        a commit already on the default branch, still moves the remote's ref."""
        base = self.tip()
        self.git(self.ws, "push", "-q", "origin", "main:feature")
        new = self.commit(self.ws, "a.txt", "a\n", "add a")
        self.assertEqual(self.push("main", "--yes")[0], 0)
        self.git(self.ws, "branch", "-q", "fresh", "main")
        code, _out, err = self.push("fresh", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tip("refs/heads/fresh"), new)
        self.git(self.ws, "branch", "-q", "-f", "feature", "main")
        self.assertEqual(self.tip("refs/heads/feature"), base)
        code, out, err = self.push("feature", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tip("refs/heads/feature"), new)
        self.assertIn("0 commits", out)

    def test_a_branch_behind_the_remote_is_no_fast_forward(self):
        old = self.tip()
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.assertEqual(self.push("main", "--yes")[0], 0)
        self.git(self.ws, "reset", "-q", "--hard", old)
        code, _out, err = self.push("main", "--yes")
        self.assertEqual(code, git_remote.REFUSED, err)
        self.assertIn("not a fast-forward", err)

    def test_the_job_naming_a_commit_the_remote_lacks_is_refused(self):
        before = self.tip()
        lie = lambda project, cwd, argv, port=None: ["sh", "-c", "printf %s " + "ab" * 20 + "; exit 3"]  # noqa: E731
        with mock.patch.object(isolation, "wrap", lie):
            code, _out, err = self.push("main", "--yes")
        self.assertEqual(code, git_remote.REFUSED, err)
        self.assertIn("doesn't have", err)
        self.assert_untouched(before)

    def test_safe_bare_repository_explicit_holds_for_the_owned_repo(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.assertEqual(self.push("main", "--yes")[0], 0)
        # control: the same global config refuses the owned repo entered with -C
        r = subprocess.run(["git", "-C", str(self.owned()), "rev-parse", "HEAD"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("safe.bareRepository", r.stderr)

    def test_interactive_confirmation(self):
        before = self.tip()
        self.commit(self.ws, "a.txt", "a\n", "add a")
        code, _out, err = self.push("main", stdin_tty=True, answer="n")
        self.assertEqual(code, git_remote.REFUSED, err)
        self.assert_untouched(before)
        code, _out, err = self.push("main", stdin_tty=True, answer="y")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(self.tip(), before)

    def test_a_new_author_email_is_flagged(self):
        env = {**os.environ, "GIT_AUTHOR_EMAIL": "someone@else.example"}
        (self.ws / "b.txt").write_text("b\n")
        self.git(self.ws, "add", "b.txt")
        self.git(self.ws, "commit", "-q", "-m", "from elsewhere", env=env)
        code, out, err = self.push("main", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("!! author emails not in the remote's recent history: someone@else.example", out)

    def test_control_characters_from_the_project_are_shown_escaped(self):
        self.commit(self.ws, "a.txt", "a\n", "innocent\x1b[2K\rrepainted")
        code, out, err = self.push("main", "--yes")
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertIn("innocent\\x1b[2K\\rrepainted", out)


class Refusals(Base):
    """Each refusal leaves the remote as it was; the control shows plain git would have pushed it."""

    def refused(self, *args, expect=git_remote.REFUSED, says=None):
        before = {r: self.tip(r) for r in ("refs/heads/main", "refs/heads/feature", "refs/tags/v1")}
        code, _out, err = self.push(*args)
        self.assertEqual(code, expect, err)
        if says:
            self.assertIn(says, err)
        for r, sha in before.items():
            self.assert_untouched(sha, r)
        return err

    def plain_push(self, *args):
        """The control: the owner's plain git, in the workspace, as they would push without brunna."""
        r = subprocess.run(["git", "-C", str(self.ws), "push", "-q", "origin", *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_non_fast_forward(self):
        other = self.tmp / "other"
        self.git(self.tmp, "clone", "-q", str(self.remote), str(other))
        self.commit(other, "o.txt", "o\n", "someone else's")
        self.git(other, "push", "-q", "origin", "main")
        mine = self.commit(self.ws, "m.txt", "m\n", "mine")
        self.refused("main", "--yes", says="not a fast-forward")
        self.plain_push("--force", "main")
        self.assertEqual(self.tip(), mine)

    def test_force_tags_deletes_and_refspecs(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.git(self.ws, "tag", "v1")
        self.git(self.ws, "push", "-q", "origin", "HEAD:refs/heads/feature")
        cases = [("--force", "main"), ("-f", "main"), ("--force-with-lease", "main"), ("--tags",), ("--follow-tags", "main"),
                 ("refs/tags/v1",), ("--delete", "feature"), ("-d", "feature"), (":feature",), ("main:other",),
                 ("+main",), ("--mirror",), ("--all",), ("main", "feature"), ("refs/heads/main",), ("HEAD",)]
        for case in cases:
            with self.subTest(case=case):
                self.calls.clear()
                self.refused(*case, "--yes")
                self.assertEqual(self.calls, [], "refused before any git ran")
        # controls: plain git pushes a tag, deletes a branch, pushes a refspec
        self.plain_push("refs/tags/v1")
        self.assertTrue(self.tip("refs/tags/v1"))
        self.plain_push(":feature")
        self.assertEqual(self.tip("refs/heads/feature"), "")
        self.plain_push("main:other")
        self.assertTrue(self.tip("refs/heads/other"))

    def test_the_default_branch_must_be_known(self):
        empty = self.tmp / "empty.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(empty))
        self.docs["proj"]["paths"]["origin"] = str(empty)
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.refused("main", "--yes", says="default branch can't be told")
        self.assertEqual(self.bare(empty, "for-each-ref"), "")
        # ls-remote failing outright (no such remote) pushes nothing either
        self.docs["proj"]["paths"]["origin"] = str(self.tmp / "missing.git")
        self.refused("main", "--yes", expect=git_remote.FAILED, says="ls-remote")
        # control: plain git pushes the first branch to the empty remote
        r = subprocess.run(["git", "-C", str(self.ws), "push", "-q", str(empty), "main"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_github_change_needs_the_owners_allowance(self):
        new = self.commit(self.ws, ".github/workflows/ci.yml", "on: push\n", "add a workflow")
        self.refused("main", "--yes", says=".github/")
        self.docs["proj"]["enforce"]["git_ci"] = True
        code, out, err = self.push("main", "--yes")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("!! .github/ differs"), out)
        self.assertIn("!!A .github/workflows/ci.yml", out)
        self.assertEqual(self.tip(), new)

    def test_a_github_change_control(self):
        self.commit(self.ws, ".github/workflows/ci.yml", "on: push\n", "add a workflow")
        self.refused("main", "--yes")
        self.plain_push("main")

    def test_a_dot_git_tree_entry_is_refused_by_fsck(self):
        r = subprocess.run(["git", "-C", str(self.ws), "hash-object", "-w", "--stdin"], input="[core]\n\tfsmonitor = x\n",
                           capture_output=True, text=True, check=True)
        blob = r.stdout.strip()
        sub = subprocess.run(["git", "-C", str(self.ws), "mktree"], input=f"100644 blob {blob}\tconfig\n",
                             capture_output=True, text=True, check=True).stdout.strip()
        readme = self.git(self.ws, "rev-parse", "HEAD:README").strip()
        tree = subprocess.run(["git", "-C", str(self.ws), "mktree"],
                              input=f"040000 tree {sub}\t.git\n100644 blob {readme}\tREADME\n",
                              capture_output=True, text=True, check=True).stdout.strip()
        c = self.git(self.ws, "commit-tree", "-p", "HEAD", "-m", "a .git folder in the tree", tree).strip()
        self.git(self.ws, "update-ref", "refs/heads/main", c)
        err = self.refused("main", "--yes", says="object checks")
        self.assertIn("hasDotgit", err)
        self.assertEqual(self.bare(self.owned(), "cat-file", "-t", c), "", "the commit never reached the owned repo")
        self.plain_push("main")   # control: the remote (no fsck of its own) takes it from plain git
        self.assertEqual(self.tip(), c)

    def test_missing_origin(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        for origin in (None, "(native)", "ext::sh -c touch% /tmp/x"):
            with self.subTest(origin=origin):
                self.calls.clear()
                self.docs["proj"]["paths"]["origin"] = origin
                self.refused("main", "--yes")
                self.assertEqual(self.calls, [])
        self.plain_push("main")

    def test_an_origin_inside_a_sandboxed_projects_folder(self):
        self.docs["proj"]["paths"]["origin"] = str(self.ws / ".git")
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.refused("main", "--yes", says="a sandboxed project writes")

    def test_non_interactive_without_yes(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.refused("main", says="no terminal")
        code, _out, err = self.push("main", "--yes")   # control: --yes pushes the same
        self.assertEqual(code, 0, err)

    def test_not_sandboxed(self):
        del self.docs["proj"]["enforce"]
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.refused("main", "--yes", says="not sandboxed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.wrapped, [])


class Canary(Base):
    """What the project plants in its own .git runs as the owner when the owner's git runs in its repo, and never
    through brunna push."""

    def plant(self):
        self.marker = self.tmp / "canary"
        evil = self.tmp / "evil.sh"
        evil.write_text(f'#!/bin/sh\necho "${{CANARY_SIDE:-owner}} $0 $*" >> "{self.marker}"\ncat >/dev/null 2>&1 &\nexit 0\n')
        evil.chmod(0o755)
        for k, v in (("core.fsmonitor", evil), ("core.sshCommand", evil), ("credential.helper", f"!{evil}"),
                     ("gpg.program", evil), ("log.showSignature", "true"), ("diff.external", evil),
                     ("filter.evil.clean", evil), ("filter.evil.smudge", evil), ("core.hooksPath", ".git/hooks")):
            self.git(self.ws, "config", k, str(v))
        (self.ws / ".git" / "info").mkdir(exist_ok=True)
        (self.ws / ".git" / "info" / "attributes").write_text("* filter=evil\n")
        hooks = self.ws / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for h in ("pre-push", "reference-transaction", "post-checkout", "pre-auto-gc", "post-index-change"):
            (hooks / h).write_text(f"#!/bin/sh\n'{evil}' {h}\nexit 0\n")
            (hooks / h).chmod(0o755)

    def owner_lines(self) -> list[str]:
        text = self.marker.read_text() if self.marker.exists() else ""
        return [x for x in text.splitlines() if x.startswith("owner ")]

    def test_planted_hooks_and_config_never_run_as_the_owner(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.plant()
        code, _out, err = self.push("main", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.owner_lines(), [])
        # control: the owner's git run in the workspace runs them (status: fsmonitor; push: pre-push)
        self.commit(self.ws, "b.txt", "b\n", "add b")
        self.git(self.ws, "status", "--porcelain", check=False)
        self.git(self.ws, "push", "-q", "origin", "main", check=False)
        ran = " ".join(self.owner_lines())
        self.assertIn("pre-push", ran)
        self.assertTrue(any("evil.sh" in x for x in self.owner_lines()))


@unittest.skipUnless(seatbelt.can_sandbox() is None, "needs Seatbelt (macOS)")
class UnderSeatbelt(Base):
    """The project's job under the real Seatbelt profile, without and with the git-dir fence (enforce.git: platform):
    brunna push works for a sandboxed project switched on or not."""
    fence = None

    def as_project(self, project, cwd, argv, port=None):
        import policy
        self.wrapped.append(list(argv))
        pol = {"workspace": self.ws, "write": [], "other_projects": [], "deny_read": [], "credentials": [],
               "credential_files": [], "git_fence": policy.git_fence(self.docs["proj"], [self.ws])}
        return [seatbelt.SANDBOX_EXEC, "-p", seatbelt.service_profile(pol, [self.ws]), *argv]

    def check(self):
        new = self.commit(self.ws, "a.txt", "a\n", "add a")
        code, _out, err = self.push("main", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tip(), new)
        self.assert_owner_git_only_in_owned_repos()

    def test_the_bundle_is_made_in_the_project_sandbox(self):
        self.check()

    def test_and_under_the_git_dir_fence(self):
        import policy
        self.docs["proj"]["enforce"]["git"] = "platform"
        self.assertIsNotNone(policy.git_fence(self.docs["proj"], [self.ws]))
        self.check()


class OverHttp(Base):
    """The owner's credential helper over a real transport: git http-backend behind basic auth on 127.0.0.1 (plain
    http; TLS is git's own and checked by hand against a real remote). The helper, in the owner's temp global config,
    records each call; the project's job never reaches it."""

    def setUp(self):
        super().setUp()
        import http.server
        user, password = "owner", "s3cret"
        import base64
        want = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        root = self.tmp
        server_env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                      "GIT_PROJECT_ROOT": str(root), "GIT_HTTP_EXPORT_ALL": "1"}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_one(self):
                if self.headers.get("Authorization") != want:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="t"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path, _q, query = self.path.partition("?")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                env = {**server_env, "REQUEST_METHOD": self.command, "PATH_INFO": path, "QUERY_STRING": query,
                       "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                       "REMOTE_USER": user, "REMOTE_ADDR": "127.0.0.1",
                       "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                       "GIT_PROTOCOL": self.headers.get("Git-Protocol", "")}
                r = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
                head, _sep, payload = r.stdout.partition(b"\r\n\r\n")
                if not _sep:
                    head, _sep, payload = r.stdout.partition(b"\n\n")
                status, headers = 200, []
                for line in head.decode(errors="replace").splitlines():
                    k, _c, v = line.partition(":")
                    if k.lower() == "status":
                        status = int(v.strip().split()[0])
                    elif k:
                        headers.append((k, v.strip()))
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = handle_one

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.helper_log = self.tmp / "helper.log"
        helper = self.tmp / "helper.sh"
        helper.write_text(f'#!/bin/sh\necho "$1 ${{CANARY_SIDE:-owner}} $PWD" >> "{self.helper_log}"\n'
                          f'[ "$1" = get ] && printf "username={user}\\npassword={password}\\n"\nexit 0\n')
        helper.chmod(0o755)
        with self.gitconfig.open("a") as fh:
            fh.write(f"[credential]\n\thelper = {helper}\n[http]\n\tpostBuffer = 104857600\n")
        self.docs["proj"]["paths"]["origin"] = f"http://127.0.0.1:{self.server.server_port}/remote.git"

    def test_the_owners_helper_is_asked_from_the_owned_repo(self):
        new = self.commit(self.ws, "a.txt", "a\n", "add a")
        code, _out, err = self.push("main", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tip(), new)
        calls = self.helper_log.read_text().splitlines()
        self.assertTrue(any(c.startswith("get owner ") for c in calls), calls)
        for c in calls:
            self.assertEqual(c.split()[1], "owner", c)
            self.assertEqual(pathlib.Path(c.split(" ", 2)[2]).resolve(), self.owned().parent.resolve(), c)
        self.assert_owner_git_only_in_owned_repos()


class EndToEnd(unittest.TestCase):
    """`brunna push` as the owner types it, in a throwaway instance: the registry entry read through its index (paths.origin,
    enforce), the project's job through the real bin/sandboxed-run. Skipped where this machine can't sandbox."""

    def test_the_cli(self):
        tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for d in ("home", "config/projects", "data", "secrets", "bus", "state", "ws"):
            (tmp / d).mkdir(parents=True)
        (tmp / "gitconfig").write_text("[safe]\n\tbareRepository = explicit\n")
        (tmp / "config" / "hub.json").write_text(json.dumps({
            "owner": {"name": "alex", "tag": "Alex"}, "bus_dir": str(tmp / "bus"), "registry_dir": "projects",
            "workspace_root": str(tmp / "ws"), "memory_copies": str(tmp / "copies"), "services_root": str(tmp / "svc"),
            "language": {"ui": "en", "content": "en"}}))
        env = {**{k: v for k, v in os.environ.items() if not k.startswith(("BRUNNA_", "AGENT_HUB_", "GIT_"))
                  or k in ("AGENT_HUB_PYTHON", "BRUNNA_PYTHON")},
               "AGENT_HUB_ROOT": str(ROOT), "HOME": str(tmp / "home"), "AGENT_HUB_CONFIG": str(tmp / "config"),
               "AGENT_HUB_DATA": str(tmp / "data"), "AGENT_HUB_SECRETS": str(tmp / "secrets"),
               "AGENT_HUB_STATE": str(tmp / "state"), "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
               "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C", **IDENT}

        def git(cwd, *args):
            subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, text=True, check=True)
        remote, ws, seed = tmp / "remote.git", tmp / "ws" / "proj", tmp / "seed"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        git(tmp, "init", "-q", "-b", "main", str(seed))
        (seed / "README").write_text("r\n")
        git(seed, "add", "README")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", str(remote), "main")
        git(tmp, "clone", "-q", str(remote), str(ws))
        (ws / ".github" / "workflows").mkdir(parents=True)
        (ws / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        git(ws, "add", ".github")
        git(ws, "commit", "-q", "-m", "a workflow")
        (tmp / "config" / "projects" / "proj.yaml").write_text(
            f"v: 1\nname: proj\npaths:\n  workspace: {ws}\n  origin: {remote}\n"
            "enforce:\n  sandbox: true\n  git_ci: true\n")
        r = subprocess.run([sys.executable, str(BIN / "registry.py"), "check"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = subprocess.run([sys.executable, str(BIN / "agenthub"), "push", "proj", "main", "--yes"], env=env,
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        if "the sandbox refused" in r.stderr:
            self.skipTest(f"this machine can't sandbox the project's job: {r.stderr.strip()[-200:]}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        head = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], env=env, capture_output=True, text=True).stdout
        tip = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"], env=env, capture_output=True,
                             text=True).stdout
        self.assertEqual(tip, head)
        self.assertIn("allowed by enforce.git_ci", r.stdout)
        self.assertTrue((tmp / "state" / "git-broker" / "proj" / "remote.git" / "HEAD").is_file())


class Registry(unittest.TestCase):
    """enforce.git_ci: the owner's allowance for a .github/ change, owner-only like the rest of enforce."""

    def test_validation(self):
        import registry
        self.assertEqual(registry.validate_enforce({"enforce": {"sandbox": True, "git_ci": True}}), [])
        self.assertEqual(registry.validate_enforce({"enforce": {"sandbox": True, "git_ci": False}}), [])
        self.assertTrue(registry.validate_enforce({"enforce": {"git_ci": True}}))
        self.assertTrue(registry.validate_enforce({"enforce": {"sandbox": True, "git_ci": "yes"}}))

    def test_a_projects_own_manifest_update_cant_set_it(self):
        import registry
        cur = {"name": "proj", "enforce": {"sandbox": True}}
        said = registry.platform_owned_changes({**cur, "enforce": {"sandbox": True, "git_ci": True}}, cur)
        self.assertTrue(any("enforce" in s for s in said), said)

    def test_push_is_no_session_command(self):
        import policy
        self.assertFalse([c for c in policy.platform_cli() if c.split()[-1] == "push"])
        self.assertNotIn("push", policy.SESSION_SUBCOMMANDS)


if __name__ == "__main__":
    unittest.main()
