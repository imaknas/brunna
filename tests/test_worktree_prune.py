"""The worktree cleanup (bin/worktrees.py: auto_prune nightly, prune --yes by a person): builds real worktrees in a
temporary git repo and confirms that only what should be touched is touched. Whose, Work, Age, InUse, Person: with the
facts as project-git-job.py gathers them and the runtime's own worktree folder.

    bin/py -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import hubconf  # noqa: E402
import project_git  # noqa: E402
import runtime_ports  # noqa: E402
import worktrees  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout


class AutoPrune(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".gitignore").write_text(".venv/\nnode_modules/\n.env\n")
        (self.repo / "uv.lock").write_text("lock\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        self.saved = (worktrees.overview, worktrees._cwds_in_use, worktrees._transcript_age_days,
                      hubconf.workspace, hubconf.STATE, worktrees._agent_folders)
        hubconf.STATE = self.tmp / "state"
        hubconf.workspace = lambda name: self.repo
        worktrees._agent_folders = lambda ws: [self.tmp]
        worktrees._transcript_age_days = lambda path: None
        self.busy: list[str] = []
        worktrees._cwds_in_use = lambda: self.busy
        self.wts: list[dict] = []
        worktrees.overview = lambda: {"proj": {"worktrees": self.wts}}

    def tearDown(self):
        (worktrees.overview, worktrees._cwds_in_use, worktrees._transcript_age_days,
         hubconf.workspace, hubconf.STATE, worktrees._agent_folders) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, name, idle, merged=True, venv=True, extra_ignored=None, recipe=True):
        p = self.tmp / name
        git(self.repo, "worktree", "add", "-q", str(p), "-b", name)
        if not recipe:
            (p / "uv.lock").unlink()
            # one more commit on the branch → unmerged; merged is set by the dict below
            git(p, "commit", "-qam", "no lock")
        if venv:
            (p / ".venv").mkdir()
            (p / ".venv" / "x").write_text("x")
        if extra_ignored:
            (p / extra_ignored).write_text("secret")
        self.wts.append({"path": str(p), "branch": name, "merged": merged, "clean": True, "exists": True,
                         "age_days": idle})
        return p

    def branches(self):
        return git(self.repo, "branch", "--format=%(refname:short)").split()

    def test_recent_untouched(self):
        p = self.add("fresh", idle=1)
        r = worktrees.auto_prune(dry_run=False)
        self.assertTrue((p / ".venv").exists())
        self.assertEqual(r["deps"] + r["removed"], [])

    def test_tier1_deletes_venv_keeps_worktree_and_stays_clean(self):
        p = self.add("idle3", idle=3)
        r = worktrees.auto_prune(dry_run=False)
        self.assertFalse((p / ".venv").exists())
        self.assertTrue(p.exists())
        self.assertEqual(git(p, "status", "--porcelain"), "", "the marker must not make the worktree dirty")
        gd = pathlib.Path(git(p, "rev-parse", "--absolute-git-dir").strip())
        self.assertIn("uv sync", (gd / worktrees.MARKER).read_text())
        self.assertEqual(len(r["deps"]), 1)

    def test_tier1_needs_recipe(self):
        p = self.add("norecipe", idle=3, recipe=False)
        worktrees.auto_prune(dry_run=False)
        self.assertTrue((p / ".venv").exists(), "without uv.lock/pyproject.toml .venv is not deleted")

    def test_tier2_removes_worktree_keeps_branch(self):
        p = self.add("old", idle=20)
        r = worktrees.auto_prune(dry_run=False)
        self.assertFalse(p.exists())
        self.assertIn("old", self.branches())
        self.assertEqual(len(r["removed"]), 1)

    def test_session_open_inside_untouched(self):
        """A session idle but still open (perhaps just not back yet): its dependencies are not deleted either."""
        p = self.add("busy", idle=20)
        self.busy = [str(p / "src")]
        r = worktrees.auto_prune(dry_run=False)
        self.assertTrue(p.exists())
        self.assertTrue((p / ".venv").exists())
        self.assertEqual(r["deps"] + r["removed"], [])

    def test_tier2_unknown_ignored_skipped(self):
        p = self.add("secret", idle=20, extra_ignored=".env")
        r = worktrees.auto_prune(dry_run=False)
        self.assertTrue((p / ".env").exists())
        self.assertTrue(p.exists())
        self.assertEqual(len(r["skipped"]), 1)

    def test_unmerged_untouched(self):
        p = self.add("wip", idle=30, merged=False)
        worktrees.auto_prune(dry_run=False)
        self.assertTrue((p / ".venv").exists())
        self.assertTrue(p.exists())

    def test_dry_run_changes_nothing(self):
        p = self.add("dry", idle=20)
        r = worktrees.auto_prune(dry_run=True)
        self.assertTrue((p / ".venv").exists())
        self.assertEqual(len(r["removed"]), 1)
        self.assertFalse((hubconf.STATE / "worktree-prune.json").exists())


PROCESS_CWDS = hubconf.process_cwds   # the real one; RealFacts replaces it
OLD = time.time() - 30 * 86400
AT = f"{int(OLD)} +0000"
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
OLD_COMMIT = {"GIT_COMMITTER_DATE": AT, "GIT_AUTHOR_DATE": AT}


class RealFacts(unittest.TestCase):
    """Each worktree here was made and last touched 30 days ago, is merged and clean, unless the test says otherwise."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = self.tmp / "ws"
        self.git(self.tmp, "init", "-q", "-b", "main", str(self.ws))
        (self.ws / ".gitignore").write_text(".venv/\n.env\n/.claude/\n")
        (self.ws / "uv.lock").write_text("lock\n")
        (self.ws / "a.txt").write_text("a\n")
        self.git(self.ws, "add", ".")
        self.git(self.ws, "commit", "-qm", "init")
        (self.tmp / "no-hub").mkdir()
        self.procs = [(os.getpid(), "me", "/")]
        self.docs = {"proj": {"name": "proj", "paths": {"workspace": str(self.ws)}}}
        for target, attr, new in ((hubconf, "projects", lambda: self.docs),
                                  (hubconf, "workspace", lambda name: pathlib.Path(self.docs[name]["paths"]["workspace"])
                                   if name in self.docs else None),
                                  (hubconf, "ROOT", self.tmp / "no-hub"), (hubconf, "STATE", self.tmp / "state"),
                                  (hubconf, "process_cwds", lambda: self.procs),
                                  (runtime_ports, "installed", lambda: ["claude"]),
                                  (worktrees, "_transcript_age_days", lambda path: None)):
            p = mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {**ENV, "AGENT_HUB_LANG_UI": "en"})
        p.start()
        self.addCleanup(p.stop)

    def git(self, cwd, *args, **env) -> str:
        r = subprocess.run(["git", "-C", str(cwd), *args], env={**ENV, **env}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"git {args}: {r.stderr}")
        return r.stdout.strip()

    def worktree(self, name, at=None, old=True, venv=False) -> pathlib.Path:
        p = at or self.ws / ".claude" / "worktrees" / name
        self.git(self.ws, "worktree", "add", "-q", "-b", name, str(p), "main", **(OLD_COMMIT if old else {}))
        if venv:
            (p / ".venv").mkdir()
            (p / ".venv" / "x").write_text("x")
        if old:   # its own old commit, merged into main
            self.git(p, "commit", "-q", "--allow-empty", "-m", f"work of {name}", **OLD_COMMIT)
            self.git(self.ws, "merge", "-q", "--no-edit", name)
            self.age(p)
        return p

    def gitdir(self, p) -> pathlib.Path:
        return pathlib.Path(self.git(p, "rev-parse", "--absolute-git-dir"))

    def age(self, p):
        gd = self.gitdir(p)
        for f in (gd / "index", gd):
            os.utime(f, (OLD, OLD))

    def branches(self) -> list[str]:
        return self.git(self.ws, "branch", "--format=%(refname:short)").split()

    def auto(self) -> dict:
        return worktrees.auto_prune(dry_run=False)

    def kept(self, p, why=None) -> None:
        r = self.auto()
        self.assertTrue(p.exists(), r)
        self.assertEqual(r["removed"] + r["deps"], [], r)
        if why:
            self.assertTrue(any(why in s for s in r["skipped"]), r["skipped"])

    def manual(self, yes=True) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            worktrees.prune(None, yes)
        return buf.getvalue()


