"""E1 golden (docs/design/i18n.md) for bin/git_release.py (brunna release): what the owner sees before a release (the
plan, the archive warning, the consent) and every refusal, in the golden language. The scenario is
tests/test_git_release.py's throwaway registry, remote and stand-in gh; shas and temp paths are normalized.

    bin/run-tests <a folder holding this module>
    I18N_GOLDEN_UPDATE=1 bin/run-tests <that folder>   # rewrite (review it)
"""
import os
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import golden_lang  # noqa: E402
from test_git_release import Base, runtime_ports  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = golden_lang.GOLDEN
UPDATE = os.environ.get("I18N_GOLDEN_UPDATE") == "1"


class Golden(Base):
    maxDiff = None

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": golden_lang.LANG,
                                         "AGENT_HUB_LANG_CONTENT": golden_lang.LANG})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(golden_lang.pin_names())
        self.out = []

    def case(self, name, *args, **kw):
        code, out, err = self.run_(*args, **kw)
        self.out.append(f"=== {name}: brunna release {' '.join(args)}\n[exit {code}]\n--- stdout\n{out}--- stderr\n{err}")

    def test_texts(self):
        self.case("usage")
        self.case("bad tag", "proj", "v1..2")
        self.case("nobody at a terminal", "proj", "v1.0.0", tty=False)
        self.case("--yes alone", "proj", "v1.0.0", "--yes", tty=False)
        with mock.patch.object(runtime_ports, "current_session", lambda env=None: "claude:abc"):
            self.case("in a coding agent's session", "proj", "v1.0.0")
        self.case("--target that isn't on the remote", "proj", "v1.0.0", "--target", "nosuchbranch")
        unpushed = self.commit("u.txt", "u\n", "later work")
        self.case("unpushed target", "proj", "v1.0.0", "--target", unpushed[:12])
        self.case("plain plan, answered no", "proj", "v1.0.0", answer="n")
        self.case("--yes at a terminal, answered no", "proj", "v1.0.0", "--yes", answer="")
        self.git(self.seed, "tag", "-a", "-m", "zero nine", "v0.9", self.first)
        self.git(self.seed, "push", "-q", str(self.remote), "v0.9")
        self.commit(".zenodo.json", '{"title": "proj"}\n', "describe the archive")
        self.push()
        notes = self.tmp / "notes.md"
        notes.write_text("First public release.\n\n- the reader\n- the writer\n")
        self.case("published, with notes and an archive file", "proj", "v1.0.0", "--title", "First release",
                  "--notes-file", str(notes))
        self.case("the tag is on the remote now", "proj", "v1.0.0")
        (self.fake / "release-exists").touch()
        self.case("a draft release holds the name", "proj", "v1.1.0")
        (self.fake / "release-exists").unlink()
        (self.fake / "no-login").touch()
        self.case("gh isn't signed in", "proj", "v1.1.0")
        (self.fake / "no-login").unlink()
        older = self.git(self.seed, "rev-parse", "HEAD~1").strip()
        self.case("a tag made while the plan is shown", "proj", "v1.1.0", answer="y", on_answer=lambda: subprocess.run(
            ["git", "--git-dir", str(self.remote), "tag", "v1.1.0", older], check=True))
        subprocess.run(["git", "--git-dir", str(self.remote), "tag", "-d", "v1.1.0"], capture_output=True)
        (self.fake / "move-tag").write_text(older)
        self.case("the release went out on another commit", "proj", "v1.2.0")
        (self.fake / "move-tag").unlink()
        subprocess.run(["git", "--git-dir", str(self.remote), "tag", "v1.3.0", self.git(self.seed, "rev-parse", "HEAD")
                        .strip()], check=True)
        self.case("a tag left at the target", "proj", "v1.3.0", answer="n")
        long = self.tmp / "long.md"
        long.write_text("".join(f"- item {i}\n" for i in range(201)))
        self.case("notes too long to show", "proj", "v1.4.0", "--notes-file", str(long))
        self.git(self.seed, "branch", older, "HEAD")
        self.push(f"refs/heads/{older}")
        self.case("a --target that is also a branch", "proj", "v1.4.0", "--target", older)
        for origin, name in (("git@gitlab.com:o/proj.git", "another forge"), (None, "no remote")):
            self.docs["proj"]["paths"]["origin"] = origin
            self.case(name, "proj", "v1.1.0")
        self.docs["proj"]["enforce"] = {}
        self.case("not sandboxed", "proj", "v1.1.0")
        self.docs.clear()
        self.case("not registered", "proj", "v1.1.0")
        self.out.append("=== the project's letter\n" + "\n".join(t for _p, t, _k in self.letters) + "\n")
        text = "\n".join(self.out).replace(str(self.tmp), "<TMP>")
        text = re.sub(r"\b[0-9a-f]{7,40}\b", "<SHA>", text)
        f = GOLDEN / "git-release.txt"
        if UPDATE or not f.exists():
            f.write_text(text)
            if not UPDATE:
                self.fail(f"new golden written: {f.relative_to(ROOT)} (review it, then rerun)")
            return
        self.assertEqual(text, f.read_text())


if __name__ == "__main__":
    unittest.main()
