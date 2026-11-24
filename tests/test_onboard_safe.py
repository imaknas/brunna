"""What connecting a folder must never do: commit or publish anything of the user's, make a repo inside another one, take over
another project's notes; and a run that stopped is finished by running it again.

    bin/py -m unittest tests.test_onboard_safe
"""
import json
import pathlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import test_connect  # noqa: E402
import test_onboard_push  # noqa: E402

git = test_onboard_push.git

setUpModule, tearDownModule = test_connect.setUpModule, test_connect.tearDownModule


class Case(unittest.TestCase):
    setUp = test_onboard_push.Onboard.setUp
    onboard, reg, repo = test_onboard_push.Onboard.onboard, test_onboard_push.Onboard.reg, test_onboard_push.Onboard.repo

    def folder(self, name: str, **files) -> pathlib.Path:
        p = self.ws / name
        p.mkdir()
        for n, text in (files or {"a.txt": "a\n"}).items():
            (p / n).parent.mkdir(parents=True, exist_ok=True)
            (p / n).write_text(text)
        return p

    def git_ok(self, cwd, *args) -> bool:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, env=self.env).returncode == 0


class FirstCommit(Case):
    def empty_first_commit(self, p):
        self.assertEqual(git(p, "log", "--format=%s", env=self.env).splitlines(), ["chore: initial commit"])
        self.assertEqual(git(p, "ls-tree", "-r", "HEAD", env=self.env), "")

    def test_it_holds_nothing_of_the_users(self):
        for had_git in (False, True):
            with self.subTest(had_git=had_git):
                p = self.folder(f"shop{had_git}", **{"a.txt": "a\n", ".env": "A=1\n", "sub/deploy.key": "k\n"})
                git(self.tmp, "init", "-q", str(p / "nested"), env=self.env)   # a repo of its own with no commits
                (p / "nested" / "x").write_text("x")
                if had_git:
                    git(p, "init", "-q", env=self.env)
                before = {f: f.read_bytes() for f in p.rglob("*") if f.is_file() and ".git" not in f.parts}
                r = self.onboard("--no-remote", p)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.empty_first_commit(p)
                self.assertEqual({f: f.read_bytes() for f in before}, before)
                self.assertFalse((p / "README.md").exists())
                self.assertEqual(git(p, "status", "--porcelain", env=self.env).splitlines(),
                                 ["?? .env", "?? a.txt", "?? nested/", "?? sub/"])
                self.assertEqual(len([x for x in r.stdout.splitlines() if "not committed" in x]), 1)

    def test_an_empty_folder(self):
        r = self.onboard("--no-remote", self.ws / "fresh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.empty_first_commit(self.ws / "fresh")
        self.assertNotIn("not committed", r.stdout)
        self.assertFalse((self.ws / "fresh" / "README.md").exists())

    def test_what_is_staged_stays_staged(self):
        p = self.folder("shop", **{"a.txt": "a\n", "b.txt": "b\n"})
        git(p, "init", "-q", env=self.env)
        git(p, "add", "a.txt", env=self.env)
        self.assertEqual(self.onboard("--no-remote", p).returncode, 0)
        self.empty_first_commit(p)
        self.assertEqual(git(p, "status", "--porcelain", env=self.env).splitlines(), ["A  a.txt", "?? b.txt"])

    def test_the_folders_hooks_dont_run(self):
        p = self.folder("shop")
        git(p, "init", "-q", env=self.env)
        for h in ("pre-commit", "commit-msg", "post-commit"):
            hook = p / ".git" / "hooks" / h
            hook.parent.mkdir(exist_ok=True)
            hook.write_text(f"#!/bin/sh\ntouch {self.tmp}/ran\nexit 1\n")
            hook.chmod(0o755)
        self.assertEqual(self.onboard("--no-remote", p).returncode, 0)
        self.assertFalse((self.tmp / "ran").exists())

    def test_the_agents_files_of_this_machine_stay_out_of_the_next_ones(self):
        p = self.folder("shop", **{"a.txt": "a\n", "CLAUDE.local.md": "mine\n", "part/CLAUDE.local.md": "mine\n",
                                   ".claude/settings.local.json": "{}", "part/.claude/settings.local.json": "{}",
                                   ".claude/settings.json": "{}"})
        git(p, "init", "-q", env=self.env)
        (p / ".git" / "info").mkdir(exist_ok=True)
        (p / ".git" / "info" / "exclude").write_text("scratch")   # no line end: their pattern stays one
        (p / "scratch").write_text("x")
        self.assertEqual(self.onboard("--no-remote", p).returncode, 0)
        git(p, "add", "-A", env=self.env)
        self.assertEqual(git(p, "diff", "--cached", "--name-only", env=self.env).split(), [".claude/settings.json", "a.txt"])

    def test_a_repo_whose_files_are_elsewhere_is_refused(self):
        p, other = self.repo("shop"), self.tmp / "other"
        other.mkdir()
        git(p, "config", "core.worktree", str(other), env=self.env)
        r = self.onboard("--no-remote", p)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(str(other), r.stderr)
        self.assertEqual(list(other.iterdir()), [])
        self.assertFalse((p / ".agent-memory").exists())

    def test_a_branch_with_nothing_on_it_beside_others_isnt_committed_to(self):
        for how in ("orphan", "fetched"):
            with self.subTest(how=how):
                p = self.repo(how)
                if how == "orphan":
                    git(p, "switch", "-q", "--orphan", "drafts", env=self.env)
                else:
                    other = self.tmp / "fetched2"
                    git(self.tmp, "init", "-q", str(other), env=self.env)
                    git(other, "fetch", "-q", str(p), "main:refs/remotes/up/main", env=self.env)
                    p = other
                (p / "draft.txt").write_text("not ready\n")
                refs = git(p, "for-each-ref", env=self.env)
                r = self.onboard("--no-remote", p)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("other branches or tags", r.stderr)
                self.assertFalse(self.git_ok(p, "rev-parse", "HEAD"))
                self.assertEqual(git(p, "for-each-ref", env=self.env), refs)

    def test_a_folder_inside_a_repo_is_refused_naming_it(self):
        outer = self.repo("outer")
        (outer / "part").mkdir()
        (outer / "part" / "a.txt").write_text("a\n")
        for folder in (outer / "part", outer / "not-yet"):
            r = self.onboard("--no-remote", folder)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn(str(outer), r.stderr)
        self.assertFalse((outer / "part" / ".git").exists())
        self.assertFalse((outer / "not-yet").exists())
        self.assertIn(f"git init {outer / 'not-yet'}", r.stderr)
        git(outer / "part", "init", "-q", env=self.env)   # a project of its own, said so
        self.assertEqual(self.onboard("--no-remote", outer / "part").returncode, 0)

    def test_a_home_folder_kept_in_git_is_no_projects_parent(self):
        git(self.home, "init", "-q", env=self.env)
        p = self.folder("shop")
        for folder in (p, self.ws / "not-yet"):
            r = self.onboard("--no-remote", folder)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((folder / ".git" / "HEAD").exists())


class Names(Case):
    def test_one_that_cant_be_a_projects_is_refused_before_anything_is_made(self):
        for name in ("my app", "it's", 'a"b', "a$b", "a;b", "-a", "a*b", "a`b"):
            with self.subTest(name=name):
                r = self.onboard("--no-remote", self.ws / name)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("rename the folder", r.stderr)
                self.assertFalse((self.ws / name).exists())
        self.assertEqual(list((self.home / "git-origin").iterdir()), [])
        for name in ("c++", "app_2.0", "Über"):   # these can
            self.assertEqual(self.onboard("--no-remote", self.ws / name).returncode, 0, name)
        self.assertEqual(self.onboard("--no-remote", self.ws / "a folder" / "app").returncode, 0, "what it is in has any name")

    def test_a_git_origin_with_a_space(self):
        cfg = self.hub / "hub.json"
        cfg.write_text(json.dumps({**json.loads(cfg.read_text()), "git_origin": "~/git origin"}))
        (self.home / "git origin").mkdir()
        r = self.onboard("--no-remote", self.ws / "shop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.home / "git origin" / "shop-memory.git" / "HEAD").exists())

    def test_a_local_origin_leaves_the_config_valid(self):
        for name, origin in (("shop", str(self.tmp / "up.git")), ("shop2", "ssh://git@example.com/o/r.git"),
                             ("shop3", "git@example.com:o/r.git")):
            r = self.onboard(self.repo(name, origin=origin))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.reg(name)["paths"]["origin"], origin)
        self.assertEqual((self.hub / "mirror-list.txt").read_text().split(), ["git@example.com:o/r.git"])


class IdeaSandbox(Case):
    def test_its_own_readme_is_its_first_commit(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "create-idea-sandbox.py"), "idea-5", "give it a try"], env=self.env,
                           capture_output=True, text=True, timeout=120, cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ws = self.ws / "idea-5"
        self.assertEqual(git(ws, "ls-files", env=self.env).split(), ["README.md"])
        self.assertEqual(git(ws, "log", "--format=%s", env=self.env).splitlines(), ["chore: initial commit"])


