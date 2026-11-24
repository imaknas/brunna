"""render-project-settings: what of the user's own settings can't stay while a project is sandboxed is kept, the owner is
told of a file that can't be read, and turning the sandbox off puts it back. The sandbox is enforced throughout.

    bin/py -m unittest tests.test_settings_kept
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import alert  # noqa: E402
import hubconf  # noqa: E402
import isolation  # noqa: E402
import names  # noqa: E402

MINE = {"sandbox": {"enabled": True, "network": {"allowedDomains": ["mine.example"]}},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-hook"}]}]},
        "env": {"MINE": "1", "UV_CACHE_DIR": "/my/cache"},
        "permissions": {"allow": ["Bash(make:*)"], "deny": ["Read(/my/diary)"]}}
BAD = '{"permissions": {"allow": ["Bash(make:*)",]}}\n'


def load():
    loader = importlib.machinery.SourceFileLoader("rps_kept", str(BIN / "render-project-settings.py"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.m = load()
        self.m.RENDERED = self.tmp / "state" / "rendered-settings"
        self.ws = self.tmp / "ws" / "proj"
        (self.ws / ".claude").mkdir(parents=True)
        self.f = self.ws / ".claude" / "settings.local.json"
        self.doc = {"name": "proj", "paths": {"workspace": str(self.ws)}, "enforce": {"sandbox": True}}
        self.index = {"projects": {"proj": self.doc}}
        self.alerts, self.sent = {}, []
        p = mock.patch.dict(os.environ)   # what is told is looked for in English, whatever language the run has
        p.start()
        self.addCleanup(p.stop)
        names.env_unset(os.environ, "LANG_UI")
        names.env_set(os.environ, "LANG_UI", "en")

        def once(key, problems, ok_text, ids=None):
            self.alerts[key] = problems
            self.sent.append(problems)
            return True
        for p in (mock.patch.object(hubconf, "projects", lambda: self.index["projects"]),
                  mock.patch.object(isolation, "wrap", lambda *a, **kw: ["/usr/bin/false"]),
                  mock.patch.object(self.m, "look", lambda *_a: ([], [], [])),   # no sandbox here to walk the folder in
                  mock.patch.object(self.m, "desired_env", lambda *_a: {"UV_CACHE_DIR": "/platform/cache"}),
                  mock.patch.object(self.m.registry, "refresh", lambda *a, **kw: self.index),
                  mock.patch.object(alert, "once", once),
                  mock.patch.object(alert, "open_keys", lambda prefix: [k for k, v in self.alerts.items() if v])):
            p.start()
            self.addCleanup(p.stop)

    def on(self):
        with contextlib.redirect_stderr(io.StringIO()):
            drift = self.m.write_project("proj", self.doc, self.index, report=False)
        now = json.loads(self.f.read_text())
        self.assertTrue(now["sandbox"]["enabled"])
        self.assertNotEqual(now["sandbox"], MINE["sandbox"], "the platform's sandbox")
        self.assertNotIn("my-own-hook", self.f.read_text(), "no hook the platform didn't write")
        return drift

    def off(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.unapply("proj", {k: v for k, v in self.doc.items() if k != "enforce"})

    def write(self, content, mode=0o600):
        self.f.write_text(content if isinstance(content, str) else json.dumps(content))
        self.f.chmod(mode)

    def copy_files(self):
        """Every copy kept, wherever it is (not the live settings, the record, or notes of a link)."""
        return [f for f in self.tmp.rglob("*") if f.is_file() and not f.is_symlink() and f != self.f and f.name != "proj.json"
                and f.name != "untold.json" and not {"known", "coming", "renders"} & set(f.parts) and not f.name.startswith("before") and "dotfiles" not in f.parts and "ws" not in f.relative_to(self.tmp).parts[:1] and f.name != "zother.json"]

    def copies(self):
        """{content: file} of every copy kept, wherever it is."""
        return {f.read_text(): f for f in self.tmp.rglob("*") if f.is_file() and f != self.f and not f.is_symlink()
                and f.name != "proj.json" and not {"known", "coming", "renders"} & set(f.parts)}


class Kept(Base):
    def test_off_puts_back_what_was_the_users(self):
        self.write(MINE)
        self.on()
        self.on()
        self.off()
        now = json.loads(self.f.read_text())
        self.assertEqual(now["sandbox"], MINE["sandbox"])
        self.assertEqual([e for e in now["hooks"]["Stop"] if e in MINE["hooks"]["Stop"]], MINE["hooks"]["Stop"])
        self.assertEqual(now["env"], MINE["env"])
        self.assertEqual(now["permissions"]["allow"], MINE["permissions"]["allow"])
        self.assertEqual(now["permissions"]["deny"], MINE["permissions"]["deny"])
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o600)

    def test_a_rule_of_the_users_that_the_platform_has_too_stays(self):
        self.on()
        rule = json.loads(self.f.read_text())["permissions"]["deny"][0]
        self.off()
        self.write({"permissions": {"deny": [rule]}})
        self.on()
        self.off()
        self.assertEqual(json.loads(self.f.read_text())["permissions"]["deny"], [rule])

    def test_what_was_added_while_it_was_on_stays(self):
        self.write(MINE)
        self.on()
        now = json.loads(self.f.read_text())
        now["permissions"]["allow"].append("Bash(always:*)")
        self.write(now)
        self.on()
        self.off()
        self.assertEqual(json.loads(self.f.read_text())["permissions"]["allow"], ["Bash(make:*)", "Bash(always:*)"])

    def test_on_again_keeps_what_is_there_then(self):
        self.write(MINE)
        self.on()
        self.off()
        second = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-hook 2"}]}]}}
        self.write(second)
        self.on()
        self.off()
        now = self.f.read_text()
        self.assertIn("my-own-hook 2", now)
        self.assertNotIn("sandbox", json.loads(now), "the first time's is not put back again")
        self.assertIn(json.dumps(MINE), self.copies(), "still kept")

    def test_invalid_json_is_kept_and_told_and_the_sandbox_written(self):
        self.on()
        self.write(BAD)
        drift = self.on()
        copy = self.copies()[BAD]
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.ws, copy.parents, "not where the project could write it")
        self.assertTrue(any(str(copy) in d for d in drift), drift)
        [(key, [text])] = self.alerts.items()
        self.assertEqual(alert.route(key), "owner")
        self.assertIn(str(copy), text)
        self.on()
        self.assertEqual(len(self.alerts[key]), 1, "until the copy is dealt with")
        copy.unlink()
        self.on()
        self.assertEqual(self.alerts[key], [])

    def test_a_later_run_never_replaces_a_copy(self):
        other = BAD.replace("make", "other")
        self.write(BAD)
        self.on()
        self.write(other)
        self.on()
        self.assertLessEqual({BAD, other}, set(self.copies()))

    def test_invalid_json_before_the_sandbox_is_put_back(self):
        self.write(BAD)
        self.on()
        self.off()
        self.assertEqual(self.f.read_text(), BAD)
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o600)
        self.assertEqual([v for v in self.alerts.values() if v], [], "back where the user keeps it")

    def test_invalid_json_before_the_sandbox_never_replaces_what_came_since(self):
        self.write(BAD)
        self.on()
        now = json.loads(self.f.read_text())
        now["permissions"]["allow"].append("Bash(always:*)")
        self.write(now)
        self.on()
        self.off()
        self.assertEqual(json.loads(self.f.read_text())["permissions"]["allow"], ["Bash(always:*)"])
        self.assertIn(BAD, self.copies())

    def test_settings_of_another_shape_are_kept_too(self):
        for text in ("[1]", '{"permissions": ["Bash(make:*)"]}', '{"env": "x"}', '{"permissions": {"deny": "Read(x)"}}'):
            with self.subTest(text):
                self.write(text)
                self.on()
                self.assertIn(text, self.copies())

    def test_off_leaves_a_file_it_cant_read(self):
        self.on()
        self.write(BAD)
        self.off()
        self.off()
        self.assertEqual(self.f.read_text(), BAD)
        self.assertEqual([bool(p) for p in self.sent], [True, True], "said, and open until it is put right")
        self.write({"permissions": MINE["permissions"]})   # put right: the next run goes on
        self.off()
        self.assertEqual([v for v in self.alerts.values() if v], [])
        self.assertFalse((self.m.RENDERED / "proj.json").exists())

    def test_a_hook_planted_while_it_is_on_is_kept_but_never_put_back(self):
        self.on()
        now = json.loads(self.f.read_text())
        now["hooks"]["Stop"] = MINE["hooks"]["Stop"]
        self.write(now)
        self.on()
        self.assertTrue(any("my-own-hook" in c for c in self.copies()))
        self.off()
        self.assertNotIn("my-own-hook", self.f.read_text())

    def test_a_lost_record_never_makes_the_platforms_settings_the_users(self):
        for broken in (None, "", '{"sandbox": ', "[]"):
            with self.subTest(broken):
                self.f.unlink(missing_ok=True)
                shutil.rmtree(self.m.RENDERED, ignore_errors=True)
                self.write(MINE)
                self.on()
                rec = self.m.RENDERED / "proj.json"
                rec.unlink() if broken is None else rec.write_text(broken)
                self.on()
                if broken is not None:
                    rec.write_text(broken)   # unreadable when it is turned off as well
                self.off()
                now = json.loads(self.f.read_text())
                self.assertEqual(now["sandbox"], MINE["sandbox"])
                self.assertIn("my-own-hook", self.f.read_text())
                if broken is not None:
                    self.assertEqual(now["permissions"]["deny"], MINE["permissions"]["deny"])

    def no_room(self):
        """The disk with the platform's state is full: nothing more is kept there."""
        real = self.m.hubconf.write_atomic

        def write(path, *a, **kw):
            if "saved" in pathlib.Path(path).parts:
                raise OSError(28, "No space left on device")
            return real(path, *a, **kw)
        return mock.patch.object(self.m.hubconf, "write_atomic", write)

    def test_a_copy_that_cant_be_made_doesnt_stop_the_sandbox(self):
        """The file itself stays, under a name the tool doesn't read."""
        self.write(BAD)
        with self.no_room():
            self.on()
        [copy] = [f for f in (self.ws / ".claude").iterdir() if f != self.f]
        self.assertEqual((copy.read_text(), copy.stat().st_mode & 0o777), (BAD, 0o600))
        self.assertNotIn(copy.name, ("settings.json", "settings.local.json"))

    def test_a_name_put_there_first_doesnt_take_the_copys_place(self):
        self.write(BAD)
        for n in (f"{self.f.name}.kept-{self.f.stat().st_ino}", f"{self.f.name}.kept"):
            (self.f.parent / n).write_text("planted")
        with self.no_room():
            drift = self.on()
        self.assertTrue(any(f.read_text() == BAD for f in self.f.parent.iterdir()), drift)

    def test_a_full_disk_leaves_the_sandbox_that_is_there(self):
        """Neither a copy nor the new settings can be written: what is there, its sandbox included, stays."""
        self.on()
        now = json.loads(self.f.read_text())
        now["hooks"]["Stop"] = MINE["hooks"]["Stop"]
        self.write(now)
        with self.no_room(), mock.patch.object(self.m, "write_settings", side_effect=OSError(28, "full")):
            for _i in range(2):
                self.assertTrue(self.m.write_project("proj", self.doc, self.index, report=False))
        self.assertEqual(json.loads(self.f.read_text()), now)
        self.assertEqual(len(list((self.ws / ".claude").iterdir())), 2, "one second name, however often it is tried")

    def test_a_folder_that_is_a_link_is_said_not_a_crash(self):
        real = self.tmp / "elsewhere"
        (self.ws / ".claude").rename(real)
        (self.ws / ".claude").symlink_to(real)
        (real / "settings.local.json").write_text(BAD)
        drift = self.m.write_project("proj", self.doc, self.index, report=False)
        self.assertTrue(drift)
        self.assertEqual((real / "settings.local.json").read_text(), BAD)

    def test_an_alert_that_breaks_doesnt_stop_the_sandbox(self):
        self.write(BAD)
        with mock.patch.object(alert, "once", side_effect=OSError("no state")):
            self.on()

    def test_the_record_is_never_half_written(self):
        import resource
        import signal
        self.on()
        rec = self.m.RENDERED / "proj.json"
        before = rec.read_bytes()
        pid = os.fork()
        if pid == 0:
            try:
                signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
                resource.setrlimit(resource.RLIMIT_FSIZE, (8, resource.RLIM_INFINITY))
                with mock.patch.object(self.m, "write_settings"):   # the record's write is the one that fails
                    self.m.write_project("proj", {**self.doc, "network": ["x.example"]}, self.index, report=False)
            except BaseException:
                pass
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        self.assertEqual(rec.read_bytes(), before)

    def test_report_keeps_and_tells_nothing(self):
        self.write(BAD)
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.write_project("proj", self.doc, self.index, report=True)
        self.assertEqual((self.f.read_text(), self.copies(), self.alerts), (BAD, {}, {}))


