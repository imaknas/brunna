#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""`brunna upgrade`: move the hub checkout to a release (or the remote's main) and bring this machine in step with it.

    brunna upgrade [--to <tag> | --main] [--check] [--yes]

Which checkouts it moves. The way HEAD sits is the channel the checkout follows; nothing else is recorded:
  - detached HEAD (a clone checked out at a release tag): follows releases. Default target: the newest release tag
    (vX.Y.Z by version order; pre-releases such as v1.0.0-rc.1 only with --to)
  - on the branch main: follows main. Default target: the remote's main, fast-forwarded (= --main). Going to a tag from
    here would move the checkout back, so it is never the default; `--to <tag>` switches the checkout to releases
  - anything else is a development checkout and is refused, never moved: another branch, a linked worktree, tracked
    files with uncommitted changes, or commits that are on neither the remote's branches nor a release tag (checked
    after the fetch). The author's checkout develops on main and is ahead of its remote, so it is refused
  The remote: main's upstream (branch.main.remote), else origin, else the only remote.

What it does: refuse an unsafe tree → `git fetch --tags` → show the CHANGELOG.md sections between this checkout's
release and the target (breaking entries first; for main also the conventional titles not released yet) → ask (--yes
skips; with no terminal and no --yes it stops, exit 4) → move the checkout (a detached checkout of the tag; for main a
fast-forward) while holding the scheduler's lock, so no tick starts halfway (a tick running right now: refused) →
run the target's bin/setup-python → hand over to the target's own step list (below).

