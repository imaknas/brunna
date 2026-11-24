#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""What a memory branch may hold (docs/design/mailbox-session-first.md, step 3's record): AGENT.md, RULES and STATE.md at its top, and notes
under memory/, plain files only, none under a name a runtime takes as rules. Agents the platform starts as the user work in copies of these
branches, and an agent loads what a folder tells it to (hooks in a settings file run as the user; a rules file next to a note it reads), so
what a project writes there must stay notes. An allow list: what a runtime calls its control files is not this module's to know, and every
one of them is outside it. The rules files' names are the adapters' to declare (runtime_ports.rules_file_names), refused at any depth.

Read off the machine's 26 memory repos, all their history (11-08): those paths and no others, every mode 100644, `memory/.gitkeep` the
only name that starts with a dot.

  refused(path, mode, inert_ok) → why | None               one entry; inert_ok: a name no runtime or tool reads as settings, rules or
                                                            hooks (INERT, Finder's folder data) is let through, where it can only be in
                                                            the way (an agent's folder), never where it would enter a branch
  unstage_refused(repo) → [entry text]                      after the sync's `git add` of memory/ and STATE.md: what this would refuse
                                                            goes back out of the index, left in the folder, so a stray file never
                                                            commits a project's notes to a refusal
  tree_problems(repo, rev, inert_ok) → [entry text]         a commit's whole tree (repo: one the platform owns)
  new_commit_problems(repo, old, new) → [entry text]        every commit in old..new, each one's whole tree, so a path added and removed
                                                            again counts, and a merge needs no reasoning. What old's own tree holds is
                                                            not new: a repo that already holds such a path goes on taking notes, and a
                                                            commit removing it, while agents stay out (folder_problems)
  is_memory_tree(repo, rev) → bool                          rev's tree is a memory branch's: not empty, and nothing in it a memory
                                                            branch may not hold (merge-branch refuses to merge one into a main)
  is_checkout(folder) → bool                                a memory checkout: its own git toplevel, the memory branch checked out
                                                            (hubpaths.sh memory_checkout is the same for shell)
  under_way(folder) → bool                                  a merge, rebase, cherry-pick or revert is under way there, or the index holds
                                                            conflicts: somebody's to finish, never touched, whoever began it. Every writer
                                                            of a memory checkout or copy asks first
  begun(folder) → bool                                      the same from files alone (no git: a project's folder), without the index
  locked(folder), writing(folder)                           with-lock.sh's lock on it, held here; writing: and nothing under way, or
                                                            Blocked (a lock that can't be had is too): nothing may be written
  unfinished(folder) → [path]                               the files there that hold its conflicts
  undo_conflict(folder, theirs, rc) → [path] | None         right after the caller's own `git merge <theirs>`, with nothing under way
                                                            before it: the one merge the platform undoes
  scrub(text), one_line(text, n), quoted(text, n)           text git or a project printed, for a record, a log or an alert
  folder_problems(folder) → [entry text]                    the files themselves, at the point of use: llm.py asks it before an agent
                                                            starts in a copy of a memory branch (works_in: memory), on every runtime

CLI, in the memory checkout: memory_paths.py unstage   (git-sync-memory.sh) prints what it left out, once, on stderr
                             memory_paths.py unfinished   the files, for a message
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import gitdirs  # noqa: E402  standard library only, as this module is (the sandboxed sync job imports it)
import names  # noqa: E402  the same

BRANCH = "agent-memory"   # the memory branch's name in a project's repo (its .agent-memory worktree)
TOP = ("AGENT.md", "RULES", "STATE.md")
TREE = "memory"
KEEP = ".gitkeep"
INERT = (".DS_Store",)   # Finder's view settings for a folder: read by no runtime, tool or git as settings, rules or hooks
FILE_MODE = "100644"
MAX_TREES = 5000      # a bound on one bundle's work; memory history is far below it
MAX_SHOWN = 5
RULES_NAMES: tuple[str, ...] | None = None   # set by a caller that has them (the sandboxed sync job is handed them); else asked once


def rules_names() -> tuple[str, ...]:
    global RULES_NAMES
    if RULES_NAMES is None:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import runtime_ports
        RULES_NAMES = tuple(runtime_ports.rules_file_names())
    return RULES_NAMES


def _plain_name(name: str) -> bool:
    return bool(name) and not name.startswith(".") and not any(unicodedata.category(c) in ("Cc", "Cf") for c in name)


def refused(path: str, mode: str = FILE_MODE, inert_ok: bool = False) -> str | None:
    """Why this entry may not be on a memory branch, or None."""
    if mode != FILE_MODE:
        return f"mode {mode}"
    if path in TOP:
        return None
    first, *rest = path.split("/")
    if first != TREE or not rest:
        return "outside what a memory branch holds"
    folders, name = rest[:-1], rest[-1]
    if not (all(map(_plain_name, folders)) and (_plain_name(name) or name == KEEP or (inert_ok and name in INERT))):
        return "a hidden name"
    rules = {n.casefold() for n in rules_names()}   # case-insensitive file systems load CLAUDE.md for claude.md
    if any(unicodedata.normalize("NFC", p).casefold() in rules for p in rest):
        return "a rules file's name"
    return None


def _shown(bad: list[str]) -> list[str]:
    more = len(bad) - MAX_SHOWN
    return bad[:MAX_SHOWN] + ([f"and {more} more"] if more > 0 else [])


def _text(path: str, why: str) -> str:
    return f"{json.dumps(path)} ({why})"   # a path is the project's own text: quoted, control characters and all


class Unreadable(Exception):
    pass


def _git(repo, *args) -> str:
    # a bare repo (fetch_bundle's, doctor's) by --git-dir: the owner's safe.bareRepository=explicit refuses one entered with -C
    r = subprocess.run(["git", *gitdirs.at(repo), "--literal-pathspecs", *args], capture_output=True)
    if r.returncode != 0:
        raise Unreadable(r.stderr.decode(errors="replace").strip()[-300:])
    return r.stdout.decode(errors="surrogateescape")


def _entries(repo, rev: str) -> set[tuple[str, str, str]]:
    """(mode, object, path) of every file, link and gitlink in rev's tree."""
    out = set()
    for line in _git(repo, "ls-tree", "-r", "-z", rev).split("\0"):
        if line:
            meta, path = line.split("\t", 1)
            mode, _kind, obj = meta.split()
            out.add((mode, obj, path))
    return out


def _problems(entries, known=(), inert_ok: bool = False) -> list[str]:
    bad = []
    for mode, obj, path in entries:
        if (mode, obj, path) not in known and (why := refused(path, mode, inert_ok)):
            bad.append(_text(path, why))
    return sorted(set(bad))


def unstage_refused(repo) -> list[str]:
    """What the sync just staged under memory/ and STATE.md that a memory branch may not hold, taken back out of the index (to what HEAD
    has); the files stay where they are. Deletions are kept: they only take something away. Raises Unreadable when git can't tell."""
    changed = [p for p in _git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", "--", TREE, "STATE.md").split("\0") if p]
    if not changed:
        return []
    bad = []
    for line in _git(repo, "ls-files", "-s", "-z", "--", *changed).split("\0"):
        if line:
            meta, path = line.split("\t", 1)
            if (why := refused(path, meta.split()[0])):
                bad.append((path, why))
    if bad:
        _git(repo, "reset", "-q", "--", *(p for p, _w in bad))
    return _shown(sorted(_text(p, w) for p, w in bad))


def tree_problems(repo, rev: str, inert_ok: bool = False) -> list[str]:
    try:
        return _shown(_problems(_entries(repo, rev), inert_ok=inert_ok))
    except Unreadable as e:
        return [f"can't be read: {e}"]


def is_memory_tree(repo, rev: str) -> bool:
    """rev's tree is a memory branch's (raises Unreadable when git can't tell)."""
    entries = _entries(repo, rev)
    return bool(entries) and not _problems(entries)


def new_commit_problems(repo, old: str | None, new: str) -> list[str]:
    try:
        trees = _git(repo, "log", "--format=%T", f"{old}..{new}" if old else new).split()
        if len(set(trees)) > MAX_TREES:
            return [f"over {MAX_TREES} new trees"]
        known = _entries(repo, old) if old else set()
        bad: set[str] = set()
        for tree in set(trees):
            bad.update(_problems(_entries(repo, tree), known))
        return _shown(sorted(bad))
    except Unreadable as e:
        return [f"can't be read: {e}"]


def is_checkout(folder, timeout: float = 30) -> bool:
    """Whether git run in this folder works on a memory branch. In a plain folder it works on the repo around it, the user's own, and a
    checkout of another branch is somebody's work: nothing there is committed, pulled or pushed. Runs git in the folder, so only where
    the caller's git may (the prefix and the branch, not paths: two spellings of one path are no difference)."""
    def out(*args):
        try:
            r = subprocess.run(["git", "-C", os.fspath(folder), *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.strip() if r.returncode == 0 else None
    return out("rev-parse", "--is-inside-work-tree", "--show-prefix") == "true" and out("symbolic-ref", "-q", "--short", "HEAD") == BRANCH


# a memory sync's exits when only a person (or an agent) can change it (hubpaths.sh MEMORY_*; memory_health says what to do)
CONFLICT, UNDER_WAY, REMOTE_BAD, BRANCH_BAD = 6, 7, 8, 9
SAID = 600   # how much of what a git or a project's job printed is kept, logged and said: a lock's message whole, its path twice
# git for the notes: never the project's hooks, nothing left in a stash (hubpaths.sh memory_git)
GIT = ("-c", "core.hooksPath=/dev/null", "-c", "merge.autoStash=false", "-c", "rebase.autoStash=false")
UNDER_WAY_FILES = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def begun(folder) -> bool:
    found = gitdirs.dirs(folder)
    return not found or any(os.path.lexists(found.git_dir / f) for f in UNDER_WAY_FILES)


def under_way(folder) -> bool:
    """Can't tell counts as one: nothing is touched."""
    if begun(folder):
        return True
    try:
        return bool(_git(folder, "ls-files", "-u", "-z"))
    except Unreadable:
        return True


class Blocked(Exception):
    pass


def finish_with(folder) -> str:
    """How what is under way there is finished, for a message (hubpaths.sh memory_finish)."""
    found = gitdirs.dirs(folder)
    for files, how in ((("rebase-merge", "rebase-apply"), "rebase"), (("CHERRY_PICK_HEAD",), "cherry-pick"), (("REVERT_HEAD",), "revert")):
        if found and any(os.path.lexists(found.git_dir / f) for f in files):
            return f"git -C '{folder}' add -u && git -C '{folder}' {how} --continue"
    return f"git -C '{folder}' commit -a -m merge"


LOCK_WAIT_S = 30


@contextlib.contextmanager
def _ended_by_term():
    """A TERM is an exit while the lock is held, so the lock goes with it (only where a signal can be handled: the main thread)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = signal.signal(signal.SIGTERM, lambda *_a: sys.exit(128 + signal.SIGTERM))
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


@contextlib.contextmanager
def locked(folder):
    with _ended_by_term(), _locked(folder):
        yield


@contextlib.contextmanager
def _locked(folder):
    found = gitdirs.dirs(folder)
    if not found:
        raise Blocked(f"{folder} is in no repo")
    lock = found.git_dir / names.REPO_LOCK
    for n in range(LOCK_WAIT_S + 1):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if n < LOCK_WAIT_S:
                time.sleep(1)
    else:
        raise Blocked(f"{lock} is still held after {LOCK_WAIT_S}s. If no session or sync is at work there, one that was killed left it: "
                      f"rmdir '{lock}'")
    try:
        yield
    finally:
        lock.rmdir()


@contextlib.contextmanager
def writing(folder):
    with locked(folder):
        if under_way(folder):
            raise Blocked("a merge or the like is under way there")
        yield


def unfinished(folder) -> list[str]:
    """The files with conflicts: the index's, and those that hold conflict marks (one can have been staged as it was)."""
    out: set[str] = set()
    for args in (("diff", "--name-only", "--diff-filter=U", "-z"), ("grep", "-l", "-z", "-e", "^<<<<<<< ")):
        r = subprocess.run(["git", "-C", os.fspath(folder), *args], capture_output=True)
        out |= {one_line(os.fsdecode(p), 80) for p in r.stdout.split(b"\0") if p}
    return sorted(out)


def undo_conflict(folder, theirs: str, rc: int) -> list[str] | None:
    """The files in conflict, the merge undone; None when it isn't known to be the caller's own: 1 is git's word that the merge it was
    asked for conflicted (it exits otherwise when it refused to begin), and what is under way must be that merge. An undoing that
    fails leaves it under way."""
    try:
        if rc != 1 or _git(folder, "rev-parse", "-q", "--verify", "MERGE_HEAD").strip() != theirs:
            return None
    except Unreadable:
        return None
    files = unfinished(folder)
    r = subprocess.run(["git", "-C", os.fspath(folder), "merge", "--abort"], capture_output=True)
    return files if r.returncode == 0 else None


_URL_USER = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@")
_SCP_USER = re.compile(r"(^|[^A-Za-z0-9/])[^\[\]\s/@'\"]+@([A-Za-z0-9.-]+:)", re.M)


def scrub(text: str) -> str:
    """Without the user name and password of any URL in it (hubpaths.sh scrub_urls)."""
    return _SCP_USER.sub(r"\1\2", _URL_USER.sub(r"\1", text))


def one_line(text: str, n: int) -> str:
    """As one line of printable characters, at most n: what it says is its writer's, not the platform's."""
    line = " ".join("".join(c if unicodedata.category(c)[0] not in "CZ" else " " for c in text).split())
    return line if len(line) <= n else line[:n - 1] + "…"


def quoted(text: str, n: int) -> str:
    """Text that isn't the platform's, for a log or an alert: one short line of it, quoted, no URL's user or password in it."""
    return json.dumps(one_line(scrub(text), n), ensure_ascii=False)


def folder_problems(folder) -> list[str]:
    """What's in a copy of a memory branch, read off the files, not git: this is what an agent started there would find. Its .git is the
    platform's. A file a branch committed as a link is a plain file in these copies (hublib.NO_SYMLINKS); a link found here is not
    followed and is refused. An inert name (INERT) is let through: refusing a project's notes over it keeps nothing out."""
    bad = []
    top = os.fspath(folder)
    for root, dirs, files in os.walk(top):
        rel = os.path.relpath(root, top)
        at = "" if rel == "." else rel + "/"
        if not at:
            dirs[:] = [d for d in dirs if d != ".git"]
            files = [f for f in files if f != ".git"]
        for name in dirs:
            st = os.lstat(os.path.join(root, name))
            path = at + name
            if stat.S_ISLNK(st.st_mode):
                bad.append(_text(path, "a link"))
            elif refused(path + "/x"):
                bad.append(_text(path, "a folder a memory branch doesn't hold"))
        for name in files:
            path = at + name
            st = os.lstat(os.path.join(root, name))
            why = ("a link" if stat.S_ISLNK(st.st_mode) else "not a plain file" if not stat.S_ISREG(st.st_mode)
                   else "executable" if st.st_mode & 0o111 else refused(path, inert_ok=True))
            if why:
                bad.append(_text(path, why))
    return _shown(sorted(bad))


def main(argv: list[str]) -> int:
    if argv == ["unfinished"]:
        print(", ".join(_shown(unfinished("."))))
        return 0
    if argv != ["unstage"]:
        print("usage: memory_paths.py unstage|unfinished", file=sys.stderr)
        return 2
    try:
        left = unstage_refused(".")
    except Unreadable as e:
        print(f"memory sync: what was staged can't be checked, nothing committed ({e})", file=sys.stderr)
        return 1
    if left:
        print("memory sync: left in the folder, not committed (a memory branch holds only plain notes): " + "; ".join(left), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
