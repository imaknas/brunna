"""Fetching an installed service's code as the owner, running nothing from its repository (docs/design/
service-ecosystem.md, step 2a, "Fetching").

Every git command here runs with no global or system config (GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM): the
owner's own filters (git-lfs) or aliases never run on a stranger's tree; hooks off; objects checked as they arrive
(fetch.fsckObjects); no prompts; a timeout with the whole process group killed; the file protocol refused unless the
caller allows a local source (tests). Only https and ssh URLs otherwise; a URL or ref that could be read as an option
is refused. The manifest is read out of the object store (cat-file, capped), the tree's size summed before checkout,
and the checkout writes no symbolic links (core.symlinks=false).
"""
from __future__ import annotations

import os
import pathlib
import re
import signal
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import i18n  # noqa: E402

_ = i18n.domain(__file__)

TIMEOUT = 600
PACK_MAX = 1 << 30          # what the fetch may bring
TREE_MAX = 4 << 30          # what the checkout may write
MANIFEST_MAX = 64 << 10
MANIFEST = "brunna-service.yaml"
SSH_SCP = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+\Z")
HEX = re.compile(r"[0-9a-f]{40}\Z")


class FetchError(Exception):
    pass


def env() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": os.environ.get("HOME", "/"),
            "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", **({"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]}
                                                          if os.environ.get("SSH_AUTH_SOCK") else {})}


def flags(allow_local: bool) -> list[str]:
    return ["-c", "core.hooksPath=/dev/null", "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true",
            "-c", f"protocol.file.allow={'always' if allow_local else 'never'}", "-c", "submodule.recurse=false",
            "-c", "core.fsmonitor=false", "-c", "core.symlinks=false", "-c", "core.autocrlf=false"]


def git(args: list[str], allow_local: bool = False, timeout: int = TIMEOUT, text: bool = True) -> str | bytes:
    """Run git with the rules above; its output, or FetchError with its last line."""
    p = subprocess.Popen(["git", *flags(allow_local), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env(), start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise FetchError(_("git took longer than {s} seconds", s=timeout)) from None
    if p.returncode:
        last = (err.decode(errors="replace").strip().splitlines() or [""])[-1]
        raise FetchError(_("git failed: {err}", err=last))
    return out.decode() if text else out


def url_problem(url: str, allow_local: bool = False) -> str | None:
    if not isinstance(url, str) or not url or url.startswith("-") or any(c.isspace() for c in url):
        return _("not a repository address")
    if url.startswith(("https://", "ssh://")) or SSH_SCP.match(url):
        return None
    if allow_local and (url.startswith("file://") or url.startswith("/")):
        return None
    return _("only https:// or ssh addresses are accepted")


def ref_problem(ref: str) -> str | None:
    if ref.startswith("-"):
        return _("a ref can't start with -")
    if HEX.match(ref):
        return None
    try:
        git(["check-ref-format", "--allow-onelevel", ref], timeout=10)
    except FetchError:
        return _("{ref} is not a valid tag or branch name", ref=ref)
    return None


def _size(path: pathlib.Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def fetch(url: str, ref: str | None, repo: pathlib.Path, allow_local: bool = False) -> str:
    """Into a new bare repo at `repo`: the ref (or the remote's HEAD), depth 1. The commit it resolved to."""
    if (why := url_problem(url, allow_local)) or (ref and (why := ref_problem(ref))):
        raise FetchError(why)
    git(["init", "--quiet", "--bare", str(repo)], timeout=30)
    git(["--git-dir", str(repo), "fetch", "--quiet", "--depth=1", "--no-tags", "--", url, ref or "HEAD"],
        allow_local=allow_local)
    if _size(repo) > PACK_MAX:
        raise FetchError(_("the repository is over {gb} GB", gb=PACK_MAX >> 30))
    commit = git(["--git-dir", str(repo), "rev-parse", "--verify", "FETCH_HEAD^{commit}"], timeout=30).strip()
    if not HEX.match(commit):
        raise FetchError(_("the fetched ref is not a commit"))
    git(["--git-dir", str(repo), "update-ref", "refs/brunna/installed", commit], timeout=30)
    return commit


def manifest(repo: pathlib.Path, commit: str) -> str:
    """The manifest's text at the commit, read from the object store."""
    spec = f"{commit}:{MANIFEST}"
    try:
        size = int(git(["--git-dir", str(repo), "cat-file", "-s", spec], timeout=30).strip())
    except (FetchError, ValueError):
        raise FetchError(_("the repository has no {file} at its root", file=MANIFEST)) from None
    if size > MANIFEST_MAX:
        raise FetchError(_("{file} is over {kb} KB", file=MANIFEST, kb=MANIFEST_MAX >> 10))
    kind = git(["--git-dir", str(repo), "cat-file", "-t", spec], timeout=30).strip()
    if kind != "blob":
        raise FetchError(_("{file} is not a file", file=MANIFEST))
    raw = git(["--git-dir", str(repo), "cat-file", "blob", spec], timeout=30, text=False)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FetchError(_("{file} is not UTF-8", file=MANIFEST)) from None


def checkout(repo: pathlib.Path, commit: str, dest: pathlib.Path) -> None:
    """The commit's tree into dest (which must not exist), no links, after its size is summed."""
    total = 0
    for line in git(["--git-dir", str(repo), "ls-tree", "-r", "-l", "-z", commit], timeout=120).split("\0"):
        if not line:
            continue
        meta = line.split("\t", 1)[0].split()
        if len(meta) == 4 and meta[1] == "commit":
            raise FetchError(_("the repository has submodules, which installed services don't support"))
        if len(meta) == 4 and meta[3].isdigit():
            total += int(meta[3])
    if total > TREE_MAX:
        raise FetchError(_("the code is over {gb} GB", gb=TREE_MAX >> 30))
    dest.mkdir(parents=True, mode=0o700)
    git(["--git-dir", str(repo), "--work-tree", str(dest), "checkout", "--quiet", "-f", commit, "--", "."],
        timeout=TIMEOUT)
    index = repo / "index"
    if index.exists():
        index.unlink()