The in-step commands, each from the target's files, in this order; the first that fails stops the rest and says how to
go back (`brunna upgrade --to <previous>`, which runs the same steps on the old code):
  registry.py check · brunna config migrate (a setting an earlier release took and this one's config check refuses,
  brought in line and said, so config apply doesn't stop the upgrade halfway: e.g. a runtime.default that runs only in
  the throwaway worktree becomes none) · brunna config apply · brunna init --scheduler-only · brunna land ·
  brunna user-settings --update (the platform's own entries in the user's tool settings brought to this version: only
  entries the owner installed, such as the worktree hooks; nothing is added to settings that have none) ·
  brunna install-cli --dir <where the wrapper is> (only when the brunna on PATH is install-cli's wrapper) ·
  update-runtimes.py --first (a checked copy of each coding agent in the store; its failure is doctor's to show, it
  does not fail the upgrade) · brunna doctor (the report: its problems are shown, they do not fail the upgrade)
When they all pass, .state/upgrade.json records the commit they ran for (brunna init writes it too, for a fresh
install). The checkout already at the target with no such record for it (a manual `git pull` or checkout) gets
setup-python and the steps without a move; going back then means to the commit that record last named.

Hand-over between versions (a contract from v0.2.0 on: keep it): after moving the checkout and running the target's
bin/setup-python, this process execs the target's `bin/upgrade.py --steps --from <ref>`, where <ref> is what to go back
to: a release tag, a commit, or `main`. The target runs its own step list. A target without bin/upgrade.py (v0.1.0)
gets this version's list, run from the target's files, without the scheduler step (its init has no --scheduler-only)
and without user-settings (it has none).

Exit codes: 0 done, or nothing to do · 10 (--check) `brunna upgrade` would move the checkout · 1 the fetch, the move
or a step failed · 2 usage · 3 refused: not a checkout to move (says why and what to do) · 4 not confirmed.

doctor: doctor_lines() says which release this is and whether a newer one is known, from .state/upgrade-check.json
(written by every fetch here, and by doctor itself). Doctor touches the network only when that record is older than a
day: one `git ls-remote --tags` with a 5-second limit, no prompts; a failure is recorded too, so an offline machine
waits once a day, not every run.

`upgrade.py --release-check` (the daily job brunna/release-check): the same lookup, for a checkout that follows releases
only. A release newer than the checkout's goes into that record as `update` (current, latest, security, since, the commit
HEAD was at). security: a `### Security` heading in the CHANGELOG.md of a version between the two, read from the tag,
fetched alone into a throwaway repo in the platform's private temp area (FETCH_TIMEOUT, FETCH_MAX, no prompts; nothing of
it reaches the hub's repo): nothing of it is run or checked out, and no text of the remote's goes anywhere, an error's
included (why a lookup failed is one of reason()'s). A fetch that fails leaves it unknown (None), tried again the next day:
until it is known no session is told and the alert stays as it is.
pending() is what every place that shows it reads: the record and HEAD, no network and no git, None once HEAD moved.
What is shown is the two tags, each `v<digits>.<digits>.<digits>` exactly, in the platform's own words; nothing upgrades
by itself and no agent is told to. `updates.check: false` in the settings turns off the job's and doctor's lookup, and
the reminders.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

BIN = pathlib.Path(__file__).resolve().parent
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402
import names  # noqa: E402
import i18n  # noqa: E402

_ = i18n.domain(__file__)

ROOT = hubconf.ROOT
AVAILABLE, FAILED, USAGE, REFUSED, UNCONFIRMED = 10, 1, 2, 3, 4
CHECK_EXPIRY = datetime.timedelta(hours=24)
LS_REMOTE_TIMEOUT = 5
FETCH_TIMEOUT = 60   # the one tag whose changelog says whether a release carries a security fix
FETCH_MAX = 128 << 20   # bytes one file that fetch writes may have (its pack)
OUTPUT_MAX = 4 << 20    # of a remote's answer read
CHANGELOG_MAX = 4 << 20   # a changelog larger than this isn't read
TOLD_EVERY = datetime.timedelta(hours=24)   # sessions are told of one release once in this time, all together
RELEASE = re.compile(r"^v([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})(?:-([0-9A-Za-z.-]+))?$")
SECURITY = re.compile(r"^### Security[ \t]*$", re.M)   # cliff.toml's heading
SECTION = re.compile(r"^## (v\d+\.\d+\.\d+\S*)")
SHOWN_TYPES = re.compile(r"^(feat|fix|perf|revert)(\([^)]*\))?!?: ")   # what cliff.toml lists
BREAKING_TITLE = re.compile(r"^[a-z]+(\([^)]*\))?!: ")
WRAPPER_MARKS = tuple(f"written by {n} install-cli" for n in names.all("cli"))   # either name wrote it
# install-cli's exec line: through bin/py to bin/agenthub (until v0.7), or bin/brunna directly
WRAPPER_EXEC = re.compile(r'^exec (?:"[^"\n]*/bin/py" "[^"\n]*/bin/(?:agenthub|brunna)"|"[^"\n]*/bin/brunna") "\$@"$', re.M)
# the wrapper whole, as any version wrote it: its comment and its exec line (or cut off after the comment). Anything more in
# it is somebody's own script
WRAPPER = re.compile(r'#!/bin/sh\n#([^\n]*)\n?(?:exec (?:"[^"\n]*/bin/py" )?"([^"\n]*)/bin/(?:agenthub|brunna)" "\$@"\n?)?')

USAGE_TEXT = """usage: brunna upgrade [--to <tag> | --main] [--check] [--yes]
  (no option)   the newest release; a checkout on main follows main instead
  --to <tag>    that release (going back, too); a commit id also works
  --main        the remote's main, for following development
  --check       only say what would change: exit 0 up to date, 10 an upgrade is there, 3 the checkout is not one to move
  --yes         do not ask before moving the checkout
"""


# ---------- git ----------

def git(*args, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, encoding="utf-8",
                          errors="replace", env=env, stdin=subprocess.DEVNULL)


def out(*args) -> str | None:
    r = git(*args)
    return r.stdout.strip() if r.returncode == 0 else None


def is_checkout() -> bool:
    """The hub root is the top of a git checkout (a tarball or a copy is not)."""
    top = out("rev-parse", "--show-toplevel")
    return bool(top) and pathlib.Path(top).resolve() == ROOT.resolve()


def describe() -> str:
    """The release this checkout is at (= agenthub --version): `git describe` against the v* tags; an untagged commit
    reads <tag>-<n>-g<sha>, a checkout with no release yet its short sha, a tree outside git `unknown`."""
    return out("describe", "--tags", "--match", "v[0-9]*", "--always", "--dirty") or "unknown"


def nearest_release(rev: str = "HEAD") -> str | None:
    return out("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", rev)


def version_key(tag: str | None) -> tuple | None:
    m = RELEASE.match(tag or "")
    if not m:
        return None
    return int(m[1]), int(m[2]), int(m[3]), 0 if m[4] else 1, m[4] or ""


def plain(tag) -> bool:
    """A release's tag, v<digits>.<digits>.<digits> and nothing else: a tag is the remote's data, and only one of this
    shape is shown."""
    m = RELEASE.fullmatch(tag) if isinstance(tag, str) else None
    return bool(m) and not m[4]


def newest(tags) -> str | None:
    """The newest release by version order; pre-releases are never the default."""
    return max((t for t in tags if plain(t)), key=version_key, default=None)


def remote_name() -> str | None:
    remotes = (out("remote") or "").split()
    up = out("config", "--get", "branch.main.remote")
    if up in remotes:
        return up
    if "origin" in remotes:
        return "origin"
    return remotes[0] if len(remotes) == 1 else None


def sha(rev: str) -> str | None:
    return out("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")


def branch() -> str | None:
    return out("symbolic-ref", "--quiet", "--short", "HEAD")


def is_ancestor(a: str, b: str) -> bool:
    return git("merge-base", "--is-ancestor", a, b).returncode == 0


def linked_worktree() -> bool:
    gd, cd = out("rev-parse", "--absolute-git-dir"), out("rev-parse", "--path-format=absolute", "--git-common-dir")
    return bool(gd and cd) and pathlib.Path(gd).resolve() != pathlib.Path(cd).resolve()


def local_commits(rev: str, remote: str) -> int:
    """Commits of rev that are on none of the remote's branches and no release tag."""
    n = out("rev-list", "--count", rev, "--not", f"--remotes={remote}", "--tags=v[0-9]*")
    return int(n) if n and n.isdigit() else 0


# ---------- records (.state) ----------

def check_file() -> pathlib.Path:
    return hubconf.STATE / "upgrade-check.json"


def steps_file() -> pathlib.Path:
    return hubconf.STATE / "upgrade.json"


def read_json(f: pathlib.Path) -> dict | None:
    try:
        d = json.loads(f.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write_json(f: pathlib.Path, d: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)   # f is in hubconf.STATE, which no sandbox may write
    fd, tmp = tempfile.mkstemp(prefix=f".{f.name}.", dir=hubconf.STATE)   # its own: the job and doctor write at the same time
    try:
        with os.fdopen(fd, "w") as h:
            h.write(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, f)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


@contextlib.contextmanager
def record_lock():
    """One writer of the record at a time (the job, doctor, upgrade: each reads what is there and writes it on). Without a
    lock file (a full disk) it goes on."""
    fd = None
    try:
        fd = os.open(hubconf.STATE / "upgrade-check.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        pass
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def record_check(remote: str, newest_tag: str | None, error: str | None) -> dict:
    with record_lock():
        prev = (read_json(check_file()) or {}).get("update")
        d = {"checked": now().isoformat(timespec="seconds"), "remote": remote, "newest": newest_tag, "error": error}
        # a lookup that failed says nothing about what is out: what was known stays
        up = prev if error else update_of(newest_tag, prev)
        if up:
            d["update"] = up
        try:
            write_json(check_file(), d)
        except OSError:   # a full disk: what was found is still the answer
            print(_("the record of the check for newer releases can't be written"), file=sys.stderr)
    return d


def record_security(up: dict) -> None:
    """What the fetch found, into the record as it is now: only where it is still about the same two releases."""
    with record_lock():
        rec = read_json(check_file())
        cur = rec.get("update") if rec else None
        if isinstance(cur, dict) and all(cur.get(k) == up[k] for k in ("current", "latest", "head")):
            cur["security"] = up["security"]
            with contextlib.suppress(OSError):
                write_json(check_file(), rec)


def record_in_step() -> None:
    """The in-step commands have run for this commit (agenthub upgrade after its steps; agenthub init after a fresh
    setup). A development checkout is not recorded: it moves by merges, and doctor would nag after every one."""
    if not is_checkout() or local_mode()[0] == "dev":
        return
    write_json(steps_file(), {"commit": sha("HEAD"), "version": describe(), "ref": here_ref(),
                              "at": now().isoformat(timespec="seconds")})
    with record_lock():
        rec = read_json(check_file())
        if rec:   # what is still out, seen from where the checkout is now
            prev = rec.pop("update", None)
            up = update_of(rec.get("newest") or (prev.get("latest") if isinstance(prev, dict) else None), prev)
            write_json(check_file(), {**rec, **({"update": up} if up else {})})


def local_mode() -> tuple[str, str]:
    """('release' | 'main' | 'dev', why it is dev) from what is on disk, without the network: the remote's refs are as
    the last fetch left them."""
    if linked_worktree():
        return "dev", _("{root} is a linked worktree of another checkout", root=ROOT)
    b = branch()
    if b and b != "main":
        return "dev", _("HEAD is on the branch {branch}: a development checkout", branch=b)
    remote = remote_name()
    if remote is None:
        return "dev", _("the checkout has no remote to upgrade from")
    n = local_commits("HEAD", remote)
    if n:
        return "dev", _("{n} {n:commit|commits} here {n:is|are} on no branch of {remote} and no release tag: a development "
                        "checkout", n=n, remote=remote)
    return ("main" if b == "main" else "release"), ""


# ---------- a newer release (docs/install.md, "The daily look for a new release") ----------

def checks_on() -> bool:
    return hubconf.get("updates.check") is not False


def security_in(low: str, high: str, git_dir: pathlib.Path | None = None) -> bool | None:
    """Whether a version after `low` up to `high` has a Security heading in the changelog at the tag `high` (of git_dir:
    the throwaway repo's; else the checkout's own). None: it isn't there, can't be read, or is over CHANGELOG_MAX."""
    where = ["--git-dir", str(git_dir)] if git_dir else []
    spec = f"refs/tags/{high}:CHANGELOG.md"
    r = git(*where, "cat-file", "-s", spec, env=quiet_env())
    if r.returncode != 0 or not r.stdout.strip().isdigit() or int(r.stdout) > CHANGELOG_MAX:
        return None
    r = git(*where, "cat-file", "blob", spec, env=quiet_env())
    return any(SECURITY.search(s) for _t, s in between(r.stdout, low, high)) if r.returncode == 0 else None


def update_of(latest: str | None, prev) -> dict | None:
    """The record's `update` when `latest` is newer than the release this checkout is at and it follows releases, else
    None. What `prev` found out stays while the checkout is at the same release: about the same newer one, and that there
    is a security fix also for one after it (it is in that one too)."""
    if not plain(latest) or local_mode()[0] != "release":
        return None
    current = newest((out("tag", "--merged", "HEAD", "--list", "v[0-9]*") or "").split())
    if not current or version_key(latest) <= version_key(current):
        return None
    prev = prev if isinstance(prev, dict) and plain(prev.get("latest")) else {}
    same = prev.get("latest") == latest
    known = prev.get("security") if prev.get("current") == current and (
        same or prev.get("security") is True and version_key(prev["latest"]) < version_key(latest)) else None
    return {"current": current, "latest": latest,
            "since": same and prev.get("since") or now().isoformat(timespec="seconds"),
            "security": known if isinstance(known, bool) else security_in(current, latest), "head": sha("HEAD")}


def pending(rec: dict | None = None) -> dict | None:
    """{current, latest, security, since} of the release this machine could upgrade to, or None: from the record alone,
    and only while HEAD is where the check saw it (a detached HEAD's file holds the commit). security None: not known yet."""
    up = (rec or read_json(check_file()) or {}).get("update") if checks_on() else None
    if not isinstance(up, dict) or not plain(up.get("current")) or not plain(up.get("latest")) \
            or version_key(up["latest"]) <= version_key(up["current"]):
        return None
    try:
        head = (ROOT / ".git" / "HEAD").read_text().strip()
    except (OSError, ValueError):
        return None
    if not head or head != up.get("head"):
        return None
    return {"current": up["current"], "latest": up["latest"], "since": str(up.get("since") or ""),
            "security": up["security"] if isinstance(up.get("security"), bool) else None}


def public(up: dict) -> dict:
    """What a client gets of pending() (glance.v1's and session-surface.v1's `update`): security only when it is known."""
    return {"current": up["current"], "latest": up["latest"], "security": up["security"] is True}


def line(up: dict) -> str:
    """The one line for the owner (glance, the status page, a session's pane, the alert)."""
    if up["security"]:
        return _("Security update: brunna {latest} is out (this machine has {current}). Run brunna upgrade yourself, in a "
                 "terminal", latest=up["latest"], current=up["current"])
    return _("brunna {latest} is out (this machine has {current}). Run brunna upgrade yourself, in a terminal",
             latest=up["latest"], current=up["current"])


def told_file() -> pathlib.Path:
    return hubconf.STATE / "release-told.json"


def claim(latest: str, security: bool) -> bool:
    """Whether a session may be told of this release now: the marker is written before anything is said, under a lock,
    so of all sessions starting at once one says it. That it carries a security fix is said even when the release alone was
    already. A marker that can't be read or written: nobody says it."""
    try:
        fd = os.open(told_file(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            told = json.loads(os.read(fd, 4096) or b"{}")
            if told.get("release") == latest and (told.get("security") or not security) and datetime.timedelta(0) <= \
                    now() - datetime.datetime.fromisoformat(told["at"]) < TOLD_EVERY:
                return False
        except (ValueError, KeyError, TypeError, AttributeError):
            pass   # a marker that says nothing readable is written again
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"release": latest, "security": security, "at": now().isoformat(timespec="seconds")}).encode(), 0)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def session_line() -> str | None:
    """What one session is told (hub-event at its start, the MCP server's inbox), at most once per release per
    TOLD_EVERY for all of them together, once it is known whether it carries a security fix. Words only: the user
    upgrades, never the agent."""
    up = pending()
    if not up or up["security"] is None or not claim(up["latest"], up["security"]):
        return None
    if up["security"]:
        return _("brunna {latest} is out with a security fix (this machine has {current}). Tell the user; they run "
                 "`brunna upgrade` themselves. Don't run it.", latest=up["latest"], current=up["current"])
    return _("brunna {latest} is out (this machine has {current}). Tell the user; they run `brunna upgrade` themselves. "
             "Don't run it.", latest=up["latest"], current=up["current"])


ALERT_KEY = "release-"


def settle_alert(rec: dict | None = None) -> None:
    """The owner's one notice of a security release (alert.py, key release-<tag>): sent once, never repeated daily,
    closed once this machine has it, a newer one replaced it, or the check is off. Left as it is while it isn't known
    whether the newer release carries a security fix. rec: the record just made (the file may not take it)."""
    import alert
    up = pending(rec)
    if up and up["security"] is None:
        return
    key = ALERT_KEY + up["latest"] if up and up["security"] else None
    for k in sorted({*alert.open_keys(ALERT_KEY), *(k for k in alert.failures() if k.startswith(ALERT_KEY))} - {key}):
        latest = k.removeprefix(ALERT_KEY)
        alert.once(k, [], _("the notice about brunna {latest} is closed: this machine is past it, or a newer release "
                            "replaced it", latest=latest) if checks_on() else
                   _("the notice about brunna {latest} is closed: the check for newer releases is off (updates.check)",
                     latest=latest))
    if key and key not in alert.open_keys(ALERT_KEY):
        alert.once(key, [line(up)], "")


def reason(error) -> str:
    """Why a lookup failed, from the record: one of these, never a text of the remote's (or of an older record's)."""
    if error == "timeout":
        return _("no answer within {n} seconds", n=LS_REMOTE_TIMEOUT)
    if isinstance(error, str) and re.fullmatch(r"git exit -?[0-9]{1,4}", error):
        return error
    return _("no answer")


def release_check() -> int:
    """The daily job. Exit 0 whatever the remote answers: an offline machine is nothing to alert about (doctor says it)."""
    rec = None
    if checks_on() and is_checkout() and local_mode()[0] == "release":
        remote = remote_name()
        rec = record_check(remote, *ls_remote_newest(remote))
        up = rec.get("update")
        if rec["error"]:
            print(_("could not check for newer releases: {error}", error=reason(rec["error"])))
        elif up and up["security"] is None:
            up["security"] = fetched_security(remote, up)
            record_security(up)
    settle_alert(rec)
    return 0


# ---------- what changes ----------

def changelog(rev: str) -> str:
    return out("show", f"{rev}:CHANGELOG.md") or ""


def sections(text: str) -> list[tuple[str, str]]:
    """(tag, the section's text) of each `## vX.Y.Z …` section, in file order."""
    found: list[tuple[str, list[str]]] = []
    cur = None
    for line in text.splitlines():
        m = SECTION.match(line)
        if m:
            cur = (m[1], [line])
            found.append(cur)
        elif line.startswith("## "):
            cur = None
        elif cur:
            cur[1].append(line)
    return [(tag, "\n".join(lines).rstrip()) for tag, lines in found]


def between(text: str, low: str | None, high: str | None) -> list[tuple[str, str]]:
    """The sections after release `low` up to and including `high` (None: no bound)."""
    lo, hi = version_key(low), version_key(high)
    return [(t, s) for t, s in sections(text) if version_key(t)
            and (lo is None or version_key(t) > lo) and (hi is None or version_key(t) <= hi)]


def bullets(section: str) -> list[str]:
    """The `- …` entries of a section, continuation lines joined."""
    items: list[str] = []
    open_ = False
    for line in section.splitlines():
        if line.startswith("- "):
            items.append(line[2:].strip())
            open_ = True
        elif open_ and line.strip() and not line.startswith("#"):
            items[-1] += " " + line.strip()
        else:
            open_ = False
    return items


def unreleased(tip: str, *exclude: str) -> list[tuple[str, bool]]:
    """(title, breaking) of the commits in tip but not in exclude that a changelog would list."""
    r = git("log", "--format=%s%x1f%b%x1e", tip, "--not", *exclude)
    titles = []
    for rec in (r.stdout.split("\x1e") if r.returncode == 0 else []):
        title, _sep, body = rec.strip().partition("\x1f")
        breaking = bool(BREAKING_TITLE.match(title)) or "BREAKING CHANGE:" in body
        if title and (SHOWN_TYPES.match(title) or breaking):
            titles.append((title, breaking))
    return titles


def changes(target: str) -> tuple[bool, list[tuple[str, str]], list[tuple[str, bool]]]:
    """(forward, changelog sections, unreleased titles) between HEAD and target: forward lists what arrives (from the
    target's CHANGELOG.md), back what is undone (from this checkout's)."""
    cur_rel, tgt_rel = nearest_release("HEAD"), nearest_release(target)
    forward = is_ancestor("HEAD", target) or (not is_ancestor(target, "HEAD")
                                               and (version_key(tgt_rel) or ()) >= (version_key(cur_rel) or ()))
    if forward:
        secs = between(changelog(target), cur_rel, tgt_rel)
        titles = unreleased(target, "HEAD", *([tgt_rel] if tgt_rel else []))
    else:
        secs = between(changelog("HEAD"), tgt_rel, cur_rel)
        titles = unreleased("HEAD", target, *([cur_rel] if cur_rel else []))
    return forward, secs, titles


def show_changes(current: str, label: str, target: str) -> None:
    forward, secs, titles = changes(target)
    print(_("brunna {current} → {target}", current=current, target=label))
    breaking = [(t, b) for t, s in secs for b in bullets(s) if "**breaking:**" in b] + \
               [(_("not released yet"), t) for t, br in titles if br]
    if breaking:
        print("\n" + (_("⚠️ Breaking changes (read these first):") if forward else
                      _("⚠️ Breaking changes this undoes (read these first):")))
        for where, text in breaking:
            print(f"  {where}: {text}")
    if not forward:
        print("\n" + _("Going back undoes these changes. Data a newer version wrote in a newer contract version may not "
                       "be readable by the older code (CHANGELOG.md marks such changes as breaking)."))
    for _tag, text in secs:
        print("\n" + text)
    if titles:
        print("\n" + (_("Not released yet (on {target}):", target=label) if forward else
                      _("Not released yet, undone too:")))
        for t, br in titles:
            print(f"- {t}" + ("  ⚠️" if br else ""))
    if not secs and not titles:
        print("\n" + _("(no changelog entries between these two)"))


# ---------- the steps ----------

def py_step(*argv) -> list[str]:
    return [sys.executable, *map(str, argv)]


def own_wrapper(path) -> str | None:
    """Whether this file is an install-cli wrapper and nothing more, so brunna's to replace or remove: the hub it runs ("" when
    it is cut off before that), None for anything else (a link too, whatever it points to)."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        with open(path, "rb") as f:
            text = f.read(4096).decode()
    except (OSError, UnicodeDecodeError):
        return None
    m = WRAPPER.fullmatch(text)
    return (m.group(2) or "") if m and (m.group(2) or any(x in m.group(1) for x in WRAPPER_MARKS)) else None


def cli_wrapper() -> pathlib.Path | None:
    """The CLI on PATH, under either name (docs/design/rename-brunna.md), when it is install-cli's wrapper (never
    someone else's program of that name)."""
    import hubconf
    # not the platform's own folder that services and scheduled commands have last on PATH (hubconf.cli_folder)
    shared = os.path.realpath(hubconf.cli_folder())
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                           if d and os.path.realpath(d) != shared)
    for name in names.all("cli"):
        w = shutil.which(name, path=path)
        if not w:
            continue
        try:
            with open(w, "rb") as f:
                head = f.read(1024).decode(errors="ignore")
        except OSError:
            continue
        # the mark, or the wrapper's exec line itself: a wrapper written before the mark was in English (or in another
        # interface language) has only the line
        if any(m in head for m in WRAPPER_MARKS) or WRAPPER_EXEC.search(head):
            return pathlib.Path(w)
    return None


def step_list(full: bool) -> list[tuple[str, list[str]]]:
    """full: the target has this command (v0.2.0 on); v0.1.0's files have no init --scheduler-only, no user-settings and
    no config migrate."""
    b = ROOT / "bin"
    steps = [("registry", py_step(b / "registry.py", "check"))]
    if full:
        steps.append(("config migrate", py_step(b / "agenthub", "config", "migrate")))
    steps.append(("config apply", py_step(b / "agenthub", "config", "apply")))
    if full:
        steps.append(("scheduler", py_step(b / "agenthub", "init", "--scheduler-only")))
    steps.append(("land", py_step(b / "agenthub", "land")))
    if full:
        steps.append(("user settings", py_step(b / "agenthub", "user-settings", "--update")))
    w = cli_wrapper()
    if w and own_wrapper(w) is not None:   # one the user made theirs (a link, more lines in it) install-cli leaves
        steps.append(("install-cli", py_step(b / "agenthub", "install-cli", "--dir", w.parent)))
    return steps


# the oldest release this one can go back to: everything older can't read what the rename to brunna writes (job state
# under brunna/…, brunna.json, the new labels, letters to brunna; docs/design/rename-brunna.md). v0.6.0 reads the
# spellings but not letters addressed to the new name; v0.6.1 does
OLDEST_BACK = "v0.6.1"


def too_old(ref: str) -> bool:
    """A release before OLDEST_BACK, or a commit that comes before it (in a repo that has OLDEST_BACK)."""
    if not sha(OLDEST_BACK):
        return False
    if version_key(ref):
        return version_key(ref) < version_key(OLDEST_BACK)
    c = sha(ref)
    return bool(c) and c != sha(OLDEST_BACK) and git("merge-base", "--is-ancestor", c, OLDEST_BACK).returncode == 0


def go_back(ref: str) -> str:
    """How to go back to ref; one older than OLDEST_BACK (an upgrade that jumped from there) goes back to that."""
    if ref != "main" and too_old(ref):
        ref = OLDEST_BACK
    cli = names.current("cli")   # a new install has only this one
    return f"{cli} upgrade --main" if ref == "main" else f"{cli} upgrade --to {ref}"


def here_ref() -> str:
    """What `--from` / going back names for where HEAD is: main, a release tag, or the commit."""
    if branch() == "main":
        return "main"
    return out("describe", "--tags", "--exact-match", "--match", "v[0-9]*", "HEAD") or sha("HEAD") or "HEAD"


def failed(name: str, rc: int, back: str | None, manual: bool = False) -> int:
    print("\n" + _("✗ step {name} failed (exit {rc}); the steps after it did not run. The checkout is at {version} now.",
                   name=name, rc=rc, version=describe()), file=sys.stderr)
    if back is None:   # nothing moved, and this machine has no record of an earlier version in step
        print(_("  Fix what it says, then run brunna upgrade again (it reruns the steps)"), file=sys.stderr)
    elif manual:   # the target has no upgrade command: back by hand first
        how = f"git -C {ROOT} checkout main" if back == "main" else f"git -C {ROOT} checkout --detach {back}"
        print(_("  Fix what it says (this release has no brunna upgrade to rerun the steps), or go back: {checkout}, "
                "then {upgrade}", checkout=how, upgrade=go_back(back)), file=sys.stderr)
    else:
        print(_("  Fix what it says, then run brunna upgrade again (it reruns the steps), or go back: {how}",
                how=go_back(back)), file=sys.stderr)
    return FAILED


def run_steps(back: str | None, full: bool = True, manual_back: bool = False) -> int:
    """The in-step commands from the files now on disk; the first failure stops the rest."""
    for name, argv in step_list(full):
        print(f"\n→ {name}", flush=True)
        rc = subprocess.run(argv, cwd=ROOT).returncode
        if rc != 0:
            return failed(name, rc, back, manual_back)
    record_in_step()
    try:
        settle_alert()
    except ImportError:   # a tree without alert.py
        pass
    except Exception as e:   # the upgrade is done; the daily check settles it
        print(f"release alert: {e!r}", file=sys.stderr)
    if full and (ROOT / "bin" / "update-runtimes.py").exists():
        # a checked copy of each coding agent in the store (docs/design/runtime-versions.md section 3): a failure here
        # (offline, no npm, no token for the checks) is for doctor to show, never a halted upgrade
        print("\n→ runtimes", flush=True)
        subprocess.run(py_step(ROOT / "bin" / "update-runtimes.py", "--first"), cwd=ROOT)
    print("\n→ doctor", flush=True)
    rc = subprocess.run(py_step(ROOT / "bin" / "agenthub", "doctor"), cwd=ROOT).returncode
    print("\n" + _("✓ brunna is at {version}", version=describe())
          + ("" if rc == 0 else _(" (doctor found problems, above; the upgrade itself is done)")))
    return 0


# ---------- the command ----------

def refuse(why: str, what: str) -> int:
    print(_("brunna upgrade: refused, nothing changed: {why}", why=why), file=sys.stderr)
    print("  " + what, file=sys.stderr)
    return REFUSED


def scheduler_lock():
    """Take the scheduler's lock without waiting (the tick holds it for its whole run): the fd, or None while a tick
    runs."""
    f = hubconf.STATE / "scheduler.lock"
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(_("not confirmed: there is no terminal to ask in; run it again with --yes to go ahead (nothing changed)"),
              file=sys.stderr)
        return False
    try:
        return input(_("{question} [y/N] ", question=question)).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def parse(argv: list[str]) -> dict | None:
    a = {"to": None, "main": False, "check": False, "yes": False, "steps": False, "from": None}
    it = iter(argv)
    for x in it:
        if x in ("--to", "--from"):
            a[x[2:]] = next(it, None)
            if not a[x[2:]]:
                return None
        elif x in ("--main", "--check", "--yes", "--steps"):
            a[x[2:]] = True
        else:
            return None
    if a["to"] and a["main"] or a["steps"] and not a["from"]:
        return None
    return a


def main(argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        print(_(USAGE_TEXT), end="")
        return 0
    if argv == ["--release-check"]:   # the job brunna/release-check (see the header)
        return release_check()
    a = parse(argv)
    if a is None:
        print(_(USAGE_TEXT), end="", file=sys.stderr)
        return USAGE
    if a["steps"]:   # the hand-over from the version that moved the checkout (see the header)
        return run_steps(a["from"])
    if not is_checkout():
        print(_("brunna upgrade: {root} is not a git checkout, so there is nothing to upgrade from (install by git "
                "clone; see README.md)", root=ROOT), file=sys.stderr)
        return FAILED

    # 1. refuse an unsafe tree (what can be known before the fetch)
    if linked_worktree():
        return refuse(_("{root} is a linked worktree of another checkout", root=ROOT),
                      _("run it in the main checkout, or update this worktree with git yourself"))
    b = branch()
    if b and b != "main":
        return refuse(_("HEAD is on the branch {branch}: a development checkout", branch=b),
                      _("update it with git yourself; brunna upgrade moves only a checkout at a release or on main"))
    if out("status", "--porcelain", "--untracked-files=no"):
        return refuse(_("tracked files have uncommitted changes"),
                      _("commit them on a branch, or drop them (git -C {root} stash), then run it again", root=ROOT))
    remote = remote_name()
    if remote is None:
        return refuse(_("the checkout has no remote to upgrade from"),
                      _("add the repo you installed from: git -C {root} remote add origin <url>", root=ROOT))

    # 2. fetch
    print(_("fetching from {remote} …", remote=remote), flush=True)
    r = git("fetch", "--quiet", "--tags", remote, f"+refs/heads/main:refs/remotes/{remote}/main")
    if r.returncode != 0:
        print(_("brunna upgrade: the fetch from {remote} failed: {error}", remote=remote,
                error=r.stderr.strip()[-400:]), file=sys.stderr)
        return FAILED
    n = local_commits("HEAD", remote)
    if n:
        return refuse(_("{n} {n:commit|commits} here {n:is|are} on no branch of {remote} and no release tag: a development "
                        "checkout", n=n, remote=remote),
                      _("push them or move them to a branch; brunna upgrade never moves a checkout with work of its own"))
    tags = (out("tag", "--list", "v[0-9]*") or "").split()
    latest = newest(tags)
    record_check(remote, latest, None)

    # the target
    if a["to"]:
        if a["to"] in tags and version_key(a["to"]):
            kind, target, label = "tag", a["to"], a["to"]
        elif sha(a["to"]) and re.fullmatch(r"[0-9a-f]{7,40}", a["to"]):
            kind, target, label = "commit", sha(a["to"]), a["to"]
        else:
            print(_("brunna upgrade: {remote} has no release {tag} (releases: {tags})", remote=remote, tag=a["to"],
                    tags=", ".join(sorted((t for t in tags if version_key(t)), key=version_key)) or "-"),
                  file=sys.stderr)
            return USAGE
    elif a["main"] or b == "main":
        kind, target, label = "main", f"{remote}/main", f"{remote}/main"
        if not a["main"]:
            print(_("this checkout is on main, so it follows main (to follow releases instead: brunna upgrade --to "
                    "{tag})", tag=latest or "<tag>"))
    elif latest:
        kind, target, label = "tag", latest, latest
    else:
        print(_("brunna upgrade: {remote} has no release yet; brunna upgrade --main follows its main",
                remote=remote), file=sys.stderr)
        return FAILED
    if a["to"] and too_old(target):
        return refuse(_("{ref} is older than {oldest}, which can't read what this version has written (it was renamed "
                        "to brunna in v0.7)", ref=a["to"], oldest=OLDEST_BACK),
                      _("the oldest release to go back to is {oldest}: brunna upgrade --to {oldest}", oldest=OLDEST_BACK))
    if kind == "main" and b != "main" and git("rev-parse", "--verify", "--quiet", "refs/heads/main").returncode == 0 \
            and local_commits("main", remote):
        return refuse(_("the local branch main has commits that are not on {remote}", remote=remote),
                      _("push them, or move them to another branch, before following main"))

    head, tgt = sha("HEAD"), sha(target)
    same = head == tgt and (b == "main") == (kind == "main")
    rec = read_json(steps_file()) or {}
    pending = rec.get("commit") != head
    current = describe()
    back = here_ref()

    # 3. what changes
    if same:
        print(_("brunna {version} is up to date ({target})", version=current, target=label))
        if pending:
            print(_("the in-step commands have not run for this commit on this machine (a git pull or checkout by "
                    "hand?): brunna upgrade runs them"))
        if a["check"]:
            return 0
        if not pending:
            return 0
        if not confirm(_("run the in-step commands now?"), a["yes"]):
            return UNCONFIRMED
        # going back = to where the steps last passed, if this machine knows (not where HEAD is)
        return bring_in_step(rec.get("ref") if rec.get("commit") else None, moved=False)
    show_changes(current, label, target)
    if a["check"]:
        print("\n" + _("brunna upgrade{opt} installs it", opt=f" --to {a['to']}" if a["to"] else
                       " --main" if a["main"] else ""))
        return AVAILABLE
    if not confirm(_("move this checkout from {current} to {target}?", current=current, target=label), a["yes"]):
        return UNCONFIRMED

    # 4. move the checkout, holding the scheduler's lock so no tick starts halfway
    fd = scheduler_lock()
    if fd is None:
        return refuse(_("a scheduler tick is running right now"), _("run brunna upgrade again in a minute or two"))
    try:
        if kind == "main" and b == "main":
            r = git("merge", "--ff-only", "--quiet", target)
        elif kind == "main":
            r = git("checkout", "--quiet", "-B", "main", target)
            if r.returncode == 0:
                git("branch", "--quiet", f"--set-upstream-to={remote}/main", "main")
        else:
            r = git("checkout", "--quiet", "--detach", tgt)
    finally:
        os.close(fd)
    if r.returncode != 0:
        print(_("brunna upgrade: moving the checkout failed, it stays at {version}: {error}", version=current,
                error=r.stderr.strip()[-400:]), file=sys.stderr)
        return FAILED
    print("\n" + _("✓ the checkout is at {version}", version=describe()))

    # 5. bring everything in step, from the target's code
    return bring_in_step(back, moved=True)


def bring_in_step(back: str | None, moved: bool) -> int:
    """setup-python from the files on disk, then the step list: after a move the target's own (its upgrade.py, a new
    process), with nothing moved this process's (it is the target's code)."""
    mine = ROOT / "bin" / "upgrade.py"
    print("\n→ setup-python", flush=True)
    rc = subprocess.run([str(ROOT / "bin" / "setup-python")], cwd=ROOT).returncode
    if rc != 0:
        return failed("setup-python", rc, back, manual=not mine.exists())
    if not moved:
        return run_steps(back)
    if mine.exists():
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, str(mine), "--steps", "--from", back])
    # a target older than this command (v0.1.0): this version's list, run from its files
    return run_steps(back, full=False, manual_back=True)


