"""A scheduled job that had nothing to run on: the feature is off on this machine (no model set up), said with the
reason instead of passing for a success (docs/design/tool-agnostic.md principle 3).

The job exits 0 all the same: for the scheduler it is a completed run (not retried, not failing, and the jobs with
after: on it go on). What differs is what people see, and that comes from here:
  job_off.off(reason)    called by the job: prints the reason (so it is in the job's own log) and records it in
                         .state/jobs-off/<job>.json
  job_off.clear()        called by the job when it does run
  job_off.last(name)     the status page's: {"reason", "at"} when the job's last run was off, else None
Which job: the scheduled run's own name (event_jobs.JOB_VAR); run by hand without one, nothing is recorded. brunna run
asks llm_ready.job before it starts one.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import event_jobs  # noqa: E402
import hubconf  # noqa: E402
import names  # noqa: E402


def _file(name: str) -> pathlib.Path:
    return hubconf.STATE / "jobs-off" / f"{name.replace('/', '__')}.json"


def current() -> str | None:
    return names.env(event_jobs.JOB_VAR) or None


def off(reason: str) -> None:
    if not sys.stdout.isatty():   # on a terminal the job's own log() has printed it
        print(reason, flush=True)
    if not (name := current()):
        return
    f = _file(name)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"reason": reason, "at": datetime.datetime.now().isoformat(timespec="seconds")},
                              ensure_ascii=False) + "\n")
    tmp.replace(f)


def clear() -> None:
    if (name := current()):
        _file(name).unlink(missing_ok=True)


def last(name: str) -> dict | None:
    try:
        d = json.loads(_file(name).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("reason") else None
