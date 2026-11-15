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
    os.mkdir(r / project, 0o700)
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
    if not w.get("start"):
        return True
    import runtime_ports
    return runtime_ports.process_start(pid) == w["start"]


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


def take_in(project: str, letter=None, log=None, confirm_session=None) -> list[str]:
    """One project's request folder → its queue. → the ids handled. letter(project, text, key): an info letter to the
    project; confirm_session(project, session) → the session when hubbus saw it there, else None."""
    log = log or (lambda m: None)
    ensure_folder(project, log)
    r = root()
    names = nofollow.entries(r, project) or []
    _old_results(project, names)
    reqs = []
    for n in names:
        if not n.endswith(REQUEST) or n.startswith("."):
            continue
        st = nofollow.lstat(r, f"{project}/{n}")
        reqs.append((st.st_mtime if st else 0, n))
    done = []
    for _m, n in sorted(reqs):
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
            if ID.fullmatch(rid) and not any(t.get("id") == rid for t in taken(project)):
                _taken_line(project, {"id": rid, "outcome": "malformed"})
                write_result(project, result(project, rid, "refused", _("the request was malformed: {why}", why=why),
                                             "project", "request"), log)
            elif not ID.fullmatch(rid) and letter:
                letter(project, _("A file in {project}'s push request folder was not a push request and has been deleted "
                                  "({name}): {why}", project=project, name=n[:80], why=why), f"push-malformed-{project}")
            nofollow.remove(r, rel)
            continue
        if any(t.get("id") == rid for t in taken(project)):
            nofollow.remove(r, rel)   # taken in before: the earlier result stands
            continue
        if "session" in fields and confirm_session:
            fields["session"] = confirm_session(project, fields["session"])
        _queue(project, rid, fields, log)
        nofollow.remove(r, rel)
        done.append(rid)
    return done


def _queue(project: str, rid: str, fields: dict, log) -> None:
    lim = limits()
    with queue_locked(project):
        today = datetime.date.today().isoformat()
        n_today = sum(1 for t in taken(project) if str(t.get("at", "")).startswith(today))
        _taken_line(project, {"id": rid, "branch": fields["branch"], "sha": fields["sha"]})
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


def _old_results(project: str, names) -> None:
    cut = time.time() - RESULT_KEEP_S
    for n in names:
        if n.endswith(RESULT):
            st = nofollow.lstat(root(), f"{project}/{n}")
            if st and st.st_mtime < cut:
                nofollow.remove(root(), f"{project}/{n}")


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
                        "--type", "info", "--text", text], capture_output=True, text=True, cwd=broker(project))
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


def close_todos(project: str, landed=None, log=None) -> list[str]:
    """After a push of the project landed: every open to-do closes, except a "branch not allowed" one, which closes only
    when what landed contains its sha (landed(sha) → bool)."""
    f = broker(project) / "todos.json"
    todos = _read_json(f, {})
    closed = []
    for cause, t in todos.items():
        if not t.get("sent") or t.get("closed"):
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
    """A request's end: the result file, the letter, the request and worker records gone, served now."""
    log = log or (lambda m: None)
    res = {**res, "branch": res.get("branch") or req.get("branch")}
    try:
        write_result(project, res, log)
    except OSError as e:
        log(_("{project}: the result of {id} could not be written: {e}", project=project, id=res["id"], e=e))
    try:
        (letter or project_letter)(project, letter_text(res), f"push-{res['id']}")
    except Exception as e:   # noqa: BLE001 (a letter that can't go out changes nothing else)
        log(_("{project}: the letter about {id} could not be sent: {e!r}", project=project, id=res["id"], e=e))
    with queue_locked(project):
        (broker(project) / "requests" / f"{req['id']}.json").unlink(missing_ok=True)
        w = worker(project)
        if not w or w.get("id") == req["id"]:
            (broker(project) / "worker.json").unlink(missing_ok=True)
        _write_json(broker(project) / "served.json", {"at": now_iso(), "id": req["id"]})