def nested(depth: int) -> str:
    return '{"x": ' + "[" * depth + "]" * depth + "}"


class Malformed(Base):
    """A settings file in any shape: kept, told, the sandbox written; never an error that stops the run."""

    SHAPES = ('{"x": 1e999}', '{"permissions": {"deny": [{}]}}', '{"permissions": {"allow": [1]}}', nested(50_000), nested(400),
              '{"x": NaN}', '{"x": "\\ud800"}', "\xff\xfe", '{"x": ' + "9" * 5000 + "}")

    def test_it_is_invalid(self):
        for text in self.SHAPES:
            with self.subTest(text[:30]):
                self.f.write_bytes(text.encode("latin-1"))
                self.on()
                self.assertTrue([v for v in self.alerts.values() if v])
                self.assertIn(text.encode("latin-1"), [c.read_bytes() for c in self.copy_files()])

    def test_what_couldnt_be_read_is_back_as_it_was(self):
        for text in self.SHAPES:
            if text.startswith('{"permissions'):   # these can: only what the tool doesn't take is out meanwhile
                continue
            with self.subTest(text[:30]):
                shutil.rmtree(self.m.RENDERED, ignore_errors=True)
                self.f.write_bytes(text.encode("latin-1"))
                self.on()
                self.off()
                self.assertEqual(self.f.read_bytes(), text.encode("latin-1"))

    def test_in_a_worktree_too(self):
        """Its own file, and what the workspace's gives it: its sandbox is written, the record made."""
        wf = self.ws / ".claude" / "worktrees" / "w1" / ".claude" / "settings.local.json"
        wf.parent.mkdir(parents=True)
        for text in (*self.SHAPES[:3], '{"permissions": {"additionalDirectories": 1}}', '{"permissions": {"sandbox": [{}], "deny": ["a"]}}',
                     '{"permissions": {"defaultMode": [1]}}', '{"hooks": {"Stop": 1}}'):
            for f in (self.f, wf):
                with self.subTest(text[:40], where=f.parent.parent.name):
                    (self.m.RENDERED / "proj.json").unlink(missing_ok=True)
                    self.write({"permissions": {"defaultMode": "plan"}})
                    f.write_text(text)
                    self.on()
                    self.on()
                    self.assertTrue(json.loads(wf.read_text())["sandbox"]["enabled"])
                    self.assertTrue((self.m.RENDERED / "proj.json").exists())

    def test_hooks_in_any_shape(self):
        for hooks in ([1], "x", {"Stop": 1}, {"Stop": [1]}, {"Stop": [{"hooks": 1}]}, {"WorktreeCreate": {"a": 1}}):
            with self.subTest(hooks):
                self.write({"hooks": hooks, "permissions": {"allow": ["Bash(make:*)"]}})
                self.on()
                self.assertIn("Bash(make:*)", json.loads(self.f.read_text())["permissions"]["allow"])
                self.off()

    def test_a_record_in_any_shape_counts_as_absent(self):
        for rec in ('{"deny": [{}]}', '{"env": 1}', '{"allow": "x"}', nested(50_000)):
            with self.subTest(rec[:30]):
                self.on()
                (self.m.RENDERED / "proj.json").write_text(rec)
                self.on()

    def test_its_own_committed_settings_dont_stop_it(self):
        (self.ws / ".claude" / "settings.json").write_text(nested(50_000))
        self.assertTrue(self.on(), "said")


