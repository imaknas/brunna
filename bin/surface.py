#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""brunna surface --runtime <name> --session <native id> --cwd <folder> [--json]: what brunna shows and offers inside one
coding agent session (docs/design/agent-surface.md, contract contracts/session-surface.v1.schema.json). For a surface
adapter (the Claude Code mod in integrations/claude-code/), which draws only what this answers and composes no wording.

Whose session: the native id is qualified here (runtime_ports.qualify) and everything is decided from the session's own
record (the one its hooks write) and the registry, never from the environment this runs in. A session with no record is
not served: the platform's own runs and headless sessions record none. The folder only says whether the session is in a
project at all (hubbus.project_of, which reads the .git pointer files by path and runs no git or script of the folder).

It reads and writes nothing: the bus opened read-only (no schema run, no presence, no telling, no holding), the session's
record, the status page's last tick (glance). No lock; exit 0 whenever it could answer, problems in the answer.

served: false carries why_not (not_a_project, not_recorded, not_served: a runtime the platform can't confine in a
sandboxed project, no_bus_access: the platform's state can't be read from here) and nothing else, except gated (this
session's tool version failed a safety check in a sandboxed project: the platform tells it no letters), which keeps the
owner's part and the lines. lines hold counts and states only, never a letter's sender, a to-do's text or an alert.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import glance  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import runtime_ports  # noqa: E402

_ = i18n.domain(__file__)
_c = i18n.domain(__file__, content=True)
WHY_NOT = ("not_a_project", "not_recorded", "gated", "not_served", "no_bus_access")


class Unreadable(Exception):
    """The platform's state is there but can't be read from here (a sandbox that denies it)."""


def _exists(path: pathlib.Path) -> bool:
    """Whether a file is there; Unreadable when it can't be told (a denied stat is not a missing file)."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        raise Unreadable(str(path)) from e
    return True


def held_show_min(hb) -> int:
    """How long a letter this session holds goes unhandled before the band shows it: half the hold's window
    (hubbus TAKE_TIMEOUT_MIN), so the owner sees it before it passes to another session."""
    return max(1, hb.TAKE_TIMEOUT_MIN // 2)


def _live(hb, session, c):
    """hubbus.Live, minus its log line when the session lookup fails: this command writes nothing."""
    class Live(hb.Live):
        def load(self):
            if self.live is not None or self.failed:
                return
            try:
                self.rt = self.rt or hb.sessions_port()
                self.live = self.rt.live_sessions()
            except Exception:
                self.failed = True
    return Live(me=session, c=c)


def mail(hb, project: str, session: str) -> dict:
    """held: the letters told to this session that it still holds, open and not taken; waiting: how many a hook would
    tell it next (hubbus.hook_shows). Ids, senders and types only."""
    db = hb.DB
    if not _exists(db):
        return {"held": [], "waiting": 0}
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("SELECT 1 FROM events LIMIT 1")
    except sqlite3.Error as e:
        raise Unreadable(str(db)) from e
    try:
        live = _live(hb, session, c)
        rows = hb.inbox(c, project, session, live=live)
        held = []
        for r in rows:
            if not hb.actionable(r):
                continue
            st = hb.letter_state(c, r, live)
            if st.held and st.holder == session and not st.taken:
                held.append({"id": r["id"], "from": r["source"], "type": hb.payload(r).get("type") or "",
                             "since": st.route.ts if st.route else r["ts"]})
        waiting = sum(1 for r in rows if hb.hook_shows(c, r, session, live))
    finally:
        c.close()
    return {"held": held, "waiting": waiting}


def open_letters(hb, project: str) -> int:
    """The requests and questions still open for the project, whoever holds them (a gated session's line)."""
    db = hb.DB
    if not _exists(db):
        return 0
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            return sum(1 for r in hb.addressed_to(c, project) if hb.actionable(r))
        finally:
            c.close()
    except sqlite3.Error as e:
        raise Unreadable(str(db)) from e


def labels() -> dict:
    """The words an adapter draws around the data (its pane, its button), in the interface language, so it composes none."""
    return {"open": _("needs you"), "title": _("brunna: needs you"), "empty": _("Nothing needs you."),
            "refresh": _("refresh"), "todo": _("to-do"), "question": _("question"), "ask": _("ask here"),
            "copy": _("copy"), "health": _("Platform health"),
            "read_only": _("Settled on {assistant}'s side; shown here only."),
            "unreadable": _("What needs you could not be read here.")}


def _when(ts: str) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(ts).astimezone()
    except (TypeError, ValueError):
        return datetime.datetime.now().astimezone()   # an unreadable time counts as now: not shown as late


def lines(owner: dict, held: list, hold_min: int, now: datetime.datetime, gated_open: int | None = None) -> list[str]:
    """The band's text, in the interface language: only what needs the owner, as counts and states."""
    parts = []
    if owner["needs_you"]:
        parts.append(_("{n} {n:needs|need} you", n=owner["needs_you"]))
    if owner["health"] == "problem":
        parts.append(_("the platform has problems"))
    late = [h for h in held if now - _when(h["since"]) >= datetime.timedelta(minutes=hold_min)]
    if late:
        parts.append(_("{n} {n:letter|letters} held here for over {min} min, not handled yet", n=len(late), min=hold_min))
    if gated_open:
        parts.append(_("{n} {n:letter waits|letters wait} for another session: this session's version failed the "
                       "platform's sandbox check", n=gated_open))
    return [_("brunna · {line}", line=parts[0]), *parts[1:]] if parts else []


def commands(inbox: bool) -> list[dict]:
    out = [{"name": "inbox", "summary": _("Read and handle this project's letters"),
            "prompt": _c("Run `brunna inbox` and handle the letters as your rules say.")}] if inbox else []
    return out + [{"name": "brunna", "summary": _("What needs you, and the platform's health")}]


def surface(runtime: str, native: str, cwd: str) -> dict:
    if not hubconf.config_path().exists():
        return {"v": 1, "set_up": False}
    hb = glance._hubbus()

    def not_served(why: str) -> dict:
        return {"v": 1, "set_up": True, "served": False, "why_not": why}

    try:
        found = hb.project_of(pathlib.Path(cwd)) if cwd else None
    except OSError:
        found = None
    if not found:
        return not_served("not_a_project")
    session = runtime_ports.qualify(runtime, native) if runtime in runtime_ports.RUNTIMES else None
    f = hubconf.session_file(session, runtime_ports.SESSION_RECORD) if session else None
    try:
        _exists(hb.DB)
        rec = runtime_ports._read_record(f) if f and _exists(f) else None
    except Unreadable:
        return not_served("no_bus_access")
    if not rec or rec["runtime"] != runtime:
        return not_served("not_recorded")
    named = rec.get("project")
    project = hb.now_named(named) if isinstance(named, str) and named else found
    if hb.outboxed(project) and not set(runtime_ports.CONFINED_SESSION_PORTS) <= runtime_ports.capabilities(runtime):
        return not_served("not_served")   # as hub-event's CAGED_NEEDS: its hooks do nothing there either
    now = datetime.datetime.now().astimezone()
    own = glance.owner()
    try:
        if runtime_ports.gated(session, rec):
            return {**not_served("gated"), "project": project, "owner": own, "labels": labels(),
                    "lines": lines(own, [], held_show_min(hb), now, gated_open=open_letters(hb, project)),
                    "commands": commands(inbox=False)}
        got = mail(hb, project, session)
    except Unreadable:
        return not_served("no_bus_access")
    return {"v": 1, "set_up": True, "served": True, "why_not": None, "project": project, "owner": own,
            "mail": got, "labels": labels(), "lines": lines(own, got["held"], held_show_min(hb), now),
            "commands": commands(inbox=True)}


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(_(__doc__).strip())
        return 0
    args, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--runtime", "--session", "--cwd") and i + 1 < len(argv):
            args[a[2:]] = argv[i + 1]
            i += 2
            continue
        rest.append(a)
        i += 1
    if set(rest) - {"--json"} or set(args) != {"runtime", "session", "cwd"}:
        print(_("usage: brunna surface --runtime <name> --session <id> --cwd <folder> [--json]"), file=sys.stderr)
        return 2
    out = surface(args["runtime"], args["session"], args["cwd"])
    if "--json" in rest:
        import json
        print(json.dumps(out, ensure_ascii=False, indent=1))
    elif out.get("lines"):
        print("\n".join(out["lines"]))
    elif out.get("why_not"):
        print(_("not served here ({why})", why=out["why_not"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
