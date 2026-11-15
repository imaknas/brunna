"""The platform's git in its own bare repos still works under the owner's safe.bareRepository=explicit (the git broker's
precondition, docs/design/git-broker.md r2 B2). With it set, git refuses a bare repo it finds on its own, so `git -C
<bare>` fails and the memory sync, the nightly backup's bundles and memory-gaps all stopped on 11-14. Every call into a
bare repo names it with --git-dir. The owner's global config is never touched: GIT_CONFIG_GLOBAL points at a temp file.
memory_paths' check of a bundle's commits (fetch_bundle, doctor) was missed the first time, so the sandboxed projects'
memory sync went on failing ("can't be read"); it runs here end to end, with backup's verify and weekly-reflect's count.
"""
import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import gitdirs  # noqa: E402
import memory_paths  # noqa: E402
import project_git  # noqa: E402

# the files whose git runs in bare repos the platform owns (memory bare repos, ~/git-origin, upgrade-idea's own.git)
BARE_CALLERS = ["bin/project_git.py", "bin/backup.py", "bin/check-memory-gaps.py", "bin/upgrade-idea.py",
                "bin/recover-project.sh", "bin/memory_paths.py", "bin/weekly-reflect.py", "bin/rename-project.sh"]
RULES = ("RULESFILE.md",)   # memory_paths' rules-file names, given as the sandboxed sync job is given them


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ExplicitBare(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cfg = self.tmp / "gitconfig"
        cfg.write_text("[safe]\n\tbareRepository = explicit\n")
        env = {"GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bare = self.tmp / "x-memory.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "agent-memory", str(self.bare)], check=True)
        work = self.tmp / "work"
        subprocess.run(["git", "init", "-q", "-b", "agent-memory", str(work)], check=True)
        (work / "memory").mkdir()
        (work / "memory" / "2026-11-15.md").write_text("note\n")
        git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "note"], check=True)
        subprocess.run([*git, "push", "-q", str(self.bare), "agent-memory"], check=True)
        self.work, self.git = work, git
        patcher = mock.patch.object(memory_paths, "RULES_NAMES", RULES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, rel: str, text: str = "x\n") -> str:
        f = self.work / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        subprocess.run([*self.git, "add", "-A"], check=True)
        subprocess.run([*self.git, "commit", "-q", "-m", rel], check=True)
        return subprocess.run([*self.git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    def bundle(self) -> pathlib.Path:
        out = self.tmp / "sync.bundle"
        out.unlink(missing_ok=True)
        subprocess.run([*self.git, "bundle", "create", "-q", str(out), "HEAD"], check=True, capture_output=True)
        return out

    def tip(self) -> str:
        return project_git.owned_git(self.bare, "rev-parse", "refs/heads/agent-memory").stdout.strip()

    def test_control_minus_c_is_refused(self):
        """The control: with the setting, git entering the bare repo with -C is refused (so the tests below test it)."""
        r = subprocess.run(["git", "-C", str(self.bare), "rev-parse", "--is-bare-repository"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("safe.bareRepository", r.stderr)

    def test_owned_git(self):
        r = project_git.owned_git(self.bare, "rev-parse", "--verify", "-q", "refs/heads/agent-memory")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_bundle(self):
        """What backup.py runs for each bare repo."""
        out = self.tmp / "x.bundle"
        r = subprocess.run(["git", *gitdirs.at(self.bare), "bundle", "create", str(out), "--all"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_at_tells_bare_from_a_checkout(self):
        self.assertEqual(gitdirs.at(self.bare), ["--git-dir", str(self.bare)])
        self.assertEqual(gitdirs.at(self.tmp / "work"), ["-C", str(self.tmp / "work")])
        self.assertEqual(gitdirs.at(self.tmp / "missing"), ["-C", str(self.tmp / "missing")])

    def test_memory_paths_reads_a_bare_repo(self):
        """What fetch_bundle and doctor ask of the memory bare repo; the control: the -C form it ran before is refused."""
        r = subprocess.run(["git", "-C", str(self.bare), "--literal-pathspecs", "ls-tree", "-r", "agent-memory"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("safe.bareRepository", r.stderr)
        self.assertEqual(memory_paths.tree_problems(self.bare, "refs/heads/agent-memory"), [])
        self.assertTrue(memory_paths.is_memory_tree(self.bare, "refs/heads/agent-memory"))
        self.assertEqual(memory_paths.new_commit_problems(self.bare, None, "refs/heads/agent-memory"), [])

    def test_memory_paths_still_refuses_in_a_bare_repo(self):
        """It reads the commits, not just answers: a path a memory branch may not hold is named, never "can't be read"."""
        old = self.tip()
        new = self.commit("run.sh")
        subprocess.run([*self.git, "push", "-q", str(self.bare), "agent-memory"], check=True)
        bad = memory_paths.new_commit_problems(self.bare, old, new)
        self.assertEqual(bad, ['"run.sh" (outside what a memory branch holds)'])

    def test_fetch_bundle_end_to_end(self):
        """The hourly sync's landing of a sandboxed project's bundle: a note fast-forwards the branch."""
        new = self.commit("memory/2026-11-16.md")
        self.assertIsNone(project_git.fetch_bundle(self.bare, self.bundle(), "agent-memory"))
        self.assertEqual(self.tip(), new)
        self.assertNotEqual(project_git.owned_git(self.bare, "rev-parse", "--verify", "-q", project_git.INCOMING).returncode, 0)

    def test_fetch_bundle_refuses_what_a_branch_may_not_hold(self):
        old = self.tip()
        self.commit("memory/2026-11-16.md")
        self.commit("memory/RULESFILE.md")
        why = project_git.fetch_bundle(self.bare, self.bundle(), "agent-memory")
        self.assertTrue(why and why.startswith(project_git.REFUSED), why)
        self.assertIn('"memory/RULESFILE.md" (a rules file\'s name)', why)
        self.assertNotIn("can't be read", why)
        self.assertEqual(self.tip(), old)

    def test_backup_verify_repo(self):
        """backup's verify runs `git bundle verify` in an empty bare repo of its own: named, not entered from its cwd."""
        repo, out = self.tmp / ".verify-repo", self.tmp / "x.bundle"
        subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        subprocess.run(["git", *gitdirs.at(self.bare), "bundle", "create", "-q", str(out), "--all"], check=True,
                       capture_output=True)
        control = subprocess.run(["git", "bundle", "verify", str(out)], cwd=repo, capture_output=True, text=True)
        self.assertNotEqual(control.returncode, 0)
        self.assertIn("need a repository", control.stderr)   # the bare repo isn't found at all from its cwd
        r = subprocess.run(["git", "--git-dir", str(repo), "bundle", "verify", str(out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_weekly_reflect_counts_a_mirror(self):
        """weekly-reflect counts a project's commits in its mirror, a --mirror clone (bare)."""
        import datetime
        reflect = _load("weekly_reflect_bare", "bin/weekly-reflect.py")
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertEqual(reflect.git_count(self.bare, now - datetime.timedelta(days=1), now + datetime.timedelta(days=1)), 1)

    def test_memory_gaps(self):
        import datetime
        gaps = _load("check_memory_gaps", "bin/check-memory-gaps.py")
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        self.assertEqual(gaps.note_commits_since(self.bare, since), 1)


class NoMinusCIntoBare(unittest.TestCase):
    def test_bare_callers_name_the_repo(self):
        """None of the bare-repo callers enters a bare repo with -C again (backup's own.git/bare/repo variables)."""
        pat = re.compile(r'"-C",\s*str\((bare|own|repo)\)|git -C "\$HOME/\$bare_rel"|git -C ~/git-origin'
                         r'|\["git", "bundle", "verify"')
        for rel in BARE_CALLERS:
            with self.subTest(rel):
                self.assertIsNone(pat.search((ROOT / rel).read_text()), rel)


if __name__ == "__main__":
    unittest.main()