class OneAtATime(Base):
    """Whatever goes wrong with one project, the next one's sandbox is written."""

    def setUp(self):
        super().setUp()
        self.other = self.tmp / "ws" / "other"
        (self.other / ".claude").mkdir(parents=True)
        self.index["projects"]["zother"] = {"name": "zother", "paths": {"workspace": str(self.other)}, "enforce": {"sandbox": True}}
        for p in (mock.patch.object(self.m.hubconf, "worktree_lookup", lambda: False),
                  mock.patch.object(sys, "argv", ["render-project-settings.py"])):
            p.start()
            self.addCleanup(p.stop)

    def other_is_sandboxed(self):
        f = self.other / ".claude" / "settings.local.json"
        return f.exists() and json.loads(f.read_text())["sandbox"]["enabled"]

    def runs(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.m.main()
            self.m.apply(self.index)
        return rc, out.getvalue() + err.getvalue()

    def test_each_step(self):
        real = {n: getattr(self.m, n) for n in ("gate", "compose", "push_folder", "foreign_controls", "keep_what_goes", "keep_before", "tell")}
        for step, boom in (("gate", TypeError), ("compose", RecursionError), ("push_folder", TypeError),
                           ("foreign_controls", RecursionError), ("keep_what_goes", KeyError), ("keep_before", UnicodeEncodeError)):
            with self.subTest(step):
                self.f.write_text(BAD)
                (self.other / ".claude" / "settings.local.json").unlink(missing_ok=True)

                def fails(*a, step=step, boom=boom, **kw):
                    if a and (a[0] == "proj" or a[0] == self.ws):
                        raise boom("utf-8", "x", 0, 1, "made up") if boom is UnicodeEncodeError else boom("made up")
                    return real[step](*a, **kw)
                with mock.patch.object(self.m, step, fails):
                    rc, said = self.runs()
                self.assertTrue(self.other_is_sandboxed())
                self.assertEqual(rc, 1)
                self.assertIn("proj", said)
                if step not in ("compose", "push_folder"):   # nor its own
                    self.assertTrue(json.loads(self.f.read_text())["sandbox"]["enabled"])

    def test_one_whose_hooks_cant_be_merged(self):
        """Not sandboxed, its settings in a shape the session hooks can't be put into."""
        self.doc.pop("enforce")
        (self.ws / ".agent-memory").mkdir()
        for text in ('{"hooks": [1]}', '{"hooks": {"Stop": [1]}}', "[1]", nested(50_000)):
            with self.subTest(text[:20]):
                self.f.write_text(text)
                (self.other / ".claude" / "settings.local.json").unlink(missing_ok=True)
                self.runs()
                self.assertTrue(self.other_is_sandboxed())
                self.assertEqual(self.f.read_text(), text)

    def test_a_drift_that_isnt_text(self):
        """A file name a project gave needn't be UTF-8, and the job's output is."""
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8")
        with mock.patch.object(self.m, "foreign_controls", lambda *_a: ["a\udcff.md"]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = self.m.main()
        out.flush()
        self.assertEqual(rc, 1)
        self.assertEqual(raw.getvalue().count(b"a\\udcff.md"), 2, "both projects'")
        self.assertTrue(self.other_is_sandboxed())

    def test_a_print_that_fails_is_only_that_projects(self):
        real = print

        def failing(*a, **kw):
            if a and "proj'" in str(a[0]):
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "made up")
            return real(*a, **kw)
        self.f.write_text(BAD)
        with mock.patch.object(self.m, "print", failing, create=True):
            self.assertEqual(self.runs()[0], 1)
        self.assertTrue(self.other_is_sandboxed())

    def test_a_fifo_among_its_files_holds_nothing_up(self):
        """Its settings are written before its other files are looked at, and none of those is waited on."""
        import subprocess
        for d in ("agents", "skills/x"):
            (self.ws / ".claude" / d).mkdir(parents=True)
            os.mkfifo(self.ws / ".claude" / d / "a.md")
        code = f"""
import importlib.machinery, importlib.util, json, pathlib, sys
sys.path.insert(0, {str(BIN)!r})
import runtime_claude
print(runtime_claude.foreign_controls(pathlib.Path({str(self.ws)!r})))
"""
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertTrue("agents/a.md" in r.stdout and "skills/x/a.md" in r.stdout, r.stdout + r.stderr)
        order = []
        with mock.patch.object(self.m, "foreign_controls", lambda *a: order.append(self.f.exists()) or []):
            self.runs()
        self.assertEqual(order[0], True, "the sandbox first")

    def test_a_fifo_as_an_unsandboxed_projects_settings_holds_nothing_up(self):
        import subprocess
        self.f.unlink(missing_ok=True)
        os.mkfifo(self.f)
        code = f"import sys; sys.path.insert(0, {str(BIN)!r}); import runtime_claude; runtime_claude.install_lifecycle({str(self.ws)!r}, check=True)"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)

    def test_a_plan_says_the_one_it_cant_tell(self):
        with mock.patch.object(self.m, "compose", side_effect=TypeError("made up")):
            self.assertIn("agent-settings:proj", [c.target for c in self.m.plan(self.index)])


class Links(Base):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "dotfiles" / "settings.json"
        self.target.parent.mkdir()
        self.target.write_text(json.dumps(MINE))
        self.f.symlink_to(self.target)

    def test_it_is_named_and_comes_back(self):
        drift = self.on()
        self.on()
        self.assertTrue(any(str(self.target) in d for d in drift), drift)
        self.assertEqual([p for p in self.sent if any(str(self.target) in t for t in p)], [self.sent[0]], "the owner, once")
        self.assertEqual(alert.route(next(iter(self.alerts))), "owner")
        self.assertEqual(json.loads(self.target.read_text()), MINE)
        self.off()
        self.assertEqual(os.readlink(self.f), str(self.target))
        self.assertEqual(json.loads(self.target.read_text()), MINE)
        self.assertEqual([v for v in self.alerts.values() if v], [], "and its item is closed")

    def test_what_was_added_while_it_was_on_is_kept(self):
        self.on()
        now = json.loads(self.f.read_text())
        now["permissions"]["allow"].append("Bash(always:*)")
        self.write(now)
        self.on()
        self.off()
        self.assertTrue(self.f.is_symlink())
        self.assertTrue(any("Bash(always:*)" in c for c in self.copies()))

    def test_one_planted_while_it_is_on_never_comes_back(self):
        self.f.unlink()
        self.on()
        self.f.unlink()
        self.f.symlink_to(self.target)
        drift = self.on()
        self.assertTrue(any(str(self.target) in d for d in drift), drift)
        self.off()
        self.assertFalse(self.f.is_symlink())

    def test_a_target_that_isnt_text(self):
        """Where a link points need be no text: the sandbox is written, the owner told, the same link back."""
        target = os.fsencode(self.target) + b"-\xff\xfe"
        self.f.unlink()
        os.symlink(target, os.fsencode(self.f))
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            drift = self.m.write_project("proj", self.doc, self.index, report=False)
            self.assertTrue(json.loads(self.f.read_text())["sandbox"]["enabled"])
            print("; ".join(drift))
            self.assertTrue([v for v in self.alerts.values() if v])
            "".join(next(iter(self.alerts.values()))).encode()
            self.m.unapply("proj", {k: v for k, v in self.doc.items() if k != "enforce"})
            out.flush()
        self.assertEqual(os.readlink(os.fsencode(self.f)), target)

    def test_one_the_user_put_back_themselves(self):
        """Before the sandbox went off, or by a run that stopped right after: it is done, and nothing else is said of it."""
        self.on()
        self.f.unlink()
        self.f.symlink_to(self.target)
        self.off()
        self.assertEqual(os.readlink(self.f), str(self.target))
        self.assertFalse((self.m.RENDERED / "proj.json").exists())
        self.assertEqual([v for v in self.alerts.values() if v], [])

    def test_the_owner_is_told_again_until_it_has_gone(self):
        with mock.patch.object(alert, "once", side_effect=OSError("no state")):
            self.on()
        self.on()
        self.assertEqual(len(self.sent), 1)