class Whose(RealFacts):
    def test_an_old_worktree_of_an_agent_is_removed_its_branch_kept(self):
        p = self.worktree("w", venv=True)
        r = self.auto()
        self.assertFalse(p.exists(), r)
        self.assertEqual(len(r["removed"]), 1, r)
        self.assertIn("w", self.branches())

    def test_a_worktree_the_user_made_is_not_touched(self):
        p = self.worktree("mine", at=self.tmp / "mine", venv=True)
        self.kept(p)
        self.assertTrue((p / ".venv" / "x").exists())

    def test_with_no_runtime_that_makes_worktrees_nothing_is_touched(self):
        p = self.worktree("w", venv=True)
        with mock.patch.object(runtime_ports, "installed", lambda: ["codex"]):
            self.kept(p)

    def test_the_main_checkout_is_never_a_candidate(self):
        """the registered workspace is a linked worktree, the user's main checkout is on a merged branch"""
        linked = self.tmp / "linked"
        self.git(self.ws, "checkout", "-q", "-b", "side")
        self.git(self.ws, "worktree", "add", "-q", str(linked), "main")
        self.docs["proj"]["paths"]["workspace"] = str(linked)
        (self.ws / ".venv").mkdir()
        (self.ws / ".venv" / "x").write_text("x")
        self.git(self.ws, "commit", "-q", "--allow-empty", "-m", "old", **OLD_COMMIT)
        self.git(linked, "merge", "-q", "side")
        os.utime(self.ws / ".git" / "index", (OLD, OLD))
        os.utime(self.ws / ".git", (OLD, OLD))
        with mock.patch.object(worktrees, "_agent_folders", lambda ws: [self.tmp], create=True):
            r = self.auto()
        self.assertTrue((self.ws / ".venv" / "x").exists(), r)
        self.assertEqual(r["removed"] + r["deps"], [], r)
        self.assertNotIn("side", self.manual(yes=False))

    def test_a_worktree_on_the_base_branch_is_never_a_candidate(self):
        self.git(self.ws, "checkout", "-q", "-b", "elsewhere")
        p = self.ws / ".claude" / "worktrees" / "onmain"
        self.git(self.ws, "worktree", "add", "-q", str(p), "main")
        self.git(p, "commit", "-q", "--allow-empty", "-m", "old", **OLD_COMMIT)
        self.age(p)
        self.kept(p)
        self.manual()
        self.assertTrue(p.exists())


