#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""Whether each project's memory notes reach the platform: how its last syncs went, told to whoever can do something.

Every sync of a project's memory (a session's end, the hourly sync, a platform script's; sandboxed or not) is recorded here:
a failure with when it began, what kind it is (the sync's exit) and what was printed, a success clears it. The session-end
hook has no one to say it to, and a conflict that was undone leaves nothing to see, so the project's next session start says
it (notice), and the hourly sync alerts the owner of a project that keeps failing (alert): one that only a person or an agent
can fix (KINDS: a conflict, a merge under way, a remote that isn't the platform's, another branch checked out) at the hourly
sync's next round, any other after three hours. One alert per project, under one id: alert.once repeats it daily while it
stands, never hourly, whatever its wording does. A failure that was once of the first kind stays so until it's over.

What is said is the platform's: a sentence for the kind, with the command. What a git or a project's job printed is the
project's own text (file names, its git's messages; a sandboxed project's job chooses all of it): one line of it, short, quoted,
marked as such, with no URL's user name or password.

  record(name, why | None, rc, memdir)   one sync's outcome (why: None = it went)
  sync(memdir, name) → (rc, text)        an unsandboxed project's sync (with-lock.sh git-sync-memory.sh), recorded; an offline
                                         round clears nothing
  notice(name) → text | None             what a session of it is told at its start
  alert(name)                            the alert for its standing, and the one telling, once for each, that a code remote of the
                                         project's is known here to have its memory branch (nothing pushed there is taken back)

CLI: memory_health.py sync <memdir> [<name>]           (hub-event, worktree-memory.sh, sync-all-memory.sh)
     memory_health.py not-a-checkout <name> <memdir>   (sync-all-memory.sh; nothing for a project with memory: false)
     memory_health.py alert <name>
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import memory_paths  # noqa: E402
import names  # noqa: E402

_ = i18n.domain(__file__)

BIN = pathlib.Path(__file__).resolve().parent
FILE = hubconf.STATE / "memory-sync.json"   # {project: {"since", "rc", "said", "stuck"} while failing, "told": [digests], "told_n"}
STUCK_AFTER = datetime.timedelta(minutes=50)      # the hourly sync's next round
FAILING_AFTER = datetime.timedelta(minutes=170)   # its fourth
SAID = memory_paths.SAID
MAX_TOLD = 5     # code remotes told about, per project, ever: their names are the project's to make
PLAIN_FOLDER = 10   # no sync's exit: the folder isn't a checkout at all
ID = ["memory-sync"]


# only a person or an agent can fix these
KINDS = (memory_paths.CONFLICT, memory_paths.UNDER_WAY, memory_paths.REMOTE_BAD, memory_paths.BRANCH_BAD, PLAIN_FOLDER)
_LIVE = FILE


def _kind_text(rc, path: str) -> str:
    """What a sync's exit is and what to do about it, in the platform's words."""
    branch = memory_paths.BRANCH
    if rc == memory_paths.CONFLICT:
        return _("the notes there and the hub's both changed the same files, and nothing is pushed until somebody merges them: "
                 "git -C '{path}' pull --no-rebase, take the conflict marks out, then git -C '{path}' commit -a -m merge", path=path)
    if rc == memory_paths.UNDER_WAY:
        return _("a merge or the like is under way there, and nothing is committed or pushed until somebody finishes it: take the "
                 "conflict marks out of its files (git -C '{path}' status lists them), then run {command}", path=path,
                 command=memory_paths.finish_with(path))
    if rc == memory_paths.REMOTE_BAD:
        return _("its branch {branch} doesn't follow the platform's memory repo, so nothing is pushed: git -C '{path}' config "
                 "branch.{branch}.remote memory, and git -C '{path}' remote -v says where that leads", path=path, branch=branch)
    if rc == memory_paths.BRANCH_BAD:
        return _("{path} is not a checkout of {branch}, so the notes in it are not committed or pushed: check that branch out there",
                 path=path, branch=branch)
    if rc == PLAIN_FOLDER:
        return _("{path} is a plain folder, not a checkout of {branch}, so the notes in it are not committed or pushed: move it "
                 "aside, run git -C '{ws}' worktree add -f .agent-memory {branch}, and copy the notes back in", path=path, branch=branch,
                 ws=os.path.dirname(os.path.abspath(path)))
    return _("the sync in {path} fails", path=path)


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


@contextlib.contextmanager
def _state():
    """The file's content, written back when it changed; one writer at a time (hooks and the hourly sync meet here)."""
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FILE.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _load()
        before = json.dumps(d, sort_keys=True)
        yield d
        if json.dumps(d, sort_keys=True) != before:
            tmp = FILE.with_name(f".{FILE.name}.{os.getpid()}")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
            tmp.replace(FILE)


def record(name: str | None, why: str | None, rc: int = 1, memdir=None, now: datetime.datetime | None = None) -> None:
    """A record that can't be written changes nothing about the sync."""
    if not name or ("unittest" in sys.modules and FILE == _LIVE):   # a test never writes the live one
        return
    try:
        with _state() as d:
            e = d.setdefault(name, {})
            if why is None:
                for k in ("since", "rc", "said", "stuck", "path"):
                    e.pop(k, None)
            else:
                e.update(since=e.get("since") or (now or datetime.datetime.now()).isoformat(timespec="seconds"), rc=rc,
                         said=memory_paths.one_line(memory_paths.scrub(why), SAID), stuck=bool(e.get("stuck")) or rc in KINDS,
                         **({"path": str(memdir)} if memdir else {}))
            if not e:
                del d[name]
    except OSError as err:
        print(f"memory_health: not recorded: {err}", file=sys.stderr)


def standing(name: str | None) -> dict | None:
    e = _load().get(name or "")
    return e if isinstance(e, dict) and e.get("since") else None


def why_text(e: dict) -> str:
    text = _kind_text(e.get("rc"), str(e.get("path") or ".agent-memory"))
    if e.get("said"):
        text += " " + _("(printed there, the project's own text: {said})", said=json.dumps(str(e["said"])[:SAID], ensure_ascii=False))
    return text


def _name_of(memdir) -> str | None:
    import project_git
    found = project_git.memory_project(memdir)
    return found[0] if found else None


def sync(memdir, name: str | None = None) -> tuple[int, str]:
    name = name or _name_of(memdir)
    r = subprocess.run([str(BIN / "with-lock.sh"), str(BIN / "git-sync-memory.sh")], cwd=memdir, capture_output=True, text=True,
                       errors="replace")
    said = memory_paths.scrub(r.stderr + r.stdout)
    if r.returncode != 0:
        record(name, said or f"exit {r.returncode}", r.returncode, memdir)
    elif not names.env("OFFLINE"):   # an offline round only commits: it says nothing about what a push found
        record(name, None)
    return r.returncode, said


def not_a_checkout(name: str, memdir) -> None:
    if (hubconf.projects().get(name) or {}).get("memory") is False:
        record(name, None)
        return
    record(name, "", memory_paths.BRANCH_BAD if os.path.lexists(os.path.join(memdir, ".git")) else PLAIN_FOLDER, memdir)


def notice(name: str | None) -> str | None:
    e = standing(name)
    if not e or not (e.get("stuck") or _age(e) >= FAILING_AFTER):
        return None
    return _("⚠️ This project's memory notes have not reached the hub since {since} (notes written here stay here, and nothing that "
             "reads them sees them). Tell the user, and fix it if you can:\n{why}", since=str(e["since"])[:16].replace("T", " "),
             why=why_text(e))


def _age(e: dict, now: datetime.datetime | None = None) -> datetime.timedelta:
    try:
        return (now or datetime.datetime.now()) - datetime.datetime.fromisoformat(str(e["since"]))
    except ValueError:
        return datetime.timedelta(0)


def alert(name: str, now: datetime.datetime | None = None) -> None:
    """A broken alert doesn't change the outcome."""
    import alert as alerts
    key, e = f"memory-sync:{name}", standing(name)
    refused = f"memory-sync-refused:{name}"
    # a sandboxed project's refused notes have their own (project_git.refusal_alert)
    due = e and _age(e, now) >= (STUCK_AFTER if e.get("stuck") else FAILING_AFTER) and refused not in alerts.open_keys(refused)
    try:
        if due:
            alerts.once(key, [_("{project}: its memory notes have not reached the hub since {since}: {why}", project=name,
                                since=str(e["since"])[:16].replace("T", " "), why=why_text(e))], "", ids=ID)
        elif not e and key in alerts.open_keys(key):
            alerts.once(key, [], _("{project}: its memory notes reach the hub again", project=name), ids=[])
        tell_code_remotes(name)
    except Exception as err:
        print(f"memory_health: the alert was not sent: {err!r}", file=sys.stderr)


def tell_code_remotes(name: str) -> None:
    """Once for each remote (by a digest of its name; at most MAX_TOLD times, ever): alert.once, asked again, would repeat it daily, and
    nothing here makes it go away (the notes there are the owner's to take down). Under one id: one found later is told as a
    new alert, the standing one closed first."""
    import alert as alerts
    import project_git
    doc = hubconf.projects().get(name)
    if not doc:
        return
    try:
        found = project_git.code_remotes_with_notes(name, doc)
    except project_git.Unavailable:
        return
    e = _load().get(name) or {}
    key, told, n = f"memory-on-code-remote:{name}", list(e.get("told") or []), int(e.get("told_n") or 0)
    digest = {r: hashlib.sha256(r.encode(errors="replace")).hexdigest()[:12] for r in found}
    new = [r for r in found if digest[r] not in told][:max(0, MAX_TOLD - n)]
    closed = _("{project}: its memory notes are only in the platform's memory repo, as far as this machine knows", project=name)
    if (new or not found) and key in alerts.open_keys(key) and not alerts.once(key, [], closed, ids=[]):
        return
    still = [t for t in told if t in digest.values()]   # one that went and comes back is told again (MAX_TOLD counts it)
    if still != told:
        with _state() as d:
            d.setdefault(name, {})["told"] = still
    if not new:
        return
    shown = ", ".join(memory_paths.quoted(r, 40) for r in new)
    if alerts.once(key, [_("{project}: its memory notes (branch {branch}) are on its code remotes {remotes}, not only in the platform's "
                           "memory repo. Anyone who can read that repo can read them. brunna doesn't delete anything there: take the "
                           "branch down yourself if they shouldn't be there", project=name, branch=memory_paths.BRANCH, remotes=shown)], closed, ids=["memory-on-code-remote"]):
        with _state() as d:
            d.setdefault(name, {}).update(told=still + [digest[r] for r in new], told_n=n + len(new))


def main(argv: list[str]) -> int:
    if argv[:1] == ["sync"] and len(argv) in (2, 3):
        rc, said = sync(pathlib.Path(argv[1]), argv[2] if len(argv) == 3 else None)
        if said.strip():   # into the hourly log: the project's own text
            print(memory_paths.quoted(said, SAID), file=sys.stderr)
        return rc
    if argv[:1] == ["not-a-checkout"] and len(argv) == 3:
        not_a_checkout(argv[1], argv[2])
        return 0
    if argv[:1] == ["alert"] and len(argv) == 2:
        alert(argv[1])
        return 0
    print("usage: memory_health.py sync <memdir> [<name>] | not-a-checkout <name> <memdir> | alert <name>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
