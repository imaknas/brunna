"""What a run billed per use may spend: the day's budget, held before the run starts.

For a runtime whose login is billed per use and which says what a run cost (REPORTS_COST): today that is the owner's own API
key (runtime.login: key). Everything else is as it was: a plan's login has no budget, and a provider's calls keep theirs
(completion.run).

- {settings} llm.budget.daily_usd is the day's limit; none set = no such run starts.
- Each run reserves its point's cap before it starts (reserve: one at a time, under a lock): what is spent today, what is
  reserved and this cap must fit the budget. The run itself is stopped at its cap by its tool (Task.max_cost_usd). Its real
  cost replaces the reservation when it ends; a run that ends without one, or is killed, counts as its cap.
- A cap is its group's, or {settings} llm.budget.caps.<point>.
- The nightly tidy-up comes first: until it has run today, other work may use only part of the budget (KEEP).
- A point whose run was stopped at its cap isn't started again for the same project for BACKOFF_DAYS: it would be paid for and
  thrown away every night.
- blocked(): why a run couldn't start now, asked by callers before they take a turn, a letter or an event for it.
The tool's own estimate, checked between requests: a run can pass its cap by one request. The hard limit is the one the owner
sets at the provider.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import usage_log  # noqa: E402

_ = i18n.domain(__file__)

CAPS = {"fast": 0.25, "standard": 1.00, "strong": 4.00}               # an agent run, by its group
ANSWER_CAPS = {"fast": 0.10, "standard": 0.50, "strong": 2.00}        # a completion
KEEP = 0.4            # of the budget, kept for the tidy-up until it has run today
TIDY_UP = ("librarian-state", "librarian-state-edits", "librarian-todo-format", "librarian-todo-format-edits", "librarian-index",
           "librarian-index-edits")
BACKOFF_DAYS = 14
LOCK_WAIT_S = 10
PROJECT_WORK = ("agent-job", "agent-job-confined", "agent-job-event", "project-agent")   # what a project asked for itself


def log() -> pathlib.Path:
    import completion
    return completion.USAGE


def budget() -> float | None:
    llm = hubconf.get("llm")
    b = llm.get("budget") if isinstance(llm, dict) else None
    v = b.get("daily_usd") if isinstance(b, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def held(runtime: str, mode: str) -> bool:
    """Whether runs of this runtime on this login are held to the budget."""
    import runtime_ports
    if runtime not in runtime_ports.RUNTIMES or "auth" not in runtime_ports.capabilities(runtime):
        return False
    mod = runtime_ports._module(runtime)
    return bool(getattr(mod, "REPORTS_COST", False)) and mod.billing(mode, {}, True) == "metered"


def cap(point: str, spec: dict, runtime: str, group: str | None = None, model: str | None = None) -> float:
    """The most one run of the point may cost. model: what it runs, when the owner pinned one: its own group's cap."""
    llm = hubconf.get("llm")
    own = ((llm.get("budget") or {}) if isinstance(llm, dict) else {}).get("caps") or {}
    v = own.get(point) if isinstance(own, dict) else None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    import runtime_ports
    table = ANSWER_CAPS if spec.get("shape") == "completion" else CAPS
    group = group or spec.get("model_group")
    if model and runtime in runtime_ports.RUNTIMES:
        mine = [g for g, m in runtime_ports.models(runtime).items() if m == model and g in table]
        if mine and group not in mine:
            group = max(mine, key=table.get)
    return table.get(group, table["standard"])


# ---------- the day ----------

def _today() -> str:
    return datetime.date.today().isoformat()


def spent(day: str | None = None) -> tuple[float, float, list[dict]]:
    """(settled today, reserved now, today's rows). The open runs are looked at before the rows and again after, until nothing
    moved in between: a run being settled is in one or the other. Raises OSError when it can't be read."""
    f = log()
    usage_log.sweep(f)
    for _try in range(50):
        before, reserved = _reserved(f)
        if f.exists():
            with f.open("rb"):   # unreadable is said, never nothing
                pass
        rows = usage_log.rows(f, day or _today())
        if _reserved(f)[0] == before:
            done = sum(usage_log.number(r.get("cost_usd")) for r in rows if r.get("billing", "metered") == "metered")
            return done, reserved, rows
        time.sleep(0.02)
    raise OSError("the open runs keep changing")


def _reserved(f: pathlib.Path) -> tuple[list[str], float]:
    """(the open runs' files, what is held for them)."""
    folder = usage_log.open_dir(f)
    if not folder.exists():
        return [], 0.0
    for _try in range(50):
        seen = sorted(p.name for p in folder.iterdir() if p.suffix in (".json", ".claim") and not p.name.startswith(".") and not p.is_symlink())
        total = 0.0
        try:
            for name in seen:
                try:
                    row = json.loads((folder / name).read_text(errors="replace"))
                except (ValueError, RecursionError):
                    continue
                if isinstance(row, dict):
                    total += usage_log.number((row.get("final") or {}).get("cost_usd") if isinstance(row.get("final"), dict) else row.get("reserved_usd"))
            return seen, total
        except FileNotFoundError:   # it moved while it was read: again
            time.sleep(0.02)
    raise OSError("the open runs keep changing")


