#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The platform push's request queue (docs/design/platform-push.md section 3, r1 and r2): what a sandboxed project's
session asks for in `<push_dir>/<project>/`, taken in by the mailbox dispatch, served by a detached worker
(`git_remote.py serve <project>`), answered by a result file there and an info letter.

The session request folder (r2 K1): the platform makes `<push_dir>` (0700) and each sandboxed project's folder in it
(ensure_folder: render-project-settings before it renders any session settings, and take-in every minute); one that is
missing, a link or not a folder is made again, and that is logged. Every read, removal and write names the base
`<push_dir>` and the relative `<project>/<name>` (nofollow), so a link the session puts where its folder was is never
followed: the session's sandbox can write its folder's own entry.

Take-in (hubbus dispatch, as the owner, take_in): only `<id>.request.json` files are read (nothing else there is touched),
oldest first by mtime, regular single-link files of at most CAP. The project is the folder's; only branch and sha are
read (session is kept when hubbus confirms it, for the log). An id taken before does nothing. A well-formed request becomes
a fresh platform-made `<state>/git-broker/<project>/requests/<id>.json` (r2 S-f), which no sandbox writes, and the
original is removed. Limits: one queued or running per project, except that a queued one not yet started is replaced by
a newer request for the same branch (r2 S-g; the worker checks the newer sha descends from it); the daily cap,
refusals included. A refusal writes the result file at once; a request whose id can't be read gets a letter.

The worker (tick): each dispatch tick starts a detached worker (double fork and setsid, like run-agent-job.sh) for each
project with a queued request and no live worker, at most git_push.concurrency at once, the project served least
recently first. A worker is live while its recorded pid runs with its recorded start time (r2 S-k); one that died is
started again once, and a request whose second worker died too ends as `failed: worker lost`.

