"""Renaming a project moves its folder; every worktree of its repo keeps working, the user's as well as the notes'.

    bin/py -m unittest tests.test_rename_worktrees
"""
import os
import pathlib
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_onboard_push  # noqa: E402

git = test_onboard_push.git


class Rename(unittest.TestCase):
    setUp = test_onboard_push.Onboard.setUp
    onboard, repo = test_onboard_push.Onboard.onboard, test_onboard_push.Onboard.repo

    def rename(self, *args):
        r = subprocess.run(["bash", str(ROOT / "bin" / "rename-project.sh"), *map(str, args)], capture_output=True, text=True,
                           env=self.env, timeout=120, cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_worktrees_inside_the_folder_and_elsewhere(self):
        p = self.repo("shop")
        self.assertEqual(self.onboard("--no-remote", p).returncode, 0)
        (self.hub / "legacy-paths.tsv").write_text("name\tpath\tnote\n")
        inside, outside = p / ".claude" / "worktrees" / "fix", self.home / "elsewhere" / "try out"
        gone = self.home / "unplugged"
        for n, wt in (("fix", inside), ("try", outside), ("gone", gone)):
            git(p, "worktree", "add", "-q", "-b", n, str(wt), env=self.env)
        with open(p / ".git" / "info" / "exclude", "a") as f:
            f.write(".claude/\n.agent-memory\n")
        gone.rename(self.home / "unplugged-disk")
        for wt in (inside, outside):
            (wt / ".agent-memory").symlink_to(p / ".agent-memory")
        (outside / "wip.txt").write_text("not committed\n")
        out = self.rename("shop", "store", "--apply")
        new = self.ws / "store"
        self.assertFalse(p.exists())
        for wt in (new / ".claude" / "worktrees" / "fix", outside, new / ".agent-memory"):
            with self.subTest(worktree=wt):
                common = git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir", env=self.env).strip()
                self.assertEqual(os.path.realpath(common), os.path.realpath(new / ".git"))
                git(wt, "status", env=self.env)
        listed = git(new, "worktree", "list", "--porcelain", env=self.env)
        for wt in (new / ".claude" / "worktrees" / "fix", outside):
            self.assertIn(f"worktree {wt.resolve()}\n", listed)
            self.assertTrue((wt / ".agent-memory" / "STATE.md").exists(), "its link to the notes follows")
        self.assertNotIn(str(p) + "/", listed)
        self.assertEqual((outside / "wip.txt").read_text(), "not committed\n")
        # the one that wasn't there is named, with the command that puts it right
        line = next(x for x in out.splitlines() if str(gone) in x)
        (self.home / "unplugged-disk").rename(gone)
        subprocess.run(line[line.index("git -C"):], shell=True, check=True, env=self.env, capture_output=True)
        git(gone, "status", env=self.env)

    def test_without_other_worktrees(self):
        p = self.repo("shop")
        self.assertEqual(self.onboard("--no-remote", p).returncode, 0)
        (self.hub / "legacy-paths.tsv").write_text("")
        self.rename("shop", "store", "--apply")
        git(self.ws / "store" / ".agent-memory", "status", env=self.env)
        shutil.rmtree(self.ws / "store")


if __name__ == "__main__":
    unittest.main()
