"""The user's own files (a project's and the user's tool settings, the registry, the settings file) are never left half
written, and an instance whose settings file can't be read says so in one line.

A full disk is played by a limit on how big a file the child may write (RLIMIT_FSIZE), set just before the write.

    bin/py -m unittest tests.test_user_files
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
FULL = """
import resource, signal
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
def full():
    resource.setrlimit(resource.RLIMIT_FSIZE, (8, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
"""
MERGE = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("mhs", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
sys.argv = sys.argv[1:]
full()
m.main()
"""
HOOKED = {"permissions": {"allow": ["Bash(make:*)"]},
          "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]},
                             {"hooks": [{"type": "command", "command": "/opt/agent-hub/bin/hub-event x"}]}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "config" / "projects").mkdir(parents=True)
        self.conf = self.tmp / "config" / "brunna.json"
        self.conf.write_text(json.dumps({"v": 1, "registry_dir": "projects", "owner": {"address": "owner", "tag": "owner"}}))
        self.home = self.tmp / "home"
        self.env = {**{k: v for k, v in os.environ.items()
                       if not k.startswith(("AGENT_HUB_", "BRUNNA_")) or k in ("AGENT_HUB_PYTHON", "BRUNNA_PYTHON")},
                    "HOME": str(self.home), "BRUNNA_CONFIG": str(self.tmp / "config"), "PYTHONPATH": str(BIN),
                    "BRUNNA_STATE": str(self.tmp / "state"), "BRUNNA_LANG_UI": "en", "PYTHONDONTWRITEBYTECODE": "1"}

    def child(self, code, *args):
        return subprocess.run([sys.executable, "-c", textwrap.dedent(code), *map(str, args)], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def settings(self, folder, mode=0o600):
        f = folder / ".claude" / "settings.local.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(HOOKED))
        f.chmod(mode)
        return f

    def mode(self, f):
        return f.stat().st_mode & 0o777


class FullDisk(Base):
    """Whatever the write was, the file is what it was before it."""

    def unharmed(self, f, code, *args):
        before = f.read_bytes()
        beside = sorted(p.name for p in f.parent.iterdir())
        r = self.child(FULL + code, *args)
        self.assertNotEqual(r.returncode, 0, "a write that failed is said: " + r.stdout)
        self.assertIn("File too large", r.stderr)
        self.assertEqual(f.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in f.parent.iterdir()), beside, "nothing left beside it")

    def test_project_hooks_installed(self):
        f = self.settings(self.tmp / "ws")
        self.unharmed(f, "import sys, runtime_claude\nfull()\nruntime_claude.install_lifecycle(sys.argv[1])", self.tmp / "ws")

    def test_project_hooks_removed(self):
        f = self.settings(self.tmp / "ws")
        self.unharmed(f, "import sys, runtime_claude\nfull()\nruntime_claude.remove_lifecycle(sys.argv[1])", self.tmp / "ws")

    def user_settings(self):
        f = self.home / ".claude" / "settings.json"
        f.parent.mkdir(parents=True)
        r = self.child("import runtime_claude, json\n"
                       "print(json.dumps(runtime_claude.merge_worktree_hooks({'env': {'A': '1'}})))")
        f.write_text(r.stdout)
        return f

    def test_user_settings_at_uninstall(self):
        self.unharmed(self.user_settings(), "import runtime_claude\nfull()\nruntime_claude.user_cleanup(True)")

    def test_merge_hook_settings(self):
        f = self.settings(self.tmp / "ws")
        self.unharmed(f, MERGE, BIN / "merge-hook-settings.py", f)

    def test_merge_hook_settings_of_the_user(self):
        f = self.settings(self.tmp / "ws")
        self.unharmed(f, MERGE, BIN / "merge-hook-settings.py", "--worktree", f)

    def test_registry_files_rewritten(self):
        f = self.tmp / "config" / "projects" / "v1.yaml"
        f.write_text("v: 1\nname: v1\nrole: x\nruntime:\n  test: make test\n")
        self.unharmed(f, "import registry\nfull()\nregistry.cmd_upgrade_files(True)")

    def test_registry_add_and_rename(self):
        f = self.tmp / "config" / "projects" / "v1.yaml"
        f.write_text("v: 1\nname: v1\nrole: x\n")
        self.unharmed(f, "import registry\nfull()\nregistry.cmd_add('new', '/nowhere/new', 'origin', '/nowhere/new.git')")
        self.unharmed(f, "import registry\nfull()\nregistry.cmd_rename('v1', 'new', '/nowhere/new', 'o', '/nowhere/new.git')")


class ModeAndLinks(Base):
    """A file the user keeps to themselves stays so, and one they keep elsewhere (a link) stays a link."""

    def linked(self, f):
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        f.rename(real)
        f.symlink_to(real)
        return real

    def check(self, f, real, code, *args):
        r = self.child(code, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(f.is_symlink(), "the link is still a link")
        self.assertEqual(self.mode(real), 0o600)
        self.assertIn("SessionStart", json.loads(real.read_text())["hooks"])

    def test_install(self):
        f = self.settings(self.tmp / "ws")
        self.check(f, self.linked(f), "import sys, runtime_claude; runtime_claude.install_lifecycle(sys.argv[1])", self.tmp / "ws")

    def test_merge_hook_settings(self):
        f = self.settings(self.tmp / "ws")
        self.check(f, self.linked(f), MERGE.replace("full()\n", ""), BIN / "merge-hook-settings.py", f)

    def test_remove_keeps_the_mode(self):
        f = self.settings(self.tmp / "ws")
        r = self.child("import sys, runtime_claude; runtime_claude.remove_lifecycle(sys.argv[1])", self.tmp / "ws")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(f.read_text())["hooks"], {"Stop": HOOKED["hooks"]["Stop"][:1]})
        self.assertEqual(self.mode(f), 0o600)

    def test_a_link_to_what_isnt_there_is_not_replaced(self):
        f = self.settings(self.tmp / "ws")
        f.unlink()
        f.symlink_to(self.tmp / "not-mounted" / "settings.json")
        r = self.child("import sys, runtime_claude; runtime_claude.install_lifecycle(sys.argv[1])", self.tmp / "ws")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(f.is_symlink())
        self.assertFalse((self.tmp / "not-mounted").exists())

    def test_a_link_planted_where_it_writes_first_is_never_written_through(self):
        """Whatever a name beside the file is guessed to be, a write makes its own."""
        folder, victim = self.tmp / "d", self.tmp / "victim"
        folder.mkdir()
        victim.write_text("theirs")
        r = self.child("""
            import os, pathlib, sys, hubconf
            f, victim = pathlib.Path(sys.argv[1]), sys.argv[2]
            f.with_name(f".{f.name}.{os.getpid()}.tmp").symlink_to(victim)
            real = os.open
            def planted(p, flags, *a, **kw):   # and one at the very name it opens
                if flags & os.O_CREAT and not os.path.lexists(p):
                    os.symlink(victim, p)
                return real(p, flags, *a, **kw)
            for keep in (False, True):
                hubconf.write_atomic(f, "new", keep=keep)
            os.open = planted
            try:
                hubconf.write_atomic(f, "newer")
            except FileExistsError:
                print("refused")
            """, folder / "state.json", victim)
        self.assertEqual((r.returncode, r.stdout), (0, "refused\n"), r.stderr)
        self.assertEqual((victim.read_text(), (folder / "state.json").read_text()), ("theirs", "new"))

    def test_one_the_user_cant_write_is_refused(self):
        if os.geteuid() == 0:
            self.skipTest("root writes anything")
        f = self.settings(self.tmp / "ws", mode=0o444)
        before = f.read_text()
        r = self.child("import sys, runtime_claude; runtime_claude.install_lifecycle(sys.argv[1])", self.tmp / "ws")
        self.assertIn("PermissionError", r.stderr)
        self.assertEqual((f.read_text(), self.mode(f)), (before, 0o444))

    def test_a_hard_links_other_name_keeps_what_it_had(self):
        f = self.settings(self.tmp / "ws")
        other = self.tmp / "other"
        os.link(f, other)
        before = f.read_text()
        r = self.child("import sys, runtime_claude; runtime_claude.install_lifecycle(sys.argv[1])", self.tmp / "ws")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(other.read_text(), before)
        self.assertIn("SessionStart", f.read_text())

    def test_the_platforms_own_files_are_replaced_whole(self):
        """write_atomic as its other callers use it: a link there is replaced, never written through."""
        f, real = self.tmp / "state.json", self.tmp / "elsewhere"
        real.write_text("theirs")
        f.symlink_to(real)
        r = self.child("import sys, pathlib, hubconf; hubconf.write_atomic(pathlib.Path(sys.argv[1]), 'new')", f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((f.is_symlink(), f.read_text(), real.read_text()), (False, "new", "theirs"))


class BrokenSettings(Base):
    def test_one_line_that_says_how_to_get_it_back(self):
        for text in ("", '{"v": 1,', "[]", "\x00\x00"):
            with self.subTest(text=text):
                self.conf.write_text(text)
                r = self.child("import hubconf")
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
                self.assertIn(f"git -C {self.tmp / 'config'} checkout -- brunna.json", r.stderr)

    def test_it_says_where_first(self):
        self.conf.write_text('{\n "v": 1\n "owner": {}\n}\n')
        err = self.child("import hubconf").stderr
        self.assertIn("line 3, column 2", err)
        self.assertLess(err.index("line 3"), err.index("checkout"), "putting it right comes first")

    def test_going_back_keeps_the_edit(self):
        """The command it offers, run as it stands: the committed version is back, and the edit beside it."""
        folder = self.tmp / "con fig"   # a space: quoted
        folder.mkdir()
        self.env["BRUNNA_CONFIG"] = str(folder)
        conf = folder / "brunna.json"
        conf.write_text('{"v": 1}\n')
        conf.chmod(0o600)
        git = {**self.env, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
            subprocess.run(["git", "-C", str(folder), *cmd], check=True, env=git, capture_output=True)
        conf.write_text('{"my": "edit",')
        err = self.child("import hubconf").stderr
        subprocess.run(["sh", "-c", err[err.index("cp -p "):].strip()], check=True, env=git, capture_output=True)
        self.assertEqual(conf.read_text(), '{"v": 1}\n')
        self.assertEqual([f.read_text() for f in folder.iterdir() if f.name.startswith("brunna.json.")], ['{"my": "edit",'])

    def test_the_line_is_translated(self):
        self.conf.write_text("")
        self.env["BRUNNA_LANG_UI"] = "zh-Hant"
        r = self.child("import hubconf")
        self.assertIn(f"git -C {self.tmp / 'config'} checkout -- brunna.json", r.stderr)
        self.assertNotIn("last committed", r.stderr)

    def test_config_check_names_it(self):
        self.conf.write_text("{")
        r = subprocess.run([sys.executable, str(BIN / "config-check")], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual((r.returncode, len(r.stderr.splitlines())), (1, 1), r.stderr)
        # the commit's own check, of an exported index: its message, not the line above
        r = subprocess.run([sys.executable, str(BIN / "config-check"), "--child"], env=self.env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("brunna.json: cannot read it", r.stderr)

    def test_code_that_expects_a_broken_file_still_catches_it(self):
        self.conf.write_text("{")
        r = self.child("try:\n    import hubconf\nexcept ValueError:\n    print('caught')")
        self.assertEqual((r.returncode, r.stdout), (0, "caught\n"), r.stderr)


if __name__ == "__main__":
    unittest.main()