Results and letters: the result file (`nofollow.replace`, never through a link), an info letter to the project, and an
owner to-do only for what the project can't fix (raise_todo: one per cause and project per episode).
"""
from __future__ import annotations

import contextlib
import datetime
import errno
import fcntl
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import nofollow  # noqa: E402
import policy  # noqa: E402

_ = i18n.domain(__file__)

ID = re.compile(r"^m\d{8}-[0-9a-f]{12}$")
SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REQUEST, RESULT = ".request.json", ".result.json"
CAP = 64 << 10
RESULT_KEEP_S = 7 * 86400
CLEAR_RUN = 1000               # entries that aren't requests removed from a folder per tick
TAKEN_KEEP_S = 7 * 86400       # taken.jsonl: ids seen (a request file older than this is long answered)
WORKER_MAX_S = 3 * 1800 + 900  # the lock (git_remote.TIMEOUT_S), the bundle, the push, and a margin
ATTEMPTS = 2                   # workers started for one request before it ends as worker lost
DEFAULTS = {"requests_per_day": 20, "concurrency": 2, "max_commits": 500, "max_total_mb": 200, "scan_timeout_s": 300,
            "wait_s": 90}
STATES = ("pushed", "nothing", "refused", "failed", "superseded")


def limits() -> dict:
    got = hubconf.get("git_push") or {}
    return {k: int(got.get(k, v)) if isinstance(got.get(k, v), int) else v for k, v in DEFAULTS.items()}


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def root() -> pathlib.Path:
    return hubconf.push_dir()


def broker(project: str) -> pathlib.Path:
    return policy.git_broker_dir(project)


def sandboxed_projects() -> list[str]:
    return sorted(n for n, d in hubconf.projects().items() if n != hubconf.hub_project() and policy.mail_outbox(d))


# ---------- the session request folders (r2 K1) ----------

def ensure_root() -> pathlib.Path:
    r = root()
    st = os.lstat(r) if os.path.lexists(r) else None
    if st is not None and not stat.S_ISDIR(st.st_mode):
        raise OSError(_("{path} is not a folder: the platform's push request root must be one", path=r))
    if st is None:
        r.mkdir(mode=0o700, parents=True)
    os.chmod(r, 0o700)
    return r


def ensure_folder(project: str, log=None) -> bool:
    """<push_dir>/<project> as a plain folder the platform made: one that is missing is made, one that is a link or not a
    folder is removed (a link itself, never what it points to) and made again, which is logged. → whether it was fine."""
    r = ensure_root()
    st = nofollow.lstat(r, project)
    if st is not None and stat.S_ISDIR(st.st_mode):
        return True
    if st is not None:
        nofollow.remove(r, project)
        if log:
            log(_("{project}: its push request folder was not a folder (a link or a file); made again", project=project))
    rootfd = os.open(r, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)   # the entry made by name from its root, no joined path
    try:
        os.mkdir(project, 0o700, dir_fd=rootfd)
    finally:
        os.close(rootfd)
    return st is None


# ---------- the queue ----------

@contextlib.contextmanager
def queue_locked(project: str):
    d = broker(project)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(d / "queue.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def queued(project: str) -> list[dict]:
    d = broker(project) / "requests"
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            raw = nofollow.read(d, f.name, CAP, single=True)
            req = json.loads(raw) if raw else None
        except (OSError, ValueError):
            continue
        if isinstance(req, dict) and req.get("id") == f.stem:
            out.append(req)
    return sorted(out, key=lambda q: (q.get("seq", 0), q["id"]))


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def taken(project: str) -> list[dict]:
    out = []
    try:
        for line in (broker(project) / "taken.jsonl").read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _taken_line(project: str, rec: dict) -> None:
    f = broker(project) / "taken.jsonl"
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(f, "a") as fh:
        fh.write(json.dumps({"at": now_iso(), **rec}, ensure_ascii=False) + "\n")


def worker(project: str) -> dict:
    return _read_json(broker(project) / "worker.json", {})


def alive(w: dict) -> bool:
    """The recorded worker still runs: its pid with its recorded start time (a reused pid never counts)."""
    pid = w.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if worker_age(w) >= WORKER_MAX_S:
        # a worker counts only while one could still be running: a reused pid never keeps a request waiting, and a
        # stuck one is stopped by tick and its slot freed
        return False
    if not w.get("start"):
        return True
    import runtime_ports
    return runtime_ports.process_start(pid) == w["start"]


def worker_age(w: dict) -> float:
    """Seconds since the worker was started (spawned_at, else started_at); unknown counts as too old."""
    try:
        spawned = datetime.datetime.fromisoformat(w.get("spawned_at") or w.get("started_at") or "")
    except ValueError:
        return float("inf")
    return (datetime.datetime.now().astimezone() - spawned).total_seconds()


def stop_stuck(w: dict, log) -> None:
    """A worker past WORKER_MAX_S whose pid still runs with its recorded start: killed (its process group), said."""
    import signal
    import runtime_ports
    pid = w.get("pid")
    if not isinstance(pid, int) or pid <= 0 or worker_age(w) < WORKER_MAX_S:
        return
    if w.get("start") and runtime_ports.process_start(pid) == w["start"]:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        log(_("a push worker ran past its limit and was stopped (pid {pid})", pid=pid))


def parse(raw: bytes, rid: str) -> dict:
    """A request file's fields, or ValueError with why."""
    try:
        req = json.loads(raw)
    except ValueError:
        raise ValueError(_("not JSON")) from None
    if not isinstance(req, dict) or req.get("v") != 1:
        raise ValueError(_("not a push request (an object with v 1)"))
    if req.get("id") != rid:
        raise ValueError(_("its id is not its file's name"))
    b, sha = req.get("branch"), req.get("sha")
    if not isinstance(b, str) or not b or len(b) > 255 or not b.isprintable() or b.startswith("-"):
        raise ValueError(_("branch must be a branch name"))
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError(_("sha must be a full commit id"))
    s = req.get("session")
    return {"branch": b, "sha": sha, **({"session": s} if isinstance(s, str) and len(s) <= 200 else {})}


def result(project: str, rid: str, state: str, reason: str, fix: str | None = None, check: str | None = None,
           **extra) -> dict:
    return {"v": 1, "id": rid, "project": project, "state": state, "reason": reason, "fix": fix, "check": check,
            "at": now_iso(), **extra}


