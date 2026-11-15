"""The platform's git in its own bare repos still works under the owner's safe.bareRepository=explicit (the git broker's
precondition, docs/design/git-broker.md r2 B2). With it set, git refuses a bare repo it finds on its own, so `git -C
<bare>` fails and the memory sync, the nightly backup's bundles and memory-gaps all stopped on 11-14. Every call into a
bare repo names it with --git-dir. The owner's global config is never touched: GIT_CONFIG_GLOBAL points at a temp file.
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
import project_git  # noqa: E402

# the files whose git runs in bare repos the platform owns (memory bare repos, ~/git-origin, upgrade-idea's own.git)
BARE_CALLERS = ["bin/project_git.py", "bin/backup.py", "bin/check-memory-gaps.py", "bin/upgrade-idea.py",
                "bin/recover-project.sh"]


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
        r = subprocess.run(["git", "--git-dir", str(self.bare), "bundle", "create", str(out), "--all"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_memory_gaps(self):
        import datetime
        gaps = _load("check_memory_gaps", "bin/check-memory-gaps.py")
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        self.assertEqual(gaps.note_commits_since(self.bare, since), 1)


class NoMinusCIntoBare(unittest.TestCase):
    def test_bare_callers_name_the_repo(self):
        """None of the bare-repo callers enters a bare repo with -C again (backup's own.git/bare/repo variables)."""
        pat = re.compile(r'"-C",\s*str\((bare|own|repo)\)|git -C "\$HOME/\$bare_rel"')
        for rel in BARE_CALLERS:
            with self.subTest(rel):
                self.assertIsNone(pat.search((ROOT / rel).read_text()), rel)


if __name__ == "__main__":
    unittest.main()
