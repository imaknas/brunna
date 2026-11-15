"""E1 golden (docs/design/i18n.md) for the git broker's refusal texts (bin/git_allow.py, bin/git_broker.py,
bin/git_wrapper.py; docs/design/git-broker.md, "Step 2 build list", slice a), on zh-Hant: what the allowlist says for a
set of calls, and every message the broker and the wrapper give, each filled with its placeholders' names. In-process,
nothing runs.

    bin/run-tests   (I18N_GOLDEN_UPDATE=1 rewrites it: only on purpose, then review it)
"""
import ast
import os
import pathlib
import string
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import golden_lang  # noqa: E402

GOLDEN = golden_lang.GOLDEN / "git-broker.txt"
UPDATE = os.environ.get("I18N_GOLDEN_UPDATE") == "1"
CALLS = [
    (["frob"], {}), (["submodule", "add", "u"], {}), (["bisect", "start"], {}), (["daemon"], {}),
    (["commit", "--exe", "x"], {}), (["commit", "--author"], {}), (["gc", "--quiet=1"], {}), (["gc", "x"], {}),
    (["commit", "-S", "-m", "x"], {}), (["tag", "-s", "v1"], {}), (["clone", "--bare", "u"], {}),
    (["merge", "-s", "evil", "x"], {}), (["add", "-p"], {}), (["commit", "-e", "-m", "x"], {}),
    (["commit", "--fixup=reword:HEAD"], {}), (["commit"], {"EDITOR": "vim"}), (["notes", "add"], {}),
    (["notes", "--ref", "refs/heads/x", "add", "-m", "n"], {}), (["notes", "add", "-m", "n", "a", "b"], {}),
    (["notes", "edit"], {}), (["-c", "core.fsmonitor=x", "add", "."], {}), (["-c", "user.name", "add", "."], {}),
    (["-c", "commit.gpgSign=true", "add", "."], {}), (["-c", "user.name=a\nb", "add", "."], {}),
    (["add", "."], {"GIT_SSH_COMMAND": "x"}), (["add", "."], {"GIT_TRACE": "/tmp/t"}),
    (["config", "--global", "a.b", "c"], {}), (["config", "--add", "a.b", "c"], {}),
    (["config", "--bool", "a.b", "c"], {}), (["config", "--unset", "a", "b"], {}), (["config", "set", "a.b"], {}),
    (["remote", "add", "o"], {}), (["remote", "add", "o/x", "u"], {}), (["remote", "set-branches", "o", "x"], {}),
    (["reflog", "expire"], {}), (["archive", "--remote=u"], {}), (["fsck", "--lost-found"], {}),
    (["format-patch", "-o", "d"], {}), (["hash-object", "-w", "--literally", "f"], {}), (["credential-store"], {}),
]


def msgids(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    return list(dict.fromkeys(n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call)
                              and isinstance(n.func, ast.Name) and n.func.id == "_" and n.args
                              and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)))


def render() -> str:
    import git_allow
    import git_broker
    import git_wrapper
    out = []
    for argv, env in CALLS:
        c = git_allow.classify(argv, env)
        shown = " ".join(argv).replace("\n", "\\n") + "".join(f"  [{k}={v}]" for k, v in env.items())
        out.append(f"$ git {shown}\n{git_allow.message(c) if c.reason else c.kind}")
    for mod in (git_allow, git_broker, git_wrapper):
        out.append(f"--- {pathlib.Path(mod.__file__).name}")
        for m in msgids(pathlib.Path(mod.__file__)):
            kw = {f: f"<{f}>" for _l, f, _s, _c in string.Formatter().parse(m) if f}
            out.append(mod._(m, **kw))
    return "\n".join(out) + "\n"


class Golden(unittest.TestCase):
    maxDiff = None

    def test_refusal_texts(self):
        with mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": golden_lang.LANG, "BRUNNA_LANG_UI": golden_lang.LANG}):
            got = render()
        if UPDATE:
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(got)
        self.assertEqual(got, GOLDEN.read_text())


if __name__ == "__main__":
    unittest.main()