def write_result(project: str, res: dict, log=None) -> None:
    """The result file, replaced, never written through a link (the folder made again first when it isn't one)."""
    ensure_folder(project, log)
    nofollow.replace(root(), f"{project}/{res['id']}{RESULT}", json.dumps(res, ensure_ascii=False).encode())


class Taken:
    """taken.jsonl read once per take-in (review S1): the ids seen, today's count; appended to as requests come in, and
    lines older than TAKEN_KEEP_S dropped once per take-in."""

    def __init__(self, project: str):
        self.project = project
        self.f = broker(project) / "taken.jsonl"
        cut = (datetime.datetime.now().astimezone() - datetime.timedelta(seconds=TAKEN_KEEP_S)).isoformat()
        recs = taken(project)
        keep = [t for t in recs if str(t.get("at", "")) >= cut]
        if len(keep) != len(recs):
            self.f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.f.with_name(f".{self.f.name}.{os.getpid()}.new")
            tmp.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in keep))
            os.replace(tmp, self.f)
        self.ids = {t.get("id") for t in keep}
        today = datetime.date.today().isoformat()
        self.today = sum(1 for t in keep if str(t.get("at", "")).startswith(today))

    def add(self, rec: dict) -> None:
        _taken_line(self.project, rec)
        self.ids.add(rec.get("id"))
        self.today += 1


def _clear(r: pathlib.Path, rel: str) -> None:
    """One entry gone: a folder (empty or not) removed, anything else unlinked; never followed. No path is ever joined
    under the request root (the third re-review's B-1: a joined path follows a link the session swaps in for its
    folder): the project's folder is opened with O_NOFOLLOW from the root, and everything below goes by that handle
    (shutil.rmtree with dir_fd, which is safe against links where the system allows it, and is never used otherwise)."""
    import shutil
    project, _sep, name = rel.partition("/")
    if not name or "/" in name or name in (".", ".."):
        raise OSError(errno.EINVAL, f"not one entry of a project's folder: {rel}")
    rootfd = os.open(r, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=rootfd)
    finally:
        os.close(rootfd)
    try:
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(st.st_mode):
            os.unlink(name, dir_fd=fd)
        elif shutil.rmtree.avoids_symlink_attacks:
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.rmdir(name, dir_fd=fd)   # only an empty one where the system can't remove a tree safely
    finally:
        os.close(fd)


_SAID: set = set()


def _clear_said(r: pathlib.Path, rel: str, log=None) -> bool:
    """_clear, an entry that can't be removed (a folder of mode 000, a race) said once to the owner's log and left:
    one never stops take-in (the fourth review's 1). → whether it went."""
    try:
        _clear(r, rel)
        return True
    except OSError as e:
        key = (rel, e.errno)
        if log and key not in _SAID:
            _SAID.add(key)
            log(_("{entry} in the push request folder can't be removed ({why}); left there", entry=rel,
                  why=e.strerror or e))
        return False