class Work(RealFacts):
    def test_edits_git_is_told_not_to_look_at(self):
        for flag in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(flag):
                p = self.worktree(f"w{flag}")
                self.git(p, "update-index", flag, "a.txt")
                (p / "a.txt").write_text("edited\n")
                self.age(p)
                self.kept(p)
                self.assertIn("a.txt", self.manual())
                self.assertEqual((p / "a.txt").read_text(), "edited\n")
                # the removal asks again itself, whatever was seen before
                r = project_git.job("proj", self.ws, "worktree-remove", self.ws, p)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual((p / "a.txt").read_text(), "edited\n")

    def test_a_file_left_out_of_the_checkout_is_none(self):
        """the platform's own worktrees of a sandboxed project: control files skip-worktree, and not there"""
        p = self.worktree("w")
        self.git(p, "update-index", "--skip-worktree", "a.txt")
        (p / "a.txt").unlink()
        self.age(p)
        r = self.auto()
        self.assertFalse(p.exists(), r)

    def test_untracked_files_whatever_status_is_set_to_show(self):
        self.git(self.ws, "config", "status.showUntrackedFiles", "no")
        p = self.worktree("w")
        (p / "new.txt").write_text("unsaved\n")
        self.kept(p)
        self.manual()
        self.assertTrue((p / "new.txt").exists())
        r = project_git.job("proj", self.ws, "worktree-remove", self.ws, p)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((p / "new.txt").exists())

    def test_ignored_files_whatever_status_is_set_to_show(self):
        for show in ("no", "normal", "all"):
            with self.subTest(show):
                self.git(self.ws, "config", "status.showUntrackedFiles", show)
                p = self.worktree(f"w{show}")
                (p / ".env").write_text("SECRET=1\n")
                r = self.auto()
                self.assertTrue((p / ".env").exists(), r)
                self.assertTrue(any(".env" in s for s in r["skipped"]), r["skipped"])

    def test_dependencies_are_rebuildable_whatever_status_is_set_to_show(self):
        self.git(self.ws, "config", "status.showUntrackedFiles", "all")
        p = self.worktree("w", venv=True)
        r = self.auto()
        self.assertFalse(p.exists(), r)

    def test_a_worktree_git_cant_read_is_not_clean(self):
        p = self.worktree("w", venv=True)
        gd = self.gitdir(p)
        (gd / "index").write_bytes(b"not an index")
        for f in (gd / "index", gd):
            os.utime(f, (OLD, OLD))
        self.kept(p)
        self.assertTrue((p / ".venv" / "x").exists())

    def test_ignored_files_that_cant_be_listed_keep_it(self):
        p = self.worktree("w")
        self.assertEqual(worktrees._unknown_ignored(p, "proj"), [])
        gd = self.gitdir(p)
        (gd / "index").write_bytes(b"not an index")
        self.assertIsNone(worktrees._unknown_ignored(p, "proj"))