class OffAndOn(Base):
    def test_off_then_on_before_the_hooks_are_installed_again(self):
        self.write(MINE)
        self.on()
        self.off()
        mine2 = json.loads(self.f.read_text())
        mine2["sandbox"]["network"]["allowedDomains"] = ["second.example"]
        self.write(mine2)
        self.on()
        self.off()
        self.assertEqual(json.loads(self.f.read_text())["sandbox"]["network"]["allowedDomains"], ["second.example"])
        self.assertNotIn("worktree-make", self.f.read_text())
        self.assertIn("SessionStart", json.loads(self.f.read_text())["hooks"], "the session hooks any project has stay")

    def test_off_deleted_then_on_and_off_puts_nothing_back(self):
        self.write(MINE)
        self.on()
        self.off()
        self.f.unlink()
        self.on()
        self.off()
        self.assertNotIn("mine.example", self.f.read_text())

    def test_one_a_crash_left_isnt_what_comes_back(self):
        """The first run stopped after keeping the file, before writing anything: the user's next edit counts."""
        self.write(MINE)
        with mock.patch.object(self.m, "write_settings", side_effect=OSError(28, "full")):
            self.m.write_project("proj", self.doc, self.index, report=False)
        later = {**MINE, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-hook later"}]}]},
                 "sandbox": {"later": True}}
        self.write(later)
        self.on()
        self.off()
        now = json.loads(self.f.read_text())
        self.assertEqual(now["sandbox"], {"later": True})
        self.assertIn("my-own-hook later", self.f.read_text())

    def test_one_a_crash_left_with_the_file_gone_since(self):
        self.write(MINE)
        with mock.patch.object(self.m, "write_settings", side_effect=OSError(28, "full")):
            self.m.write_project("proj", self.doc, self.index, report=False)
        self.f.unlink()
        self.on()
        self.off()
        self.assertNotIn("mine.example", self.f.read_text())

    def test_a_policy_that_couldnt_be_written_isnt_on_record(self):
        self.on()
        rec = (self.m.RENDERED / "proj.json").read_text()
        with mock.patch.object(self.m, "write_settings", side_effect=OSError(28, "full")), \
                mock.patch.object(self.m, "desired_env", lambda *_a: {"UV_CACHE_DIR": "/new"}):
            self.m.write_project("proj", self.doc, self.index, report=False)
        self.assertEqual((self.m.RENDERED / "proj.json").read_text(), rec)

    def test_invalid_while_on_then_off_puts_back_all_there_was_before(self):
        self.write({**MINE, "model": "x"})
        self.on()
        self.write(BAD)
        self.on()
        self.off()
        now = json.loads(self.f.read_text())
        self.assertEqual(now["permissions"]["allow"], MINE["permissions"]["allow"])
        self.assertEqual(now["permissions"]["deny"], MINE["permissions"]["deny"])
        self.assertEqual((now["env"], now["sandbox"], now["model"]), (MINE["env"], MINE["sandbox"], "x"))

    def test_a_rule_removed_while_on_stays_removed(self):
        self.write(MINE)
        self.on()
        now = json.loads(self.f.read_text())
        now["permissions"]["allow"].remove("Bash(make:*)")
        self.write(now)
        self.on()
        self.off()
        self.assertNotIn("Bash(make:*)", self.f.read_text())


class Copies(Base):
    def test_a_sandbox_changed_while_on_is_kept(self):
        self.on()
        now = json.loads(self.f.read_text())
        self.write({**now, "sandbox": {**now["sandbox"], "by": "hand"}})
        self.on()
        self.assertTrue(any('"by": "hand"' in c for c in self.copies()))

    def test_an_env_variable_changed_while_on_is_kept(self):
        self.on()
        now = json.loads(self.f.read_text())
        now["env"]["UV_CACHE_DIR"] = "/changed/by/hand"
        self.write(now)
        self.on()
        self.assertTrue(any("/changed/by/hand" in c for c in self.copies()))

    def older(self):
        for f in self.copy_files():   # in order, whatever the clock's grain
            os.utime(f, (f.stat().st_mtime - 10,) * 2)

    def test_there_is_a_limit_and_the_newest_stay(self):
        self.write(MINE)
        self.on()
        for i in range(self.m.KEEP_MAX + 5):
            self.write({**MINE, "sandbox": {"n": f"made-{i}"}})
            self.on()
            self.older()
        kept = [f for f in self.copy_files() if f.name not in ("before.json", "untold.json")]
        self.assertEqual(len(kept), self.m.KEEP_MAX)
        self.assertIn(f"made-{i}", "".join(f.read_text() for f in kept))
        self.off()
        self.assertEqual(json.loads(self.f.read_text())["sandbox"], MINE["sandbox"], "what comes back is never dropped")

    def test_one_the_owner_hasnt_dealt_with_stays(self):
        self.on()
        self.write(BAD)
        self.on()
        self.older()
        for i in range(self.m.KEEP_MAX + 5):
            self.write({**MINE, "sandbox": {"n": i}})
            self.on()
            self.older()
        self.assertIn(BAD, self.copies())
        self.assertTrue(self.alerts["project-settings:proj"], "nor is its alert closed")

    def test_invalid_ones_have_a_limit_too(self):
        """Beyond it the file itself stays, under another name: nothing is lost, and nothing the owner waits for goes."""
        self.on()
        for i in range(self.m.KEEP_MAX + 2):
            self.write(BAD.replace("make", f"make{i}."))
            self.on()
        self.assertEqual(len([f for f in self.copy_files() if f.name.endswith(".invalid.json")]), self.m.KEEP_MAX)
        self.assertEqual({i for i in range(self.m.KEEP_MAX + 2) if any(f"make{i}." in c for c in self.copies())}, set(range(self.m.KEEP_MAX + 2)))


