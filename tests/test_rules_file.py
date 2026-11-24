"""sync-local-setup.sh and the user's rules file: the platform writes between its own markers only, a file whose markers
don't pair is left as it is and said, and the file stays where and how the user keeps it (a link, its mode).

    bin/py -m unittest tests.test_rules_file
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import isolation  # noqa: E402
import names  # noqa: E402
import seatbelt  # noqa: E402

B, E = names.block()
OLD_B, OLD_E = names.block(names.other("marker"))


class PutBlock(unittest.TestCase):
    def test_a_begin_without_its_end_is_refused(self):
        for text in (f"{B}\nmine\n",
                     f"mine\n{E}\n{B}\nmine\n",                        # the end before it isn't its own
                     f"{B}\nmine\n\n{B}\nold\n{E}\n",                  # what an earlier run appended after a lone one
                     f"{OLD_B}\nmine\n\n{B}\nold\n{E}\n",
                     f"{B}\nold\n{E}\nmine\n{B}\nmine\n",
                     f"{B}\nold\n{E}\nmine\n{OLD_B}\nmine\n",
                     f"{B}\nmine\n{OLD_E}\n",
                     f"{B}\n{OLD_B}\nmine\n{OLD_E}\n{E}\n"):
            with self.subTest(text):
                with self.assertRaises(names.Unpaired):
                    names.put_block(text, "x")

    def test_everything_outside_whole_blocks_stays(self):
        for text in ("mine\n", f"mine\n{B}\nold\n{E}\nmine too\n", f"mine\n{OLD_B}\nold\n{OLD_E}\nmine too\n",
                     f"{E}\nmine\n", f"{B}\na\n{E}\nmine\n{OLD_B}\nb\n{OLD_E}\nmine too\n"):
            with self.subTest(text):
                once = names.put_block(text, "x")
                self.assertEqual(once.count("mine"), text.count("mine"))
                self.assertEqual(names.put_block(once, "x"), once)


class RulesFile(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for d in ("home", "config/projects", "data", "secrets", "state"):
            (self.tmp / d).mkdir(parents=True)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith(("BRUNNA_", "AGENT_HUB_", "GIT_", "HUB_"))},
                    "BRUNNA_PYTHON": sys.executable, "HOME": str(self.tmp / "home"), "BRUNNA_LANG_UI": "en",
                    **{f"BRUNNA_{k}": str(self.tmp / k.lower()) for k in ("CONFIG", "DATA", "SECRETS", "STATE")},
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        (self.tmp / "config" / "brunna.json").write_text(json.dumps({"owner": {"name": "alex", "tag": "Alex"}, "registry_dir": "projects"}))
        self.ws = self.tmp / "ws" / "proj"
        (self.ws / ".agent-memory").mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.f = self.ws / "CLAUDE.local.md"

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.ws, env=self.env, check=True, capture_output=True)

    def sync(self, *args, **env):
        return subprocess.run([str(BIN / "sync-local-setup.sh"), *args, str(self.ws)], env={**self.env, **env},
                              capture_output=True, text=True, timeout=180)

    def sandboxed(self):
        d = self.tmp / "state" / "shared" / "registry"
        d.mkdir(parents=True)
        (d / "index.json").write_text(json.dumps({"projects": {"proj": {
            "name": "proj", "paths": {"workspace": str(self.ws)}, "enforce": {"sandbox": True}}}}))

    def test_a_lone_begin_changes_nothing_however_often_it_runs(self):
        text = f"# mine\n\n{B}\nmy own notes\n"
        self.f.write_text(text)
        for _i in range(3):
            r = self.sync("--quiet")
            self.assertEqual(self.f.read_text(), text)
        for out in (r.stderr, r.stdout):   # stdout is what a session is told
            self.assertIn("CLAUDE.local.md", out)
            self.assertIn(":begin -->", out)
        self.assertNotIn("aligned", self.sync().stdout.split("hooks")[0])

    def test_what_an_earlier_run_left_after_a_lone_begin(self):
        text = f"# mine\n{B}\nmy own notes\n\n{B}\nold rules\n{E}\n"
        self.f.write_text(text)
        self.sync("--quiet")
        self.assertEqual(self.f.read_text(), text)

    def test_a_whole_block_is_rewritten_and_nothing_said(self):
        self.f.write_text(f"mine\n{OLD_B}\nold\n{OLD_E}\nmine too\n")
        self.f.chmod(0o600)
        r = self.sync("--quiet")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertTrue(self.f.read_text().startswith(f"mine\n{B}\n"))
        self.assertTrue(self.f.read_text().endswith(f"{E}\nmine too\n"))
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.ws.iterdir() if "tmp" in p.name], [])

    def linked(self, text="mine\n", root=None):
        real = (root or self.tmp) / "dotfiles" / "rules.md"
        real.parent.mkdir()
        real.write_text(text)
        real.chmod(0o640)
        self.f.symlink_to(real)
        return real

    def test_a_link_stays_a_link(self):
        real = self.linked()
        r = self.sync("--quiet")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertTrue(self.f.is_symlink())
        self.assertTrue(real.read_text().startswith(f"mine\n\n{B}\n"))
        self.assertEqual(real.stat().st_mode & 0o777, 0o640)

    def left_alone(self, real, **env):
        r = self.sync("--quiet", **env)
        self.assertTrue(self.f.is_symlink())
        self.assertEqual(real.read_text(), "mine\n")
        self.assertIn("CLAUDE.local.md", r.stdout)
        self.assertIn("is a link", r.stdout)

    def test_a_committed_link_is_not_written_through(self):
        """The repo says where it points (a fresh clone's, anywhere): nothing outside is written on its word."""
        real = self.linked()
        self.git("add", "-f", "CLAUDE.local.md")
        self.left_alone(real)

    def test_a_sandboxed_projects_link_is_written_only_as_the_project(self):
        real = self.linked()
        self.sandboxed()
        self.left_alone(real)
        self.sync("--quiet", HUB_AS_PROJECT="1")   # inside its sandbox: it can write only what the project can
        self.assertIn(B, real.read_text())
        self.assertTrue(self.f.is_symlink())

    def cant_write(self, real, r):
        self.assertTrue(self.f.is_symlink())
        self.assertEqual(real.read_text(), "mine\n")
        for out in (r.stdout, r.stderr):
            self.assertIn("CLAUDE.local.md", out)
            self.assertIn("could not be updated", out)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_target_folder_that_isnt_writable_is_said(self):
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        real = self.linked()
        real.parent.chmod(0o555)
        self.addCleanup(real.parent.chmod, 0o755)
        for _i in range(2):   # every session
            self.cant_write(real, self.sync("--quiet"))

    @unittest.skipUnless(seatbelt.can_sandbox() is None, "needs Seatbelt")
    def test_a_target_the_sandbox_doesnt_let_write_is_said(self):
        """As a sandboxed project's session start runs it: its own sandbox, the rules file kept elsewhere."""
        # kept outside the temp area, as a user's dotfiles are: the profile lets everything write the temp area, and a
        # clone that isn't an install (CI) keeps its temp folders there. HOME (the run's own, in the tree) or the tree
        base = next((d for d in (pathlib.Path.home(), ROOT / ".state", ROOT) if d.is_dir() and not isolation.in_system_tmp(d)), None)
        if base is None:
            self.skipTest("HOME and the tree are both in the temp area, which the profile lets everything write")
        out = pathlib.Path(tempfile.mkdtemp(prefix="zz-rules-", dir=base)).resolve()
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        real = self.linked(root=out)
        self.sandboxed()
        pol = {"workspace": self.ws, "write": [], "other_projects": [], "deny_read": [], "credentials": [], "credential_files": [],
               "protect": [], "protect_roots": []}
        prof = seatbelt.service_profile(pol, [self.ws])
        r = subprocess.run([seatbelt.SANDBOX_EXEC, "-p", prof, str(BIN / "sync-local-setup.sh"), "--quiet", str(self.ws)],
                           env={**self.env, "HUB_AS_PROJECT": "1"}, capture_output=True, text=True, timeout=180)
        self.cant_write(real, r)

    def test_a_rules_file_that_isnt_text_is_said(self):
        self.f.write_bytes(b"\xff\xfe not utf-8")
        r = self.sync("--quiet")
        self.assertIn("could not be updated", r.stdout)
        self.assertEqual(self.f.read_bytes(), b"\xff\xfe not utf-8")

    def test_no_agent_set_up_and_a_committed_local_path(self):
        """A first connect with no coding agent: there is no rules file to name, and the setup goes to its end."""
        (self.tmp / "config" / "brunna.json").write_text(json.dumps(
            {"owner": {"name": "alex", "tag": "Alex"}, "registry_dir": "projects", "runtime": {"default": "none"}}))
        (self.ws / "README").write_text("see /Us" + "ers/someone/workspace/x\n")
        self.git("add", "README")
        self.git("commit", "-qm", "x")
        for args in ((), ("--quiet",)):
            r = self.sync(*args)
            self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
            self.assertIn("README", r.stdout)
            self.assertNotIn("{", r.stdout.split("git grep")[0])
        self.assertFalse(self.f.exists())

    def test_a_link_to_nothing_is_left_alone(self):
        self.f.symlink_to(self.tmp / "not-mounted" / "rules.md")
        r = self.sync("--quiet")
        self.assertTrue(self.f.is_symlink())
        self.assertFalse((self.tmp / "not-mounted").exists())
        self.assertIn("is a link", r.stdout)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