def take_in(project: str, letter=None, log=None, confirm_session=None) -> list[str]:
    """One project's request folder → its queue. → the ids handled. letter(project, text, key): an info letter to the
    project; confirm_session(project, session) → the session when hubbus saw it there, else None. At most the daily cap
    plus one request files are read per take-in; the rest are deleted with one letter (review S1: the dispatch tick holds
    the mail while it runs)."""
    log = log or (lambda m: None)
    ensure_folder(project, log)
    r = root()
    names = nofollow.entries(r, project) or []
    _old_results(project, names, log)
    reqs, cleared = [], 0
    for n in names:
        if not n.endswith(REQUEST) or n.startswith("."):
            # nothing but requests, results and a client's half-written dot-file belongs here: anything else goes (at
            # most CLEAR_RUN a tick), and a dot-file once it is an hour old
            st = nofollow.lstat(r, f"{project}/{n}")
            stale = st is not None and n.startswith(".") and st.st_mtime < time.time() - 3600
            if cleared < CLEAR_RUN and (stale or (not n.startswith(".") and not n.endswith(RESULT))):
                _clear_said(r, f"{project}/{n}", log)
                cleared += 1
            continue
        st = nofollow.lstat(r, f"{project}/{n}")
        if st is None:
            continue
        if not stat.S_ISREG(st.st_mode):   # a folder, a link, a FIFO by a request's name: gone, never read
            _clear_said(r, f"{project}/{n}", log)
            continue
        reqs.append((st.st_mtime, n))
    reqs.sort()
    most = limits()["requests_per_day"] + 1
    if len(reqs) > most:
        for _m, n in reqs[most:]:
            _clear_said(r, f"{project}/{n}", log)
        sent = broker(project) / "flood-letter"
        today = datetime.date.today().isoformat()
        if letter and _read_json(sent, "") != today:   # at most one a day, recorded here
            letter(project, _("{n} push request files in {project}'s folder were over what one day allows and have been "
                              "deleted unread", n=len(reqs) - most, project=project), letter_key(f"flood-{project}"))
            _write_json(sent, today)
        reqs = reqs[:most]
    seen = Taken(project)
    done, misnamed = [], []
    for _m, n in reqs:
        rid = n[:-len(REQUEST)]
        rel = f"{project}/{n}"
        try:
            raw = nofollow.read(r, rel, CAP, single=True)
            if raw is None:
                continue
            if not ID.fullmatch(rid):
                raise ValueError(_("its name is not <id>.request.json with an id of the form m<MMDDhhmm>-<12 hex digits>"))
            fields = parse(raw, rid)
        except (OSError, ValueError) as e:
            why = str(e)[:200]
            if ID.fullmatch(rid) and rid not in seen.ids:
                seen.add({"id": rid, "outcome": "malformed"})
                write_result(project, result(project, rid, "refused", _("the request was malformed: {why}", why=why),
                                             "project", "request"), log)
            elif not ID.fullmatch(rid):
                misnamed.append(n[:80])
            _clear_said(r, rel, log)
            continue
        if rid in seen.ids:
            nofollow.remove(r, rel)   # taken in before: the earlier result stands
            continue
        if "session" in fields and confirm_session:
            fields["session"] = confirm_session(project, fields["session"])
        _queue(project, rid, fields, log, seen)
        nofollow.remove(r, rel)
        done.append(rid)
    if misnamed and letter:   # one letter a tick, however many
        letter(project, _("{n} {n:file|files} in {project}'s push request folder {n:was|were} not a push request and "
                          "{n:has|have} been deleted (e.g. {name})", n=len(misnamed), project=project, name=misnamed[0]),
               letter_key(f"malformed-{project}"))
    return done


def _queue(project: str, rid: str, fields: dict, log, seen: Taken) -> None:
    lim = limits()
    with queue_locked(project):
        n_today = seen.today
        seen.add({"id": rid, "branch": fields["branch"], "sha": fields["sha"]})
        if n_today >= lim["requests_per_day"]:
            write_result(project, result(project, rid, "refused", _("the daily limit of {n} push requests is reached: "
                                                                    "ask again tomorrow", n=lim["requests_per_day"]),
                                         "wait", "limit", branch=fields["branch"]), log)
            return
        cur = queued(project)
        supersedes = None
        if cur:
            q, w = cur[0], worker(project)
            started = w.get("started") == q["id"] and alive(w)
            if not started and q["branch"] == fields["branch"]:
                supersedes = {"id": q["id"], "sha": q["sha"]}
                write_result(project, result(project, q["id"], "superseded",
                                             _("a newer request for {branch} ({id}) carries these commits",
                                               branch=q["branch"], id=rid), "wait", None, by=rid,
                                             branch=q["branch"]), log)
                (broker(project) / "requests" / f"{q['id']}.json").unlink(missing_ok=True)
            else:
                write_result(project, result(project, rid, "refused", _("one push at a time: {id} is pending",
                                                                         id=q["id"]), "wait", "limit",
                                             branch=fields["branch"]), log)
                return
        seq = int(time.time() * 1000)
        _write_json(broker(project) / "requests" / f"{rid}.json",
                    {"v": 1, "id": rid, "project": project, **fields, "taken_at": now_iso(), "seq": seq,
                     **({"supersedes": supersedes} if supersedes else {})})