class Published(Case):
    def setUp(self):
        Case.setUp(self)
        fake = self.tmp / "fakebin"
        fake.mkdir()
        self.log = self.tmp / "gh.log"
        (fake / "gh").write_text('#!/bin/sh\necho "gh $*"\necho "$*" >> "$GH_LOG"\n'
                                 'mkdir -p "$REMOTES/o"; git init -q --bare "$REMOTES/$3.git"\n'
                                 'git remote add origin "git@github.com:$3.git"\n')
        (fake / "gh").chmod(0o755)
        with open(self.home / ".gitconfig", "a") as f:
            f.write(f'[url "{self.tmp}/remotes/"]\n\tinsteadOf = git@github.com:\n')
        self.env |= {"PATH": f"{fake}:{self.env['PATH']}", "GH_LOG": str(self.log), "REMOTES": str(self.tmp / "remotes")}

    def test_nothing_of_the_users_is_published(self):
        p = self.folder("shop", **{"a.txt": "a\n", ".env": "A=1\n"})
        r = self.onboard(p)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        there = self.tmp / "remotes" / "o" / "shop.git"
        self.assertEqual(git(there, "log", "--format=%s", "main", env=self.env).splitlines(), ["chore: initial commit"])
        self.assertEqual(git(there, "ls-tree", "-r", "main", env=self.env), "")
        self.assertFalse(self.git_ok(p, "config", "--get", "brunna.creating"))

    def test_a_repo_that_couldnt_be_made_is_no_repo_to_push_to(self):
        gh = self.tmp / "fakebin" / "gh"
        ok = gh.read_text()
        gh.write_text("#!/bin/sh\necho no >&2\nexit 1\n")
        p = self.folder("shop")
        r = self.onboard(p)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.git_ok(p, "config", "--get", "brunna.creating"))
        gh.write_text(ok)
        self.assertEqual(self.onboard(p).returncode, 0)

    def test_a_run_killed_between_the_repo_and_the_push_is_never_done_without_it(self):
        gh = self.tmp / "fakebin" / "gh"
        gh.write_text(gh.read_text() + 'kill -9 $PPID\n')
        p = self.folder("shop")
        self.assertNotEqual(self.onboard(p).returncode, 0)
        for _again in range(2):
            r = self.onboard(p)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertNotIn("Done", r.stdout)
            self.assertIn(f"git -C {p} push -u origin main", r.stderr)
        git(p, "push", "-q", "-u", "origin", "main", env=self.env)
        r = self.onboard(p)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.git_ok(p, "config", "--get", "brunna.creating"))

    def test_a_push_that_failed_says_how_to_go_on(self):
        p = self.folder("shop")
        hooks = self.tmp / "remotes" / "o" / "shop.git"
        self.env["REMOTES"] = str(self.tmp / "nowhere")
        r = self.onboard(p)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"git -C {p} push -u origin main", r.stderr)
        git(self.home, "init", "-q", "--bare", str(hooks), env=self.env)
        git(p, "push", "-q", "-u", "origin", "main", env=self.env)
        self.assertEqual(self.onboard(p).returncode, 0)
        self.assertEqual(len(self.log.read_text().splitlines()), 1, "not made twice")