class Age(RealFacts):
    def test_recent_work_on_an_old_tip_counts(self):
        p = self.worktree("windex", venv=True)
        os.utime(self.gitdir(p) / "index")
        self.kept(p)
        self.assertTrue((p / ".venv" / "x").exists())
        p = self.worktree("whead", venv=True)   # a commit undone, a checkout: only the reflog says it
        self.git(p, "reset", "-q", "--soft", "HEAD~1")
        self.git(p, "reset", "-q", "--soft", "ORIG_HEAD")
        self.age(p)
        self.kept(p)
        self.assertTrue((p / ".venv" / "x").exists())

    def test_gc_and_maintenance_do_not_make_it_younger(self):
        """they write in every worktree's admin folder, and logs/HEAD anew"""
        p = self.worktree("w")
        self.age(p)
        os.utime(self.gitdir(p) / "logs" / "HEAD", (OLD, OLD))
        for cmd in (("gc", "-q"), ("reflog", "expire", "--all"), ("pack-refs", "--all"), ("worktree", "list")):
            self.git(self.ws, *cmd)
        self.assertGreater((self.gitdir(p) / "logs" / "HEAD").stat().st_mtime, OLD + 86400, "they did")
        r = self.auto()
        self.assertFalse(p.exists(), r)

    def test_a_tool_that_polls_status_does_not_make_it_younger(self):
        for cache in ("false", "true"):
            with self.subTest(untracked_cache=cache):
                p = self.worktree(f"w{cache}")
                self.git(self.ws, "config", "core.untrackedCache", cache)
                for f in (p, *p.rglob("*")):   # left alone: everything older than the index
                    if f.name != ".git":
                        os.utime(f, (OLD - 3600, OLD - 3600))
                self.git(p, "status")          # the first look after that may note it
                self.age(p)
                for _i in range(3):
                    self.git(p, "status")
                r = self.auto()
                self.assertFalse(p.exists(), r)

    def test_an_expired_or_missing_reflog_says_nothing(self):
        p = self.worktree("w")
        (self.gitdir(p) / "logs" / "HEAD").write_text("")
        self.age(p)
        q = self.worktree("q")
        (self.gitdir(q) / "logs" / "HEAD").unlink()
        self.age(q)
        r = self.auto()
        self.assertEqual((p.exists(), q.exists()), (False, False), r)

    def test_looking_does_not_make_it_younger(self):
        """the status page looks every tick"""
        p = self.worktree("w")
        os.utime(p / "a.txt", (OLD, OLD))   # other than what the index noted: a plain status writes the index anew
        self.age(p)
        worktrees.overview()
        worktrees._unknown_ignored(p, "proj")
        self.assertEqual(int((self.gitdir(p) / "index").stat().st_mtime), int(OLD))

    def test_deleting_its_dependencies_does_not_make_it_younger(self):
        p = self.worktree("w", venv=True)
        r = project_git.job("proj", p, "remove-dep", p, ".venv", worktrees.MARKER, "line")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("line", (self.gitdir(p) / worktrees.MARKER).read_text())
        r = self.auto()
        self.assertFalse(p.exists(), r)