def tidied(rows: list[dict]) -> bool:
    """Whether the tidy-up has run today (or is off here): only then is all of the budget everybody's. From the scheduler's
    state; what can't be told keeps nothing back."""
    import llm_ready
    name = f"{hubconf.hub_project()}/librarian"
    try:
        if llm_ready.librarian() is not None:
            return True
        state = json.loads((hubconf.STATE / "scheduler-last-run.json").read_text())
        if name not in state:
            return True   # it has never run here: not scheduled
        return state.get(name) == _today()
    except Exception:   # noqa: BLE001
        return True


def room(point: str, amount: float, more: float = 0.0) -> str | None:
    """Why a run of this cap doesn't fit today's budget, or None. more: what is about to be held. Raises OSError (spent)."""
    limit = budget()
    if limit is None:
        return _("no daily budget is set ({settings} llm.budget.daily_usd), and runs here are billed per use")
    done, reserved, rows = spent()
    free = limit - done - reserved - more
    if point not in TIDY_UP and not tidied(rows):
        free -= limit * KEEP
    if amount > free + 1e-9:
        return _("today's budget (llm.budget.daily_usd ${budget}) has ${free} left for this, and {point} may cost ${cap}", budget=limit,
                 free=f"{max(free, 0):.2f}", point=point, cap=f"{amount:.2f}")
    return None


# ---------- before a run ----------

def allowed(point: str, project: str | None, mode: str) -> str | None:
    """Why a project's own work isn't run on a login billed per use, or None: the owner allows that per project."""
    if point not in PROJECT_WORK or not project or project == hubconf.hub_project() or mode == "subscription":
        return None
    import registry
    doc = registry.refresh()["projects"].get(project)
    if doc and doc.get("metered_models") is True:
        return None
    return _("{point} for {project} would be paid per use, and the owner has not allowed that for {project} (registry "
             "metered_models); not run", point=point, project=project)


def login_missing(runtime: str, mode: str) -> str | None:
    import runtime_ports
    env, why = runtime_ports._module(runtime).auth_env(mode, {})
    return why if env is None else None


def terms(point: str, tier: str | None = None, auth: str | None = None) -> tuple[str, str, float] | None:
    """(the runtime, the login, the cap) of a run of this point where runs of that login are held; None where they aren't (or it
    can't be told: the run's own check says)."""
    import llm_ready
    import runtime_ports
    try:
        spec = llm_ready.declaration(point)
        pin = ((hubconf.get("llm") or {}).get("points") or {}).get(point)
        pin = pin if isinstance(pin, dict) else {}
        runtime = pin.get("runtime") or spec.get("runtime") or runtime_ports.default_name()
        if spec.get("shape") == "completion":
            import completion
            if completion.routed(point, spec):
                return None
        mode = auth or spec.get("auth") or runtime_ports.default_login(runtime)
        return (runtime, mode, cap(point, spec, runtime, tier, pin.get("model"))) if held(runtime, mode) else None
    except Exception:   # noqa: BLE001
        return None


def never(budget_usd: float) -> list[tuple[str, float]]:
    """(point, what a day's budget has to be over) for the work that is on here whose cap a budget of this much never leaves room for."""
    import registry
    on = set(registry.enabled_modules())
    out = []
    for point in ("librarian-state", "mailbox-escalation", "agent-job", *(p for m, p in (("consolidate", "consolidate"), ("reflect", "reflect-week"))
                                                                          if m in on)):
        if (t := terms(point)):
            least = t[2] if point in TIDY_UP else t[2] / (1 - KEEP)
            if least > budget_usd + 1e-9:
                out.append((point, least))
    return out


def lasting(point: str, project: str | None = None, tier: str | None = None, auth: str | None = None, what: str | None = None,
            tidy: bool = False) -> bool:
    """Whether a run of this point is held up by something that doesn't pass today by itself: a job that waits for it needn't wait
    on. Not a budget that is only in use right now, nor the tidy-up's share, except when the tidy-up is what waits (tidy)."""
    if (t := terms(point, tier, auth)) is None:
        return False
    runtime, mode, amount = t
    try:
        if login_missing(runtime, mode) or allowed(point, project, mode) or stopped(point, what or project, amount):
            return True
        limit = budget()
        if limit is None or amount > limit:
            return True
        done, _reserved_now, rows = spent()
        kept = limit * KEEP if tidy and point not in TIDY_UP and not tidied(rows) else 0.0
        return amount > limit - done - kept + 1e-9
    except Exception:   # noqa: BLE001
        return False


