"""E1 golden (docs/design/i18n.md) for bin/git_remote.py (brunna push): what the owner sees before a push (the summary,
its flags) and every refusal, in the golden language. The scenario is tests/test_git_remote.py's throwaway project and
remote; shas and temp paths are normalized.

    bin/py -m unittest tests.test_i18n_golden_git_remote
    I18N_GOLDEN_UPDATE=1 bin/py -m unittest tests.test_i18n_golden_git_remote   # rewrite (review it)
"""
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import golden_lang  # noqa: E402
from test_git_remote import Base  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = golden_lang.GOLDEN
UPDATE = os.environ.get("I18N_GOLDEN_UPDATE") == "1"


class Golden(Base):
    maxDiff = None

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": golden_lang.LANG})
        p.start()
        self.addCleanup(p.stop)
        self.out = []
        self.addCleanup(golden_lang.pin_names())

    def case(self, name, *args, **kw):
        code, out, err = self.push(*args, **kw)
        self.out.append(f"=== {name}: brunna push proj {' '.join(args)}\n[exit {code}]\n--- stdout\n{out}--- stderr\n{err}")

    def test_texts(self):
        from git_remote import main
        self.case("nothing to push", "main", "--yes")
        self.commit(self.ws, ".github/workflows/ci.yml", "on: push\n", "add a workflow")
        env = {**os.environ, "GIT_AUTHOR_EMAIL": "someone@else.example"}
        (self.ws / "b.txt").write_text("b\n")
        self.git(self.ws, "add", "b.txt")
        self.git(self.ws, "commit", "-q", "-m", "from elsewhere", env=env)
        self.case(".github refused", "main", "--yes")
        self.docs["proj"]["enforce"]["git_ci"] = True
        self.case("no terminal", "main")
        self.case("declined", "main", stdin_tty=True, answer="n")
        self.case("pushed", "main", "--yes")
        for args in (("--force", "main"), ("--tags",), ("refs/tags/v1",), (":feature",), ("main:other",), ("+main",),
                     ("main", "feature"), ("refs/heads/main",), ("--bogus",), ("bad..name",)):
            self.case("refused", *args, "--yes")
        other = self.tmp / "other"
        self.git(self.tmp, "clone", "-q", str(self.remote), str(other))
        self.commit(other, "o.txt", "o\n", "someone else's")
        self.git(other, "push", "-q", "origin", "main")
        self.commit(self.ws, "m.txt", "m\n", "mine")
        self.case("not a fast-forward", "main", "--yes")
        self.case("no such branch", "nope", "--yes")
        for origin in (None, "(native)", "ext::sh", str(self.ws / ".git")):
            self.docs["proj"]["paths"]["origin"] = origin
            self.case(f"origin {origin and origin.replace(str(self.tmp), '<TMP>')}", "main", "--yes")
        empty = self.tmp / "empty.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(empty))
        self.docs["proj"]["paths"]["origin"] = str(empty)
        self.case("empty remote", "main", "--yes")
        del self.docs["proj"]["enforce"]
        self.case("not sandboxed", "main", "--yes")
        del self.docs["proj"]
        self.case("not registered", "main", "--yes")
        import io
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = main(["push"])
        self.out.append(f"=== usage: brunna push\n[exit {code}]\n--- stderr\n{err.getvalue()}")
        text = "\n".join(self.out).replace(str(self.tmp), "<TMP>")
        text = re.sub(r"\b[0-9a-f]{7,40}\b", "<SHA>", text)
        f = GOLDEN / "git-remote.txt"
        if UPDATE or not f.exists():
            f.write_text(text)
            if not UPDATE:
                self.fail(f"new golden written: {f.relative_to(ROOT)} (review it, then rerun)")
            return
        self.assertEqual(text, f.read_text())


if __name__ == "__main__":
    unittest.main()