class Copied(Case):
    def test_a_copy_of_a_project_never_takes_over_the_originals_notes(self):
        p = self.folder("shop")
        self.assertEqual(self.onboard("--no-remote", p).returncode, 0)
        url, head = git(p, "remote", "get-url", "memory", env=self.env), git(p, "rev-parse", "agent-memory", env=self.env)
        copy = self.ws / "shop2"
        subprocess.run(["cp", "-a", str(p), str(copy)], check=True)
        r = self.onboard("--no-remote", copy)

        def the_original_is_as_it_was():
            self.assertEqual(git(p, "remote", "get-url", "memory", env=self.env), url, "the original's notes go elsewhere")
            self.assertEqual(git(p, "rev-parse", "agent-memory", env=self.env), head)
            git(p / ".agent-memory", "status", env=self.env)
            self.assertEqual(git(self.home / "git-origin" / "shop-memory.git", "rev-parse", "agent-memory", env=self.env), head)

        the_original_is_as_it_was()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.hub / "projects" / "shop2.yaml").exists())
        commands = r.stderr.strip().splitlines()[-1].strip()
        self.assertIn("rm -rf", commands)
        subprocess.run(commands, shell=True, check=True, env=self.env, cwd=self.home)
        the_original_is_as_it_was()
        r = self.onboard("--no-remote", copy)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        the_original_is_as_it_was()
        self.assertEqual(git(copy, "remote", "get-url", "memory", env=self.env).strip(),
                         str(self.home / "git-origin" / "shop2-memory.git"))
        self.assertNotEqual(git(copy, "rev-parse", "agent-memory", env=self.env), head, "notes of its own")

    def test_a_copy_that_was_repaired_still_isnt_the_original(self):
        p = self.folder("shop")
        self.assertEqual(self.onboard("--no-remote", p).returncode, 0)
        copy = self.ws / "shop2"
        subprocess.run(["cp", "-a", str(p), str(copy)], check=True)
        shutil.rmtree(p)
        self.assertNotEqual(self.onboard("--no-remote", copy).returncode, 0)
        git(copy, "worktree", "repair", ".agent-memory", env=self.env)
        r = self.onboard("--no-remote", copy)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse((self.home / "git-origin" / "shop2-memory.git").exists())

    def test_a_remote_of_the_users_with_the_notes_remotes_name_is_left(self):
        p = self.repo("shop")
        git(p, "remote", "add", "memory", "git@github.com:me/memory.git", env=self.env)
        before = (p / ".git" / "config").read_text()
        r = self.onboard("--no-remote", p)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / ".git" / "config").read_text(), before)
        self.assertFalse((p / ".agent-memory").exists())
        self.assertIn("git@github.com:me/memory.git", r.stderr)
        git(p, "remote", "rename", "memory", "mem", env=self.env)
        self.assertEqual(self.onboard("--no-remote", p).returncode, 0)
        for url in ("git@example.com:git-origin/my-memory.git", "/srv/git-origin/my-memory.git", "other:~/git-origin/my-memory.git"):
            q = self.repo("with-" + url.split(":")[0].strip("/").replace("/", "-").replace("@", "-"))
            git(q, "remote", "add", "memory", url, env=self.env)   # somebody else's
            self.assertNotEqual(self.onboard("--no-remote", q).returncode, 0, url)
        for url in (str(self.home / "git-origin" / "old-memory.git"), "hubbox:~/git-origin/old-memory.git"):
            q = self.repo("from-" + url[0].strip("/"))
            git(q, "remote", "add", "memory", url, env=self.env)   # a copy's, or an earlier connection's
            self.assertEqual(self.onboard("--no-remote", q).returncode, 0, url)

    def test_a_repo_of_the_users_where_the_notes_go_is_left(self):
        bare = self.home / "git-origin" / "shop-memory.git"
        other = self.repo("other")
        git(self.tmp, "clone", "-q", "--bare", str(other), str(bare), env=self.env)
        before = git(bare, "for-each-ref", env=self.env) + (bare / "HEAD").read_text()
        folder = self.home / "git-origin" / "shop2-memory.git"
        folder.mkdir()
        (folder / "mine.txt").write_text("mine")
        for name in ("shop", "shop2"):
            r = self.onboard("--no-remote", self.folder(name))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn(f"{name}-memory.git", r.stderr)
            self.assertFalse((self.ws / name / ".git").exists(), "refused before anything is made")
        self.assertEqual(git(bare, "for-each-ref", env=self.env) + (bare / "HEAD").read_text(), before)
        self.assertEqual([p.name for p in folder.iterdir()], ["mine.txt"])

    def test_a_folder_of_that_name_of_the_users(self):
        p = self.repo("shop")
        (p / ".agent-memory").mkdir()
        (p / ".agent-memory" / "mine.txt").write_text("mine")
        r = self.onboard("--no-remote", p)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((p / ".agent-memory" / "mine.txt").read_text(), "mine")