def _old_results(project: str, names, log=None) -> None:
    """Results older than RESULT_KEEP_S go; anything but a plain file by a result's name goes at once."""
    cut = time.time() - RESULT_KEEP_S
    for n in names:
        if n.endswith(RESULT):
            st = nofollow.lstat(root(), f"{project}/{n}")
            if st and (not stat.S_ISREG(st.st_mode) or st.st_mtime < cut):
                _clear_said(root(), f"{project}/{n}", log)


# ---------- workers ----------

def tick(spawn=None, letter=None, log=None) -> list[str]:
    """Start the workers the queues need (→ the projects started); end a request whose workers died twice."""
    log = log or (lambda m: None)
    spawn = spawn or start_worker
    lim, running, want = limits(), 0, []
    for project in sandboxed_projects():
        reqs = queued(project)
        w = worker(project)
        if w and alive(w):
            running += 1
            continue
        if w:
            stop_stuck(w, log)
        if not reqs:
            if w:
                (broker(project) / "worker.json").unlink(missing_ok=True)
            continue
        req = reqs[0]
        if w.get("id") == req["id"] and int(w.get("attempts") or 0) >= ATTEMPTS:
            finish(project, req, result(project, req["id"], "failed", _("the worker serving it stopped before it was "
                                                                        "done (worker lost): ask again"), "wait",
                                        "worker", branch=req.get("branch")), letter=letter, log=log)
            continue
        want.append((_read_json(broker(project) / "served.json", {}).get("at", ""), project, req, w))
    started = []
    for _at, project, req, w in sorted(want, key=lambda x: (x[0], x[1])):
        if running >= lim["concurrency"]:
            break
        attempts = int(w.get("attempts") or 0) + 1 if w.get("id") == req["id"] else 1
        try:
            pid, start = spawn(project)
        except OSError as e:
            log(_("{project}: the push worker could not be started: {e!r}", project=project, e=e))
            continue
        _write_json(broker(project) / "worker.json", {"pid": pid, "start": start, "id": req["id"], "attempts": attempts,
                                                      "spawned_at": now_iso()})
        running += 1
        started.append(project)
    return started


def start_worker(project: str) -> tuple[int, str | None]:
    """`git_remote.py serve <project>` detached (double fork and setsid: a plain child dies when launchd ends the
    dispatch tick's process group, known-issues.md) → (its pid, its start time)."""
    log = broker(project) / "worker.log"
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    argv = [*hubconf.launch(BIN / "git_remote.py"), "serve", project]
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:   # the child: a new session, then the worker as its child, and out
        try:
            os.close(r)
            os.setsid()
            pid2 = os.fork()
            if pid2:
                os.write(w, str(pid2).encode())
                os._exit(0)
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
            os.closerange(3, 1024)
            os.execv(argv[0], argv)
        finally:
            os._exit(127)
    os.close(w)
    os.waitpid(pid, 0)
    got = b""
    while chunk := os.read(r, 32):
        got += chunk
    os.close(r)
    if not got:
        raise OSError(_("the worker's process did not start"))
    wpid = int(got)
    import runtime_ports
    return wpid, runtime_ports.process_start(wpid)


# ---------- the end of a request: result, record, letter, to-dos ----------

def letter_key(what: str) -> str:
    """A letter's id the platform makes (its time and a random part): never one a project's file name could pick."""
    import secrets
    return f"push-{what}-{int(time.time())}-{secrets.token_hex(4)}"


def letter_text(res: dict) -> str:
    branch = res.get("branch") or "?"
    if res["state"] == "pushed":
        head = _("brunna push {id}: pushed {branch} ({range})", id=res["id"], branch=branch, range=res.get("range") or "")
    elif res["state"] == "nothing":
        head = _("brunna push {id}: nothing to push for {branch}", id=res["id"], branch=branch)
    elif res["state"] == "superseded":
        head = _("brunna push {id}: replaced by a newer request for {branch}", id=res["id"], branch=branch)
    elif res["state"] == "refused":
        head = _("brunna push {id}: {branch} was not pushed (refused)", id=res["id"], branch=branch)
    else:
        head = _("brunna push {id}: {branch} was not pushed (failed)", id=res["id"], branch=branch)
    what = {"project": _("you can fix this in the project, then run brunna push again"),
            "owner": _("only the owner can fix this; they have a to-do"),
            "wait": _("wait, then ask again")}.get(res.get("fix") or "", "")
    return " ".join(x for x in (head + ".", res.get("reason") or "", ("(" + what + ")") if what else "") if x)