class NotTaken(Base):
    """The tool loads nothing of a file with one value it doesn't take, the sandbox included: only what it takes whatever
    its value stands next to the platform's."""

    GOOD = {"permissions": {"allow": ["Bash(make:*)"], "deny": ["Read(/d)"], "ask": ["Bash(git push:*)"],
                            "additionalDirectories": ["./x"], "defaultMode": "plan"},
            "env": {"MINE": "1"}, "model": "sonnet", "outputStyle": "x", "language": "en", "enabledMcpjsonServers": ["a"],
            "disabledMcpjsonServers": ["b"], "enableAllProjectMcpServers": False}
    BADS = ({"model": 5}, {"permissions": {"defaultMode": 7}}, {"permissions": {"defaultMode": "no-such"}}, {"enabledPlugins": "x"},
            {"enabledPlugins": {"a@b": True}}, {"statusLine": {"type": "command", "command": "x"}}, {"notAKey": 5}, {"language": []},
            {"enabledMcpjsonServers": 5}, {"enableAllProjectMcpServers": "x"}, {"env": {"A": 5}}, {"env": "x"}, {"permissions": "x"},
            {"permissions": {"allow": "x"}}, {"permissions": {"additionalDirectories": [5]}}, {"permissions": {"whatever": []}},
            {"disableAllHooks": True}, {"apiKeyHelper": "x"}, {"cleanupPeriodDays": -1})

    def test_what_is_taken_stays(self):
        self.write(self.GOOD)
        self.on()
        now = json.loads(self.f.read_text())
        for k, v in self.GOOD.items():
            if k in ("permissions", "env"):
                for i, w in v.items():
                    self.assertTrue(set(w) <= set(now[k][i]) if isinstance(w, list) else now[k][i] == w, (k, i))
            else:
                self.assertEqual(now[k], v)
        self.assertEqual(self.sent, [], "nothing to say")

    def test_what_isnt_is_kept_said_and_comes_back(self):
        for bad in self.BADS:
            with self.subTest(bad):
                shutil.rmtree(self.m.RENDERED, ignore_errors=True)
                self.alerts.clear()
                self.write(bad)
                drift = self.on()
                now = json.loads(self.f.read_text())
                top, inner = next(iter(bad.items()))
                if isinstance(inner, dict) and top in ("permissions", "env"):
                    for k, v in inner.items():
                        left = now.get(top, {}).get(k)
                        self.assertFalse(set(map(str, v)) & set(left) if isinstance(v, list) and left else left == v, (k, left))
                else:
                    self.assertNotEqual(now.get(top), inner)
                self.assertTrue(any(top in t for v in self.alerts.values() for t in v), self.alerts)
                self.assertTrue(drift)
                self.assertIn(json.dumps(bad), self.copies())
                self.assertEqual(self.on(), [], "said once")
                self.off()
                back = json.loads(self.f.read_text())
                for k, v in bad.items():
                    if isinstance(v, dict) and k in ("permissions", "env"):
                        self.assertEqual({i: back[k].get(i) for i in v}, v)
                    else:
                        self.assertEqual(back.get(k), v)

    def test_one_put_in_while_it_is_on(self):
        self.on()
        now = json.loads(self.f.read_text())
        self.write({**now, "model": 5})
        self.on()
        self.assertNotIn("model", json.loads(self.f.read_text()))
        self.assertTrue(any('"model": 5' in c for c in self.copies()))

    def test_in_a_worktree(self):
        wf = self.ws / ".claude" / "worktrees" / "w1" / ".claude" / "settings.local.json"
        wf.parent.mkdir(parents=True)
        self.write({"model": 5})
        self.on()
        self.assertNotIn("model", json.loads(wf.read_text()))

    def test_its_hooks_cant_be_turned_off_by_a_committed_file(self):
        self.on()
        self.assertIs(json.loads(self.f.read_text())["disableAllHooks"], False)
        self.off()
        self.assertNotIn("disableAllHooks", self.f.read_text())

    def test_a_committed_file_that_widens_the_sandbox_is_said(self):
        import runtime_claude
        for doc in ({"sandbox": {"filesystem": {"allowWrite": ["~/"]}}}, {"permissions": {"additionalDirectories": ["~/"]}}):
            (self.ws / ".claude" / "settings.json").write_text(json.dumps(doc))
            self.assertTrue(runtime_claude.foreign_controls(self.ws), doc)
        (self.ws / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(make:*)"]}}')
        self.assertEqual(runtime_claude.foreign_controls(self.ws), [])


class WhoseItIs(Base):
    def test_another_workspaces_record_and_copy_are_not_this_ones(self):
        """The project's name was used again for another folder."""
        self.write(MINE)
        self.on()
        self.ws = self.tmp / "ws" / "second"
        (self.ws / ".claude").mkdir(parents=True)
        self.f = self.ws / ".claude" / "settings.local.json"
        self.doc["paths"] = {"workspace": str(self.ws)}
        rule = json.loads((self.m.RENDERED / "proj.json").read_text())["deny"][0]
        self.write({"permissions": {"deny": [rule]}, "sandbox": {"second": True}})
        self.on()
        self.off()
        now = json.loads(self.f.read_text())
        self.assertEqual((now["sandbox"], now["permissions"]["deny"]), ({"second": True}, [rule]))
        self.assertNotIn("my-own-hook", self.f.read_text())

    def test_what_was_there_before_says_whose_it_is(self):
        self.write(MINE)
        self.on()
        self.assertEqual(self.m.before("proj", self.doc)[0], MINE)
        self.assertEqual(self.m.before("proj", {**self.doc, "paths": {"workspace": str(self.tmp / "second")}}), ({}, {}))

    def test_off_leaves_another_workspaces_file(self):
        self.on()
        second = self.tmp / "ws" / "second"
        (second / ".claude").mkdir(parents=True)
        f = second / ".claude" / "settings.local.json"
        f.write_text(json.dumps(MINE))
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.unapply("proj", {"name": "proj", "paths": {"workspace": str(second)}})
        self.assertEqual(json.loads(f.read_text()), MINE)

    def test_a_record_from_before_it_carried_one(self):
        self.write({"permissions": {"allow": ["Bash(make:*)"]}})
        self.on()
        rec = self.m.RENDERED / "proj.json"
        got = json.loads(rec.read_text())
        got.pop("workspace", None)
        rec.write_text(json.dumps(got))
        self.on()
        self.off()
        self.assertEqual(json.loads(self.f.read_text())["permissions"], {"allow": ["Bash(make:*)"], "deny": []})

    def test_a_lost_record_under_a_changed_policy(self):
        """What the platform rendered is never taken for the user's, whatever it would render now."""
        self.write(MINE)
        self.on()
        (self.m.RENDERED / "proj.json").unlink()
        real = self.m.desired

        def other(*a):
            sandbox, deny, allow = real(*a)
            return {**sandbox, "filesystem": {"allowWrite": ["/changed"], "denyRead": []}}, deny, allow
        with mock.patch.object(self.m, "desired", other):
            self.on()
        self.off()
        self.assertEqual(json.loads(self.f.read_text())["sandbox"], MINE["sandbox"])

    def test_what_the_platform_set_too_and_no_longer_sets(self):
        self.write({"env": {"UV_CACHE_DIR": "/mine"}})
        self.on()
        rule = json.loads(self.f.read_text())["permissions"]["deny"][0]
        self.off()
        self.write({"env": {"UV_CACHE_DIR": "/mine"}, "permissions": {"deny": [rule]}})
        self.on()
        real = self.m.desired

        def fewer(*a):
            sandbox, deny, allow = real(*a)
            return sandbox, [r for r in deny if r != rule], allow
        with mock.patch.object(self.m, "desired_env", lambda *_a: {}), mock.patch.object(self.m, "desired", fewer):
            self.on()
            self.on()
            now = json.loads(self.f.read_text())
            self.assertEqual((now["env"], rule in now["permissions"]["deny"]), ({"UV_CACHE_DIR": "/mine"}, True), "the user's is back at once")
        self.off()
        now = json.loads(self.f.read_text())
        self.assertEqual((now["env"], now["permissions"]["deny"]), ({"UV_CACHE_DIR": "/mine"}, [rule]))


class Worktrees(Base):
    def setUp(self):
        super().setUp()
        self.wt = self.ws / ".claude" / "worktrees" / "w1"
        self.wf = self.wt / ".claude" / "settings.local.json"
        self.wf.parent.mkdir(parents=True)
        self.on()

    def kept(self, text):
        return any(text in c for f, c in ((f, f.read_text()) for f in self.copy_files()) if f != self.wf)

    def change(self, **perms):
        now = json.loads(self.wf.read_text())
        for k, v in perms.items():
            now["permissions"].setdefault(k, []).extend(v)
        self.wf.write_text(json.dumps(now))

    def test_what_was_allowed_there_is_kept(self):
        self.change(allow=["Bash(in-the-worktree:*)"])
        self.on()
        self.assertTrue(self.kept("in-the-worktree"))
        self.assertNotIn("in-the-worktree", self.wf.read_text(), "what it gets is unchanged")

    def test_a_worktrees_own_file_at_its_start(self):
        self.wf.write_text(json.dumps(MINE))
        with mock.patch.object(self.m.registry, "refresh", lambda *a, **kw: self.index), contextlib.redirect_stderr(io.StringIO()):
            self.m.render_worktree("proj", self.wt)
        self.assertTrue(self.kept("my-own-hook"))
        self.assertTrue(json.loads(self.wf.read_text())["sandbox"]["enabled"])

    def test_an_env_variable_set_there_is_kept(self):
        now = json.loads(self.wf.read_text())
        self.wf.write_text(json.dumps({**now, "env": {**now["env"], "ONLY_HERE": "1"}}))
        self.on()
        self.assertTrue(self.kept("ONLY_HERE"))

    def test_one_that_cant_be_read_is_kept(self):
        self.wf.write_text(BAD)
        self.on()
        self.assertTrue(self.kept(BAD))
        self.assertTrue(json.loads(self.wf.read_text())["sandbox"]["enabled"])

    def test_the_platforms_own_is_not_copied(self):
        self.on()
        with mock.patch.object(self.m, "desired_env", lambda *_a: {"UV_CACHE_DIR": "/new"}):
            self.on()
        self.assertEqual([f for f in self.copy_files() if f.name != "before.json"], [])

    def test_two_worktrees_of_the_same_content(self):
        self.change(allow=["Bash(same:*)"])
        w2 = self.ws / ".claude" / "worktrees" / "w2" / ".claude"
        w2.mkdir(parents=True)
        shutil.copy2(self.wf, w2 / self.wf.name)
        self.on()
        self.assertEqual(len([f for f in self.copy_files() if "Bash(same:*)" in f.read_text()]), 2)


class ToTheByte(Base):
    """What is in the file is what would be written, byte for byte: equal isn't the same (1 == True), and what Python
    reads (UTF-16) the tool may not."""

    def live(self, change):
        now = json.loads(self.f.read_text())
        change(now)
        return now

    def test_a_number_where_a_switch_belongs(self):
        self.on()
        good = self.f.read_bytes()

        def numbers(now):
            now["sandbox"] = {k: 1 if v is True else 0 if v is False else v for k, v in now["sandbox"].items()}
            now["disableAllHooks"] = 0
        self.write(self.live(numbers))
        self.assertIn("agent-settings:proj", [c.target for c in self.m.plan(self.index)])
        numbered = self.f.read_text()
        self.assertIn(self.m._("sandbox was changed"), self.on())
        self.assertEqual(self.f.read_bytes(), good)
        self.assertIn(numbered, self.copies())
        self.assertEqual(self.m.plan(self.index), [])

    def test_another_encoding(self):
        self.on()
        good = self.f.read_bytes()
        for enc in ("utf-16", "utf-32", "utf-16-le"):
            with self.subTest(enc):
                self.f.write_bytes(good.decode().encode(enc))
                self.assertTrue(self.m.plan(self.index))
                self.assertTrue(self.on())
                self.assertEqual(self.f.read_bytes(), good)

    def test_a_users_own_value_is_compared_by_kind(self):
        for given in (1, 1.0, "true"):
            self.assertEqual(self.m.minus({"a": given}, {"a": True}), {"a": given})
        self.assertIs(self.m.minus({"a": [1, True]}, {"a": [True, 1]}), self.m.NOTHING)
        self.assertEqual(self.m.minus({"a": [1]}, {"a": [True]}), {"a": [1]})
        self.write({"disableAllHooks": 0})
        self.on()
        self.assertIn('{"disableAllHooks": 0}', self.copies())
        self.off()
        now = json.loads(self.f.read_text())
        self.assertTrue(now["disableAllHooks"] == 0 and now["disableAllHooks"] is not False, now["disableAllHooks"])

    def test_long_lists_take_no_time(self):
        a, b = [f"r{i}" for i in range(60_000)], [f"r{i}" for i in range(1, 60_001)]
        calls = []

        class Counted(str):
            __slots__ = ()
            __hash__ = str.__hash__

            def __eq__(self, other):
                calls.append(1)
                return str.__eq__(self, other)
        real = self.m.key
        with mock.patch.object(self.m, "key", lambda x: Counted(real(x))):
            self.assertEqual(self.m.minus(a[:3000], b[:3000]), ["r0"])
            self.assertEqual(self.m.plus(a[:3000], b[:3000])[2999:], ["r2999", "r3000"])
        self.assertLess(len(calls), 100_000, "not every one with every other")
        self.assertEqual(self.m.minus(a, b), ["r0"])


class FromBefore(Base):
    """Installs whose record is from before it carried the workspace (v0.21.0)."""

    def upgraded(self, extra):
        self.on()
        rec = self.m.RENDERED / "proj.json"
        got = json.loads(rec.read_text())
        got.pop("workspace")
        rec.write_text(json.dumps(got))
        now = json.loads(self.f.read_text())
        now.pop("disableAllHooks")
        self.write({**now, **extra})
        self.drift = self.on()

    def test_what_stood_there_is_kept_for_good_and_truly_said(self):
        self.upgraded({"statusLine": {"type": "command", "command": "mine"}, "model": "sonnet"})
        [copy] = [f for f in self.copy_files() if "statusLine" in f.read_text()]
        [text] = [t for v in self.alerts.values() for t in v]
        self.assertIn(str(copy), text)
        self.assertIn("statusLine", text)
        self.assertNotIn("while the sandbox was on", text)
        self.assertIn("by themselves", text)
        self.assertEqual(json.loads(self.f.read_text())["model"], "sonnet")
        for i in range(self.m.KEEP_MAX + 5):
            self.write({**json.loads(self.f.read_text()), "sandbox": {"n": i}})
            self.on()
            for f in self.copy_files():
                os.utime(f, (f.stat().st_mtime - 10,) * 2)
        self.assertTrue(copy.exists(), "never dropped")
        self.off()
        self.assertNotIn("statusLine", self.f.read_text(), "nothing comes back by itself")

    def test_the_next_one_is_said_as_usual(self):
        self.upgraded({})
        self.assertEqual(self.sent, [])
        self.write({**json.loads(self.f.read_text()), "statusLine": "x"})
        self.on()
        self.assertIn("while the sandbox was on", "".join(t for v in self.alerts.values() for t in v))


class LostRecord(Base):
    def lose(self):
        (self.m.RENDERED / "proj.json").unlink()

    def test_an_unreadable_file_or_a_link_never_takes_the_place_of_what_was_there(self):
        for what in ("bad", "link"):
            with self.subTest(what):
                shutil.rmtree(self.m.RENDERED, ignore_errors=True)
                self.f.unlink(missing_ok=True)
                self.write(MINE)
                self.on()
                self.lose()
                self.f.unlink()
                self.f.symlink_to(self.tmp / "elsewhere") if what == "link" else self.write(BAD)
                self.on()
                self.off()
                self.assertEqual(json.loads(self.f.read_text())["sandbox"], MINE["sandbox"])

    def test_off_still_takes_the_sandbox_out(self):
        self.write(MINE)
        self.on()
        self.lose()
        self.assertIn("agent-settings:proj", [c.target for c in self.m.plan({"projects": {"proj": {k: v for k, v in self.doc.items() if k != "enforce"}}})])
        self.off()
        self.assertEqual(json.loads(self.f.read_text())["sandbox"], MINE["sandbox"])
        self.assertNotIn("worktree-make", self.f.read_text())

    def test_off_with_a_file_it_cant_read_says_so(self):
        self.on()
        self.write(MINE)
        shutil.rmtree(self.m.RENDERED)
        self.on()
        self.lose()
        self.alerts.clear()
        self.write(BAD)
        self.off()
        self.assertTrue([v for v in self.alerts.values() if v])
        self.assertEqual(self.f.read_text(), BAD)

    def test_a_project_that_was_never_sandboxed_is_left_alone(self):
        for text in (BAD, json.dumps(MINE), ""):
            self.write(text)
            self.off()
            self.assertEqual((self.f.read_text(), self.alerts), (text, {}))

    def test_off_bad_on_off(self):
        """What was put back is done with: the next time keeps what is there then, whatever it is."""
        self.write(MINE)
        self.on()
        self.off()
        self.write(BAD)
        self.on()
        self.off()
        self.assertEqual(self.f.read_text(), BAD)


class WorktreesToo(Base):
    def setUp(self):
        super().setUp()
        self.wf = {}
        for w in ("w1", "w2"):
            self.wf[w] = self.ws / ".claude" / "worktrees" / w / ".claude" / "settings.local.json"
            self.wf[w].parent.mkdir(parents=True)
        self.on()

    def change(self, w, **kw):
        self.wf[w].write_text(json.dumps({**json.loads(self.wf[w].read_text()), **kw}))

    def test_the_owner_is_told(self):
        self.change("w1", statusLine="x")
        self.on()
        self.assertTrue(any("w1" in t and "statusLine" in t for v in self.alerts.values() for t in v), self.alerts)

    def test_one_full_of_junk_takes_nothing_of_anothers(self):
        self.change("w2", statusLine="the one")
        self.on()
        for i in range(self.m.KEEP_MAX + 5):
            self.change("w1", statusLine=f"junk {i}")
            self.on()
            for f in self.copy_files():
                os.utime(f, (f.stat().st_mtime - 10,) * 2)
        texts = [f.read_text() for f in self.copy_files()]
        self.assertEqual(len([t for t in texts if "junk" in t]), self.m.KEEP_MAX)
        self.assertTrue(any("the one" in t for t in texts))

    def test_off_takes_the_sandbox_out_of_them(self):
        perms = json.loads(self.wf["w1"].read_text())["permissions"]
        self.change("w1", permissions={**perms, "allow": [*perms["allow"], "Bash(there:*)"]})
        self.change("w2", statusLine="added since")
        self.off()
        for w, f in self.wf.items():
            now = json.loads(f.read_text())
            self.assertNotIn("sandbox", now)
            self.assertNotIn("disableAllHooks", now)
            self.assertNotIn("worktree-make", f.read_text())
            self.assertIn("SessionStart", now["hooks"])
            self.assertNotIn("Read(", "".join(now["permissions"]["allow"] + [r for r in now["permissions"]["deny"] if "AGENT.md" not in r and "RULES" not in r and ".git" not in r]))
        self.assertEqual(json.loads(self.wf["w1"].read_text())["permissions"]["allow"], ["Bash(there:*)"])
        self.assertNotIn("statusLine", self.wf["w2"].read_text())
        self.assertTrue(any("added since" in f.read_text() for f in self.copy_files()))

    def test_off_leaves_ones_that_arent_the_platforms(self):
        self.wf["w1"].write_text(json.dumps(MINE))
        self.wf["w2"].write_text(BAD)
        self.off()
        self.assertEqual((json.loads(self.wf["w1"].read_text()), self.wf["w2"].read_text()), (MINE, BAD))


class Meanwhile(Base):
    """The tool writes the file while the platform works on it."""

    def appending(self, f, times=1):
        real, left = self.m.keep_what_goes, [times]

        def keep(*a, **kw):
            got = real(*a, **kw)
            if left[0] and (a[-1] if isinstance(a[-1], pathlib.Path) else None) == (None if f == self.f else f.parent.parent):
                left[0] -= 1
                now = json.loads(f.read_text())
                now.setdefault("permissions", {}).setdefault("allow", []).append(f"Bash(meanwhile-{left[0]}:*)")
                f.write_text(json.dumps(now))
            return got
        return mock.patch.object(self.m, "keep_what_goes", keep)

    def test_a_rule_appended_meanwhile_stays(self):
        self.write({"permissions": {"allow": ["Bash(first:*)"]}})
        with self.appending(self.f, 2):
            self.on()
        allow = json.loads(self.f.read_text())["permissions"]["allow"]
        for r in ("Bash(first:*)", "Bash(meanwhile-1:*)", "Bash(meanwhile-0:*)"):
            self.assertIn(r, allow)

    def test_one_that_never_stops_is_replaced_all_the_same(self):
        self.write({"permissions": {"allow": []}})
        with self.appending(self.f, 1000):
            self.on()

    def test_in_a_worktree_it_is_kept(self):
        wf = self.ws / ".claude" / "worktrees" / "w1" / ".claude" / "settings.local.json"
        wf.parent.mkdir(parents=True)
        self.on()
        wf.write_text("{}")
        with self.appending(wf):
            self.on()
        self.assertTrue(any("meanwhile-0" in f.read_text() for f in self.copy_files()))


class Bounds(Base):
    def test_a_render_can_be_read_back(self):
        """Whatever is carried: next time the platform reads its own file."""
        self.write({"permissions": {"allow": [f"B{i}" for i in range(90_000)]}})
        self.assertLess(self.f.stat().st_size, 1 << 20, "this can be read")
        self.on()
        self.assertEqual(self.m.read_settings(self.doc)[1], None)
        self.assertTrue(any("B89999" in c for c in self.copies()))
        self.assertEqual(self.on(), [], "and is what it should be")
        self.off()
        self.assertIn("B89999", self.f.read_text())

    def test_a_path_no_folder_can_have_isnt_carried(self):
        import runtime_claude
        long = ["/" + "x" * 300, "/" + "/".join(["y" * 100] * 20), "/a\0b", ""]
        self.assertEqual(runtime_claude.carried({"permissions": {"additionalDirectories": ["/ok", *long]}}),
                         {"permissions": {"additionalDirectories": ["/ok"]}})

    def test_what_is_said_is_one_line_of_bounded_length(self):
        self.write({("k" * 5000) + "\n\u2028fake line": 1, "permissions": {"a\nb": 1}})
        self.on()
        [text] = [t for v in self.alerts.values() for t in v]
        self.assertEqual(len(text.splitlines()), 1)
        self.assertLess(len(text), 1500)

    def test_what_is_put_in_just_before_off_doesnt_stay(self):
        for extra in ({"apiKeyHelper": "x"}, {"hooks": MINE["hooks"]}):
            with self.subTest(extra):
                self.f.unlink(missing_ok=True)
                self.on()
                now = json.loads(self.f.read_text())
                self.write({**now, **({"hooks": {**now["hooks"], "Stop": now["hooks"]["Stop"] + MINE["hooks"]["Stop"]}} if "hooks" in extra else extra)})
                self.off()
                for gone in ("apiKeyHelper", "my-own-hook"):
                    self.assertNotIn(gone, self.f.read_text())
                self.assertTrue(any("apiKeyHelper" in c or "my-own-hook" in c for c in self.copies()))
                self.assertIn("SessionStart", self.f.read_text())

    def test_renamed_moves_what_is_kept(self):
        self.write(MINE)
        self.on()
        new = self.tmp / "ws" / "second"
        self.ws.rename(new)
        self.m.renamed("proj", "second", str(new))
        self.assertEqual([f.name for f in self.m.RENDERED.glob("*.json")], ["second.json"])
        doc = {"name": "second", "paths": {"workspace": str(new)}}
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.unapply("second", doc)
        self.assertEqual(json.loads((new / ".claude" / "settings.local.json").read_text())["sandbox"], MINE["sandbox"])


class LinkedWorktrees(Base):
    """The project's `.claude/worktrees` (or one of them, between two looks) is a link: main is written, recorded and, at
    off, put back once; nothing is done through the link."""

    def setUp(self):
        super().setUp()
        self.out = self.tmp / "out"
        (self.out / "w1" / ".claude").mkdir(parents=True)
        (self.out / "w1" / ".claude" / "settings.local.json").write_text("{}")

    def link(self):
        (self.ws / ".claude" / "worktrees").symlink_to(self.out)

    def test_the_folder(self):
        import runtime_claude
        self.link()
        self.assertEqual(runtime_claude.session_folders(self.ws), [self.ws])
        (self.ws / ".claude" / "worktrees").unlink()
        (self.ws / ".claude" / "worktrees" / "real").mkdir(parents=True)
        (self.ws / ".claude" / "worktrees" / "file").write_text("")
        (self.ws / ".claude" / "worktrees" / "link").symlink_to(self.out)
        self.assertEqual(runtime_claude.session_folders(self.ws), [self.ws, self.ws / ".claude" / "worktrees" / "real"])

    def test_where_one_is_cant_be_told(self):
        self.write(MINE)
        wt = self.ws / ".claude" / "worktrees" / "w1"
        (wt / ".claude").mkdir(parents=True)
        real = self.m.settings_rel

        def rel(doc, folder=None):
            if folder is not None:
                raise RuntimeError("Symlink loop")
            return real(doc, folder)
        with mock.patch.object(self.m, "settings_rel", rel):
            self.assertTrue(self.on())
            self.assertTrue((self.m.RENDERED / "proj.json").exists())
            self.off()
        self.assertEqual(json.loads(self.f.read_text())["sandbox"], MINE["sandbox"])

    def swapped(self):
        """Found as a folder, a link when it is read."""
        wt = self.ws / ".claude" / "worktrees" / "w1"
        self.link()
        return mock.patch.object(self.m.runtime_claude, "session_folders", lambda ws: [ws, wt])

    def test_on_and_off(self):
        self.write(MINE)
        with self.swapped():
            drift = self.on()
            self.assertTrue(drift)
            self.assertTrue((self.m.RENDERED / "proj.json").exists())
            self.assertTrue([v for v in self.alerts.values() if v])
            self.off()
            now = json.loads(self.f.read_text())
            self.assertEqual(now["sandbox"], MINE["sandbox"])
            self.write({**now, "added": "after"})
            self.off()
            self.assertEqual(json.loads(self.f.read_text())["added"], "after", "main is put back once")
        self.assertEqual((self.out / "w1" / ".claude" / "settings.local.json").read_text(), "{}")

    def test_a_loop(self):
        loop = self.ws / ".claude" / "worktrees"
        loop.symlink_to(loop)
        with mock.patch.object(self.m.runtime_claude, "session_folders", lambda ws: [ws, loop / "w1"]):
            self.on()
            self.off()
        self.assertFalse((self.m.RENDERED / "proj.json").exists())


class OffInWorktrees(Base):
    def setUp(self):
        super().setUp()
        self.wf = {}
        for w in ("w1", "w2", "w3"):
            self.wf[w] = self.ws / ".claude" / "worktrees" / w / ".claude" / "settings.local.json"
            self.wf[w].parent.mkdir(parents=True)

    def sandboxed(self):
        return {w for w, f in self.wf.items() if "sandbox" in json.loads(f.read_text())}

    def test_one_that_cant_be_written_leaves_no_other_sandboxed_and_is_tried_again(self):
        self.write(MINE)
        self.on()
        denied = json.loads(self.f.read_text())["permissions"]["deny"][-1]
        for boom in (PermissionError(13, "denied"), OSError(28, "full"), RuntimeError("made up")):
            with self.subTest(boom):
                real = self.m.write_settings

                def write(doc, content, folder=None):
                    if folder is not None and folder.name == "w1":
                        raise boom
                    return real(doc, content, folder)
                with mock.patch.object(self.m, "write_settings", write), contextlib.redirect_stderr(io.StringIO()):
                    self.off()
                self.assertEqual(self.sandboxed(), {"w1"})
                now = {**json.loads(self.f.read_text()), "added": "after"}
                self.write(now)
                self.off()
                self.assertEqual((self.sandboxed(), json.loads(self.f.read_text())), (set(), now), "and main isn't done again")
                self.assertNotIn(denied, self.wf["w1"].read_text(), "with what the platform would write now")
                self.write(MINE)
                self.on()

    def test_while_main_is_a_link(self):
        self.on()
        self.f.unlink()
        self.f.symlink_to(self.tmp / "elsewhere")
        self.off()
        self.assertEqual(self.sandboxed(), set())

    def test_over_another_workspaces_record(self):
        self.on()
        rec = self.m.RENDERED / "proj.json"
        rec.write_text(json.dumps({**json.loads(rec.read_text()), "workspace": "/another"}))
        self.off()
        self.assertEqual(self.sandboxed(), set())

    def test_with_no_record(self):
        self.on()
        (self.m.RENDERED / "proj.json").unlink()
        self.off()
        self.assertEqual(self.sandboxed(), set())

    def test_one_right_to_the_byte(self):
        self.on()
        good = self.wf["w1"].read_bytes()
        now = json.loads(good)
        now["sandbox"]["enabled"] = 1
        self.wf["w1"].write_text(json.dumps(now, indent=2, ensure_ascii=False) + "\n")
        self.on()
        self.assertEqual(self.wf["w1"].read_bytes(), good)

    def test_the_folders_kept_for_them_are_bounded(self):
        self.on()
        base = self.ws / ".claude" / "worktrees"
        with mock.patch.object(self.m, "WORKTREES_MAX", 3, create=True):
            for i in range(6):
                f = base / f"n{i}" / ".claude" / "settings.local.json"
                f.parent.mkdir(parents=True)
                f.write_text(json.dumps({"statusLine": f"line {i}"}))
                self.on()
                if i < 4:
                    shutil.rmtree(base / f"n{i}")   # gone: its copies may go
            kept = self.m.saved_dir("proj") / "worktrees"
            self.assertEqual(len(list(kept.iterdir())), 3)
            self.assertTrue({"n4", "n5"} <= {d.name for d in kept.iterdir()})
            for i in (6, 7):   # none may go: the file itself stays beside
                f = base / f"n{i}" / ".claude" / "settings.local.json"
                f.parent.mkdir(parents=True)
                f.write_text(json.dumps({"statusLine": f"line {i}"}))
            self.on()
            self.assertEqual(len(list(kept.iterdir())), 3)
            here = [f.read_text() for f in self.tmp.rglob("*") if f.is_file()]
            for i in range(4, 8):
                self.assertTrue(any(f"line {i}" in t for t in here), i)


class Fixpoint(Base):
    def test_the_first_write_is_the_last(self):
        self.on()
        deny = json.loads(self.f.read_text())["permissions"]["deny"]
        self.off()
        cases = ({"permissions": {"ask": ["Bash(a)"], "defaultMode": "plan"}}, {"permissions": {"additionalDirectories": ["/x"], "ask": []}},
                 {"permissions": {"deny": ["Read(/a)", deny[3], "Read(/b)", deny[0], "Read(/a)"]}},
                 {"env": {"UV_CACHE_DIR": "/mine", "MINE": "1"}}, NotTaken.GOOD, MINE,
                 {"enabledMcpjsonServers": ["b", "a"], "enableAllProjectMcpServers": True, "language": "x", "model": "y"}, {})
        for c in cases:
            with self.subTest(c):
                shutil.rmtree(self.m.RENDERED, ignore_errors=True)
                self.write(c)
                self.m.apply(self.index)
                first = self.f.read_bytes()
                self.assertEqual(self.m.plan(self.index), [])
                self.m.apply(self.index)
                self.assertEqual(self.f.read_bytes(), first)
                with mock.patch.object(self.m, "desired_env", lambda *_a: {}):   # the platform stops setting one
                    self.m.apply(self.index)
                    self.assertEqual(self.m.plan(self.index), [])


class Edges(Base):
    def test_off_right_after_the_upgrade(self):
        self.on()
        rec = self.m.RENDERED / "proj.json"
        got = json.loads(rec.read_text())
        got.pop("workspace")
        rec.write_text(json.dumps(got))
        self.write({**json.loads(self.f.read_text()), "statusLine": "mine"})
        self.off()
        [copy] = [f for f in self.copy_files() if "statusLine" in f.read_text()]
        self.assertTrue(copy.name.endswith(".kept.json"))
        [text] = self.sent[0]
        self.assertTrue(str(copy) in text and "statusLine" in text and "by themselves" in text, text)
        self.assertNotIn("while the sandbox was on", text)

    def big(self, n=90_000):
        return {"permissions": {"allow": [f"B{i}" for i in range(n)]}}

    def test_a_crash_before_the_record_loses_nothing_of_what_couldnt_be_carried(self):
        self.write(self.big())
        self.on()
        (self.m.RENDERED / "proj.json").unlink()   # as if it stopped right before
        self.on()
        self.off()
        self.assertIn("B89999", self.f.read_text())

    def test_one_too_big_to_be_read(self):
        big = {**self.big(120_000), "model": "sonnet", "statusLine": "x"}
        self.write(big)
        self.assertGreater(self.f.stat().st_size, 1 << 20)
        self.on()
        self.on()
        self.off()
        now = json.loads(self.f.read_text())
        self.assertEqual((now["model"], now["statusLine"], now["permissions"]["allow"]), ("sonnet", "x", big["permissions"]["allow"]))

    def test_off_over_a_link_is_done_with_what_was_before(self):
        self.write(MINE)
        self.on()
        self.f.unlink()
        self.f.symlink_to(self.tmp / "elsewhere")
        self.off()
        self.f.unlink()
        self.write(BAD)
        self.on()
        self.off()
        self.assertEqual(self.f.read_text(), BAD)

    def test_off_without_a_record_is_done_with_it_too(self):
        self.write(MINE)
        self.on()
        (self.m.RENDERED / "proj.json").unlink()
        self.f.unlink()
        self.off()
        self.write(BAD)
        self.on()
        self.off()
        self.assertEqual(self.f.read_text(), BAD)


if __name__ == "__main__":
    unittest.main()
