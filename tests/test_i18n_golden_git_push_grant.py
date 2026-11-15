"""E1 golden (docs/design/i18n.md) for bin/git_push_grant.py (brunna push setup): what the owner sees setting up, checking
and revoking a push grant, and every refusal, in the golden language. The scenario is tests/test_git_push_grant.py's
throwaway secrets, stand-in gh and ssh; temp paths and key fingerprints are normalized.

    bin/py -m unittest tests.test_i18n_golden_git_push_grant
    I18N_GOLDEN_UPDATE=1 bin/py -m unittest tests.test_i18n_golden_git_push_grant   # rewrite (review it)
"""
import io
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import golden_lang  # noqa: E402
from test_git_push_grant import HAS_KEYGEN, Base, gpg  # noqa: E402

GOLDEN = golden_lang.GOLDEN
UPDATE = os.environ.get("I18N_GOLDEN_UPDATE") == "1"


@unittest.skipUnless(HAS_KEYGEN, "no ssh-keygen (OpenSSH's client) on this machine")
class Golden(Base):
    maxDiff = None

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": golden_lang.LANG, "AGENT_HUB_LANG_CONTENT": golden_lang.LANG})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(golden_lang.pin_names())
        self.out: list[str] = []

    def case(self, name, *argv, tty=False, answers=()):
        out, err = io.StringIO(), io.StringIO()
        it = iter(answers)
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty)), \
                mock.patch("builtins.input", lambda prompt="": (a := next(it), out.write(f"{prompt}{a}\n"))[0]):
            code = gpg.main(list(argv))
        self.out.append(f"=== {name}: {' '.join(argv)}\n[exit {code}]\n--- stdout\n{out.getvalue()}--- stderr\n"
                        f"{err.getvalue()}")

    def test_texts(self):
        with mock.patch.object(gpg, "field_supported", lambda: False):
            self.case("usage")
            self.case("usage, two projects", "setup", "a", "b")
            self.case("bad --branches", "setup", "proj", "--branches", "most", "--yes")
            self.case("bad --email", "setup", "proj", "--email", "nope", "--yes")
            self.case("not registered", "setup", "nosuch", "--yes")
            del self.docs["proj"]["enforce"]
            self.case("not sandboxed", "setup", "proj", "--yes")
            self.docs["proj"]["enforce"] = {"sandbox": True}
            for origin in ("git@gitlab.com:o/proj.git", "/srv/git/proj.git", "git@github.com:o/a/b.git", "(native)"):
                self.docs["proj"]["paths"]["origin"] = origin
                self.case(f"origin {origin}", "setup", "proj", "--yes")
            self.docs["proj"]["paths"]["origin"] = "git@github.com:o/proj.git"
            self.case("status before", "status", "proj")
            self.vis = "public"
            self.case("public, nobody answering: none", "setup", "proj", "--yes")
            with self.denied():
                self.case("the key doesn't work", "setup", "proj", "--yes", "--branches", "all")
            self.vis = "private"
            self.case("interactive", "setup", "proj", tty=True, answers=["all", ""])
            self.case("again: the grant is kept", "setup", "proj", "--yes", "--branches", "others", "--email",
                      "a@b.example")
            self.case("status", "status", "proj")
            self.docs["proj"]["paths"]["origin"] = "git@github.com:o/other.git"
            self.case("origin changed", "status", "proj")
            self.docs["proj"]["paths"]["origin"] = "git@github.com:o/proj.git"
            self.vis = "unknown"
            self.case("unknown visibility: the noreply address only", "setup", "proj", "--yes", "--branches", "all")
            self.case("revoke", "setup", "proj", "--revoke")
            self.case("revoke again", "setup", "proj", "--revoke")
        with mock.patch.object(gpg, "visibility", lambda origin: "public"):
            self.case("onboard, public, nobody chose", "onboard", "proj")
        text = "\n".join(self.out).replace(str(self.tmp), "<TMP>")
        text = re.sub(r"SHA256:[A-Za-z0-9+/]{43}", "<FP>", text)
        f = GOLDEN / "git-push-grant.txt"
        if UPDATE or not f.exists():
            f.write_text(text)
            if not UPDATE:
                self.fail(f"new golden written: {f.name} (review it, then rerun)")
            return
        self.assertEqual(text, f.read_text())


if __name__ == "__main__":
    unittest.main()