def project_letter(project: str, text: str, key: str) -> None:
    """An info letter from the platform to the project (a notice: no handler, wake or round limit)."""
    r = subprocess.run([*hubconf.launch(BIN / "hubbus"), "send", "--from", hubconf.hub_project(), "--to", project,
                        "--type", "info", "--id", key, "--text", text], capture_output=True, text=True, cwd=broker(project))
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).strip()[-300:])


def owner_notify(*args: str) -> None:
    r = subprocess.run([*hubconf.launch(BIN / "hubbus"), "send", "--from", hubconf.hub_project(), "--to",
                        hubconf.owner_address(), *args], capture_output=True, text=True, cwd=hubconf.STATE)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).strip()[-300:])


def todo_key(project: str, cause: str, n: int) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", f"push-{project}-{cause}-{n}")[:128]


def raise_todo(project: str, cause: str, text: str, sha: str | None = None, log=None) -> str | None:
    """This cause's owner to-do for the project, sent once per episode (open until closed) → its key."""
    f = broker(project) / "todos.json"
    todos = _read_json(f, {})
    cur = todos.get(cause) or {}
    if cur.get("sent") and not cur.get("closed"):
        return cur.get("key")
    n = int(cur.get("n") or 0) + 1
    key = todo_key(project, cause, n)
    try:
        owner_notify("--type", "task", "--id", key, "--text", text[:1500], "--context", "push")
    except OSError as e:
        if log:
            log(_("⚠️ the owner to-do {key} could not be sent ({why})", key=key, why=e))
        return None
    todos[cause] = {"n": n, "key": key, "sent": now_iso(), "closed": False, **({"sha": sha} if sha else {})}
    _write_json(f, todos)
    return key


def close_todos(project: str, landed=None, log=None, still=()) -> list[str]:
    """After a push of the project landed: every open to-do closes, except a "branch not allowed" one, which closes only
    when what landed contains its sha (landed(sha) → bool), and the causes in still, which this push didn't settle (a
    visibility that still differs from what setup recorded)."""
    f = broker(project) / "todos.json"
    todos = _read_json(f, {})
    closed = []
    for cause, t in todos.items():
        if not t.get("sent") or t.get("closed") or cause in still:
            continue
        if cause == "branch" and not (t.get("sha") and landed and landed(t["sha"])):
            continue
        try:
            owner_notify("--type", "done", "--task", t["key"], "--text", _("pushed"), "--context", "push")
        except OSError as e:
            if log:
                log(_("⚠️ the owner to-do {key} could not be closed ({why})", key=t["key"], why=e))
            continue
        t["closed"] = True
        closed.append(t["key"])
    _write_json(f, todos)
    return closed


def finish(project: str, req: dict, res: dict, letter=None, log=None) -> None:
    """A request's end: the letter, then the result file (a waiting client that sees the result finds the letter
    sent), the request and worker records gone, served now."""
    log = log or (lambda m: None)
    res = {**res, "branch": res.get("branch") or req.get("branch")}
    try:
        # the request id and when it was taken in: a request id used again later still gets its letter
        (letter or project_letter)(project, letter_text(res), f"push-{res['id']}-{req.get('seq') or int(time.time())}")
    except Exception as e:   # noqa: BLE001 (a letter that can't go out changes nothing else)
        log(_("{project}: the letter about {id} could not be sent: {e!r}", project=project, id=res["id"], e=e))
    try:
        write_result(project, res, log)
    except OSError as e:
        log(_("{project}: the result of {id} could not be written: {e}", project=project, id=res["id"], e=e))
    with queue_locked(project):
        (broker(project) / "requests" / f"{req['id']}.json").unlink(missing_ok=True)
        w = worker(project)
        if not w or w.get("id") == req["id"]:
            (broker(project) / "worker.json").unlink(missing_ok=True)
        _write_json(broker(project) / "served.json", {"at": now_iso(), "id": req["id"]})
