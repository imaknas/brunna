#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The single scheduler: reads schedule.yaml and, when a job is due, runs its command, once a day.
Called by launchd every 15 minutes; adding a schedule only takes editing schedule.yaml, no new plist.

Besides time, each job can add (all optional; without them, daily):
  days_of_week:  [mon, wed]       run only on these days
  days_of_month: [1, 15]          run only on these days of the month
  months:        [1, 4, 7, 10]    run only in these months (with days_of_month for quarterly schedules)
  end_date:      2026-11-01       no runs after this day (this day included); one-off jobs pair it with
                                  months+days_of_month
  max_attempts_per_day: 2         how many times to retry on the same day after a failure; without it, the original
                                  behaviour (retry every tick until it succeeds): expensive LLM jobs
                                  must set it, or one failure burns a run every 15 minutes all day

Interval jobs: no time, every_minutes instead (the date conditions above still apply):
  every_minutes: 60               run again once this long has passed since the last success (the tick is 15 minutes,
                                  so the actual interval lands on the first tick after every_minutes)
  between: "08:00-23:00"          run only within this window (optional, doesn't cross midnight)
  for interval jobs max_attempts_per_day is "at most this many runs in a day in total" (successes count)

Long jobs: add background: true
  Runs in the background (detached from the scheduler's process group) and this tick moves straight on to other
  jobs: jobs run one at a time, and a 30–90 minute job would make every punctual job after it (muninn-tick, uploads)
  wait. The result of a finished run is written to .state/runs/<name>.json and merged back into the state on the next
  tick (only a success counts as "ran today"); while it's running, a second copy isn't started. The log is still
  .logs/<name>-<date>.log.

Time limit: every run is stopped past its limit (its whole process group killed: what it started too) and counts as a
failed run, so one hung job can't hold up every tick after it (round 6, 11-01). Default FOREGROUND_TIMEOUT_MIN for a
job the tick waits on, BACKGROUND_TIMEOUT_MIN for a background one; a job sets its own with
  timeout_minutes: 90              (the registry's `timeout: 90m`)

Order: add after: [job name]
  While a listed job is due today but not finished (not yet time, still running, failed but will retry), this job
  doesn't start; the next tick looks again. Once that job succeeded today, used up max_attempts_per_day, isn't
  scheduled today or is disabled, it's no longer waited for.
  Background jobs can't be kept apart by time alone (e.g. consolidate-memory 02:00 → librarian 03:00 both touch
  ~/.memory-copies, and once the tick drifts they may overlap). If the job waited on has no max_attempts_per_day and
  keeps failing, the wait lasts all day: when hanging an after on it, better give the waited-on job a limit.

Event-triggered jobs (registry only, trigger: instead of at/every; docs/design/event-jobs.md, bin/event_jobs.py):
  due when bus events matching trigger.events/projects/sources/where are waiting after the job's cursor
  (<job>.event_seq, tied to the bus file's generation <job>.event_gen) and trigger.debounce has passed. A new job's
  cursor starts at the bus head (never history). The run takes the oldest waiting events that fit in 64 KB
  (AGENT_HUB_EVENTS); success moves the cursor past them, failure leaves them for the next attempt
  (max_attempts_per_day, default 12). Events whose chain holds the job, or whose depth is MAX_DEPTH, are held back (loops);
  they are counted in <job>.event_held. Days/between filters keep events waiting; after:/before: work as for interval
  jobs (waited for only while running). schedulectl events/skip inspect and drop a job's waiting events.

Every run gets AGENT_HUB_JOB (its name), AGENT_HUB_JOB_CHAIN (the causal chain: [job], or the batch's chains + job) and
AGENT_HUB_JOB_DEPTH; hubbus stamps both on every event the run publishes. job.failed carries the run's chain and, for registry jobs,
to_project = the job's project.

One writer: the tick holds .state/scheduler.lock for its whole run (held by someone else for over TICK_LOCK_WAIT =
this tick does nothing, not even the heartbeat); schedulectl's state writes take the same lock. A background run closes its inherited copy at once.

At the start of every tick and before and after every foreground job, .state/scheduler-heartbeat is written, for
scheduler-watchdog.py (a separate launchd job) to tell whether the scheduler has stopped; .state/scheduler-tick.json says
which ticks completed (bin/tick_record.py), so one that crashes every time is told too.

What one broken thing may stop:
- a schedule.yaml that can't be read: its own jobs. A job whose definition can't be used (unsound) or whose due logic
  raises: that job. Each is logged, on the status page and alerted (scheduler-broken:<project>, one alert per project)
- an unreadable registry: nothing while the last index can be used
- an unreadable state file: moved aside as scheduler-last-run.broken-<time>.json and alerted; the tick goes on from the
  copy save_state keeps beside it. Without a usable copy, every job that would be due at once counts as run (hold), so
  nothing runs twice because of it
- a state that can't be saved (full disk): the tick, visibly (StateNotSaved); nothing runs without its attempt recorded,
  and no file of the scheduler's is cut (tick_record.save)
"""
from __future__ import annotations  # the system python3 (3.9) must be able to import this too

import contextlib
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import event_jobs  # noqa: E402
import hubconf  # noqa: E402
import names  # noqa: E402
import i18n  # noqa: E402
import memlock  # noqa: E402
import tick_record  # noqa: E402

_ = i18n.domain(__file__)

HUB = hubconf.ROOT
FOREGROUND_TIMEOUT_MIN = 30   # the longest foreground job ran about 8 minutes (11-01, a week of logs)
BACKGROUND_TIMEOUT_MIN = 240  # the longest background job ran about 31 minutes
TIMED_OUT = 124

# Environment variables for the projects' jobs, deliberately named without the tool's name: a project repo's code only
# reads $SAFE_PUSH (falling back to git push without it) and never hardcodes the path here: the repo may become public
# later, and committed content mustn't show this mechanism's name or paths.
JOB_ENV = {**os.environ, "SAFE_PUSH": str(HUB / "bin" / "git-safe-push.sh")}
SCHEDULE = hubconf.config_file("schedule.yaml")
STATE_FILE = HUB / ".state" / "scheduler-last-run.json"
RUNS_DIR = HUB / ".state" / "runs"
HEARTBEAT = HUB / ".state" / "scheduler-heartbeat"
LOG_DIR = HUB / ".logs"
LOCK_FILE = HUB / ".state" / "scheduler.lock"
LOCK_FD: int | None = None   # held for the whole tick; a forked background run closes its copy at once
TICK_LOCK_WAIT = 60          # a schedulectl write is short: wait this long for it before giving the tick up


ALIAS: dict[str, str] = {}  # during the switch-over: registry job name ↔ old schedule.yaml name (filled by all_jobs)


def fs(name: str) -> str:
    """job name → file name (registry jobs are called <project>/<job>, and a slash can't go into a file name)."""
    return name.replace("/", "__")


def schedule_jobs() -> tuple[list, set[str], str | None]:
    """schedule.yaml's entries, the names in it, and why it can't be used (None when it can). One that can't be read
    still gives the names written in it: a registry job whose legacy_name is among them must not start running beside
    its own old entry (all_jobs)."""
    if not SCHEDULE.exists():
        return [], set(), None
    text = ""
    try:
        text = SCHEDULE.read_text()
        doc = yaml.safe_load(text) or {}
        jobs = (doc.get("jobs") or []) if isinstance(doc, dict) else None
        if not isinstance(jobs, list):
            raise ValueError(_("it must be a list under jobs:"))
    except (OSError, ValueError, yaml.YAMLError) as e:
        return [], set(re.findall(r"""^\s*(?:-\s+)?name:\s*['"]?([^\s'"#]+)""", text, re.M)), str(e)
    return jobs, {j["name"] for j in jobs if isinstance(j, dict) and isinstance(j.get("name"), str)}, None


def unsound(job) -> str | None:
    """Which rule keeps a job from being scheduled (a field missing, or one the due logic can't use), None when none
    does. The rule only, never the field's value: it goes into an alert, and the value is the project's text.

    Shared with registry.validate this way: the registry refuses a new entry (its rules may be stricter and say what is
    wrong with the value); this is the scheduler's net under it, for what no validation saw: schedule.yaml, and an entry
    accepted under older rules that the registry still holds as its last valid one. It refuses only what would raise."""
    if not isinstance(job, dict):
        return _("it isn't a mapping")
    if not isinstance(job.get("name"), str) or not job["name"]:
        return _("it has no name")
    if not job.get("enabled", True):   # never looked at further
        return None
    if not isinstance(job.get("command"), str):
        return _("it has no command")
    day = datetime.datetime(2000, 1, 1)

    def number(v) -> bool:
        return isinstance(v, (int, float))
    try:
        if "trigger" in job:
            field = "trigger"
            on = job[field]
            if not isinstance(on["events"], (list, str)) or not isinstance(on.get("where") or {}, dict) \
                    or not number(on.get("debounce_minutes") or 0):
                raise ValueError
        elif "every_minutes" in job:
            field = "every_minutes"
            if not number(job[field]):
                raise ValueError
        else:
            field = "time"
            hhmm(day, job[field])
        field = "between"
        if field in job and ("trigger" in job or "every_minutes" in job):   # a daily job's isn't used
            start, end = job[field].split("-")
            hhmm(day, start), hhmm(day, end)
        field = "end_date"
        if field in job:
            datetime.date.fromisoformat(str(job[field]))
        field = "timeout_minutes"
        float(job.get(field) or 0)
    except (AttributeError, KeyError, TypeError, ValueError):
        return _("{field} can't be used (a time is a quoted HH:MM, a date YYYY-MM-DD)", field=field)
    for field, one in (("days_of_week", "mon"), ("days_of_month", 1), ("months", 1)):
        try:   # as scheduled_on asks; a name it never finds, or none at all, only means the job doesn't run
            one in job.get(field, [])   # noqa: B015
        except TypeError:
            return _("{field} can't be used (a list of days or months)", field=field)
    cap = job.get("max_attempts_per_day")
    if cap is not None and not number(cap):
        return _("{field} can't be used (a number)", field="max_attempts_per_day")
    after = job.get("after", [])
    if not isinstance(after, (list, str)) or not all(isinstance(x, str) for x in after):
        return _("{field} can't be used (a list of job names)", field="after")
    return None


def last_index() -> dict | None:
    """The registry's index as it was last built; None when there is none or it can't be read."""
    try:
        index = json.loads(hubconf.REGISTRY_INDEX.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(index, dict) or not isinstance(index.get("projects"), dict):
        return None
    return {"modules": {}, "errors": {}, **index}


def told(group: str, why: str) -> dict[str, str]:
    """An entry of info["broken"]: whose it is (a project or module; SCHEDULE_YAML, REGISTRY) and the rule broken."""
    return {"group": group, "why": why}


SCHEDULE_YAML, REGISTRY = "(schedule.yaml)", "(registry)"


def registry_jobs(index: dict, taken: set[str], broken: dict[str, dict]) -> tuple[list[dict], list[str], dict[str, str]]:
    """registry.scheduler_jobs, without the entries it can't read (named in broken): one entry never stops the others."""
    import registry
    try:
        return registry.scheduler_jobs(index, taken)
    except Exception:
        good = {"projects": {}, "modules": {}}
        for kind, docs in good.items():
            for name, doc in (index.get(kind) or {}).items():
                try:
                    registry.scheduler_jobs({**index, "projects": {}, "modules": {}, kind: {name: doc}}, taken)
                    docs[name] = doc
                except Exception as e:
                    broken[name] = told(name, _("its entry can't be read ({error})", error=type(e).__name__))
        return registry.scheduler_jobs({**index, **good}, taken)


def all_jobs(log_errors: bool = False) -> tuple[list[dict], dict]:
    """schedule.yaml's jobs + the project registry's jobs, the sound ones. During the switch-over schedule.yaml wins: a
    registry job whose legacy_name is still in schedule.yaml isn't used (deleting that schedule.yaml entry switches it
    over). A broken registry doesn't affect schedule.yaml, nor the other way round.
    Returns (jobs, info); info: alias (the two sides' names, for after:), renames (new→old, for moving state),
    skipped, errors, broken (what isn't scheduled → told: a job, SCHEDULE_YAML, REGISTRY or an entry)."""
    legacy, taken, why = schedule_jobs()
    info = {"alias": {}, "renames": {}, "skipped": [], "errors": {}, "broken": {}}
    said = []   # what a tick's log says
    if why:
        info["broken"][SCHEDULE_YAML] = told(SCHEDULE_YAML, why)
        said.append(_("schedule.yaml can't be used, none of its jobs run ({error})", error=why))
    reg_jobs = []
    try:
        import registry
        try:
            index = registry.refresh()
        except Exception as e:   # the jobs go on from the index as it was last built
            info["errors"][REGISTRY] = [repr(e)]
            info["broken"][REGISTRY] = told(REGISTRY, type(e).__name__)
            if (index := last_index()) is None:
                raise
            said.append(_("The project registry can't be read ({error!r}); this tick runs it as it was last read",
                          error=e))
        reg_jobs, skipped, renames = registry_jobs(index, taken, info["broken"])
        for full, why in (index.get("unreadable") or {}).items():   # a job of theirs the registry couldn't read
            info["broken"].setdefault(full, told(full.split("/", 1)[0], _("its entry can't be read ({error})", error=why)))
        try:   # what their runs would refuse (they still run, and fail saying so): listed for the status page
            by_rules = registry.rules_refused(index)
        except Exception as e:
            by_rules = {"*": str(e)}
        info.update(renames=renames, skipped=skipped, errors={**index["errors"], **info["errors"]},
                    refused={**(index.get("refused") or {}), **by_rules})
        # module jobs have old names too
        for d in [*index["projects"].values(), *(index.get("modules") or {}).values()]:
            for j in d.get("jobs") or []:
                if j.get("legacy_name"):
                    full = f"{d['name']}/{j['name']}"
                    info["alias"][full] = j["legacy_name"]
                    info["alias"][j["legacy_name"]] = full
    except Exception as e:  # the whole registry can't be read: run only schedule.yaml's
        reg_jobs = []
        info["errors"][REGISTRY] = [repr(e)]
        info["broken"][REGISTRY] = told(REGISTRY, type(e).__name__)
        said.append(_("The project registry can't be read ({error!r}); this tick runs only schedule.yaml", error=e))
    jobs = []
    for n, job in enumerate(legacy + reg_jobs, 1):
        if (why := unsound(job)) is None:
            jobs.append(job if isinstance(job.get("command"), str) else {**job, "command": ""})   # a disabled one's
            continue
        name = job.get("name") if isinstance(job, dict) else None
        name = name if isinstance(name, str) and name else _("(entry {n} of schedule.yaml)", n=n)
        info["broken"][name] = told(job.get("project") if n > len(legacy) else SCHEDULE_YAML, why)
        said.append(_("job '{name}' isn't scheduled: {why}", name=name, why=why))
    if log_errors:
        for line in said:
            log(line)
    return jobs, info


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:   # a line that can't be written (a full disk) never ends a tick
        print(line)
    except OSError:
        pass
    try:
        with open(LOG_DIR / "scheduler.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


@contextlib.contextmanager
def state_lock(timeout: float = 0, path: pathlib.Path | None = None):
    """The scheduler's state has one writer at a time: the tick holds this for its whole run, and schedulectl's writes
    (run, skip) take it too, so an event job's cursor never moves back to a stale copy (docs/design/event-jobs.md
    section 4). Waits up to timeout seconds, then yields False (never blocks forever: a run that calls schedulectl
    while its own tick holds the lock must fail, not hang)."""
    global LOCK_FD
    path = path or LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        if not memlock.flock(fd, timeout):
            yield False
            return
        LOCK_FD = fd
        yield True
    finally:
        LOCK_FD = None
        os.close(fd)


class StateNotSaved(Exception):
    """The state can't be written (a full disk): the tick ends here, visibly. Running on would run jobs whose attempts
    are not recorded."""


def state_copy(path: pathlib.Path | None = None) -> pathlib.Path:
    """The state as it was last saved, beside it: what a tick goes on from when the state itself can't be read."""
    path = path or STATE_FILE
    return path.with_name(path.stem + ".copy.json")


def save_state(state: dict, path: pathlib.Path | None = None) -> None:
    text = json.dumps(state, indent=2)
    path = path or STATE_FILE
    try:
        tick_record.save(path, text)
        tick_record.save(state_copy(path), text)
    except OSError as e:
        raise StateNotSaved(f"{path.name}: {e}") from e


# the copy is the state's own last save when it isn't older than it by more than one job's run. An older one is from
# before another version's ticks (a rollback) or a write that isn't the scheduler's: it says too little about today
COPY_GAP = datetime.timedelta(minutes=45)


def read_state(path: pathlib.Path) -> dict:
    """ValueError when it isn't a JSON object (empty, cut, not text)."""
    state = json.loads(path.read_text())
    if not isinstance(state, dict):
        raise ValueError(f"{path.name} is not an object")
    return state


def peek_state(path: pathlib.Path) -> dict:
    """The state, to look at: the copy when it can't be read, {} when that can't either."""
    for p in (path, state_copy(path)):
        try:
            return read_state(p)
        except (OSError, ValueError):
            pass
    return {}


def load_state(jobs, now: datetime.datetime, today: str, path: pathlib.Path | None = None) -> dict:
    """The state, for a caller that holds the lock and saves it. One that can't be read (its text, or the file itself: no
    permission, an I/O error) is moved aside and the caller goes on from the copy; with no copy, or one that isn't the
    last save, every job due now counts as run (hold). It is never taken for a fresh install's, which would run every job
    at once: the copy is written before the broken file leaves its place, so whatever stops this in between, the next one
    finds one of the two. A failing disk leaves the broken file where it is: one broken state, one file aside, one alert.
    jobs: the list, or a call that gives it (only needed for hold)."""
    path = path or STATE_FILE
    copy = state_copy(path)
    try:
        return read_state(path)
    except FileNotFoundError:
        if not copy.exists():
            return {}   # a fresh install: nothing has run yet
        saved, there = now.timestamp(), False
    except (OSError, ValueError):
        there = True
        try:
            saved = path.stat().st_mtime
        except OSError:
            saved = now.timestamp()
    try:
        state = read_state(copy)
        good = saved - copy.stat().st_mtime <= COPY_GAP.total_seconds()
    except (OSError, ValueError):
        state, good = {}, False
    if not good:
        hold(jobs() if callable(jobs) else jobs, state, now, today)
    try:
        tick_record.save(copy, json.dumps(state, indent=2))
        if there:
            os.replace(path, path.with_name(f"{path.stem}.broken-{now:%Y%m%d-%H%M%S}.json"))
    except OSError as e:
        raise StateNotSaved(f"{path.name}: {e}") from e
    save_state(state, path)
    log(f"{path.name} could not be read; going on from " + ("its copy" if good else
        "what is known: the jobs due now count as run"))
    return state


def hold(jobs: list[dict], state: dict, now: datetime.datetime, today: str) -> None:
    """Count the jobs that would run in this tick as run: with no record of what ran, running them all at once (the
    expensive ones again, letters sent twice) is worse than leaving out one run. Daily jobs run again tomorrow, interval
    jobs after their interval; an event job with no cursor starts at the bus head (first_sight)."""
    for job in jobs:
        name = job["name"]
        if "trigger" in job:
            continue
        try:
            if "every_minutes" in job:
                ran_at(state, name, now.isoformat(timespec="seconds"), now.timestamp())
            elif runs_today(job, now, state) and is_due(job, state, now, today):
                state[name] = today
        except Exception as e:   # its record in an old copy can't be used: its own turn says so
            log(f"job '{name}': {e!r}")


def broken_states() -> list[pathlib.Path]:
    return sorted(STATE_FILE.parent.glob(f"{STATE_FILE.stem}.broken-*.json"))


BROKEN_TOLD = datetime.timedelta(hours=22)   # a state kept aside is a problem for this long: told once, never reminded


def broken_state_lines(now: datetime.datetime) -> dict[str, str]:
    """What to say of each state kept aside lately (the alert and the status page)."""
    lines = {}
    for p in broken_states():
        try:
            when = datetime.datetime.strptime(p.stem.rsplit("broken-", 1)[1], "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if now - when < BROKEN_TOLD:   # a day's alert: an hour more or less doesn't matter
            lines[p.name] = _("The scheduler's record of what ran ({file}) could not be read at {at}. It went on from the "
                              "copy it keeps; without a usable one, the jobs due at that moment were counted as run. The "
                              "broken file is kept as {aside}", file=STATE_FILE.name, at=f"{when:%m-%d %H:%M}", aside=p)
    return lines


def alert_broken_state(now: datetime.datetime) -> None:
    alert_each("scheduler-state:", {name: {name: line} for name, line in broken_state_lines(now).items()}, lambda name: _("The scheduler's record of what ran is readable again"))


FAILING_FILE = HUB / ".state" / "scheduler-failing.json"
# alert only after failing this long (no noise about the ones the next tick's retry fixes)
ALERT_AFTER = datetime.timedelta(minutes=60)


FOUND = 4   # alert.FOUND (not imported: a broken alert.py must not stop the scheduling; a test checks they agree)


def completed(job: dict, rc: int) -> bool:
    """Whether a run's exit is a completed run: 0, or FOUND from a job that declares found_exit (a check that ran and
    delivered what it found itself; docs/design/alert-routing.md step 3). Any other job's 4 is a failure."""
    return rc == 0 or (rc == FOUND and job.get("found_exit") is True)


def finished_line(name: str, rc: int) -> str:
    """The log line of a completed run (gen-status-page.py's LOG_LINE reads it)."""
    return f"job '{name}' finished OK" if rc == 0 else f"job '{name}' finished, problems found and sent (exit {rc})"


def failing_jobs() -> dict:
    """scheduler-failing.json's jobs; one that can't be read counts as empty (the next failure writes it new)."""
    try:
        d = json.loads(FAILING_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def mark_job(name: str, failed: bool, detail: str = "") -> None:
    """Record which jobs are failing now (last run failed): removed on success. For alert_failing. Failing to write
    doesn't affect scheduling."""
    try:
        d = failing_jobs()
        if failed:
            prev = d.get(name) or {}
            d[name] = {"since": prev.get("since") or datetime.datetime.now().isoformat(timespec="seconds"),
                       "detail": detail}
        elif name in d:
            del d[name]
        else:
            return
        tick_record.save(FAILING_FILE, json.dumps(d, ensure_ascii=False, indent=1))
    except OSError:
        pass


def failing_problems(failing: dict, now: datetime.datetime) -> list[str]:
    """Jobs failing for longer than ALERT_AFTER → one line each (alert_failing sends each under its own key, the job's
    name its id, so a line's changing detail is not a new problem)."""
    out = []
    for name, rec in sorted(failing.items()):
        try:
            since = datetime.datetime.fromisoformat(rec["since"])
        except (KeyError, TypeError, ValueError):
            continue
        if now.timestamp() - since.timestamp() >= ALERT_AFTER.total_seconds():
            out.append(_("Scheduled job {name} has been failing since {since} ({detail})", name=name,
                         since=rec["since"][5:16].replace("T", " "), detail=rec.get("detail") or _("see the log")))
    return out


JOB_KEY = "scheduler-job:"   # one alert per failing job: alert.once's key is this + the job's name
BROKEN_KEY = "scheduler-broken:"   # ... and per project (or file) with a job that can't be scheduled (alert.BROKEN_KEY)


def alert_each(prefix: str, lines: dict[str, dict[str, str]], ok_text, names: dict[str, list[str]] | None = None) -> None:
    """One alert per name under prefix: {problem's id: text}; the names told before and not in lines any more are closed.
    names: the jobs one of them lists (by_project). Broken alerts don't affect scheduling, nor one name's the others'."""
    try:
        import alert
        known = {k[len(prefix):] for k in (*alert.open_keys(prefix), *alert.failures()) if k.startswith(prefix)}
    except Exception as e:
        log(_("The scheduled-job alert was not sent: {error!r}", error=e))
        return
    for name in sorted(set(lines) | known):
        ids = sorted(lines.get(name) or {})
        try:
            alert.once(prefix + name, [lines[name][i] for i in ids], ok_text(name), ids=ids,
                       **({"names": names[name]} if names and name in names else {}))
        except Exception as e:
            log(_("The scheduled-job alert was not sent: {error!r}", error=e))


def whose(prefix: str, name: str) -> str | None:
    """The project whose agents hear of this job first (alert.route); None for what is the platform's or the owner's."""
    try:
        import alert
        to = alert.route(prefix + name)
        return to if to not in ("owner", hubconf.hub_project()) else None
    except Exception:   # a broken alert.py is alert_each's to say
        return None


def by_project(prefix: str, items: dict[str, tuple[str, str]], join,
               own=frozenset()) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    """({alert's name: {id: text}}, {project's alert: its jobs}) of {job: (its group, its line)}. A project's jobs are one
    alert whose id is the project, so neither a thirty-first job nor a renamed one is a new letter (alert.once's names: a
    few updates in its thread); its text is bounded. What is the platform's, a module's or the
    owner's is told by name: a second one is news. own: the jobs that keep an alert of their own all the same."""
    out: dict[str, dict[str, str]] = {}
    guests: dict[str, list[str]] = {}
    names: dict[str, list[str]] = {}
    for job, (group, line) in sorted(items.items()):
        if job not in own and (project := whose(prefix, job if "/" in job else group)):
            guests.setdefault(project, []).append(line[:220])
            names.setdefault(project, []).append(job)
        else:
            out.setdefault(group, {})[job] = line
    for project, them in guests.items():
        more = [_("and {n} more", n=len(them) - 5)] if len(them) > 5 else []
        out[project] = {project: join(project, "; ".join(them[:5] + more))}
    return out, names


def alert_broken(broken: dict[str, dict]) -> None:
    """What can't be scheduled, with the rule each breaks. A job the mailbox needs is its own alert: that one can't go as a
    letter (alert.route)."""
    try:
        from alert import OWNER_JOBS
    except Exception:
        OWNER_JOBS = ()
    items = {name: (name if name.split("/", 1)[-1] in OWNER_JOBS else rec["group"], f"{name[:60]}: {rec['why'][:160]}")
             for name, rec in broken.items()}
    lines, names = by_project(BROKEN_KEY, items, lambda project, text: _(
        "Not everything of {group} can be scheduled: {items}", group=project, items=text))
    alert_each(BROKEN_KEY, lines, lambda name: _("Everything of {group} can be scheduled again", group=name), names)


def alert_failing() -> None:
    """10-27: contracts-check failed two nights running and nobody knew: a failed scheduled job used to show only as red
    on the status page. Now persistent failures raise an alert (alert.py: new problems at once, unresolved ones
    reminded every 24 hours, recovery sent when fixed).

    One alert per job, the job's name its id (11-18): all of them under one key were sent again whenever any one
    changed, and a job's line carries its log's name, which changes every day, so the same failure came back as new for
    two weeks. A job that stops failing gets its own recovery; the old single key is closed once. An ordinary project's
    are one alert, its id the project (by_project): each is a letter that starts a run of the project's agents, and a
    project with thirty failing jobs, or one that renames them, gets one, listing what fails when it is sent; the status
    page has each."""
    failing, now = failing_jobs(), datetime.datetime.now()
    # failing_problems skips the ones not failing long enough: one job at a time, so a line keeps its job's name
    items = {name: (name, line) for name in sorted(failing) for line in failing_problems({name: failing[name]}, now)}
    try:   # one opened per job by a version from before by_project stays that, its letter's day and all, until it is over
        import alert
        own = {k[len(JOB_KEY):] for k in alert.open_keys(JOB_KEY)}
    except Exception:
        own = set()
    lines, names = by_project(JOB_KEY, items, lambda project, text: text, own)
    projects = {name for name in own | set(lines) if "/" not in name and whose(JOB_KEY, name) == name}
    alert_each(JOB_KEY, lines, lambda name: _("The scheduled jobs of {name} all work again", name=name) if name in projects
               else _("Scheduled job {name} works again", name=name), names)
    try:
        import alert
        alert.once("scheduler-jobs", [], _("The scheduled jobs are all back to normal"))   # the key before 11-18
    except Exception as e:  # broken alerts don't affect scheduling
        log(_("The scheduled-job alert was not sent: {error!r}", error=e))


def job_failed(name: str, code: int, detail: str = "", project: str | None = None,
               chain: list[str] | None = None, depth: int = 1) -> None:
    """job.failed on the bus (read by the status page, the assistant and event jobs); failing to write doesn't affect
    scheduling. A registry job's failure belongs to its project (to_project), and carries the run's chain so an event
    job can't trigger itself through its own failure (event-jobs.md section 7)."""
    mark_job(name, True, detail or f"exit {code}")
    to = ["--to", project] if project else []
    env = dict(os.environ)
    names.env_set(env, event_jobs.CHAIN_VAR, json.dumps(chain or [name], ensure_ascii=False))
    names.env_set(env, event_jobs.DEPTH_VAR, str(depth))
    try:
        subprocess.run([sys.executable, str(HUB / "bin" / "hubbus"), "publish", "job.failed", "--source", "scheduler",
                        *to, "--payload", json.dumps({"job": name, "exit": code, "detail": detail}, ensure_ascii=False)],
                       capture_output=True, timeout=30, env=env)
    except Exception as e:   # past its 30 s (a busy bus), or not started: the failure is in the log and marked already
        log(f"job '{name}': its failure did not reach the bus ({e!r})")


def heartbeat() -> None:
    """Only a write that went through makes it newer: on a full disk the heartbeat stays as it was, and grows old."""
    tick_record.save(HEARTBEAT, datetime.datetime.now().isoformat(timespec="seconds") + "\n")


def tick_file() -> pathlib.Path:
    return HEARTBEAT.with_name("scheduler-tick.json")


def write_json(path: pathlib.Path, data: dict) -> None:
    tick_record.save(path, json.dumps(data, indent=2))


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_mark(pid: int) -> str | None:
    """What tells a process from a later one with the same number: on Linux this boot's id and when in it the process
    started, elsewhere its start time, as the kernel noted it. None when it can't be told. Never compared with a clock:
    one that is set doesn't move it."""
    try:
        if sys.platform.startswith("linux"):
            boot = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            # the name, in parentheses, may hold anything; the start is the 20th field after it
            return f"{boot} {pathlib.Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()[19]}"
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout.strip() or None
    except Exception:
        return None


def still_ours(rec: dict) -> bool:
    """Whether the process with the run's pid is the run, not one that got the number after a reboot. A record from before
    `proc`, or a mark that can't be read: taken as it."""
    mark = rec.get("proc") and process_mark(rec["pid"])
    return not mark or mark == rec["proc"]


def reconcile(state: dict, jobs: dict[str, dict], merged: list[pathlib.Path]) -> set[str]:
    """Merge the background jobs' results into state; return the names of jobs still running. The merged run files
    go into merged: the caller deletes them only after saving the state (a crash in between must not lose a success,
    or an event job's batch would be run again)."""
    running = set()
    for f in RUNS_DIR.glob("*.json"):
        name = f.stem.replace("__", "/")
        try:
            rec = json.loads(f.read_text())
            started, date = datetime.datetime.fromisoformat(rec["started"]), str(rec["date"])
        except FileNotFoundError:
            continue
        except (OSError, KeyError, TypeError, ValueError) as e:
            log(f"job '{name}': its run's record could not be read ({type(e).__name__}); dropped")
            try:
                f.unlink()
            except OSError:
                pass
            continue
        now = datetime.datetime.now()
        if "exit" not in rec:
            # the instant before the pid is written (just forked) also counts as running
            if (pid_alive(rec.get("pid")) and still_ours(rec)) or (
                    rec.get("pid") is None and now.timestamp() - started.timestamp() < 60):
                running.add(name)
                continue
            # gone without writing a result (reboot, killed): counts as a failure; the attempt was recorded before it
            # started
            log(f"job '{name}' FAILED (exit -1), background process gone without a result")
            job_failed(name, -1, _("the background run vanished midway (reboot or killed)"), rec.get("project"),
                       rec.get("chain"), rec.get("depth", 1))
        elif completed(rec, rec["exit"]):
            state[name] = date
            mark_job(name, False)
            if "every_minutes" in jobs.get(name, {}):
                ran_at(state, name, rec["started"], rec.get("started_at"))
            if "events_upto" in rec:
                consumed(state, name, rec["events_upto"], rec.get("events_count", 0), rec["started"])
                held_back(state, name, rec.get("events_held") or [], date)
        merged.append(f)
    return running


def run_bounded(command: str, job_log, minutes: float, env: dict | None = None) -> int:
    """Run the job's command in its own process group; past `minutes`, SIGTERM the group, SIGKILL it 10 s later, and
    return TIMED_OUT."""
    import signal
    # brunna on PATH by name, after the shell's profile (hubconf.job_command)
    p = subprocess.Popen([hubconf.login_shell(), "-lc", hubconf.job_command(command)], stdout=job_log,
                         stderr=subprocess.STDOUT, env=env or JOB_ENV, start_new_session=True)
    try:
        return p.wait(timeout=minutes * 60)
    except subprocess.TimeoutExpired:
        for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, None)):
            try:
                os.killpg(p.pid, sig)
            except OSError:
                pass
            try:
                p.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        job_log.write(f"\n[scheduler] stopped after {minutes:g} minutes (the job's time limit)\n")
        return TIMED_OUT


def timeout_of(job: dict) -> float:
    return float(job.get("timeout_minutes") or (BACKGROUND_TIMEOUT_MIN if job.get("background")
                                                else FOREGROUND_TIMEOUT_MIN))


def spawn_background(name: str, command: str, job_log_path: pathlib.Path,
                     now: datetime.datetime, today: str, env: dict | None = None, extra: dict | None = None,
                     minutes: float = BACKGROUND_TIMEOUT_MIN) -> None:
    """double fork + setsid: launchd kills what's left in the same process group when the tick ends
    (run-agent-job.sh hit this on 2026-10-14). The grandchild writes its log and result file itself when done.
    extra goes into the run file (project, chain, an event job's events_upto) for reconcile."""
    run_file = RUNS_DIR / f"{fs(name)}.json"
    rec = {"date": today, "started": now.isoformat(timespec="seconds"), "started_at": now.timestamp(), "pid": None,
           **(extra or {})}
    write_json(run_file, rec)
    sys.stdout.flush()
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return
    try:
        if LOCK_FD is not None:
            os.close(LOCK_FD)   # the tick's lock must not live as long as this run
        os.setsid()
        if os.fork():
            os._exit(0)
        rec.update(pid=os.getpid(), proc=process_mark(os.getpid()))
        write_json(run_file, rec)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        with open(job_log_path, "a") as job_log:
            rc = run_bounded(command, job_log, minutes, env)
        rec.update(exit=rc, finished=datetime.datetime.now().isoformat(timespec="seconds"))
        write_json(run_file, rec)
        log(finished_line(name, rc) if completed(rec, rc) else
            f"job '{name}' FAILED (exit {rc}), will retry next tick")
        if not completed(rec, rc):
            job_failed(name, rc, f"log:.logs/{job_log_path.name}", rec.get("project"), rec.get("chain"),
                       rec.get("depth", 1))
    finally:
        os._exit(0)


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def scheduled_on(job: dict, d: datetime.date) -> bool:
    """The day filters alone (days_of_week, days_of_month, months)."""
    if "days_of_week" in job and WEEKDAYS[d.weekday()] not in job["days_of_week"]:
        return False
    if "days_of_month" in job and d.day not in job["days_of_month"]:
        return False
    if "months" in job and d.month not in job["months"]:
        return False
    return True


CATCH_UP_DAYS = 62   # how far back catch_up: period looks for the day a job missed (covers a monthly job)


def missed_day(job: dict, now: datetime.datetime, state: dict | None) -> datetime.date | None:
    """catch_up: period (docs/design/newcomer-path.md B7): the last scheduled day before today, when the job hasn't run
    on or since it (the machine was asleep or off that day). A weekly or monthly job then runs at the first tick after
    waking instead of skipping its whole period. None: no catch-up (not opted in, or it ran)."""
    if job.get("catch_up") != "period" or state is None or "time" not in job:
        return None
    last = state.get(job["name"]) or ""
    for back in range(1, CATCH_UP_DAYS + 1):
        d = now.date() - datetime.timedelta(days=back)
        if scheduled_on(job, d):
            return d if last < d.isoformat() else None
    return None


def runs_today(job: dict, now: datetime.datetime, state: dict | None = None) -> bool:
    if "end_date" in job and now.date() > datetime.date.fromisoformat(str(job["end_date"])):
        return False
    return scheduled_on(job, now.date()) or missed_day(job, now, state) is not None


def hhmm(now: datetime.datetime, s: str) -> datetime.datetime:
    hh, mm = map(int, s.split(":"))
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def waiting_on(job: dict, by_name: dict[str, dict], state: dict, running: set[str],
               now: datetime.datetime, today: str) -> str | None:
    """If a job listed in after: hasn't finished today, return whom we're waiting for; None if no need to wait."""
    for dep in job.get("after", []):
        d = by_name.get(dep)
        # during the switch-over: after names the new name while the job still has the old one (or vice versa)
        if d is None and dep in ALIAS:
            dep = ALIAS[dep]
            d = by_name.get(dep)
        if d is None and "/" in dep:   # a project's former name, the platform's other spelling included
            proj, short = dep.split("/", 1)
            for now_name in {hubconf.hub_project()} if proj in names.all("project") else set():
                if (d := by_name.get(f"{now_name}/{short}")):
                    dep = f"{now_name}/{short}"
        try:
            if d is not None and unfinished_today(dep, d, state, running, now, today):
                return dep
        except Exception as e:   # a job whose due logic is broken doesn't run: it holds nobody
            log(f"not waiting for '{dep}': {e!r}")
    return None


def unfinished_today(dep: str, d: dict, state: dict, running: set[str], now: datetime.datetime, today: str) -> bool:
    """Whether the job dep (d) is due today and not finished: what after: waits for."""
    if not d.get("enabled", True) or not runs_today(d, now, state):
        return False
    if dep in running:
        return True
    if "every_minutes" in d or "trigger" in d:  # interval and event jobs have no "done for today"; wait only for this
        return False                           # run to finish
    if now < hhmm(now, d["time"]) and not (not scheduled_on(d, now.date()) and missed_day(d, now, state)):
        return True
    if state.get(dep) == today:
        return False
    att = state.get(f"{dep}.attempts", {})
    tried = att.get("count", 0) if att.get("date") == today else 0
    if d.get("max_attempts_per_day") is not None and tried >= d["max_attempts_per_day"]:
        return False  # given up for today; stop waiting
    return True


def ran_at(state: dict, name: str, started: str, at: float | None) -> None:
    """An interval job's success. last_ok is the local reading every version reads; last_ok_at the same moment as a time,
    which tells the two readings of the hour daylight saving repeats apart."""
    state[f"{name}.last_ok"] = started
    if isinstance(at, (int, float)):
        state[f"{name}.last_ok_at"] = int(at)   # whole seconds, as the reading: a tick's fraction must not be waited for


def last_ok_at(state: dict, name: str, last: str) -> float:
    """last_ok as a time: last_ok_at when it is of the same moment (another version's tick writes last_ok alone)."""
    at = state.get(f"{name}.last_ok_at")
    if isinstance(at, (int, float)) and datetime.datetime.fromtimestamp(at).isoformat(timespec="seconds") == last:
        return at
    return datetime.datetime.fromisoformat(last).timestamp()


def is_due(job: dict, state: dict, now: datetime.datetime, today: str) -> bool:
    name = job["name"]
    if "every_minutes" in job:
        if "between" in job:
            start, end = job["between"].split("-")
            if not hhmm(now, start) <= now <= hhmm(now, end):
                return False
        last = state.get(f"{name}.last_ok")
        # one minute's grace: launchd's tick drifts by a few seconds, or every_minutes: 15 would become 30
        if last is None:
            return True
        # as times, not as readings of a clock that daylight saving moves; a last run in the future (the clock was set
        # back) says nothing: due
        since = now.timestamp() - last_ok_at(state, name, last)
        return since < 0 or since >= (job["every_minutes"] - 1) * 60
    if not scheduled_on(job, now.date()) and missed_day(job, now, state):   # catching up: due now, whatever the time
        return state.get(name) != today
    return now >= hhmm(now, job["time"]) and state.get(name) != today


def consumed(state: dict, name: str, upto: int, count: int, started: str) -> None:
    """A successful run of an event job took its batch: the cursor moves to upto (never back)."""
    state[f"{name}.event_seq"] = max(state.get(f"{name}.event_seq", 0), upto)
    state[f"{name}.event_last"] = {"at": started, "count": count}


def held_back(state: dict, name: str, held: list[dict], today: str) -> None:
    """Events the loop chain kept from triggering this job, counted per day for the status page (never silent)."""
    if not held:
        return
    rec = state.get(f"{name}.event_held") or {}
    n = rec.get("count", 0) if rec.get("date") == today else 0
    state[f"{name}.event_held"] = {"date": today, "count": n + len(held), "last": held[-1]["id"]}
    log(f"job '{name}': {len(held)} event(s) held back by the loop chain ({held[-1]['id']})")


def adopt_cursor(job: dict, state: dict) -> None:
    """A renamed project's job takes the cursor its old name had (hubconf.former_names)."""
    project, short, name = job.get("project"), job["name"].split("/", 1)[-1], job["name"]
    for old in hubconf.former_names(project) if project else []:
        seq, gen = state.get(f"{old}/{short}.event_seq"), state.get(f"{old}/{short}.event_gen")
        if seq is None:
            continue
        if f"{name}.event_seq" not in state:
            state[f"{name}.event_seq"], state[f"{name}.event_gen"] = seq, gen
        elif gen == state.get(f"{name}.event_gen"):
            # a background run under the old name may finish after the copy: its advance must still count
            state[f"{name}.event_seq"] = max(state[f"{name}.event_seq"], seq)


def adopt_state(job: dict, state: dict) -> None:
    """A renamed project's job takes the daily state its old names left (the platform's own rename agent-hub ↔ brunna
    counts both ways, hubconf.former_names), newest wins, and the old keys go: a daily job never runs twice because its
    project changed name, on an upgrade, a rollback or a jump (docs/design/rename-brunna.md). Its failing record moves
    too, so an open alert still gets its recovery message."""
    project, short, name = job.get("project"), job["name"].split("/", 1)[-1], job["name"]
    for old in hubconf.former_names(project) if project else []:
        was = f"{old}/{short}"
        if was == name:
            continue
        if was in state:
            state[name] = max(state.get(name) or "", state.pop(was))
        if f"{was}.last_ok" in state:
            state[f"{name}.last_ok"] = max(state.get(f"{name}.last_ok") or "", state.pop(f"{was}.last_ok"))
            if (at := state.pop(f"{was}.last_ok_at", None)) is not None and state[f"{name}.last_ok"] \
                    == datetime.datetime.fromtimestamp(at).isoformat(timespec="seconds"):
                state[f"{name}.last_ok_at"] = at
        if f"{was}.attempts" in state:
            a, b = state.pop(f"{was}.attempts") or {}, state.get(f"{name}.attempts") or {}
            if (a.get("date") or "", a.get("count", 0)) > (b.get("date") or "", b.get("count", 0)):
                state[f"{name}.attempts"] = a
        try:
            failing = failing_jobs()
            if was in failing:
                failing.setdefault(name, failing[was])
                del failing[was]
                tick_record.save(FAILING_FILE, json.dumps(failing, ensure_ascii=False, indent=1))
        except OSError:
            pass


def first_sight(job: dict, state: dict) -> None:
    """An event job with no cursor (new, or renamed without an old one) starts at the bus head: from now on, never
    history. Runs before the date filters, so events on a day the job doesn't run wait for it."""
    adopt_cursor(job, state)
    if f"{job['name']}.event_seq" in state or not (hubconf.BUS_DIR / "bus.db").exists():
        return   # no bus file yet: no cursor either (one taken now couldn't tell a bus moved in later from history)
    try:
        head, gen, _rows = event_jobs.read(0, [])
    except Exception as e:
        log(_("job '{name}': the bus can't be read ({error!r}); its events wait", name=job["name"], error=e))
        return
    state[f"{job['name']}.event_seq"], state[f"{job['name']}.event_gen"] = head, gen


def event_batch(job: dict, state: dict, now: datetime.datetime, today: str) -> dict | None:
    """An event job's turn: the batch to run with (event_jobs.take: {events, upto, left}), or None when there is
    nothing to run now. Moves the cursor where nothing waits (first sight, nothing pending, a new bus file).
    docs/design/event-jobs.md section 4."""
    name = job["name"]
    key, gkey = f"{name}.event_seq", f"{name}.event_gen"
    if key not in state:
        return None
    try:
        got = event_jobs.pending(job, state[key])
    except Exception as e:  # an unreadable bus must not stop the other jobs
        log(_("job '{name}': the bus can't be read ({error!r}); its events wait", name=name, error=e))
        return None
    if got["head"] < state[key] or (state.get(gkey) and got["generation"] and got["generation"] != state[gkey]):
        log(_("job '{name}': the bus file was replaced (cursor {cursor}, head now {head}); starting from the head",
              name=name, head=got["head"], cursor=state[key]))
        state[key], state[gkey] = got["head"], got["generation"]
        return None
    state[gkey] = got["generation"] or state.get(gkey)
    if not got["events"]:
        held_back(state, name, got["held"], today)
        state[key] = got["head"]
        return None
    if "between" in job:
        start, end = job["between"].split("-")
        if not hhmm(now, start) <= now <= hhmm(now, end):
            return None
    if not event_jobs.quiet(job, got["events"], now):
        return None
    batch = event_jobs.take(got)
    batch["held"] = [h for h in got["held"] if h["seq"] <= batch["upto"]]
    return batch


def main() -> None:
    if hubconf.worktree_lookup():   # it would run the machine's jobs from a half-edited copy
        sys.exit(_("scheduler: {root} is a linked worktree of the hub; the scheduler runs only from the main checkout "
                   "({main})", root=hubconf.ROOT, main=hubconf.MAIN))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with state_lock(timeout=TICK_LOCK_WAIT) as mine:
        if not mine:   # no heartbeat either: a tick that can't run must not look alive
            log(f"the state has been held for over {TICK_LOCK_WAIT}s (another tick, or a schedulectl write); "
                "this tick does nothing")
            return
        tick_record.begin(tick_file(), datetime.datetime.now())
        heartbeat()
        try:
            tick()
        except Exception as e:   # visible: the log, the status page, the record the watchdog and doctor read, exit 1
            log(f"the tick did not complete: {e!r}")
            try:
                tick_record.failed(tick_file(), repr(e))
            except OSError:   # a full disk: the record still says it runs, which the next tick takes as one that died
                pass
            status_page()
            raise
        tick_record.complete(tick_file(), datetime.datetime.now())
        status_page()


def status_page() -> None:
    """The schedule status page (served by local.status-page, on 127.0.0.1 by default), regenerated every tick; a failure
    doesn't affect scheduling."""
    try:
        subprocess.run([sys.executable, str(HUB / "bin" / "gen-status-page.py")], capture_output=True, timeout=300)
    except Exception as e:
        log(f"gen-status-page did not finish: {e!r}")


def turn(job: dict, by_name: dict[str, dict], state: dict, running: set[str], now: datetime.datetime,
         today: str) -> dict | None:
    """Whether this job runs now: {batch, tried} (an event job's events; the attempts it has had today), else None."""
    name = job["name"]
    if "trigger" in job:
        first_sight(job, state)
    if not job.get("enabled", True) or not runs_today(job, now, state) or name in running:
        return None
    batch = None
    if "trigger" in job:
        if not (batch := event_batch(job, state, now, today)):
            return None
    elif not is_due(job, state, now, today):
        return None
    if dep := waiting_on(job, by_name, state, running, now, today):
        log(f"job '{name}' waiting for '{dep}' (after:)")
        return None
    attempts = state.get(f"{name}.attempts", {})
    tried = attempts.get("count", 0) if attempts.get("date") == today else 0
    if job.get("max_attempts_per_day") is not None and tried >= job["max_attempts_per_day"]:
        return None
    return {"batch": batch, "tried": tried}


def run_job(job: dict, batch: dict | None, state: dict, running: set[str], now: datetime.datetime, today: str) -> None:
    """One run, its attempt already recorded. A failed one is recorded here; only a state that can't be saved, or a run
    that can't be started, raises."""
    name = job["name"]
    job_log_path = LOG_DIR / f"{fs(name)}-{today}.log"
    events = batch["events"] if batch else None
    chain, depth = event_jobs.run_chain(name, events or []), event_jobs.run_depth(events or [])
    env = {**JOB_ENV, **event_jobs.run_env(name, chain, depth, events)}
    extra = {"project": job.get("project"), "chain": chain, "depth": depth,
             **({"found_exit": True} if job.get("found_exit") is True else {})}   # for completed() after the run
    if batch:
        extra.update(events_upto=batch["upto"], events_count=len(events),
                     events_held=[{"id": h["id"]} for h in batch["held"]])
        log(f"job '{name}' triggered by {len(events)} event(s) (seq {events[0]['seq']}..{events[-1]['seq']})")
    if job.get("background"):
        log(f"running job '{name}' (background): {job['command']}")
        spawn_background(name, job["command"], job_log_path, now, today, env, extra, timeout_of(job))
        running.add(name)  # jobs with after: on it in the same tick must wait too
        return
    log(f"running job '{name}': {job['command']}")
    with open(job_log_path, "a") as job_log:
        rc = run_bounded(job["command"], job_log, timeout_of(job), env)
    if not completed(job, rc):
        log(f"job '{name}' FAILED (exit {rc}), will retry next tick")
        job_failed(name, rc, f"log:.logs/{job_log_path.name}", job.get("project"), chain, depth)
        return
    state[name] = today
    if "every_minutes" in job:
        ran_at(state, name, now.isoformat(timespec="seconds"), now.timestamp())
    if batch:
        consumed(state, name, batch["upto"], len(events), now.isoformat(timespec="seconds"))
        held_back(state, name, batch["held"], today)
    save_state(state)
    mark_job(name, False)
    log(finished_line(name, rc))


def tick() -> None:
    jobs, info = all_jobs(log_errors=True)
    broken = dict(info.get("broken") or {})
    ALIAS.clear()
    ALIAS.update(info["alias"])

    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    state = load_state(jobs, now, today)
    alert_broken_state(now)
    by_name = {j["name"]: j for j in jobs}
    merged: list[pathlib.Path] = []
    running = reconcile(state, by_name, merged)
    # jobs just switched from schedule.yaml to the registry: move their state over from the old name, or they'd rerun
    # today (after reconcile: the old name's background run may have just finished and its result only now merged)
    for new, old in info["renames"].items():
        for suffix in ("", ".attempts", ".last_ok", ".last_ok_at", ".event_seq"):
            if old + suffix in state and new + suffix not in state:
                state[new + suffix] = state[old + suffix]
        if old in running:
            running.add(new)  # the old name is still running in the background: the new name counts as running too
    for job in jobs:   # a renamed project's job whose old name still runs in the background: not a second copy
        short = job["name"].split("/", 1)[-1]
        if job.get("project") and any(f"{old}/{short}" in running for old in hubconf.former_names(job["project"])):
            running.add(job["name"])
        try:
            adopt_state(job, state)
        except Exception as e:   # what its old name left can't be used: the job's own turn goes on without it
            log(f"job '{job['name']}': the state of its former name can't be used ({e!r})")
    save_state(state)
    for f in merged:
        f.unlink(missing_ok=True)

    for job in jobs:
        name = job["name"]
        try:
            due = turn(job, by_name, state, running, now, today)
        except Exception as e:   # this job alone: its fields or its record can't be used
            log(f"job '{name}' FAILED (exit -1), whether it is due can't be told: {e!r}")
            broken[name] = told(job.get("project") or SCHEDULE_YAML, _("whether it is due can't be told ({error})",
                                                                       error=type(e).__name__))
            continue
        if due is None:
            continue
        state[f"{name}.attempts"] = {"date": today, "count": due["tried"] + 1}
        # save before running: if the scheduler is killed mid-job (reboot, OOM), the attempt is already
        # recorded, so max_attempts_per_day can hold.
        save_state(state)
        heartbeat()
        try:
            run_job(job, due["batch"], state, running, now, today)
        except StateNotSaved:
            raise
        except Exception as e:   # a run that couldn't be started or logged is this job's failure, not the tick's
            log(f"job '{name}' FAILED (exit -1), will retry next tick: {e!r}")
            job_failed(name, -1, repr(e)[:300], job.get("project"), [name])
        heartbeat()

    save_state(state)
    alert_failing()
    alert_broken(broken)


def record_run(name: str, code: int) -> None:
    """A manual run's result (`schedulectl run`, which starts the job outside a tick): it clears or sets the job's
    failing mark as a scheduled run's would, so a job fixed and run by hand doesn't stay "failing" until its next
    scheduled day (a monthly job: weeks of daily alerts). Only the mark; the day's run mark is schedulectl's. A job
    that declares found_exit is looked up, so its FOUND counts as completed here too."""
    job = next((j for j in all_jobs()[0] if j.get("name") == name), {}) if code == FOUND else {}
    mark_job(name, not completed(job, code), f"exit {code} (run by hand)")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--record-run"] and len(sys.argv) == 4:
        if hubconf.worktree_lookup():
            sys.exit(_("scheduler: {root} is a linked worktree of the hub; nothing recorded", root=hubconf.ROOT))
        try:
            record_run(sys.argv[2], int(sys.argv[3]))
        except ValueError:
            sys.exit(_("scheduler --record-run <job> <exit code>"))
        sys.exit(0)
    main()
