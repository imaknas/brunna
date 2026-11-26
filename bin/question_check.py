#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""question-check: a question for the owner, read as the owner will read it, before it is sent
(docs/design/owner-questions.md, part 1).

What the model reads: only what the owner will see (the question's fields: text, context, options, default, see), the
project's name and registry purpose, and the project's open questions as labels q1… (the newest OPEN_MAX, each cut to
OPEN_CHARS; never an id). Built here, never by the asker: a sandboxed project sends the fields to the capability runner,
which calls check() itself. All of it goes in as one JSON block marked as data.

What comes back is used only as quotes of those fields, fixed kinds and labels (verdict): a quote that isn't a span of
what was sent is dropped, and every word the asker reads is the platform's, so nothing the model writes reaches it.

    check(project, fields, open_qs, session) → Verdict   count the day's checks, ask, decide; never raises
    via_runner(ws, fields, session) → Verdict           a sandboxed session's: the capability runner checks it

A question is never lost to the check: no answer (no model, the point off, a refusal of the budget, a timeout, an
error, an answer outside the schema) or the project's DAILY checks used up → sent unchecked, said in fixed words.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402

_ = i18n.domain(__file__)

POINT = "question-check"
DAILY = 20            # checks per project per day; past it, sent unchecked
OPEN_MAX = 30         # the newest open questions shown to the model
OPEN_CHARS = 200      # of each
FIELD_CHARS = 4000    # of any one field
MAX_ITEMS = 20        # options, see, replaces
MIN_QUOTE = 2         # a quote shorter than this is no span of the question (one character at a time would spell anything)
MIN_DECISION = 8      # what a decision quote must be at least
MAX_LINES = 6         # defect lines told at most
ABSENT = ("no_work", "no_choice", "no_detail")       # a part that isn't there: no quote
SPAN = ("unexplained", "no_consequence")              # a span of the question: quoted
KEEP_DAYS = 2         # what the count file keeps


@dataclasses.dataclass
class Verdict:
    """sent: whether the question goes; lines: what the asker is told (the platform's words, with quotes of its own
    question); repeats: ids of open questions the model says it asks again; unchecked: why it wasn't checked (None when
    it was); decisions: the spans that ask to decide (a task's check says only whether it has one)."""
    sent: bool
    lines: list[str] = dataclasses.field(default_factory=list)
    repeats: list[str] = dataclasses.field(default_factory=list)
    unchecked: str | None = None
    decisions: list[str] = dataclasses.field(default_factory=list)


# ---------- the fields ----------

def fields(text, context=None, options=None, default=None, see=None, replaces=None) -> dict:
    """The question's fields as the check reads them: strings cut to FIELD_CHARS, options as [{id, label}]."""
    cut = lambda s: str(s or "")[:FIELD_CHARS]   # noqa: E731
    opts = []
    for o in (options or [])[:MAX_ITEMS]:
        if isinstance(o, dict):
            opts.append({"id": cut(o.get("id")), "label": cut(o.get("label"))})
        elif isinstance(o, (list, tuple)) and len(o) == 2:
            opts.append({"id": cut(o[0]), "label": cut(o[1])})
    return {"text": cut(text), "context": cut(context), "options": opts, "default": cut(default),
            "see": [cut(s) for s in (see or [])][:MAX_ITEMS], "replaces": [cut(r) for r in (replaces or [])][:MAX_ITEMS]}


def _said(f: dict) -> str:
    """Every span a quote may come from, joined: the question as the owner sees it."""
    return "\n".join([f["text"], f["context"], f["default"], *(f"{o['id']} {o['label']}" for o in f["options"]),
                      *f["see"]])


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# the double ones (curly, low, guillemets, CJK corner brackets) and the single ones, as plain quotation marks
QUOTE_MARKS = str.maketrans({c: '"' for c in "\u201c\u201d\u201e\u00ab\u00bb\u300c\u300d\u300e\u300f"}
                            | {c: "'" for c in "\u2018\u2019\u201a"})


def _fold(s: str) -> str:
    """For telling whether a quote is the question's: spaces, case and the kind of quotation mark aside."""
    return _norm(s).translate(QUOTE_MARKS).casefold()


def quoted(q, f: dict, least: int = MIN_QUOTE) -> str | None:
    """q when it is a span of the question (spaces, case and quotation marks aside) at least least characters long,
    else None."""
    if not isinstance(q, str) or len(_norm(q)) < least:
        return None
    return _norm(q)[:300] if _fold(q) in _fold(_said(f)) else None


def _unique(xs) -> list:
    return list(dict.fromkeys(x for x in xs if x))


# ---------- the prompt ----------

def label(i: int) -> str:
    return f"q{i + 1}"


def prompt(project: str, purpose: str | None, f: dict, open_qs: list[tuple[str, str]]) -> str:
    """The point's prompt: the platform's template with the data as one JSON block (every < written \\u003c, so no tag
    closes). open_qs: (id, text) of this project's open questions, oldest first; the newest OPEN_MAX go in, by label."""
    import prompts
    shown = open_qs[-OPEN_MAX:]
    data = {"project": {"name": project, "description": str(purpose or "")[:500]},
            "question": {k: f[k] for k in ("text", "context", "options", "default", "see")},
            "open": [{"label": label(i), "text": _norm(t)[:OPEN_CHARS]} for i, (_id, t) in enumerate(shown)]}
    block = json.dumps(data, ensure_ascii=False, indent=1).replace("<", "\\u003c")
    return prompts.render(POINT, project=project, data=block)


# ---------- the answer ----------

def verdict(got, f: dict, open_qs: list[tuple[str, str]]) -> Verdict:
    """The platform's decision from the model's answer (None: no answer). Refused when nothing asks to decide, when a
    defect holds, or when it repeats an open question --replaces doesn't name; sent otherwise, with a note when part of
    it is only to look at."""
    if not isinstance(got, dict) or not all(isinstance(got.get(k), list) for k in ("decisions", "reviews", "defects", "repeats")):
        return Verdict(True, [_("not checked: the check gave no usable answer")], unchecked="no answer")
    decisions = _unique(quoted(x, f, MIN_DECISION) for x in got["decisions"])
    reviews = _unique(quoted(x, f, MIN_DECISION) for x in got["reviews"])
    shown = open_qs[-OPEN_MAX:]
    ids = {label(i): qid for i, (qid, _t) in enumerate(shown)}
    repeats = []
    for lab in got["repeats"]:
        if isinstance(lab, str) and lab in ids and ids[lab] not in repeats and ids[lab] not in f["replaces"]:
            repeats.append(ids[lab])
    lines, seen = [], set()
    for d in got["defects"]:
        if not isinstance(d, dict) or d.get("kind") not in (*ABSENT, *SPAN):
            continue
        kind, q = d["kind"], quoted(d.get("quote"), f) if d["kind"] in SPAN else None
        if kind in SPAN and not q:
            continue   # a span defect has to point at the question's own words
        if (kind, q) in seen:
            continue
        seen.add((kind, q))
        if len(lines) < MAX_LINES:
            lines.append(defect_line(kind, q))
    if not decisions:
        lines.insert(0, _("It asks nothing to decide. What the owner should only look at goes on the review list "
                          "(`brunna send --to {alias} --type review`), or in your notes.", alias=hubconf.owner_alias()))
    if repeats:
        lines.append(_("It asks again what {ids} asked: add `--replaces {ids}`, which closes what it replaces, or "
                       "`--as-is \"why\"` saying why it is new.", ids=" ".join(repeats)))
    if lines:
        return Verdict(False, lines, repeats, decisions=decisions)
    note = [_("Sent. Part of it is only for the owner to look at ({quote}): that belongs on the review list "
              "(`brunna send --to {alias} --type review`).", quote=reviews[0], alias=hubconf.owner_alias())] if reviews else []
    return Verdict(True, note, decisions=decisions)


def defect_line(kind: str, q: str | None) -> str:
    return {
        "no_work": _("Nothing says what the work is or why it needs the owner: add --context, in plain words."),
        "no_choice": _("Nothing says what exactly is to be decided: say it in --text."),
        "no_detail": _("It needs more than it says and points nowhere: name the file with the detail (--see <path>)."),
        "unexplained": _("\"{quote}\" is never explained, and the owner can't know it: say what it means.", quote=q),
        "no_consequence": _("What \"{quote}\" leads to isn't said: say it (in the option's label, or in --context).",
                            quote=q),
    }[kind]


# ---------- the day's count ----------

def _dir(make: bool = False) -> pathlib.Path:
    """Under the secrets directory, which no sandbox reads or writes: a sandboxed project's checks are counted by the
    runner, never by the project."""
    d = hubconf.secrets_dir() / "question-check"
    if make:
        d.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        d.mkdir(mode=0o700, exist_ok=True)
    return d


@contextlib.contextmanager
def _counts():
    """The count file, read and written under its lock: {day: {project: n}, "refused": {session hash: day}}. What isn't
    of that shape is dropped."""
    d = _dir(make=True)
    with open(d / "lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        f = d / "counts.json"
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        data = {k: v for k, v in data.items() if isinstance(v, dict)
                and all(isinstance(x, (int, str)) and not isinstance(x, bool) for x in v.values())}
        yield data
        days = sorted(k for k in data if re.fullmatch(r"\d{4}-\d{2}-\d{2}", k))
        for k in days[:-KEEP_DAYS]:
            data.pop(k, None)
        today = datetime.date.today().isoformat()
        data["refused"] = {k: v for k, v in (data.get("refused") or {}).items() if v == today}
        tmp = d / f".counts.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, sort_keys=True))
        os.replace(tmp, f)


def _session_key(session) -> str:
    return hashlib.sha256(str(session).encode()).hexdigest()[:16]


def left(project: str) -> bool:
    """Whether the project has a check left today (nothing counted: the runner refuses a request past it at once)."""
    try:
        with _counts() as data:
            return data.get(datetime.date.today().isoformat(), {}).get(project, 0) < DAILY
    except OSError:
        return True


def take(project: str) -> bool:
    """One of the project's checks for today, if any is left."""
    today = datetime.date.today().isoformat()
    with _counts() as data:
        day = data.setdefault(today, {})
        if day.get(project, 0) >= DAILY:
            return False
        day[project] = day.get(project, 0) + 1
        return True


def refused_before(session) -> bool:
    """Whether this session had a question refused today; marks it as having had one."""
    if not session:
        return False
    k, today = _session_key(session), datetime.date.today().isoformat()
    with _counts() as data:
        before = (data.get("refused") or {}).get(k) == today
        data.setdefault("refused", {})[k] = today
        return before


# ---------- the check ----------

def off() -> str | None:
    """Why the check can't run here (None when it can): questions.check off, or no model for the point."""
    if (hubconf.get("questions") or {}).get("check") == "off":
        return _("the check is off here (questions.check)")
    try:
        import completion
        import llm
        import runtime_ports
        tgt = llm.completion_target(POINT, llm.points()["points"][POINT])
        if tgt.get("impl") in runtime_ports.RUNTIMES and (why := runtime_ports.no_checked_copy(tgt["impl"])):
            return why
    except completion.Refused as e:
        return str(e)
    except Exception as e:   # noqa: BLE001  a broken settings file or registry: off, with the type only
        return type(e).__name__
    return None


def ask(text: str):
    """The model's answer (llm.answer: prints and publishes nothing), or None."""
    import llm
    try:
        return llm.answer(POINT, text)
    except Exception:   # noqa: BLE001
        return None


def check(project: str, f: dict, open_qs: list[tuple[str, str]], session=None, mark: bool = True) -> Verdict:
    """Count, ask, decide. Never raises: a question is never lost to the check. mark: a refusal counts toward the
    session's "refused again" (a question's; a task's check only says whether it asks to decide)."""
    try:
        return _check(project, f, open_qs, session, mark)
    except Exception as e:   # noqa: BLE001
        return Verdict(True, [_("not checked: {why}", why=type(e).__name__)], unchecked="error")


def _check(project: str, f: dict, open_qs: list[tuple[str, str]], session, mark: bool) -> Verdict:
    if (why := off()):
        return Verdict(True, [_("not checked: {why}", why=why)], unchecked="off")
    try:
        if not take(project):
            return Verdict(True, [_("not checked: {project} has had its {n} checks today", project=project, n=DAILY)],
                           unchecked="daily")
    except OSError as e:
        return Verdict(True, [_("not checked: {why}", why=type(e).__name__)], unchecked="state")
    purpose = (hubconf.projects().get(project) or {}).get("purpose")
    v = verdict(ask(prompt(project, purpose, f, open_qs)), f, open_qs)
    if v.unchecked is None and (hubconf.get("questions") or {}).get("check") == "advise" and not v.sent:
        v = Verdict(True, [*v.lines, _("(questions.check is advise here: sent as it is)")], v.repeats,
                    decisions=v.decisions)
    if not v.sent and mark:
        try:
            again = refused_before(session)
        except OSError:
            again = False
        if again:
            v.lines.insert(0, _("Refused again. If it is clear to the owner as it is, send it with --as-is \"why\"."))
    return v


RUNNER_WAIT_S = 75   # a sandboxed session waits this long for the runner's answer, then sends unchecked


def via_runner(ws: pathlib.Path, f: dict, session=None, wait_s: float = RUNNER_WAIT_S, mark: bool = True) -> Verdict:
    """A sandboxed session's check: a request of kind question-check in its own folder's .hub-run, answered by the
    capability runner outside the sandbox (which builds the prompt and counts). No answer in time, or no runner: sent
    unchecked, and said so."""
    import secrets
    import time
    job_id = f"q{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    req = {"v": 1, "id": job_id, "kind": "question-check", "fields": f, "mark": mark,
           **({"session": session} if session else {})}
    try:
        d = ws / ".hub-run" / "requests"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{job_id}.tmp"
        tmp.write_text(json.dumps(req, ensure_ascii=False))
        tmp.rename(d / f"{job_id}.json")
    except OSError as e:
        return Verdict(True, [_("not checked: {why}", why=type(e).__name__)], unchecked="runner")
    job, t0 = ws / ".hub-run" / "jobs" / job_id, time.time()
    while time.time() - t0 < wait_s:
        try:
            st = json.loads((job / "state.json").read_text())
        except (OSError, ValueError):
            st = None
        if st and st.get("state") == "rejected":
            return Verdict(True, [_("not checked: {why}", why=str(st.get("reason") or "")[:300])], unchecked="runner")
        if st and st.get("state") in ("done", "lost"):
            try:
                got = json.loads((job / "stdout").read_text()) if st.get("state") == "done" and st.get("exit") == 0 else None
            except (OSError, ValueError):
                got = None
            if isinstance(got, dict) and isinstance(got.get("sent"), bool):
                keep = {k: got[k] for k in ("lines", "repeats", "decisions") if isinstance(got.get(k), list)}
                return Verdict(got["sent"], unchecked=got.get("unchecked") if isinstance(got.get("unchecked"), str) else None,
                               **{k: [str(x) for x in v] for k, v in keep.items()})
            break
        time.sleep(0.5)
    else:
        try:
            (ws / ".hub-run" / "requests" / f"{job_id}.json").unlink()   # nobody took it: the runner won't later
        except OSError:
            pass
        return Verdict(True, [_("not checked: the platform's run service didn't answer in {s} s", s=int(wait_s))],
                       unchecked="runner")
    return Verdict(True, [_("not checked: the check gave no usable answer")], unchecked="no answer")


def explain(v: Verdict, command: list[str]) -> str:
    """What a refused send prints: why, then the command as it was sent, ready to edit."""
    import shlex
    out = [_("Not sent: the owner, reading it later without this session's context, couldn't answer it as written."),
           *(f"- {line}" for line in v.lines), "", _("Fix it and send again, or add --as-is \"why\" to send it as it is "
                                                      "(through MCP: as_is). As it was sent:"),
           "  " + " ".join(shlex.quote(c) for c in command)]
    return "\n".join(out)
