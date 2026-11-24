"""Where init keeps the instance, and what it finds there: a folder that holds other things is refused untouched, an
earlier install's instance is used as it is; the settings files are replaced whole, and the command on PATH is written
only where nothing of the user's is.

    bin/py -m unittest tests.test_init_instance
"""
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import test_new_user  # noqa: E402


class Case(unittest.TestCase):
    NewUser = test_new_user.NewUser
    setUp, tearDown, hub_cmd = NewUser.setUp, NewUser.tearDown, NewUser.hub_cmd

    def init(self, *args, rc=0):
        r = self.hub_cmd("init", "--yes", "--no-launchd", "--owner", "Alex", "--workspace", str(self.home / "ws"), *args)
        self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def pointer(self) -> str | None:
        f = self.hub / ".hub-home"
        return f.read_text().strip() if f.exists() else None


class Refused(Case):
    def test_the_home_folder_its_parents_and_a_folder_with_other_things(self):
        docs = self.home / "Documents"
        (docs / "secrets").mkdir(parents=True)
        (docs / "secrets").chmod(0o755)
        (docs / "config").mkdir()
        (docs / "config" / "mine.conf").write_text("mine")
        (docs / "thesis.txt").write_text("mine")
        other = self.home / "site"   # only names brunna also uses, and a file of the user's in one
        (other / "data").mkdir(parents=True)
        (other / "data" / "mine.txt").write_text("mine")
        link = self.home / "link"
        link.symlink_to(self.home)
        before = sorted(p.relative_to(self.tmp) for p in self.home.rglob("*"))
        for folder in (self.home, self.home.parent, pathlib.Path("/"), link, docs, other):
            with self.subTest(folder=folder):
                out = self.init("--home", str(folder), rc=2)
                self.assertIn(str(folder), out)
                self.assertIsNone(self.pointer())
                self.assertEqual(sorted(p.relative_to(self.tmp) for p in self.home.rglob("*")), before)
                self.assertEqual((docs / "secrets").stat().st_mode & 0o777, 0o755)

    def test_a_home_folder_with_nothing_else_in_it(self):
        out = self.init("--home", str(self.home), rc=2)
        self.assertEqual(list(self.home.iterdir()), [], out)
        import instance_dir
        from unittest import mock
        alone = self.tmp / "users" / "me"
        alone.mkdir(parents=True)
        with mock.patch.object(instance_dir.pathlib.Path, "home", classmethod(lambda cls: alone)):
            self.assertIsNotNone(instance_dir.problem(alone))
            self.assertIsNotNone(instance_dir.problem(alone.parent))
            self.assertIsNone(instance_dir.problem(alone / ".brunna"))

    def test_the_instance_is_never_inside_the_hub(self):
        self.init("--home", str(self.hub / "instance"), rc=2)
        self.assertFalse((self.hub / "instance").exists())
        import instance_dir
        from unittest import mock
        around = self.tmp / "x"
        (around / "config").mkdir(parents=True)
        (around / "config" / "brunna.json").write_text('{"v": 1}')
        with mock.patch.object(instance_dir.hubconf, "MAIN", around / "hub"):
            self.assertIsNone(instance_dir.problem(around), "an earlier install's, with the hub in it: removing the hub is no loss")

    def test_an_empty_folder_and_one_an_init_stopped_in_are_taken(self):
        empty = self.home / "a"
        empty.mkdir()
        stopped = self.home / "b"
        for sub in ("config/projects", "data", "secrets"):
            (stopped / sub).mkdir(parents=True)
        for folder in (empty, stopped):
            self.init("--home", str(folder))
            self.assertEqual(self.pointer(), str(folder))
            (self.hub / ".hub-home").unlink()

    def test_only_the_platforms_files_are_committed(self):
        home = self.home / ".brunna"
        self.init()
        subprocess.run(["rm", "-rf", str(home / "config" / ".git")], check=True)
        (home / "config" / "own.key").write_text("mine")
        self.init()
        files = subprocess.run(["git", "-C", str(home / "config"), "ls-files"], capture_output=True, text=True).stdout.split()
        self.assertIn("brunna.json", files)
        self.assertNotIn("own.key", files)