# ---------- doctor ----------

QUIET = ("-c", "core.askPass=", "-c", "credential.interactive=false")   # with quiet_env: git asks nobody anything


def quiet_env() -> dict:
    """The environment of a git that may reach the remote: no prompt of any kind (terminal, askpass programs, ssh, the
    credential manager's window), no object fetched on demand, and no other repo named by the caller's environment."""
    env = {k: v for k, v in os.environ.items() if k not in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR", "GIT_NAMESPACE")}
    # set and empty: git takes it as the answer to which askpass program to run, before core.askPass and SSH_ASKPASS
    return env | {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "SSH_ASKPASS_REQUIRE": "never",
                  "GCM_INTERACTIVE": "never", "GIT_NO_LAZY_FETCH": "1"}


def bounded_git(timeout: int, *args) -> tuple[str | None, str]:
    """(None, its output) of one git command that reaches the remote, or (why it failed, ""): "timeout", "git exit N" or
    "no answer" (it couldn't be started). In a session of its own (ssh can't ask on the terminal), killed with it after
    `timeout` seconds. Its output goes to a file, so nothing it leaves running holds the wait; what it says on stderr is
    the remote's words and is dropped. No file it writes passes FETCH_MAX."""
    import resource

    def limit():   # in the child
        hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
        most = FETCH_MAX if hard == resource.RLIM_INFINITY else min(FETCH_MAX, hard)
        resource.setrlimit(resource.RLIMIT_FSIZE, (most, most))
    try:
        env = quiet_env()
        if not env.get("GIT_SSH_COMMAND") and not env.get("GIT_SSH") and not out("config", "--get", "core.sshCommand"):
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
        with tempfile.TemporaryFile(dir=hubconf.private_tmp()) as got:
            p = subprocess.Popen(["git", "-C", str(ROOT), *QUIET, *args], stdout=got, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, env=env, start_new_session=True, preexec_fn=limit)
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                rc = None
            if rc is None:
                with contextlib.suppress(OSError):
                    os.killpg(p.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=2)
                return "timeout", ""
            got.seek(0)
            return (f"git exit {rc}", "") if rc else (None, got.read(OUTPUT_MAX).decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError):
        return "no answer", ""


def ls_remote_newest(remote: str) -> tuple[str | None, str | None]:
    """(newest release tag on the remote, error): one `git ls-remote`, at most LS_REMOTE_TIMEOUT seconds."""
    error, o = bounded_git(LS_REMOTE_TIMEOUT, "ls-remote", "--tags", "--refs", remote)
    if error:
        return None, error
    return newest(x.split("refs/tags/", 1)[1] for x in o.splitlines() if "refs/tags/" in x), None


def fetched_security(remote: str, up: dict) -> bool | None:
    """security_in of the newer release, its tag alone and shallow, in a throwaway bare repo: the hub's repo gets no
    object, ref or FETCH_HEAD, however this ends (a killed run's folder goes with prune_private_tmp). The hub's own config
    is included for how to reach the remote (its URL, ssh command, credentials), with its hooks off. None: it failed."""
    tmp = None
    try:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="release-check-", dir=hubconf.private_tmp()))
        repo = tmp / "repo.git"
        if git("init", "--quiet", "--bare", "--template=", str(repo), env=quiet_env()).returncode != 0 or \
                git("config", "--file", str(repo / "config"), "include.path", str(ROOT / ".git" / "config")).returncode != 0:
            return None
        tag = f"refs/tags/{up['latest']}"
        error, _o = bounded_git(FETCH_TIMEOUT, "--git-dir", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
                                "-c", "maintenance.auto=false", "-c", "fetch.unpackLimit=1", "fetch", "--quiet", "--depth=1",
                                "--no-tags", "--no-filter", "--no-recurse-submodules", "--refmap=", remote, f"+{tag}:{tag}")
        return None if error else security_in(up["current"], up["latest"], repo)
    except OSError:
        return None
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def doctor_lines() -> list[tuple[str, str, str]]:
    """(ok|warn, what, how to fix) for agenthub doctor: the release, a newer one when known, and a checkout that moved
    without its in-step commands. Network only when the record is older than CHECK_EXPIRY (see the header), never with updates.check
    off."""
    if not is_checkout():
        return []
    version, rel, remote = describe(), nearest_release(), remote_name()
    rec = read_json(check_file())
    try:
        age = now() - datetime.datetime.fromisoformat(rec["checked"]) if rec else None
    except (KeyError, TypeError, ValueError):
        age = None
    if remote and checks_on() and (age is None or age > CHECK_EXPIRY or age < datetime.timedelta(0) or rec.get("remote") != remote):
        rec = record_check(remote, *ls_remote_newest(remote))
    lines = []
    latest = (rec or {}).get("newest")
    latest = latest if plain(latest) else None
    when = (rec or {}).get("checked", "")[:16].replace("T", " ")
    if not remote:
        lines.append(("ok", _("brunna {version} (no remote: not checked for newer releases)", version=version), ""))
    elif not checks_on():
        lines.append(("ok", _("brunna {version} (the check for newer releases is off: updates.check in {settings})",
                              version=version), ""))
    elif latest and (version_key(rel) is None or version_key(latest) > version_key(rel)):
        lines.append(("warn", _("brunna {latest} is out; this checkout is at {version} (checked {when})",
                                latest=latest, version=version, when=when),
                      _("brunna upgrade --check shows what changes; brunna upgrade installs it")))
    elif latest:
        lines.append(("ok", _("brunna {version}; the newest release is {latest} (checked {when})", version=version,
                              latest=latest, when=when), ""))
    else:
        lines.append(("ok", _("brunna {version} (could not check for newer releases at {when}: {error}; doctor tries "
                              "again after a day)", version=version, when=when,
                              error=reason(rec["error"]) if (rec or {}).get("error") else _("no release tags")), ""))
    done = read_json(steps_file())
    if done and done.get("commit") and done["commit"] != sha("HEAD") and local_mode()[0] != "dev":
        lines.append(("warn", _("the checkout moved to {version} after brunna upgrade last brought this machine in step "
                                "(at {old}): the Python packages, services, scheduler and hooks may be stale",
                                version=version, old=done.get("version") or done["commit"][:12]),
                      f"{names.current('cli')} upgrade"))
    return lines


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