def blocked(point: str, project: str | None = None, tier: str | None = None, auth: str | None = None, what: str | None = None,
            more: float = 0.0) -> str | None:
    """Why a run of this point couldn't start now (no login, no budget, not enough of it left, stopped at its cap lately, not
    allowed for the project), or None. Always None for a login that isn't held. what: the job it is (its own backoff). more:
    what the caller has started just now, not held yet. Never raises."""
    if (t := terms(point, tier, auth)) is None:
        return None
    runtime, mode, amount = t
    try:
        return login_missing(runtime, mode) or allowed(point, project, mode) or stopped(point, what or project, amount) or room(point, amount, more)
    except Exception as e:   # noqa: BLE001 (it can't be told: the run's own check says)
        return _("what today's runs cost can't be told: {e}", e=type(e).__name__)


@contextlib.contextmanager
def _lock():
    d = usage_log.open_dir(log())
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        end = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    raise OSError("the spending lock is held") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def reserve(point: str, provider: str, model: str | None, amount: float, project: str | None = None) -> tuple[str | None, str]:
    """Take amount of today's budget for a run and open its usage row: (its id, "") or (None, why it doesn't start)."""
    try:
        with _lock():
            if (why := room(point, amount)):
                return None, why
            rid = usage_log.begin(log(), point, provider, model, "metered", project, reserved=amount)
            return (rid, "") if rid else (None, _("what this run would cost can't be recorded; not run"))
    except OSError as e:
        return None, _("what today's runs cost can't be told: {e}", e=e)


# ---------- stopped at its cap ----------

def _stops() -> pathlib.Path:
    return hubconf.STATE / "llm-stopped"


def _stop_file(point: str, project: str | None) -> pathlib.Path:
    return _stops() / hashlib.sha256(f"{point}\0{project or ''}".encode("utf-8", "surrogatepass")).hexdigest()[:32]


def note_stop(point: str, what: str | None, amount: float) -> None:
    """A run of the point was stopped at its cap: not again for a while (backoff). Only where running it again would throw away
    as much: the tidy-up and the fold, which start over, and a job that events start, which would get the same ones."""
    if not backoff(point):
        return
    try:
        _stops().mkdir(parents=True, exist_ok=True)
        _stop_file(point, what).write_text(json.dumps({"point": point, "what": what, "cap": amount, "at": usage_log.now()}))
    except OSError:
        pass


def backoff(point: str) -> int:
    return BACKOFF_DAYS if point in (*TIDY_UP, "consolidate") else 1 if point == "agent-job-event" else 0


def stopped(point: str, project: str | None, amount: float) -> str | None:
    try:
        f = _stop_file(point, project)
        age = time.time() - f.stat().st_mtime
        was = usage_log.number(json.loads(f.read_text()).get("cap"))
    except (OSError, ValueError, AttributeError):
        return None
    if age > backoff(point) * 86400 or amount > was:
        return None
    return _("{point} was stopped at its cap for {project}; it is tried again after {days} days, or when its cap is raised "
             "({settings} llm.budget.caps.{point})", point=point, project=project or "-", days=backoff(point))


# ---------- what waits ----------

def _waiting_file() -> pathlib.Path:
    return hubconf.STATE / "llm-waiting.json"


def note_waiting(who: str, why: str | None) -> bool:
    """What waits because of the day's budget or the login (a job, the letters), for the status page and doctor; why None: it
    doesn't any more. Whether what is said changed (amounts aside), for a caller's own log."""
    f = _waiting_file()
    try:
        d = json.loads(f.read_text())
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    was = d.get(who)
    if why is None:
        if was is None:
            return False
        d.pop(who)
    else:
        d[who] = {"why": why, "at": usage_log.now(), "since": was.get("since") if isinstance(was, dict) else usage_log.now()}
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False))
        os.replace(tmp, f)
    except OSError:
        pass
    return (was is None) != (why is None)


def waiting() -> dict[str, dict]:
    """{who: {why, since}} of what was said to wait in the last two hours."""
    try:
        d = json.loads(_waiting_file().read_text())
    except (OSError, ValueError):
        return {}
    since = (datetime.datetime.now().astimezone() - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    return {k: v for k, v in d.items() if isinstance(v, dict) and str(v.get("at", "")) >= since} if isinstance(d, dict) else {}


# ---------- a login the service refuses ----------

def login_told(runtime: str, refused: str | None) -> None:
    """A run on a login billed per use that the service itself refused (refused: its answer in fixed words; no credit left, the
    key revoked): one alert, over when a run answers. The hourly check can't see it: it makes no request that costs."""
    f = hubconf.STATE / "llm-login-refused"
    if not refused and not f.exists():
        return
    try:
        import alert
        if refused:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(usage_log.now())
        alert.once("llm-login", [_("{runtime}'s runs are refused by the service ({why}): check the key and its credit", runtime=runtime, why=refused)]
                   if refused else [], _("{runtime}'s runs answer again", runtime=runtime), ids=["refused"] if refused else [])
        if not refused:
            f.unlink(missing_ok=True)
    except Exception as e:   # noqa: BLE001 (telling never changes the run)
        print(_("the alert could not be sent: {e}", e=type(e).__name__), file=sys.stderr)