class Adopted(Case):
    def earlier(self, *args, name="brunna.json") -> pathlib.Path:
        """An install whose settings were changed, and its hub gone: no pointer is left."""
        self.init(*args)
        cfg = pathlib.Path(self.pointer()) / "config"
        d = json.loads((cfg / "brunna.json").read_text())
        d["git_origin"] = str(self.home / "elsewhere")
        d["_mine"] = "kept"
        (cfg / "brunna.json").unlink()
        (cfg / name).write_text(json.dumps(d))
        (cfg / "schedule.yaml").write_text("jobs: []\n")
        (self.hub / ".hub-home").unlink()
        return cfg

    def unchanged(self, cfg, again):
        before = {p: p.read_bytes() for p in cfg.rglob("*") if p.is_file() and ".git" not in p.parts}
        out = again()
        self.assertEqual({p: p.read_bytes() for p in cfg.rglob("*") if p.is_file() and ".git" not in p.parts}, before)
        self.assertEqual(self.pointer(), str(cfg.parent))
        self.assertIn(str(cfg.parent), out, "said")
        return out

    def test_every_setting_is_kept(self):
        cfg = self.earlier()
        out = self.unchanged(cfg, lambda: self.init("--owner", "Bob", "--workspace", str(self.home / "other")))
        self.assertIn("--workspace", out, "what isn't applied is said")
        self.assertTrue((self.home / "elsewhere").is_dir(), "its settings are the ones in effect")

    def test_with_the_settings_files_name_from_before_the_rename(self):
        cfg = self.earlier(name="hub.json")
        self.unchanged(cfg, self.init)
        self.assertFalse((cfg / "brunna.json").exists())

    def test_in_a_folder_named_by_home(self):
        cfg = self.earlier("--home", str(self.home / "elsewhere-instance"))
        self.unchanged(cfg, lambda: self.init("--home", str(cfg.parent)))

    def test_when_the_pointer_is_empty(self):
        cfg = self.earlier()
        (self.hub / ".hub-home").write_text("")
        (self.hub / "hub.json").write_text("{}")   # the old layout's copy a move leaves in the hub
        self.unchanged(cfg, self.init)

    def test_settings_that_cant_be_read_are_never_written_over(self):
        cfg = self.earlier()
        (cfg / "brunna.json").write_text("")
        out = self.init(rc=2)
        self.assertIn("brunna.json", out)
        self.assertEqual((cfg / "brunna.json").read_text(), "")
        self.assertIsNone(self.pointer())

    def test_its_config_gate_is_this_hubs(self):
        cfg = self.earlier()
        subprocess.run(["git", "-C", str(cfg), "config", "core.hooksPath", "/gone/githooks/config"], check=True)
        self.init()
        got = subprocess.run(["git", "-C", str(cfg), "config", "core.hooksPath"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(got, str(self.hub / "githooks" / "config"))

    def test_an_agent_is_given_nothing_to_ask_for_it(self):
        cfg = self.earlier()
        r = self.hub_cmd("init", "--questions")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual({q["id"] for q in json.loads(r.stdout)["questions"]} - {"home"}, set(), r.stdout)
        a = self.tmp / "answers.json"
        a.write_text(json.dumps({"owner": "Bob", "language": "en"}))
        before = (cfg / "brunna.json").read_bytes()
        r = self.hub_cmd("init", "--answers", str(a), "--no-launchd")
        self.assertEqual((cfg / "brunna.json").read_bytes(), before, r.stdout + r.stderr)
        self.assertEqual(self.pointer(), str(cfg.parent))


class Missing(Case):
    """The pointer names an instance that isn't there (a disk that isn't plugged in)."""

    def test_it_is_never_pointed_elsewhere_unasked(self):
        disk = self.home / "disk" / "brunna"
        self.init("--home", str(disk))
        disk.parent.rename(self.home / "unplugged")
        for left in (None, disk):   # gone, or an empty mount point
            if left:
                left.mkdir(parents=True)
            out = self.init(rc=2)
            self.assertIn(str(disk), out)
            self.assertIn("--home", out, "how to use another folder on purpose")
            self.assertEqual(self.pointer(), str(disk))
            self.assertFalse((self.home / ".brunna").exists())
            r = self.hub_cmd("init", "--questions")
            self.assertIn("error", json.loads(r.stdout))
        self.init("--home", str(self.home / "other"))
        self.assertEqual(self.pointer(), str(self.home / "other"))


class Whole(Case):
    """The settings files are replaced whole: what another name of the old file holds is the old text, as a copy cut off
    half-way would have left it; the mode and a link stay."""

    def whole(self, f: pathlib.Path, act):
        f.chmod(0o600)
        old = f.read_bytes()
        link = f.with_name(f.name + ".before")
        link.unlink(missing_ok=True)
        os.link(f, link)
        self.addCleanup(link.unlink, missing_ok=True)
        r = act()
        self.assertNotEqual(f.read_bytes(), old, r.stdout + r.stderr)
        self.assertEqual(link.read_bytes(), old, "written in place")
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        link.unlink()

    def test_each_writer(self):
        self.init()
        cfg = self.home / ".brunna" / "config" / "brunna.json"
        jobs = cfg.parent / "schedule.yaml"
        jobs.write_text("jobs:\n- {name: tidy, command: 'true', every: 1h}\n")
        for f, act in ((cfg, lambda: self.hub_cmd("modules", "enable", "backup")),
                       (cfg, lambda: self.run_("schedulectl", "set-model", "librarian-state", "m9")),
                       (jobs, lambda: self.run_("schedulectl", "pause", "tidy"))):
            with self.subTest(file=f.name):
                self.whole(f, act)

    def run_(self, script, *args):
        return subprocess.run([sys.executable, str(self.hub / "bin" / script), *args], env=self.env, capture_output=True,
                              text=True, timeout=120, cwd=self.hub)

    def test_a_link_stays_a_link(self):
        self.init()
        cfg = self.home / ".brunna" / "config" / "brunna.json"
        real = self.home / "dotfiles.json"
        cfg.rename(real)
        cfg.symlink_to(real)
        self.hub_cmd("modules", "enable", "backup")
        self.assertTrue(cfg.is_symlink())
        self.assertIs(json.loads(real.read_text())["modules"]["backup"], True)

    def test_the_pointer(self):
        ptr = self.hub / ".hub-home"
        ptr.write_text("")
        os.link(ptr, self.tmp / "before")
        self.init()
        self.assertEqual((self.tmp / "before").read_text(), "", "written in place")
        self.assertEqual(self.pointer(), str(self.home / ".brunna"))


class InstallCli(Case):
    def setUp(self):
        Case.NewUser.setUp(self)
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)

    def test_another_program_of_that_name_is_left(self):
        for name in ("brunna", "agenthub"):
            (self.bin / name).write_text("#!/bin/sh\necho somebody else's\n")
        r = self.hub_cmd("install-cli")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        for name in ("brunna", "agenthub"):
            self.assertIn("somebody else's", (self.bin / name).read_text())
        self.assertIn(str(self.bin / "brunna"), r.stderr)
        self.assertIn("--dir", r.stderr, "with what to do")

    def test_a_link_is_never_written_through(self):
        mine = self.home / "my-script"
        mine.write_text("#!/bin/sh\necho mine\n")
        (self.bin / "brunna").symlink_to(mine)
        r = self.hub_cmd("install-cli")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(mine.read_text(), "#!/bin/sh\necho mine\n")
        (self.bin / "brunna").unlink()
        (self.bin / "brunna").symlink_to(self.home / "not-there")
        self.hub_cmd("install-cli")
        self.assertFalse((self.home / "not-there").exists())

    def test_a_wrapper_the_user_made_theirs_is_left(self):
        self.hub_cmd("install-cli")
        f = self.bin / "brunna"
        for text in (f.read_text().replace(str(self.hub), "/h").replace("exec", "export A=1\nexec"),
                     f.read_text().replace(str(self.hub), "/h") + "echo done\n"):
            f.write_text(text)
            self.assertEqual(self.hub_cmd("install-cli").returncode, 1)
            self.assertEqual(f.read_text(), text)
        other = self.home / "wrapper"   # a link to a wrapper that is out of date: neither it nor the link is written
        other.write_text('#!/bin/sh\n# the brunna CLI\nexec "/h/bin/brunna" "$@"\n')
        f.unlink()
        f.symlink_to(other)
        self.assertEqual(self.hub_cmd("install-cli").returncode, 1)
        self.assertTrue(f.is_symlink())
        self.assertIn("/h/bin/brunna", other.read_text())

    def test_its_own_from_any_version_is_brought_up_to_date(self):
        for old in ('#!/bin/sh\n# an older comment\nexec "/h/bin/py" "/h/bin/agenthub" "$@"\n',
                    '#!/bin/sh\n# the agent-hub CLI (written by agenthub install-cli; run it again)\nexec "/h/bin/agenthub" "$@"\n'):
            for name in ("brunna", "agenthub"):
                (self.bin / name).write_text(old)
            self.hub_cmd("install-cli")
            for name in ("brunna", "agenthub"):
                self.assertIn(str(self.hub / "bin" / "brunna"), (self.bin / name).read_text())
                self.assertEqual((self.bin / name).stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