class InUse(RealFacts):
    def test_not_knowing_who_is_where_touches_nothing(self):
        p = self.worktree("w", venv=True)
        for procs in ([], [(1, "init", "/")]):   # failed outright; a listing without even this process
            with self.subTest(procs):
                self.procs = procs
                self.kept(p)
                self.assertTrue(any("none was touched" in n for n in self.auto()["notes"]))
                self.assertTrue((p / ".venv" / "x").exists())
                out = self.manual()
                self.assertTrue(p.exists())
                self.assertIn("w", self.branches())
                self.assertIn("could not be found out", out)

    def test_a_process_in_it_keeps_it(self):
        p = self.worktree("w", venv=True)
        self.procs.append((4242, "editor", str(p / "src")))
        self.kept(p)
        out = self.manual()
        self.assertTrue(p.exists())
        self.assertIn("editor(4242)", out)

    def test_one_that_came_in_since_the_first_look_keeps_it(self):
        p = self.worktree("w")
        looks = []

        def procs():
            looks.append(1)
            return self.procs + ([(4242, "editor", str(p))] if len(looks) > 1 else [])
        with mock.patch.object(hubconf, "process_cwds", procs):
            self.kept(p)

    def test_the_job_fails_when_it_could_not_look(self):
        """so the scheduler alerts it, as any job that fails"""
        import runpy
        for unknown, code in ((True, 1), (False, 0)):
            res = {"dry_run": False, "deps": [], "removed": [], "skipped": [], "notes": ["n1"], "freed_mb": 0.0,
                   **({"in_use_unknown": True} if unknown else {})}
            buf = io.StringIO()
            with mock.patch.object(worktrees, "auto_prune", lambda dry_run: res), contextlib.redirect_stdout(buf), \
                    mock.patch.object(hubconf, "prune_private_tmp", lambda dry_run: {"removed": [], "cant_tell": [],
                                                                                     "not_removable": [], "unreadable": None}), \
                    mock.patch.object(sys, "argv", ["prune-worktrees", "--auto"]), self.assertRaises(SystemExit) as e:
                runpy.run_path(str(ROOT / "bin" / "prune-worktrees"), run_name="__main__")
            self.assertEqual(e.exception.code, code)
            self.assertIn("n1", buf.getvalue())

    def test_the_status_page_says_it(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("gen_status_page_prune", ROOT / "bin" / "gen-status-page.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.procs = []
        self.worktree("w")
        self.auto()
        line = m.prune_line()
        self.assertIn("could not be found out", line)
        self.assertNotIn("rebuildable", line)

    def test_no_runtime_with_worktree_folders_is_said(self):
        self.worktree("w")
        with mock.patch.object(runtime_ports, "installed", lambda: ["codex"]):
            self.assertTrue(any("cleaned up by itself" in n for n in self.auto()["notes"]))
        self.assertFalse(any("cleaned up by itself" in n for n in self.auto()["notes"]))

    def test_a_name_lsof_spells_in_escapes_is_the_folder(self):
        """without a UTF-8 locale (a service's environment) lsof writes a name's other bytes as \\xNN"""
        lsof = self.tmp / "fakebin" / "lsof"
        lsof.parent.mkdir()
        lsof.write_text("#!/bin/sh\nprintf '%s\\n' 'p7' 'csh' 'fcwd' 'n/tmp/\\xe6\\x9c\\x83/w' "
                        "'p8' 'csh' 'fcwd' 'n/tmp/q\\\\x41b'\n")
        lsof.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{lsof.parent}:/usr/bin:/bin"}):
            self.assertEqual(PROCESS_CWDS(), [(7, "sh", "/tmp/\u6703/w"), (8, "sh", "/tmp/q\\x41b")])


class Person(RealFacts):
    def test_ignored_files_keep_it_and_are_named(self):
        p = self.worktree("w")
        (p / ".env").write_text("SECRET=1\n")
        for yes in (False, True):
            out = self.manual(yes)
            self.assertIn(".env", out)
            self.assertNotIn("Can delete", out)
            self.assertNotIn("Deleting", out)
        self.assertTrue((p / ".env").exists())
        self.assertIn("w", self.branches())

    def session_worktree(self, name) -> pathlib.Path:
        """one a session of a project that isn't sandboxed started in: the links, and the settings its start writes"""
        import runtime_claude
        if not (self.ws / ".git" / "info" / "exclude").exists() or ".agent-memory" not in (
                self.ws / ".git" / "info" / "exclude").read_text():
            (self.ws / ".gitignore").write_text(".venv/\n.env\n")
            self.git(self.ws, "commit", "-qam", "tool folder not ignored")
            (self.ws / ".git" / "info").mkdir(exist_ok=True)
            (self.ws / ".git" / "info" / "exclude").write_text(
                ".agent-memory\nCLAUDE.local.md\n.claude/settings.local.json\n.claude/worktrees/\n")
            (self.ws / ".agent-memory").mkdir()
            (self.ws / ".agent-memory" / "note.md").write_text("n\n")
            self.main_settings({"permissions": {"allow": ["Bash(make *)"], "deny": ["Read(./.env)"]}})
        p = self.worktree(name)
        (p / ".agent-memory").symlink_to(self.ws / ".agent-memory")
        (p / "CLAUDE.local.md").symlink_to(self.ws / "CLAUDE.local.md")
        runtime_claude.install_worktree(p, self.ws)
        return p

    def main_settings(self, settings):
        import runtime_claude
        f = runtime_claude.settings_path(self.ws)
        f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps(runtime_claude.install_hooks(settings)))

    def settings(self, p, change):
        import runtime_claude
        f = runtime_claude.settings_path(p)
        d = json.loads(f.read_text())
        change(d)
        f.write_text(json.dumps(d))

    def test_the_nightly_cleanup_keeps_a_worktree_a_session_started_in(self):
        """its settings file, which the nightly doesn't look into; with only the links (a runtime that writes no file) it goes"""
        import runtime_claude
        p = self.session_worktree("w")
        self.kept(p, ".claude")
        self.assertTrue(runtime_claude.settings_path(p).exists())
        runtime_claude.settings_path(p).unlink()
        runtime_claude.settings_path(p).parent.rmdir()
        r = self.auto()
        self.assertFalse(p.exists(), r)
        self.assertEqual((self.ws / ".agent-memory" / "note.md").read_text(), "n\n")

    def test_a_settings_file_that_is_only_stale_is_no_reason(self):
        """main's settings gained something since, and a release changed the platform's hooks: the file is not what a start
        would write now, and still holds nothing of the user's"""
        p = self.session_worktree("w")

        def older(d):
            hooks = d["hooks"]
            first = next(iter(hooks))
            now = hooks[first][0]["hooks"][0]["command"]
            before = (f"[ -x {hubconf.hub_bin('hub-event')} ] && {hubconf.sh_py(hubconf.MAIN / 'bin' / 'hub-event')} "
                      f"--runtime claude some_older_event || true")   # what it was up to 0.21
            hooks[first] = [{"hooks": [{"type": "command", "command": c}]}
                            for c in (before, now.replace("session_start", "some_older_event"))]
            d["permissions"]["additionalDirectories"] = ["/where/it/was/.agent-memory"]
        self.settings(p, older)
        self.main_settings({"permissions": {"allow": ["Bash(make *)", "Bash(ls)"], "deny": ["Read(./.env)"]}, "model": "x"})
        out = self.manual()
        self.assertFalse(p.exists(), out)

    def test_what_the_user_set_there_keeps_it_and_is_named(self):
        import runtime_claude
        for key, change in (("model", lambda d: d.update(model="y")),
                            ("permissions.allow", lambda d: d["permissions"]["allow"].append("Bash(mine)")),
                            ("permissions.additionalDirectories",
                             lambda d: d["permissions"].setdefault("additionalDirectories", []).append("/data/shared")),
                            ("env.X", lambda d: d.update(env={"X": "1"})),
                            ("hooks.Stop", lambda d: d["hooks"].setdefault("Stop", []).append(
                                {"hooks": [{"type": "command", "command": "say done"}]}))):
            with self.subTest(key):
                p = self.session_worktree("w" + key.replace(".", "-"))
                self.settings(p, change)
                out = self.manual()
                self.assertTrue(p.exists())
                self.assertIn(f"settings.local.json ({key})", out)
                self.kept(p, ".claude")
                self.assertTrue(runtime_claude.settings_path(p).exists())

    def test_a_settings_file_that_cant_be_read_is_the_users(self):
        import runtime_claude
        p = self.session_worktree("w")
        runtime_claude.settings_path(p).write_text("{ not json")
        out = self.manual()
        self.assertTrue(p.exists())
        self.assertIn(".claude", out)

    def test_a_plain_file_by_such_a_name_is_the_users(self):
        p = self.worktree("w")
        (self.ws / ".git" / "info").mkdir(exist_ok=True)
        (self.ws / ".git" / "info" / "exclude").write_text("CLAUDE.local.md\n")
        (p / "CLAUDE.local.md").write_text("mine\n")
        self.kept(p, "CLAUDE.local.md")
        self.assertIn("CLAUDE.local.md", self.manual())
        self.assertTrue((p / "CLAUDE.local.md").exists())

    def test_it_says_why_the_others_stay(self):
        dirty = self.worktree("dirty")
        (dirty / "a.txt").write_text("edited\n")
        gone = self.worktree("gone")
        shutil.move(gone, self.tmp / "moved")
        out = self.manual()
        self.assertIn("proj dirty", out)
        self.assertIn("proj gone", out)
        self.assertIn(str(gone), self.git(self.ws, "worktree", "list", "--porcelain"))

    def test_the_rest_is_deleted_with_its_branch(self):
        p = self.worktree("w", venv=True)
        mine = self.worktree("mine", at=self.tmp / "mine")
        self.manual()
        self.assertFalse(p.exists())
        self.assertFalse(mine.exists(), "a person may clean up their own")
        self.assertEqual(self.branches(), ["main"])

    def test_a_branch_stays_when_its_worktree_could_not_be_removed(self):
        p = self.worktree("w")
        self.git(self.ws, "worktree", "lock", str(p))
        out = self.manual()
        self.assertTrue(p.exists())
        self.assertIn("locked", out)
        self.assertIn("w", self.branches())


class OnlyItsOwnEntry(RealFacts):
    """Unregistering one worktree leaves the others' entries, also of one whose folder isn't there right now (an
    unmounted disk)."""

    def setUp(self):
        super().setUp()
        (self.tmp / "disk").mkdir()
        self.away = self.worktree("away", at=self.tmp / "disk" / "away", old=False)
        shutil.move(self.tmp / "disk", self.tmp / "unmounted")

    def listed(self) -> list[str]:
        return [line[9:] for line in self.git(self.ws, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    def back(self):
        self.assertEqual(self.listed(), [str(self.ws), str(self.away)])
        shutil.move(self.tmp / "unmounted", self.tmp / "disk")
        self.assertEqual(self.git(self.away, "status", "--porcelain"), "")

    def test_removing_a_session_worktree(self):
        p = self.worktree("w", old=False)
        r = project_git.job("proj", self.ws, "session-worktree-remove", self.ws, p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(p.exists())
        self.back()

    def test_undoing_one_whose_folder_is_gone(self):
        p = self.worktree("w", old=False)
        shutil.rmtree(p)
        r = project_git.job("proj", self.ws, "session-worktree-undo", self.ws, p, "w", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("w", self.branches())
        self.back()

    def test_undoing_one_that_was_never_registered(self):
        p = self.ws / ".claude" / "worktrees" / "w"
        r = project_git.job("proj", self.ws, "session-worktree-undo", self.ws, p, "w", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.back()

    def test_undoing_one_whose_place_is_a_link_to_another_worktree(self):
        p, other = self.ws / ".claude" / "worktrees" / "w", self.worktree("other", old=False)
        (other / "work.txt").write_text("unsaved\n")
        p.symlink_to(other)
        project_git.job("proj", self.ws, "session-worktree-undo", self.ws, p, "w", "0")
        self.assertTrue((other / "work.txt").exists())
        self.assertEqual(self.listed(), [str(self.ws), str(self.away), str(other)])

    def test_a_make_that_fails(self):
        self.git(self.ws, "config", "filter.bad.smudge", "false")
        self.git(self.ws, "config", "filter.bad.required", "true")
        (self.ws / ".git" / "info").mkdir(exist_ok=True)
        (self.ws / ".git" / "info" / "attributes").write_text("a.txt filter=bad\n")
        p = self.ws / ".claude" / "worktrees" / "w"
        r = project_git.job("proj", self.ws, "session-worktree", self.ws, p, "w", "{}")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertFalse(p.exists())
        self.assertNotIn("w", self.branches())
        (self.ws / ".git" / "info" / "attributes").unlink()
        self.back()


if __name__ == "__main__":
    unittest.main()