class Stopped(Case):
    """Stopped at each step, then run again: connected, with one memory."""

    def again(self, p: pathlib.Path):
        r = self.onboard("--no-remote", p)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(p.name, self.reg(p.name)["paths"]["workspace"])
        head = git(p, "rev-parse", "agent-memory", env=self.env)
        self.assertEqual(git(self.home / ".memory-copies" / p.name, "rev-parse", "HEAD", env=self.env), head)
        self.assertEqual(len(git(p, "log", "--oneline", "agent-memory", env=self.env).splitlines()), 1)
        self.assertEqual(git(p, "status", "--porcelain", env=self.env), "?? a.txt\n")
        self.assertEqual([x.name for x in (self.home / ".memory-copies").iterdir()], [p.name], "nothing half-made is left")

    def stopped(self, p, **env):
        r = self.onboard("--no-remote", p, env=env)
        self.assertNotEqual(r.returncode, 0, "the step was to fail: " + r.stdout)

    def test_at_the_first_commit(self):
        p = self.folder("shop")
        cfg = (self.home / ".gitconfig").read_text()
        (self.home / ".gitconfig").write_text("[user]\n\tuseConfigOnly = true\n[init]\n\tdefaultBranch = main\n")
        self.stopped(p)
        (self.home / ".gitconfig").write_text(cfg)
        self.again(p)

    def test_at_the_notes_skeleton(self):
        p = self.folder("shop")
        (self.hub / "templates").unlink()
        self.stopped(p)
        self.assertTrue((p / ".agent-memory").exists(), "its notes folder was made")
        (self.hub / "templates").symlink_to(ROOT / "templates")
        self.again(p)

    def test_at_the_local_setup(self):
        stub = self.hub / "bin" / "sync-local-setup.sh"
        text = stub.read_text()
        for name, fails in (("shop", "exit 1\n"), ("shop2", ': > "${2:-.}/CLAUDE.local.md"; exit 1\n')):
            p = self.folder(name)
            stub.write_text("#!/bin/bash\n" + fails)
            self.stopped(p)
            stub.write_text(text + 'echo CLAUDE.local.md >> "$(git rev-parse --git-common-dir)/info/exclude"\n')
            r = self.onboard("--no-remote", p)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_at_the_librarians_copy(self):
        p = self.folder("shop")
        (self.home / ".memory-copies").write_text("in the way")
        self.stopped(p)
        (self.home / ".memory-copies").unlink()
        self.again(p)

    def test_at_the_registration(self):
        p = self.folder("shop")
        self.stopped(p, ONBOARD_REGISTRY_EXTRA='{"type": 5}')
        self.again(p)

    def test_a_copy_that_is_there_already_is_kept(self):
        p = self.folder("shop")
        self.stopped(p, ONBOARD_REGISTRY_EXTRA='{"type": 5}')
        copy = self.home / ".memory-copies" / "shop"
        (copy / ".git" / "brunna.lock").write_text("held\n")
        self.again(p)
        self.assertTrue((copy / ".git" / "brunna.lock").exists())

    def test_a_copy_of_other_notes_is_left(self):
        p = self.folder("shop")
        self.stopped(p, ONBOARD_REGISTRY_EXTRA='{"type": 5}')
        copy = self.home / ".memory-copies" / "shop"
        git(copy, "commit", "-q", "--allow-empty", "-m", "notes the repo doesn't have", env=self.env)
        head = git(copy, "rev-parse", "HEAD", env=self.env)
        self.stopped(p)
        self.assertEqual(git(copy, "rev-parse", "HEAD", env=self.env), head)

    def test_a_folder_of_the_users_where_the_copy_goes_is_left(self):
        for name in ("shop", "shop2"):
            p = self.folder(name)
            there = self.home / ".memory-copies" / name
            there.mkdir(parents=True)
            (there / "mine.txt").write_text("mine")
            if name == "shop2":   # a checkout of something else
                git(there, "init", "-q", env=self.env)
                git(there, "add", "-A", env=self.env)
                git(there, "commit", "-q", "-m", "mine", env=self.env)
            r = self.onboard("--no-remote", p)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn(name, r.stderr)
            self.assertEqual((there / "mine.txt").read_text(), "mine")
            self.assertFalse((self.hub / "projects" / f"{name}.yaml").exists())

    def test_a_lock_a_stopped_run_left_is_named(self):
        git(self.hub, "init", "-q", env=self.env)
        (self.hub / "bin" / "commit-hub-files.sh").write_text("#!/bin/bash\nexit 1\n")
        lock = self.hub / ".git" / "agent-hub-op.lock"
        r = self.onboard("--no-remote", self.folder("shop"))
        self.assertNotIn(str(lock), r.stderr)
        lock.mkdir()
        r = self.onboard("--no-remote", self.folder("shop2"))
        self.assertIn(str(lock), r.stderr)
        self.assertTrue(lock.exists())

    def test_between_the_registration_and_its_commit(self):
        git(self.hub, "init", "-q", env=self.env)
        git(self.hub, "add", "--", "hub.json", env=self.env)
        git(self.hub, "commit", "-q", "-m", "config", env=self.env)
        p = self.folder("shop")
        stub = self.hub / "bin" / "gen-index-roster.sh"
        stub.write_text("#!/bin/bash\nkill -9 $PPID\n")
        self.stopped(p)
        self.reg("shop")
        stub.write_text("#!/bin/bash\nexit 0\n")
        (self.hub / "bin" / "commit-hub-files.sh").write_text(
            '#!/bin/bash\ncd "$AGENT_HUB_ROOT" && git add -- projects/shop.yaml && git commit -q -m "$1" -- projects/shop.yaml\n')
        self.again(p)
        self.assertTrue(self.git_ok(self.hub, "cat-file", "-e", "HEAD:projects/shop.yaml"))
        self.assertNotEqual(self.onboard("--no-remote", p).returncode, 0, "done: not onboarded twice")

    def test_going_on_after_the_registration_decides_nothing_again(self):
        git(self.hub, "init", "-q", env=self.env)
        git(self.hub, "add", "--", "hub.json", env=self.env)
        git(self.hub, "commit", "-q", "-m", "config", env=self.env)
        (self.hub / "bin" / "commit-hub-files.sh").write_text("#!/bin/bash\nexit 1\n")   # the registration is never committed
        p = self.repo("shop", origin="git@github.com:o/shop.git")
        self.assertEqual(self.onboard("--sandbox", p).returncode, 0)
        reg = self.hub / "projects" / "shop.yaml"
        reg.write_text(reg.read_text().replace("sandbox: true", "sandbox: true\n  git_push: {branches: others}"))
        before, calls = reg.read_text(), len(test_onboard_push.Onboard.grant_calls(self))
        for _again in range(2):
            self.assertEqual(self.onboard(p).returncode, 0)
        self.assertEqual(reg.read_text(), before)
        self.assertEqual([c for c in test_onboard_push.Onboard.grant_calls(self)[calls:] if c[0] == "onboard"], [])

    def test_another_projects_notes_of_the_same_name_are_not_joined(self):
        p = self.folder("shop")
        self.stopped(p, ONBOARD_REGISTRY_EXTRA='{"type": 5}')
        bare = self.home / "git-origin" / "shop-memory.git"
        other = self.repo("other")
        git(other, "push", "-q", "-f", str(bare), "HEAD:refs/heads/agent-memory", env=self.env)
        there = git(bare, "rev-parse", "agent-memory", env=self.env)
        r = self.onboard("--no-remote", p)
        self.assertIn("already has its bare repo", r.stderr, "told apart before anything is done")
        self.assertEqual(git(bare, "rev-parse", "agent-memory", env=self.env), there)
        self.assertFalse((self.hub / "projects" / "shop.yaml").exists())


