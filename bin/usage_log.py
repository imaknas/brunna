"""What the model runs the platform starts used: one row per run in <state>/llm-usage.jsonl, whatever the point, the
runtime and the login (docs/design/llm-providers.md, "Cost and quota").

- A run is opened before it starts (begin: a file of its own in llm-open/, beside the log) and settled after it (settle: its
  row is written and the file goes). A run killed in between leaves its file: the next begin, and whatever sums up the log,
  writes the row for it (stop "no-answer", with no cost, or what was reserved for it).
- A row: ts, point, provider, model, input_tokens, output_tokens, cost_usd, stop, billing, and project when it was for one.
  billing: metered (pay-per-use, the cost is a charge) | subscription and unknown (the cost is what the tool says it would
  cost, not a charge) | local. cost_usd is the run's own: a runtime whose tool reports a whole conversation's total takes
  off what it stood at before (runtime_ports: session_cost).
- Not here: the owner's own sessions, woken ones included (the platform doesn't start them), and a run the platform
  refused before it started.

The file is completion.USAGE (tests point it elsewhere); every function takes it. A row that can't be written never fails a run.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import pathlib
import secrets


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def open_dir(log: pathlib.Path) -> pathlib.Path:
    return log.parent / "llm-open"


FAULTS = (OSError, ValueError, TypeError, RecursionError, OverflowError)   # what writing or reading a row can raise (bad text and odd values too)


def append(log: pathlib.Path, row: dict) -> bool:
    try:
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a+b") as f:
            if f.seek(0, os.SEEK_END) > 0:   # a row cut short by a full disk doesn't take this one with it
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    line = b"\n" + line
            f.write(line)
    except FAULTS:
        return False
    return True


def begin(log: pathlib.Path, point: str, provider: str, model: str | None, billing: str, project: str | None = None,
          reserved: float | None = None, gated: bool = False) -> str | None:
    """Open a run: its id, for settle. None when it could not be written (the run's row is then written whole at its end).
    gated: one whose cost must be told (a pay-per-use run at a strength its project chose: cost_unknown when it can't be)."""
    sweep(log)
    rid = secrets.token_hex(8)
    row = {"ts": now(), "pid": os.getpid(), "point": point, "provider": provider, "model": model, "billing": billing}
    if project:
        row["project"] = project
    if reserved is not None:
        row["reserved_usd"] = reserved
    if gated:
        row["gated"] = True
    try:
        _write(open_dir(log) / f"{rid}.json", row)
    except FAULTS:
        return None
    return rid


def _write(f: pathlib.Path, row: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.tmp")
    tmp.write_text(json.dumps(row, ensure_ascii=False))
    os.replace(tmp, f)


def _claim(log: pathlib.Path, rid: str) -> pathlib.Path | None:
    """Take an open run for writing its row: one process does (a rename). None when it is open no more."""
    return _take(open_dir(log) / f"{rid}.json")


def _take(f: pathlib.Path) -> pathlib.Path | None:
    """The file renamed to a claim of this moment (its time is what says a claim is somebody's: CLAIM_S), by one process."""
    claimed = f.with_name(f"{secrets.token_hex(8)}.claim")
    try:
        os.utime(f)
        os.rename(f, claimed)
    except OSError:
        return None
    return claimed


def _finish(log: pathlib.Path, claimed: pathlib.Path, row: dict) -> None:
    """The row of a claimed run; when it can't be written now, kept for a later sweep."""
    if not append(log, row):
        try:
            _write(claimed.with_suffix(".json"), {"final": row})
        except FAULTS:
            return
    try:
        claimed.unlink()
    except OSError:
        pass


def settle(log: pathlib.Path, rid: str | None, row: dict) -> None:
    """The run ended: its row, and it is open no more. A run already settled (a sweep took it for dead) isn't written twice."""
    if not rid:
        append(log, row)
        return
    if (claimed := _claim(log, rid)) is not None:
        _finish(log, claimed, row)


def drop(log: pathlib.Path, rid: str | None) -> None:
    """The run never started: no row."""
    if rid and (claimed := _claim(log, rid)) is not None:
        try:
            claimed.unlink()
        except OSError:
            pass


def alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    return True


DEAD_AFTER_S = 2 * 86400   # no run takes this long: its pid is another process's now
CLAIM_S = 60               # a claim this old is of a process killed while it wrote the row


def sweep(log: pathlib.Path, over: bool = False) -> None:
    """Runs whose process is gone without settling: their rows, so a killed run still counts. over: every open run is over,
    whatever its process (a check's world whose processes were just killed)."""
    try:
        files = sorted(open_dir(log).iterdir())
    except OSError:
        return
    for f in files:
        try:
            if f.name.startswith(".") or f.suffix not in (".json", ".claim") or f.is_symlink() or not f.is_file():
                continue
            age = datetime.datetime.now().timestamp() - f.stat().st_mtime
            row = json.loads(f.read_text(errors="replace"))
            if not isinstance(row, dict):
                row = {}
            if f.suffix == ".claim":
                if age < CLAIM_S and not over:
                    continue
            elif not over and "final" not in row and alive(row.get("pid")) and age < DEAD_AFTER_S:
                continue
            claimed = _take(f)
        except FAULTS:
            continue
        if claimed is not None:
            _finish(log, claimed, row["final"] if isinstance(row.get("final"), dict) else _killed(row))


def _killed(row: dict) -> dict:
    reserved = row.get("reserved_usd")
    return ({k: row[k] for k in ("ts", "point", "provider", "model", "billing", "project") if k in row}
            | {"input_tokens": None, "output_tokens": None, "cost_usd": reserved, "stop": "no-answer"}
            | ({"cost_assumed": True} if reserved is not None else {})
            | ({"cost_unknown": True} if reserved is None and row.get("billing") == "metered" and row.get("gated") else {}))


def rows(log: pathlib.Path, since: str = "") -> list[dict]:
    """The rows from since on (an ISO local time or day; ts compares as text). A line that isn't a row is skipped."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(r, dict) and str(r.get("ts", "")) >= since:
            out.append(r)
    return out


FIELDS = ("ts", "point", "provider", "model", "input_tokens", "output_tokens", "cost_usd", "stop", "billing", "cost_assumed")


def _when(ts) -> str:
    """A row's time, when it is one and not in the future: a day's sum takes its rows by it."""
    try:
        at = datetime.datetime.fromisoformat(str(ts)).astimezone()
        if at <= datetime.datetime.now().astimezone():
            return at.isoformat(timespec="seconds")
    except FAULTS:
        pass
    return now()


def adopt(log: pathlib.Path, other: pathlib.Path, prefix: str, over: bool = False) -> None:
    """Another state folder's rows (a conformance check's throwaway world, an eval's own) into this log, their points under
    prefix: the runs were real. Its dead runs are swept first (over: all of them); its file is taken whole, so two processes
    adopting it write each row once."""
    try:
        if other.resolve() == log.resolve():
            return
        # looked at before anything is opened or swept: the folder may be one a tool under check wrote
        folder = open_dir(other)
        if folder.is_symlink() or other.is_symlink() or (other.exists() and not other.is_file()):
            return
        if folder.is_dir():
            sweep(other, over)
        if not other.is_file():
            return
        taken = other.with_name(f"{other.name}.{secrets.token_hex(4)}")
        os.rename(other, taken)
    except FAULTS:
        return
    if all([append(log, {k: r[k] for k in FIELDS if k in r} | {"point": f"{prefix}/{r.get('point')}", "ts": _when(r.get("ts"))})
            for r in rows(taken)]):
        try:
            taken.unlink()
        except OSError:
            pass


def number(x) -> float:
    try:
        return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else 0.0
    except OverflowError:
        return 0.0


def work(row: dict) -> str:
    """The kind of work a row is for: its point, with a project's own calls (<project>/<name>), the checks of a version
    (conformance/<check>/…) and the evals (eval/…) each as one."""
    point = str(row.get("point") or "?")
    head = point.split("/", 1)[0]
    if head in ("conformance", "eval") and "/" in point:
        return head
    return "project-calls" if "/" in point else point


def summary(found: list[dict], key) -> dict:
    """key(row) → {runs, input_tokens, output_tokens, charged, estimate, unknown}: charged = metered rows' cost; estimate =
    what the tool says the others would cost (None when none said); unknown = runs that gave no cost at all."""
    out: dict = {}
    for r in found:
        v = out.setdefault(key(r), {"runs": 0, "input_tokens": 0, "output_tokens": 0, "charged": None, "estimate": None, "unknown": 0})
        v["runs"] += 1
        v["input_tokens"] += int(number(r.get("input_tokens")))
        v["output_tokens"] += int(number(r.get("output_tokens")))
        if r.get("billing") == "local":
            continue
        if r.get("cost_usd") is None:
            v["unknown"] += 1
            continue
        # rows from before billing was written are all subscription (no metered provider was set up then)
        k = "charged" if r.get("billing") == "metered" else "estimate"
        v[k] = (v[k] or 0.0) + number(r.get("cost_usd"))
    return out
