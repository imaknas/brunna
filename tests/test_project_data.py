"""A project's declared data outside its folder (bin/project_data.py, bin/check-data.py, the backup's use of them;
docs/design/project-data.md)."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import hubconf  # noqa: E402
import project_data as pd  # noqa: E402
import registry  # noqa: E402


def load_check():
    spec = importlib.util.spec_from_file_location("check_data", ROOT / "bin" / "check-data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        (self.home / "workspace").mkdir(parents=True)
        (self.home / "services").mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        cfg = {"workspace_root": "~/workspace", "services_root": "~/services"}
        get = mock.patch.object(hubconf, "get", side_effect=lambda k, d=None: cfg.get(k, d))
        get.start()
        self.addCleanup(get.stop)
        roots = mock.patch.object(pd, "_platform_roots", return_value=[self.home / ".brunna", self.home / "hub"])
        roots.start()
        self.addCleanup(roots.stop)

    def doc(self, name="voice", **kw):
        return {"name": name, "paths": {"workspace": f"~/workspace/{name}"}, **kw}

    def problem(self, raw, name="voice", doc=None):
        return pd.place_problem(name, doc or self.doc(name), pd.expand(raw))


class Places(Base):
    def test_a_folder_named_after_the_project_or_inside_its_own(self):
        for ok in ("~/voice-data/voice", "~/.cache/voice", "~/workspace/voice/data", "/Volumes/disk/voice"):
            self.assertIsNone(self.problem(ok), ok)
        self.assertIsNone(self.problem("~/services/voice/models", doc=self.doc(runtime={})))

    def test_what_a_project_may_not_point_the_backup_at(self):
        for bad in ("~/data", "~/.ssh/voice", "~/Library/voice", "~/.config/voice/x", "~/workspace/voice",
                    "~/workspace/other/voice", "~/services/other/voice", "~/.brunna/voice", "/tmp/voice",
                    "/Volumes/voice"):
            self.assertIsNotNone(self.problem(bad), bad)

    def test_the_text_rules(self):
        self.assertIsNone(pd.expand("~/a/../voice"))
        self.assertIsNone(pd.expand("~/voice*"))
        self.assertIsNone(pd.expand("voice"))
        doc = self.doc(data=[{"path": "~/voice-data/voice", "what": "x", "exclude": ["../y"]},
                             {"path": "~/.cache/voice", "what": "y", "backup": False},
                             {"path": "~/voice-data/voice/sub", "what": "z", "colour": 1}])
        errs = pd.text_problems("voice", doc)
        # matched on what each message names, not its wording (the run's language may be either)
        self.assertTrue(any("../y" in e for e in errs), errs)
        self.assertTrue(any(e.startswith("data[1]") and "why" in e for e in errs), errs)
        self.assertTrue(any("~/voice-data/voice/sub" in e for e in errs), errs)
        self.assertTrue(any("colour" in e for e in errs), errs)
        self.assertEqual(pd.text_problems("voice", self.doc(data=[{"path": "~/voice-data/voice", "what": "rec",
                                                                    "exclude": ["models/", "**/*.wav"]}])), [])

    def test_the_registry_validates_it(self):
        doc = {"v": 2, "name": "voice", "paths": {"workspace": "~/workspace/voice"},
               "data": [{"path": "~/.ssh/voice", "what": "x"}]}
        self.assertTrue(any("data[0]" in e for e in registry.validate(doc, "voice")))


class Entries(Base):
    def test_resolved_overlaps_and_symlinks(self):
        real = self.home / "voice-data" / "voice"
        real.mkdir(parents=True)
        (self.home / "link").symlink_to(self.home / ".ssh", target_is_directory=True)
        (self.home / ".ssh").mkdir()
        (self.home / "vlink").mkdir()
        (self.home / "vlink" / "voice").symlink_to(self.home / ".ssh", target_is_directory=True)
        projects = {
            "voice": self.doc(data=[{"path": "~/voice-data/voice", "what": "rec", "exclude": ["models/"]},
                                    {"path": "~/vlink/voice", "what": "sneaky"}]),
            "other": self.doc("other", data=[{"path": "~/voice-data/voice/other", "what": "x"}]),
        }
        got = {(e.project, e.raw): e for e in pd.entries(projects)}
        self.assertIsNotNone(got[("voice", "~/vlink/voice")].problem, "a symlink into ~/.ssh is refused once resolved")
        self.assertIsNotNone(got[("other", "~/voice-data/voice/other")].problem)
        self.assertIsNotNone(got[("voice", "~/voice-data/voice")].problem, "both overlapping entries are skipped")
        alone = pd.entries({"voice": projects["voice"]})
        ok = [e for e in alone if e.path]
        self.assertEqual([e.path for e in ok], [real.resolve()])
        self.assertEqual(ok[0].patterns(), [f"{real.resolve()}/models"])
        inc = pd.entries({"voice": projects["voice"]}, include=[self.home / "voice-data"])
        self.assertTrue(all(e.path is None for e in inc), "an entry overlapping the owner's include is skipped")


class Excludes(unittest.TestCase):
    CASES = [("/a/b/models/x.pt", "/a/b/models", True), ("/a/b/models/x.pt", "/a/b/models/", True),
             ("/a/b/fpo/t/1.wav", "/a/b/fpo/**/*.wav", True), ("/a/b/fpo/1.wav", "/a/b/fpo/**/*.wav", True),
             ("/a/b/fpo/1.txt", "/a/b/fpo/**/*.wav", False), ("/a/b/x/node_modules/y", "node_modules", True),
             ("/a/b/c.db-wal", "*.db-wal", True), ("/a/b/f/runs/r/step0010/llm.pt", "/a/b/f/runs/*/step*", True),
             ("/a/b/f/runs/r/history.json", "/a/b/f/runs/*/step*", False), ("/a/bc/x", "/a/b", False),
             ("/a/b/x/y.pt", "/a/*.pt", False)]

    def test_restics_rules(self):
        for path, pat, want in self.CASES:
            self.assertEqual(pd.excluded(path, pat), want, (path, pat))

    @unittest.skipUnless(shutil.which("restic"), "restic not installed")
    def test_against_restic(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = tmp / "src"
        files = ["models/x.pt", "fpo/t/1.wav", "fpo/1.wav", "fpo/1.txt", "x/node_modules/y", "c.db-wal",
                 "f/runs/r/step0010/llm.pt", "f/runs/r/history.json", "keep.txt"]
        for f in files:
            (src / f).parent.mkdir(parents=True, exist_ok=True)
            (src / f).write_text("x")
        pats = [f"{src}/models/", f"{src}/fpo/**/*.wav", "node_modules", "*.db-wal", f"{src}/f/runs/*/step*"]
        env = dict(os.environ, RESTIC_PASSWORD="test", RESTIC_REPOSITORY=str(tmp / "repo"), RESTIC_CACHE_DIR=str(tmp / "cache"))
        subprocess.run(["restic", "init", "-q"], env=env, check=True, capture_output=True)
        args = ["restic", "backup", "-q", str(src)] + [a for p in pats for a in ("--exclude", p)]
        subprocess.run(args, env=env, check=True, capture_output=True)
        listed = subprocess.run(["restic", "ls", "latest"], env=env, check=True, capture_output=True, text=True).stdout
        for f in files:
            full = f"{src}/{f}"
            self.assertEqual(full in listed.splitlines(), not any(pd.excluded(full, p) for p in pats), f)


class Check(Base):
    def test_growth(self):
        cd = load_check()
        hist = {"2026-11-10": 2 << 30}
        self.assertTrue(cd.grew(hist, "2026-11-18", 5 << 30))
        self.assertFalse(cd.grew(hist, "2026-11-18", 3 << 30), "not double")
        self.assertFalse(cd.grew({"2026-11-10": 100}, "2026-11-18", 1000), "not over 1 GB more")
        self.assertFalse(cd.grew({"2026-11-15": 1}, "2026-11-18", 5 << 30), "nothing a week old yet")

    def test_undeclared_folder_named_like_a_project(self):
        cd = load_check()
        big = self.home / "rec" / "voice"
        big.mkdir(parents=True)
        (big / "a.bin").write_bytes(b"x" * 10)
        (self.home / "rec" / "short").mkdir()   # a name under four characters is never matched
        (self.home / ".cache" / "huggingface" / "voice").mkdir(parents=True)
        projects = {"voice": self.doc(), "short": self.doc("short")}
        with mock.patch.object(cd, "ASK_MIN", 5), mock.patch.object(cd.backup, "owner_included", return_value=[]):
            found = cd.candidates(projects, [])
            self.assertEqual(found, [("voice", big.resolve())])
            declared = pd.entries({"voice": self.doc(data=[{"path": "~/rec/voice", "what": "x"}])})
            self.assertEqual(cd.candidates(projects, declared), [], "a declared folder isn't asked about")


class Backup(Base):
    def test_the_backup_takes_declared_data_with_its_excludes(self):
        import backup
        d = self.home / "voice-data" / "voice"
        (d / "models").mkdir(parents=True)
        (d / "rec.wav").write_text("x")
        (d / "models" / "m.pt").write_text("x")
        projects = {"voice": self.doc(data=[{"path": "~/voice-data/voice", "what": "rec", "exclude": ["models/"]},
                                            {"path": "~/.cache/voice", "what": "dl", "backup": False, "why": "dl"}])}
        with mock.patch.object(hubconf, "projects", return_value=projects), \
                mock.patch.object(backup, "CFG", {"include": [], "exclude": []}), \
                mock.patch.object(backup, "owner_included", return_value=[]):
            self.assertEqual(backup.included(), [d.resolve()])
            self.assertEqual(backup.excludes(), [f"{d.resolve()}/models"])
            self.assertTrue(backup.covers(d / "rec.wav"))
            self.assertFalse(backup.covers(d / "models" / "m.pt"))


if __name__ == "__main__":
    unittest.main()