class Connect(unittest.TestCase):
    setUp, run_connect = test_connect.Connect.setUp, test_connect.Connect.run_connect

    def asked(self, folder, *argv, owner="o"):
        """connect at a terminal: what was printed before each question, and whether onboarding was run"""
        import contextlib
        import io
        c, seen, ran = test_connect.connect, [], []
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(c.sys, "stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch("builtins.input", lambda q: seen.append((q, out.getvalue())) or ""), \
                mock.patch.object(c, "connect_one", lambda *a, **k: ran.append(a[0]) or 0), \
                mock.patch.object(c, "next_step", lambda: ""), \
                mock.patch.object(c.hubconf, "get", lambda k, d=None: owner if k == "github.owner" else d), \
                mock.patch("llm_ready.unsupported", lambda p: "off"), \
                mock.patch.object(c.subprocess, "run", wraps=c.subprocess.run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(c.hubconf, "launch", lambda *a: ["true"]):
                rc = c.main([str(folder), *argv])
        return rc, seen, ran, out.getvalue() + err.getvalue()

    def folder(self, **files):
        p = self.code / "shop"
        p.mkdir()
        for n, text in files.items():
            (p / n).write_text(text)
        return p

    def test_a_folder_with_no_history_is_connected_and_told_its_files_stay(self):
        p = self.folder(**{"a.txt": "a"})
        for had_git in (False, True):
            with self.subTest(had_git=had_git):
                if had_git:
                    subprocess.run(["git", "init", "-q", str(p)], check=True)
                _rc, seen, ran, _out = self.asked(p)
                self.assertEqual(ran, [p])
                self.assertIn("your files stay as they are", next(text for q, text in seen if q.startswith("Connect it?")))

    def test_that_worktree_sessions_dont_see_whats_uncommitted_is_said_once(self):
        _rc, _seen, _ran, out = self.asked(self.folder(**{"a.txt": "a"}))
        self.assertEqual(out.count("sees only what is"), 1)
        (self.code / "shop" / "a.txt").unlink()
        (self.code / "shop" / "CLAUDE.local.md").write_text("of this machine")
        _rc, _seen, _ran, out = self.asked(self.code / "shop")
        self.assertNotIn("sees only what is", out)
        _rc, _seen, _ran, out = self.asked(test_connect.repo(self.code / "app"))
        self.assertNotIn("sees only what is", out)

    def test_a_name_that_cant_be_a_projects_is_refused_before_the_question(self):
        p = self.code / "my app"
        p.mkdir()
        rc, seen, ran, out = self.asked(p)
        self.assertEqual((rc, ran, seen), (2, [], []), out)

    def test_whats_left_after_an_empty_first_commit_isnt_unsaved(self):
        p = test_connect.repo(self.code / "app")
        c = test_connect.connect
        (p / ".claude").mkdir()
        (p / ".claude" / "settings.local.json").write_text("{}")
        self.assertFalse(c.unsaved(p), "this machine's")
        (p / "draft.txt").write_text("x")
        self.assertTrue(c.unsaved(p))
        q = self.folder(**{"a.txt": "a"})
        for a in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", "-C", str(q), *a], check=True)
        self.assertFalse(c.unsaved(q))

    def test_a_folder_inside_a_repo_is_refused_naming_it(self):
        outer = test_connect.repo(self.code / "outer")
        (outer / "part").mkdir()
        (outer / "part" / "a.txt").write_text("a")
        rc, out, err = self.run_connect(str(outer / "part"), "--yes")
        self.assertEqual(rc, 2, out + err)
        self.assertIn(str(outer), err)
        self.assertFalse((outer / "part" / ".git").exists())

    def test_connecting_goes_on_where_it_stopped_after_the_registration(self):
        c = test_connect.connect
        app = test_connect.repo(self.code / "app")
        self.projects = {"app": {"paths": {"workspace": str(app)}}}
        reg = test_connect.repo(self.tmp / "cfg", {"projects/other.yaml": "x: 1\n"}) / "projects"
        with mock.patch.object(c.hubconf, "registry_dir", lambda: reg), \
                mock.patch.object(c, "connect_one", return_value=0) as one:
            self.assertFalse(c.unfinished("app"), "no such entry")
            (reg / "app.yaml").write_text("v: 1\n")
            self.assertTrue(c.unfinished("app"))
            self.assertEqual(self.run_connect(str(app), "--yes")[0], 0)
            self.assertEqual(one.call_count, 1)
            subprocess.run(["git", "-C", str(reg), "add", "app.yaml"], check=True)
            self.assertTrue(c.unfinished("app"), "staged, and the commit refused")
            subprocess.run(["git", "-C", str(reg), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"], check=True)
            self.assertFalse(c.unfinished("app"))
            (reg / "app.yaml").write_text("v: 1\nmine: 1\n")
            self.assertFalse(c.unfinished("app"), "the owner's own edit")
            self.run_connect(str(app), "--yes")
            self.assertEqual(one.call_count, 1)

    def test_git_in_a_folder_that_isnt_trusted_runs_nothing_of_its_own(self):
        import os
        app = test_connect.repo(self.code / "app")
        hook = app / ".git" / "hooks" / "post-index-change"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\necho x >> '{self.tmp}/ran'\n")
        hook.chmod(0o755)
        fsmonitor = self.tmp / "fsmonitor"
        fsmonitor.write_text(f"#!/bin/sh\necho x >> '{self.tmp}/ran-too'\n")
        fsmonitor.chmod(0o755)
        subprocess.run(["git", "-C", str(app), "config", "core.fsmonitor", str(fsmonitor)], check=True)

        def touched():
            os.utime(app / "README", (1, 1))
            return app
        test_connect.connect.unsaved(touched())
        self.assertEqual([x.name for x in self.tmp.glob("ran*")], [])
        subprocess.run(["git", "-C", str(touched()), "status"], capture_output=True)
        self.assertTrue((self.tmp / "ran").exists(), "the control: git on its own runs the hook")
        self.assertTrue((self.tmp / "ran-too").exists(), "and the fsmonitor")

    def test_a_repo_whose_connecting_stopped_before_its_first_commit_isnt_called_unsaved(self):
        p = self.folder(**{"a.txt": "a"})
        subprocess.run(["git", "init", "-q", str(p)], check=True)
        self.assertFalse(test_connect.connect.unsaved(p))


if __name__ == "__main__":
    unittest.main()
