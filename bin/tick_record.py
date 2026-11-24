"""Which of the scheduler's ticks completed: .state/scheduler-tick.json (one file, written only by scheduler.py).

The heartbeat says a tick began and is moving; a tick that begins and crashes every time looks alive by it. This record
says how the ticks end:
  started    when the newest tick began
  running    true until it ended by itself (killed, or the machine went off: it stays true)
  completed  when a tick last ran to its end
  unfinished how many ticks in a row began and didn't complete, before the running one
  since      when the first of those began
  error      why the last of them stopped

Nothing here is judged by a tick's length: a tick of hours (several foreground jobs) is one running tick and 0
unfinished. Ticks never overlap (scheduler.lock), so a tick that finds `running` still true knows the one before it died.
Read by bin/scheduler-watchdog.py (its own copy of the rule: it imports nothing of the platform), doctor, the status
page and glance. No record (a version from before it) or one that can't be read says nothing is wrong.

save() is how the scheduler writes every file of its own: a full disk leaves what was there, never a cut file.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib


def save(path: pathlib.Path, text: str) -> None:
    """path's new content, never half written: a file beside it, synced, renamed over it; nothing left behind when it
    fails."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")   # a background run writes some of the same files
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read(path: pathlib.Path) -> dict:
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _count(rec: dict) -> int:
    n = rec.get("unfinished")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def at(rec: dict, key: str) -> datetime.datetime | None:
    """A time in the record, as local time; None when it has none or it can't be read."""
    try:
        return datetime.datetime.fromtimestamp(datetime.datetime.fromisoformat(rec[key]).timestamp())
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def _iso(now: datetime.datetime) -> str:
    """With its UTC offset: daylight saving makes a local reading alone an hour wrong."""
    return now.astimezone().isoformat(timespec="seconds")


def _ended_badly(rec: dict, error: str) -> dict:
    since = rec.get("since") if _count(rec) else rec.get("started")
    return {**rec, "unfinished": _count(rec) + 1, "since": since, "error": error[:300], "running": False}


def begin(path: pathlib.Path, now: datetime.datetime) -> None:
    rec = read(path)
    if rec.get("running"):
        rec = _ended_badly(rec, "stopped midway (killed, or the machine went off)")
    save(path, json.dumps({**rec, "started": _iso(now), "running": True}, indent=1) + "\n")


def failed(path: pathlib.Path, error: str) -> None:
    save(path, json.dumps(_ended_badly(read(path), error), indent=1) + "\n")


def complete(path: pathlib.Path, now: datetime.datetime) -> None:
    save(path, json.dumps({"started": read(path).get("started"), "running": False, "unfinished": 0,
                           "completed": _iso(now)}, indent=1) + "\n")


def not_completing(rec: dict) -> dict | None:
    """{n, since, completed, error} when the ticks don't complete; None while they do. One that died before the tick now
    running (a reboot) is not it yet."""
    n = _count(rec)
    if n == 0 or (n == 1 and rec.get("running")):
        return None
    return {"n": n, "since": at(rec, "since"), "completed": at(rec, "completed"), "error": str(rec.get("error") or "")}
