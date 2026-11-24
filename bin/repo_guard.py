#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""Which folders aren't connected as they are (bin/connect.py, bin/onboard-project.sh): one whose name can't be a project's,
one inside another repo that is none itself, a repo whose files are elsewhere, and one whose current branch has no commits
beside others that do.

The folder isn't trusted yet: git runs in it with its hooks and fsmonitor off.

Usage: repo_guard.py check <folder>   exit 1 and say why, when it isn't connected
       repo_guard.py local            the lines of .git/info/exclude for the coding agents' files of this machine
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import i18n  # noqa: E402

_ = i18n.domain(__file__)
# the folder's name is the project's: in file names, remotes and commands
NOT_IN_A_NAME = re.compile(r"[\s\"'`$&;|<>()*?{}!#~\\\[\]\x00-\x1f\x7f]|^-")


def sgit(path: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    """git in a folder that isn't trusted yet: its hooks and fsmonitor off."""
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", "-C", str(path), *args],
                          capture_output=True, text=True, errors="replace")


def is_repo(p: pathlib.Path) -> bool:
    return (p / ".git").exists()


def has_commits(p: pathlib.Path) -> bool:
    return is_repo(p) and sgit(p, "rev-parse", "-q", "--verify", "HEAD").returncode == 0


def holds_nothing(p: pathlib.Path) -> bool:
    """Nothing of it is in its history: no commits, or the empty one connecting makes. What is in it is its owner's, to commit
    when they choose, and no reason to wait."""
    return not has_commits(p) or not sgit(p, "ls-tree", "HEAD").stdout


def local_files() -> list[str]:
    """Every coding agent's files that are this machine's only, installed or not."""
    import runtime_ports
    out: list[str] = []
    for name in runtime_ports.RUNTIMES:
        if name != "fake" and (fn := getattr(runtime_ports._module(name), "local_ignores", None)):
            out += [x for x in fn() if x not in out]
    return out


def parent_repo(path) -> pathlib.Path | None:
    """The repo a folder that is none itself is inside of. One at the home folder or above is of everything in it (dotfiles
    kept in git), no project the folder is a part of."""
    real = pathlib.Path(os.path.realpath(path))
    if os.path.lexists(real / ".git"):
        return None
    home = pathlib.Path(os.path.realpath(pathlib.Path.home()))
    return next((d for d in real.parents if not home.is_relative_to(d)
                 and ((d / ".git").is_file() or (d / ".git" / "HEAD").is_file())), None)


def refusal(path) -> str | None:
    """Why this folder isn't connected as it is, with what to do; None when it may be."""
    p = pathlib.Path(os.path.realpath(os.path.expanduser(str(path))))
    if NOT_IN_A_NAME.search(p.name):
        return _("{path}: a project's name is its folder's, and {name!r} can't be one (no spaces, quotes or other characters a shell "
                 "takes for its own): rename the folder (nothing was done)", path=path, name=p.name)
    if outer := parent_repo(p):
        return _("{path} is inside the repo {repo}, and a repo made in it would be apart from that one. Connect {repo}, or, if it "
                 "is a project of its own: git init {path}, then run this again (nothing was done)", path=path, repo=outer)
    if not is_repo(p):
        return None
    top = sgit(p, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return _("git can't work in {path}: {why} (nothing was done)", path=path, why=top.stderr.strip()[-200:])
    if os.path.realpath(top.stdout.strip()) != str(p):
        return _("{path} is a repo whose files are in {top}; connect that folder, or make it a repo of its own (nothing was done)",
                 path=path, top=top.stdout.strip())
    if not has_commits(p) and sgit(p, "for-each-ref", "--count=1").stdout.strip():
        return _("{path} has no commits on its current branch, but other branches or tags: check out the branch to connect, or "
                 "make the first commit yourself, then run this again (nothing was done)", path=path)
    return None


def main(argv: list[str]) -> int:
    if argv == ["local"]:
        # a pattern with a folder in it holds only at the top; the files are local at any depth
        print("\n".join(f"**/{f}" if "/" in f else f for f in local_files()))
        return 0
    if len(argv) != 2 or argv[0] != "check":
        print(_(__doc__), file=sys.stderr)
        return 2
    if why := refusal(argv[1]):
        print(why, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
