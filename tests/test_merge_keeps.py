"""merge-branch keeps what it wasn't asked to change: files the main checkout holds that git ignores (bringing in a tracked
file by the same name would overwrite them), and worktree entries of folders that aren't there right now.

    bin/py -m unittest tests.test_merge_keeps
"""
import pathlib
import shutil
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_merge_memory import Project  # noqa: E402


class IgnoredFiles(Project):
    def setUp(self):
        super().setUp()
        self.env["AGENT_HUB_LANG_UI"] = "en"
        (self.ws / ".git" / "info" / "exclude").write_text(".agent-memory/\nlocal/\nconfig.json\n")
        self.wt = self.worktree("feature")

    def track(self, *names):
        """the branch brings in tracked files by these names"""
        for name in names:
            f = self.wt / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("the branch's\n")
            self.git(self.wt, "add", "-f", name)
        self.git(self.wt, "commit", "-q", "-m", "feat: files")

    def refused(self, *named):
        status = self.git(self.ws, "status", "--porcelain")
        head = self.git(self.ws, "rev-parse", "main")
        r = self.merge(self.wt)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        for name in named:
            self.assertIn(name, r.stdout)
        self.assertIn(str(self.ws), r.stdout)
        self.assertIn("uncommitted files of the owner's", r.stdout)
        self.assertIn("never moves, changes or removes them", r.stdout)
        for word in ("Please move", "Please commit", "Aborting"):   # git's own advice, to whoever runs it
            self.assertNotIn(word, r.stdout)
        self.assertEqual(self.git(self.ws, "rev-parse", "main"), head)
        self.assertEqual(self.git(self.ws, "status", "--porcelain"), status)
        self.assertEqual(self.events("merge.done"), [])
        self.assertEqual(len(self.events("merge.failed")), 1)

    def test_an_ignored_file_is_not_overwritten(self):
        (self.ws / "config.json").write_text("mine\n")
        self.track("config.json")
        self.refused("config.json")
        self.assertEqual((self.ws / "config.json").read_text(), "mine\n")

    def test_an_untracked_file_is_the_owners(self):
        (self.ws / "notes.txt").write_text("mine\n")
        self.track("notes.txt")
        self.refused("notes.txt")
        self.assertEqual((self.ws / "notes.txt").read_text(), "mine\n")

    def test_a_staged_file_is_the_owners(self):
        (self.ws / "notes.txt").write_text("mine\n")
        self.git(self.ws, "add", "notes.txt")
        self.track("notes.txt")
        self.refused("notes.txt")
        self.assertEqual(self.git(self.ws, "show", ":notes.txt"), "mine")

    def test_a_changed_file_is_the_owners(self):
        (self.ws / "a").write_text("mine\n")
        self.track("a")
        self.refused("a")
        self.assertEqual((self.ws / "a").read_text(), "mine\n")

    def test_gits_advice_is_not_the_last_word_when_nothing_is_named(self):
        """a name in_the_way couldn't tell: git's own words, and whose the files are after them"""
        (self.ws / "notes.txt").write_text("mine\n")
        self.track("notes.txt")
        import os
        shim = self.tmp / "shim"
        shim.mkdir()
        (shim / "git").write_text(f"""#!/bin/bash
case " $* " in *" --diff-filter="*) exit 0 ;; esac
exec '{shutil.which("git")}' "$@"
""")
        (shim / "git").chmod(0o755)
        self.env["PATH"] = f"{shim}:{self.env.get('PATH') or os.defpath}"
        r = self.merge(self.wt)
        self.assertEqual(r.returncode, 1)
        self.assertIn("notes.txt", r.stdout)
        self.assertGreater(r.stdout.rindex("never moves, changes or removes them"), r.stdout.index("notes.txt"))
        self.assertGreater(r.stdout.rindex("never moves"), r.stdout.find("Please move"))

    def test_an_ignored_file_in_an_ignored_folder_is_not_overwritten(self):
        (self.ws / "local").mkdir()
        (self.ws / "local" / "\u6703\u8b70.txt").write_text("mine\n")
        self.track("local/\u6703\u8b70.txt")
        self.refused("local/\u6703\u8b70.txt")
        self.assertEqual((self.ws / "local" / "\u6703\u8b70.txt").read_text(), "mine\n")

    def test_an_ignored_file_where_a_folder_goes_is_not_overwritten(self):
        (self.ws / "config.json").write_text("mine\n")
        self.track("config.json/a")
        self.refused("config.json")
        self.assertEqual((self.ws / "config.json").read_text(), "mine\n")

    def test_an_ignored_folder_where_a_file_goes_is_not_overwritten(self):
        (self.ws / "local").mkdir()
        (self.ws / "local" / "a.txt").write_text("mine\n")
        self.track("local")
        self.refused("local")
        self.assertEqual((self.ws / "local" / "a.txt").read_text(), "mine\n")

    def test_moved_away_it_merges(self):
        (self.ws / "config.json").write_text("mine\n")
        (self.ws / "local").mkdir()
        (self.ws / "local" / "other.txt").write_text("in nobody's way\n")
        self.track("config.json")
        shutil.move(self.ws / "config.json", self.tmp / "config.json")
        r = self.merge(self.wt)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.ws / "config.json").read_text(), "the branch's\n")
        self.assertEqual((self.ws / "local" / "other.txt").read_text(), "in nobody's way\n")

    def test_a_dry_run_leaves_other_worktrees_entries(self):
        away = self.worktree("away")
        shutil.move(away, self.tmp / "unmounted")
        self.commit(self.wt, "b", "feat: b")
        r = self.merge(self.wt, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        listed = self.git(self.ws, "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {away}\n", listed + "\n")
        self.assertNotIn("merge-dry-run", listed, "its own went")
        shutil.move(self.tmp / "unmounted", away)
        self.assertEqual(self.git(away, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
