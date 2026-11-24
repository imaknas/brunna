"""agenthub uninstall (bin/uninstall.py): it lists first and changes nothing; --apply takes the platform's parts out of
a connected project and keeps the notes; --purge removes the notes and data too; the project's own files and the
user's own hooks are never touched; another hub's scheduler is left alone. No real launchd or systemd unit is touched:
the test hub runs with --no-launchd and the core-unit check is unit-tested on stand-in files.

    bin/py -m unittest tests.test_uninstall
"""
import json
import pathlib
import plistlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import runtime_claude  # noqa: E402
import test_new_user  # noqa: E402


class Uninstall(unittest.TestCase):
    NewUser = test_new_user.NewUser
    setUp, tearDown, hub_cmd = NewUser.setUp, NewUser.tearDown, NewUser.hub_cmd

    def connected(self) -> pathlib.Path:
        r = self.hub_cmd("init", "--yes", "--no-launchd", "--owner", "Alex", "--workspace", str(self.home / "ws"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = self.home / ".brunna" / "config" / "brunna.json"
        d = json.loads(cfg.read_text())
        d["runtime"] = {"default": "claude"}   # a runtime with a rules file and hooks; its binary isn't needed
        cfg.write_text(json.dumps(d))
        app = self.home / "ws" / "app"
        app.mkdir(parents=True)
        (app / "a.txt").write_text("mine\n")
        r = subprocess.run(["bash", str(self.hub / "bin" / "onboard-project.sh"), "--no-remote", str(app)],
                           env=self.env, capture_output=True, text=True, timeout=120, cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # the user's own words and hook next to the platform's
        rules = app / "CLAUDE.local.md"
        rules.write_text("my own note\n\n" + rules.read_text())
        s = json.loads((app / ".claude" / "settings.local.json").read_text())
        s["hooks"].setdefault("Stop", []).append({"hooks": [{"type": "command", "command": "say done"}]})
        (app / ".claude" / "settings.local.json").write_text(json.dumps(s))
        return app

    def test_plan_apply_purge(self):
        app = self.connected()
        before = {p: p.read_bytes() for p in app.rglob("*") if p.is_file() and ".git" not in p.parts}
        r = self.hub_cmd("uninstall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CLAUDE.local.md", r.stdout)
        self.assertEqual({p: p.read_bytes() for p in before}, before, "the plan changes nothing")

        r = self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rules = (app / "CLAUDE.local.md").read_text()
        self.assertEqual(rules, "my own note\n", "only the platform's block went")
        s = json.loads((app / ".claude" / "settings.local.json").read_text())
        self.assertEqual(s, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}})
        self.assertEqual((app / "a.txt").read_text(), "mine\n")
        self.assertTrue((app / ".agent-memory" / "STATE.md").exists(), "notes stay")
        self.assertTrue((self.home / ".brunna" / "config" / "brunna.json").exists(), "settings stay")
        self.assertIn("rm -rf", r.stdout)

        r = self.hub_cmd("uninstall", "--apply", "--yes", "--purge")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((app / ".agent-memory").exists())
        self.assertFalse((self.home / ".brunna").exists())
        self.assertFalse((self.home / "git-origin" / "app-memory.git").exists())
        self.assertEqual((app / "a.txt").read_text(), "mine\n", "the project's own files are never touched")
        branches = subprocess.run(["git", "-C", str(app), "branch", "--list", "agent-memory"], capture_output=True,
                                  text=True).stdout
        self.assertEqual(branches.strip(), "")

    def cfg(self) -> dict:
        return json.loads((self.home / ".brunna" / "config" / "brunna.json").read_text())

    def set_cfg(self, d: dict) -> None:
        (self.home / ".brunna" / "config" / "brunna.json").write_text(json.dumps(d))

    def init(self, *args):
        r = self.hub_cmd("init", "--yes", "--no-launchd", "--owner", "Alex", "--workspace", str(self.home / "ws"), *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def purge(self, rc=0):
        r = self.hub_cmd("uninstall", "--apply", "--yes", "--purge")
        self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def users_files(self, folder: pathlib.Path) -> dict:
        """The user's own, beside brunna's and under the same names."""
        files = {folder / n: n for n in ("thesis.txt", "data/mine.txt", "data/digest/ideas.md", "config/mine.conf",
                                         "config/projects/other.txt", "secrets/my-own-key", "tools/saw.txt")}
        for f, text in files.items():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        return files

    def snapshot(self, folder: pathlib.Path) -> dict:
        return {p: p.read_bytes() for p in folder.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_purge_removes_nothing_in_an_instance_put_in_a_folder_of_the_users(self):
        docs = self.home / "Documents"
        self.init("--home", str(docs))
        for m in docs.glob(".*-instance"):   # as a version that took any folder left it
            m.unlink()
        (docs / "data" / "digest").mkdir()
        (docs / "data" / "digest" / "2026-11-01.md").write_text("a digest\n")
        for d in ("credentials/app", "credentials-kept/app", "git-push", "sessions"):   # names brunna uses too
            (docs / "secrets" / d).mkdir(parents=True)
            (docs / "secrets" / d / "a.json").write_text("{}")   # a grant where they really are: never revoked, so kept
        for d in ("repos", "copies", "ws"):
            (docs / d).mkdir()
        app = docs / "ws" / "app"
        self.onboarded_in(docs, app)
        self.users_files(docs)
        before = self.snapshot(docs)
        out = self.purge()
        after = self.snapshot(docs)
        self.assertEqual({p: after.get(p) for p in before if ".claude" not in p.parts and p.name != "CLAUDE.local.md"},
                         {p: before[p] for p in before if ".claude" not in p.parts and p.name != "CLAUDE.local.md"})
        for path in (docs / "config", docs / "data", docs / "secrets", docs / "repos" / "app-memory.git", app / ".agent-memory"):
            self.assertIn(str(path), out, "named, to remove by hand")
        self.assertIn(f"{self.hub}/bin/brunna credentials revoke app a", out, "the command on PATH may be gone")
        self.assertTrue((self.hub / ".hub-home").exists(), "it is still this hub's")

    def onboarded_in(self, docs, app):
        cfg = docs / "config" / "brunna.json"
        cfg.write_text(json.dumps({**json.loads(cfg.read_text()), "git_origin": str(docs / "repos"),
                                   "bus_dir": str(docs / "bus")}))
        (docs / "bus").mkdir()
        (docs / "bus" / "bus.db").write_text("")
        r = subprocess.run(["bash", str(self.hub / "bin" / "onboard-project.sh"), "--no-remote", str(app)],
                           env=self.env, capture_output=True, text=True, timeout=120, cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_bus_folder_in_an_instance_that_goes_whole_goes_with_it(self):
        self.init()
        bus = self.home / ".brunna" / "data" / "bus"
        bus.mkdir()
        (bus / "bus.db").write_text("")
        self.set_cfg({**self.cfg(), "bus_dir": str(bus)})
        out = self.purge()
        self.assertFalse((self.home / ".brunna").exists())
        self.assertNotIn(str(bus), out, "never said to be left")
        self.init()
        beside = self.home / ".brunna" / "mailbox"   # in none of its parts: left, and said so
        beside.mkdir()
        (beside / "bus.db").write_text("")
        self.set_cfg({**self.cfg(), "bus_dir": str(beside)})
        self.assertIn(str(beside), self.purge())
        self.assertTrue((beside / "bus.db").exists())

    def test_a_link_where_the_buss_folder_goes_is_no_folder_of_its_own(self):
        self.init()
        mine = self.home / "mine"
        mine.mkdir()
        (mine / "bus.db").write_text("mine")
        bus = self.home / ".local" / "state" / "brunna-bus"
        shutil.rmtree(bus, ignore_errors=True)
        bus.parent.mkdir(parents=True, exist_ok=True)
        bus.symlink_to(mine)
        self.purge()
        self.assertEqual((mine / "bus.db").read_text(), "mine")
        self.assertTrue(bus.is_symlink())

    def test_the_old_layout_names_everything_the_hub_folder_would_take_along(self):
        self.init()
        for p, text in ((".brunna/config/brunna.json", None), (".brunna/config/projects", None)):
            shutil.rmtree(self.hub / "projects", ignore_errors=True) if p.endswith("projects") else None
            (self.home / p).rename(self.hub / pathlib.Path(p).name)
        (self.hub / ".hub-home").unlink()
        shutil.copy(next((ROOT / "projects").glob("brunna.yaml")), self.hub / "projects")
        for name, text in (("todo.md", "x"), (".secrets/k", "x"), ("digest/2026-11-01.md", "x"), ("schedule.yaml", "jobs: []\n"),
                           ("projects/mine.yaml", "v: 1\nname: mine\npaths: {}\n")):
            (self.hub / name).parent.mkdir(exist_ok=True)
            (self.hub / name).write_text(text)
        r = self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertNotIn("rm -rf", r.stdout, r.stderr)
        for name in ("brunna.json", "todo.md", ".secrets", "digest", "schedule.yaml", "projects/mine.yaml"):
            self.assertIn(str(self.hub / name), r.stdout.splitlines()[-1])
        self.assertNotIn("brunna.yaml", r.stdout.splitlines()[-1], "the platform's own")

    def test_purge_removes_nothing_in_an_instance_that_is_the_home_folder(self):
        self.init()
        for p in list((self.home / ".brunna").iterdir()):
            if p.name in ("config", "data", "secrets"):
                p.rename(self.home / p.name)
        (self.hub / ".hub-home").write_text(f"{self.home}\n")
        self.users_files(self.home)
        before = {p: x for p, x in self.snapshot(self.home).items() if p.relative_to(self.home).parts[0] in
                  ("config", "data", "secrets", "tools", "thesis.txt")}
        self.purge()
        after = self.snapshot(self.home)
        self.assertEqual({p: after.get(p) for p in before}, before)

    def test_purge_of_brunnas_own_folder_leaves_what_was_put_beside(self):
        home = self.home / "mybrunna"
        self.init("--home", str(home))
        (home / "data" / "research").mkdir()
        (home / "thesis.txt").write_text("mine")
        self.purge()
        self.assertEqual((home / "thesis.txt").read_text(), "mine")
        for part in ("config", "data", "secrets"):
            self.assertFalse((home / part).exists(), part)

    def test_what_a_purge_left_is_no_reason_to_refuse_brunnas_own_folder_again(self):
        home = self.home / "mybrunna"
        self.init("--home", str(home))
        (home / "thesis.txt").write_text("mine")
        self.purge()
        self.init("--home", str(home))
        self.assertEqual((home / "thesis.txt").read_text(), "mine")

    def test_purge_removes_nothing_from_a_bus_folder_named_in_the_settings(self):
        self.init()
        for folder in (self.home / "shared", self.home):
            with self.subTest(folder=folder):
                folder.mkdir(exist_ok=True)
                for n in ("bus.db", "bus.db.mine", "bus.db-notes", "hubbus.log"):
                    (folder / n).write_text("mine")
                d = self.cfg()
                self.set_cfg({**d, "bus_dir": str(folder)})
                r = self.hub_cmd("uninstall", "--apply", "--yes", "--purge")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                for n in ("bus.db", "bus.db.mine", "bus.db-notes", "hubbus.log"):
                    self.assertEqual((folder / n).read_text(), "mine")
                self.init()
                self.set_cfg(d)

    def test_purge_takes_only_the_buss_files_from_its_own_folder(self):
        self.init()
        bus = self.home / ".local" / "state" / "brunna-bus"
        bus.mkdir(parents=True, exist_ok=True)
        ours = ("bus.db", "bus.db-wal", "bus.db.corrupt-20261101-0203", "bus.db.corrupt-20261101-0203-shm", "dispatch.lock")
        mine = ("bus.db.mine", "bus.db-notes", "hubbus.log.keep")
        for n in ours + mine:
            (bus / n).write_text("x")
        self.purge()
        self.assertEqual(sorted(p.name for p in bus.iterdir()), sorted(mine))

    def git_(self, cwd, *a):
        return subprocess.run(["git", "-C", str(cwd), *a], env=self.env, capture_output=True, text=True)

    def test_a_purge_of_a_folder_made_for_it_says_it_is_gone(self):
        import i18n
        wrong = i18n.translate("uninstall", "{path} wasn't made for this, and may hold files of yours: nothing in it was removed. "
                               "Brunna's there, to remove by hand: {kept}", {"path": "@", "kept": "@"}, "zh-Hant").split("@")[1]
        real = self.home / "elsewhere"
        self.init("--home", str(real))
        out = self.purge()
        self.assertFalse(real.exists())
        self.assertNotIn(wrong, out)
        self.assertEqual(len([x for x in out.splitlines() if x.startswith("✓") and x.endswith(str(real))]), 1, out)
        self.init("--home", str(real))
        link = self.home / ".brunna"   # the default place is a link to the marked folder
        link.symlink_to(real)
        (self.hub / ".hub-home").write_text(f"{link}\n")
        out = self.purge()
        self.assertEqual(list(real.iterdir()), [])
        self.assertNotIn(wrong, out)
        self.assertIn(f"{link}:", out, "the user's link is named as left")
        self.assertTrue(link.is_symlink())

    def test_notes_never_sent_go_with_the_purge_and_so_does_their_remote(self):
        app = self.connected()
        notes = app / ".agent-memory"
        (notes / "memory" / "today.md").write_text("only here\n")
        self.git_(notes, "add", "-A")
        self.assertEqual(self.git_(notes, "commit", "-q", "-m", "a note").returncode, 0)
        self.purge()
        self.assertEqual(self.git_(app, "branch", "--list", "agent-memory").stdout, "")
        self.assertEqual(self.git_(app, "remote").stdout, "")
        self.assertEqual(self.git_(app, "for-each-ref", "refs/remotes").stdout, "")

    def test_a_remote_of_the_users_is_left(self):
        app = self.connected()
        self.git_(app, "remote", "set-url", "memory", "git@github.com:me/notes-memory.git")
        self.purge()
        self.assertEqual(self.git_(app, "remote", "get-url", "memory").stdout.strip(), "git@github.com:me/notes-memory.git")

    def test_a_branch_that_cant_be_deleted_is_said(self):
        app = self.connected()
        lock = app / ".git" / "refs" / "heads" / "agent-memory.lock"
        lock.write_text("")
        out = self.purge(rc=1)
        self.assertFalse([x for x in out.splitlines() if x.startswith("✓") and ".agent-memory" in x], out)
        self.assertTrue([x for x in out.splitlines() if x.startswith("⚠️") and ".agent-memory" in x], out)
        lock.unlink()
        self.purge()   # its folder is gone, its branch isn't: gone on with
        self.assertEqual(self.git_(app, "branch", "--list", "agent-memory").stdout, "")

    def test_a_folder_that_is_no_memory_repo_is_left(self):
        self.connected()
        bare = self.home / "git-origin" / "app-memory.git"
        shutil.rmtree(bare)
        bare.mkdir()
        (bare / "thesis.txt").write_text("mine")
        self.purge()
        self.assertEqual((bare / "thesis.txt").read_text(), "mine")

    def test_a_repo_of_the_users_with_a_memory_repos_name_is_left(self):
        app = self.connected()
        bare = self.home / "git-origin" / "app-memory.git"
        subprocess.run(["git", "-C", str(app), "push", "-q", str(bare), "HEAD:refs/heads/main"], env=self.env, check=True)
        out = self.purge()
        self.assertTrue((bare / "HEAD").exists())
        self.assertIn(str(bare), out)

    def test_an_instance_that_isnt_there_is_kept_in_mind(self):
        self.init()
        (self.home / ".brunna").rename(self.home / "unplugged")
        self.hub_cmd("uninstall", "--apply", "--yes", "--purge")
        self.assertEqual((self.hub / ".hub-home").read_text().strip(), str(self.home / ".brunna"))

    def test_the_hub_folder_isnt_to_be_removed_while_it_holds_what_is_kept(self):
        self.init()
        inside = self.hub / "instance"
        (self.home / ".brunna").rename(inside)
        (self.hub / ".hub-home").write_text(f"{inside}\n")
        r = self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("rm -rf", r.stdout)
        self.assertIn(str(inside), r.stdout.splitlines()[-1])

    def test_purge_removes_only_a_memory_copy_where_the_copies_are(self):
        """memory_copies set to where the projects are: the project's own folder has the copy's name"""
        app = self.connected()
        self.set_cfg({**self.cfg(), "memory_copies": str(app.parent)})
        self.purge()
        self.assertEqual((app / "a.txt").read_text(), "mine\n")

    def answered(self, *args, answers: str):
        """The command with someone at a terminal typing these answers."""
        import os
        import pty
        master, slave = pty.openpty()
        try:
            p = subprocess.Popen([sys.executable, str(self.hub / "bin" / "agenthub"), *args], env=self.env, cwd=self.hub,
                                 stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            os.write(master, answers.encode())
            out, _err = p.communicate(timeout=120)
        finally:
            os.close(master)
            os.close(slave)
        return p.returncode, out

    def test_the_purge_is_asked_on_its_own_and_enter_is_no(self):
        app = self.connected()
        rc, out = self.answered("uninstall", "--apply", "--purge", answers="\n\n\n\n\n\n")
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.home / ".brunna" / "config" / "brunna.json").exists(), out)
        self.assertTrue((app / ".agent-memory" / "STATE.md").exists(), out)
        self.assertNotIn("brunna:begin", (app / "CLAUDE.local.md").read_text(), "the rest was removed")
        rc, out = self.answered("uninstall", "--apply", "--purge", answers="y\ny\nn\nn\nn\nn\n")
        self.assertFalse((self.home / ".brunna").exists(), out)

    def test_a_removal_that_fails_is_said(self):
        import os
        if os.geteuid() == 0:
            self.skipTest("root removes it anyway")
        self.init()
        data = self.home / ".brunna" / "data"
        (data / "todo.md").write_text("x")
        data.chmod(0o500)
        self.addCleanup(lambda: data.exists() and data.chmod(0o700))
        out = self.purge(rc=1)
        self.assertIn(str(data / "todo.md"), out)
        self.assertNotIn("rm -rf", out, "the hub stays until it is gone")
        self.assertIn(f"{self.hub}/bin/brunna uninstall --apply --purge", out.splitlines()[-1], "what to run again")
        self.assertFalse([x for x in out.splitlines() if x.startswith("✓") and str(self.home / ".brunna") in x])
        data.chmod(0o700)
        self.purge()
        self.assertFalse((self.home / ".brunna").exists(), "run again, it goes on")

    def test_the_instance_stays_until_every_projects_notes_are_gone(self):
        app = self.connected()
        subprocess.run(["git", "-C", str(app), "worktree", "lock", ".agent-memory"], env=self.env, check=True)
        out = self.purge(rc=1)
        self.assertTrue((app / ".agent-memory" / "STATE.md").exists())
        self.assertTrue((self.home / ".brunna" / "config" / "brunna.json").exists(), out)
        subprocess.run(["git", "-C", str(app), "worktree", "unlock", ".agent-memory"], env=self.env, check=True)
        self.purge()
        self.assertFalse((app / ".agent-memory").exists())
        self.assertFalse((self.home / ".brunna").exists())

    def test_a_service_nothing_can_stop_is_not_said_to_be_removed(self):
        self.init()
        (self.hub / ".state" / "rendered-services.json").write_text('["com.example.shop-webhook"]')
        r = self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        lines = [x for x in r.stdout.splitlines() if "com.example.shop-webhook" in x]
        self.assertFalse([x for x in lines if x.startswith("✓")], r.stdout)
        self.assertTrue([x for x in lines if x.startswith("⚠️")], r.stdout)

    def test_what_uninstall_leaves_of_the_services_is_named(self):
        """A unit under a label of ours that isn't ours is named and left, and a service that stayed is said."""
        self.init()
        mine = self.hub / "bin" / "render-services.py"
        mine.write_text(mine.read_text().replace("def main() -> int:\n", """def left_behind():
    return ["/units/local.app.web.plist"]


def uninstall(apply):
    return [] if apply else ["local.app.web"]


def main() -> int:
""", 1))
        r = self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertIn("/units/local.app.web.plist", r.stdout)
        self.assertTrue([x for x in r.stdout.splitlines() if x.startswith("⚠️") and "local.app.web" in x], r.stdout)
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_one_project_with_broken_settings_is_no_reason_to_leave_the_others(self):
        app = self.connected()
        broken = self.home / "ws" / "aaa"
        broken.mkdir()
        (broken / "a.txt").write_text("mine\n")
        r = subprocess.run(["bash", str(self.hub / "bin" / "onboard-project.sh"), "--no-remote", str(broken)],
                           env=self.env, capture_output=True, text=True, timeout=120, cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        settings = broken / ".claude" / "settings.local.json"
        settings.write_text('{"model": "x",')
        (self.hub / ".state" / "rendered-settings").mkdir(exist_ok=True)   # a sandboxed one's
        (self.hub / ".state" / "rendered-settings" / "aaa.json").write_text('{"deny": []}')
        self.assertEqual(self.hub_cmd("uninstall").returncode, 0, "listing it works")
        r = self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("aaa", "".join(x for x in r.stdout.splitlines(True) if x.startswith("⚠️")))
        self.assertEqual(settings.read_text(), '{"model": "x",', "the user's file is left as it is")
        self.assertNotIn("brunna:begin", (app / "CLAUDE.local.md").read_text())
        rules = broken / "CLAUDE.local.md"
        self.assertTrue(not rules.exists() or "brunna:begin" not in rules.read_text(), "its other parts went")

    def test_rules_whose_begin_and_end_dont_pair_are_left_and_said(self):
        app = self.connected()
        rules = app / "CLAUDE.local.md"
        for text in ("<!-- brunna:begin -->\nold\n\nMY RULES\n\n<!-- brunna:begin -->\nnew\n<!-- brunna:end -->\n",
                     "MY RULES\n<!-- brunna:end -->\nmore\n<!-- brunna:begin -->\nx\n",
                     "<!-- agent-hub:begin -->\nold\nMY RULES\n<!-- brunna:end -->\n",
                     "MY RULES\n<!-- brunna:begin -->\nnew\n"):
            with self.subTest(text=text):
                rules.write_text(text)
                r = self.hub_cmd("uninstall", "--apply", "--yes")
                self.assertTrue(rules.exists(), "the user's rules were deleted")
                self.assertEqual(rules.read_text(), text)
                self.assertEqual(r.returncode, 1)
                self.assertIn(str(rules), r.stdout)

    def test_rules_keep_their_mode_their_link_and_their_text_around_the_block(self):
        app = self.connected()
        rules, real = app / "CLAUDE.local.md", self.home / "dotfiles-rules.md"
        real.write_bytes(b"mine\r\n\r\n<!-- brunna:begin -->\r\nours\r\n<!-- brunna:end -->\r\n\r\nand more\r\n"
                         b"a line about `<!-- brunna:begin -->` in prose\r\n")
        real.chmod(0o600)
        rules.unlink()
        rules.symlink_to(real)
        r = self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(rules.is_symlink())
        self.assertEqual(real.read_bytes(), b"mine\r\n\r\nand more\r\na line about `<!-- brunna:begin -->` in prose\r\n")
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)

    def test_a_script_of_the_users_that_calls_brunna_is_left(self):
        self.init()
        f = self.home / ".local" / "bin" / "brunna"
        f.parent.mkdir(parents=True)
        mine = f'#!/bin/sh\n# my own\ncd "$HOME"\nexec {self.hub / "bin" / "brunna"} "$@"\n'
        f.write_text(mine)
        self.assertEqual(self.hub_cmd("uninstall", "--apply", "--yes").returncode, 0)
        self.assertTrue(f.exists() and f.read_text() == mine)
        f.unlink()
        self.hub_cmd("install-cli")
        self.assertTrue(f.is_file())
        own = f.read_text()
        f.write_text(own + "# and a line of mine\n")
        self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertTrue(f.exists(), "more than brunna's own in it")
        f.write_text(own.replace(str(self.hub), "/another/hub"))
        self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertTrue(f.exists(), "another hub's")
        (f.parent / "agenthub").write_text(own)
        f.unlink()
        f.symlink_to(f.parent / "agenthub")
        self.hub_cmd("uninstall", "--apply", "--yes")
        self.assertTrue(f.is_symlink(), "the user's link")
        self.assertFalse((f.parent / "agenthub").exists(), "brunna's own goes")

    def test_refused_without_a_yes(self):
        self.connected()
        r = self.hub_cmd("uninstall", "--apply")   # no terminal and no --yes: nothing
        self.assertEqual(r.returncode, 1)
        self.assertIn("brunna:begin", (self.home / "ws" / "app" / "CLAUDE.local.md").read_text())


class Parts(unittest.TestCase):
    def test_every_block_goes_and_every_line_of_the_users_stays(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("uninstall_s", ROOT / "bin" / "uninstall.py")
        un = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(un)
        new = "<!-- brunna:begin -->\nours\n<!-- brunna:end -->\n"
        old = "<!-- agent-hub:begin -->\nours\nours too\n<!-- agent-hub:end -->\n"
        for first in (new, old):
            for second in (new, old):
                for text, want in ((f"a\n\n{first}\nb\n\n{second}\nc\n", "a\n\nb\n\nc\n"),
                                   (f"a\n{first}b\n{second}", "a\n\nb\n"),
                                   (f"{first}b\n{second}", "b\n"), (f"{first}{second}c\n", "c\n"),
                                   (f"a\n{first}{second}", "a\n"), (f"{first}\n{second}", ""),
                                   (f"a\n{first}b\n{second}c\n{new}d", "a\n\nb\n\nc\n\nd")):
                    with self.subTest(text=text):
                        self.assertEqual(un.strip_text(text), (want, True))
        self.assertEqual(un.strip_text("\nno block\n\n"), ("\nno block\n\n", True))

    def test_a_folder_that_cant_be_removed_is_reported_with_why(self):
        import tempfile

        import instance_dir
        home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / "config").mkdir()
        (home / "config" / "brunna.json").write_text('{"v": 1}')
        instance_dir.mark(home)
        with mock.patch.object(instance_dir.shutil, "rmtree",
                               lambda p, onexc: onexc(None, str(p), OSError(16, "Resource busy"))):
            failed, _kept = instance_dir.purge(home)
        self.assertEqual(failed, [f"{home / 'config'}: Resource busy"])
        self.assertTrue((home / instance_dir.MARK).exists(), "still the folder a second run knows")

    def test_settings_with_only_the_platforms_hooks_go(self):
        import tempfile
        ws = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        (ws / ".claude").mkdir()
        f = ws / ".claude" / "settings.local.json"
        f.write_text(json.dumps(runtime_claude.install_hooks({})))
        self.assertTrue(runtime_claude.remove_lifecycle(ws, check=True))
        self.assertTrue(f.exists(), "check writes nothing")
        self.assertTrue(runtime_claude.remove_lifecycle(ws))
        self.assertFalse(f.exists())
        self.assertFalse(runtime_claude.remove_lifecycle(ws))

    def test_another_hubs_scheduler_is_left_alone(self):
        import importlib.util
        import tempfile
        home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = importlib.util.spec_from_file_location("uninstall_t", ROOT / "bin" / "uninstall.py")
        un = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(un)
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "local.agent-hub-scheduler.plist").write_bytes(plistlib.dumps(
            {"Label": "local.agent-hub-scheduler", "ProgramArguments": ["/elsewhere/agent-hub/bin/py", "x"]}))
        (agents / "local.agent-hub-scheduler-watchdog.plist").write_bytes(plistlib.dumps(
            {"Label": "w", "ProgramArguments": [str(un.hubconf.MAIN / "bin" / "py"), "x"]}))
        with mock.patch.object(un.pathlib.Path, "home", classmethod(lambda cls: home)), \
                mock.patch.object(un.hubconf, "service_manager", lambda: "launchd"):
            self.assertEqual([label for label, _undo in un.core_units()], ["local.agent-hub-scheduler-watchdog"])


if __name__ == "__main__":
    unittest.main()
