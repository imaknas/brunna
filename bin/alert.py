"""Send alerts / notifications ({settings} alerts). There can be several channels; whichever are set up get the message
(docs/design/tool-agnostic.md: not tied to Discord):
  discord   discord_token_file + discord_channel_file (files with the bot token and channel id, in the secrets
            directory, not in version control)
  ntfy      {"topic": …, "base_url": default https://ntfy.sh, "token_file": optional} (the phone app receives it,
            no account needed)
  email     {"host", "port": default 587, "user", "from", "to", "password_file", "ssl": false = STARTTLS}
  macos     true (this machine's system notifications; seen only by someone sitting at this machine)
  assistant true (the personal assistant's own reminders and push, its platform-alerts contract (modules/assistant.yaml), at the
            address and with the token of hub.json assistant. Only for alert.once: it needs the problem's key. It is
            never the only way out: when the assistant is down, nothing sent to it arrives)

Standard library + the system's curl only (no Homebrew). The scheduler watchdog deliberately has its own copy and
does not import this (it must still be able to send when the scheduler and the platform code are both broken).

  alert.send(text) -> bool
  alert.once(key, problems, ok_text, ids=None) -> bool (delivered or told before; a check's alert.FOUND needs it)
                                               new problems are sent at once; unresolved ones are repeated daily (after 23
                                               hours); one recovery when resolved, to the keyed channels only (the
                                               assistant closes its item; nothing pushed, 11-18). A problem is told apart by
                                               its wording, or by its ids[i] when given (one stable name per problem, so
                                               a text that carries a changing detail, an error's tail or a count, is
                                               still the same problem and is not sent again as new). Routed first
                                               (route(key)): a problem the platform's agents can fix is a letter to that
                                               project or brunna, and reaches the owner only a day unsettled
  alert.probe() -> bool                         whether the token still works (GET /users/@me, no message sent;
                                               check-secrets calls it daily)

A failure to send does not pass silently (2026-10-24): recorded in .state/alerts-failed.json, the status page turns
red, and it is tried again next time; cleared once a send succeeds. With no channel set up at all there is nothing that
failed: nothing is recorded, and the status page shows one setup reminder instead (docs/design/new-user.md 2: a feature
that is off does not turn the page red).

Its state files never decide whether an alert goes out. Each is synced and renamed into place, with a copy beside it;
one that can't be read goes on from the copy, else from alerts-log.jsonl (what was told stays told), else from nothing.
One that can't be written (a full disk) doesn't keep anything from being sent, nor from being remembered as sent: an
empty file beside it (alerts-told.…, its name which problem, its date when) needs no space, and goes once the state can be
written again.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import smtplib
import subprocess
import sys
import urllib.parse

import hubconf
import names  # noqa: E402
import i18n
import tick_record

_ = i18n.domain(__file__)

SENT = hubconf.STATE / "alerts-sent.json"      # key -> {"problems": [...], "ids": [...] when given, "at": when last sent}
FAILED = hubconf.STATE / "alerts-failed.json"  # key -> {"at": …, "text": first line of the one not sent}
LOG = hubconf.STATE / "alerts-log.jsonl"       # one line per notice tried: {"at", "key", "state", "sent"}; never rewritten
REMIND = datetime.timedelta(hours=23)   # "every 24 hours": a daily check that starts a little earlier still reminds
# a check job's exit when it ran and everything it found is with whoever must hear it (alert-routing.md step 3): the
# scheduler takes it as a completed run, for a job that declares found_exit: true
FOUND = 4
API = "https://discord.com/api/v10"
_LIVE = (SENT, FAILED)
_LIVE_LOG = LOG
_REAL = (subprocess.run, smtplib.SMTP, smtplib.SMTP_SSL)   # what reaches the outside: curl and osascript, and mail


def _reaches_out(fn) -> bool:
    """A test about to reach a real channel (11-14: one with its own state files but this machine's assistant channel
    pushed "broken" to the owner's phone ten times): it is not sent, whatever the test replaced around it. A test that
    replaced the sending itself (subprocess.run, smtplib) is not stopped."""
    if "unittest" in sys.modules and fn in _REAL:
        print("alert: not sent, a test reached a real channel (replace the channel or alert.once)", file=sys.stderr)
        return True
    return False


def _held() -> bool:
    """A test that reaches an alert without its own state files would tell the owner for real, from the live instance a
    worktree shares (11-14: two memory-sync tests sent a project called proj to Discord): nothing is sent or recorded.
    With no channel set up (the gate's fresh instance) there is no one to reach, and nothing changes."""
    if "unittest" in sys.modules and (SENT, FAILED) == _LIVE and configured():
        print("alert: held, a test on the live instance's alert state (patch alert.SENT/FAILED or alert.once)",
              file=sys.stderr)
        return True
    return False


def _creds() -> tuple[str, str] | None:
    try:
        return (hubconf.p("alerts.discord_token_file").read_text().strip(),
                hubconf.p("alerts.discord_channel_file").read_text().strip())
    except (OSError, AttributeError):
        return None


def _curl(*args: str, data: str | None = None) -> str:
    """Returns the HTTP status code (a string); an empty string when curl itself fails."""
    if _reaches_out(subprocess.run):
        return ""
    try:
        r = subprocess.run(["/usr/bin/curl", "-sS", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}", *args],
                           input=data, capture_output=True, text=True)
    except OSError:   # no curl on this machine (a slim Linux): not sent, recorded as such, never the caller's crash
        return ""
    return r.stdout.strip()


def _discord(text: str) -> bool:
    c = _creds()
    if not c:
        return False
    token, channel = c
    return _curl("-H", f"Authorization: Bot {token}", "-H", "Content-Type: application/json", "--data-binary", "@-",
                 f"{API}/channels/{channel}/messages", data=json.dumps({"content": text[:1990]})) == "200"


def _ntfy(text: str) -> bool:
    conf = hubconf.get("alerts.ntfy") or {}
    if not conf.get("topic"):
        return False
    headers = ["-H", f"Title: {names.current('product')}"]
    if conf.get("token_file"):
        try:
            headers += ["-H", f"Authorization: Bearer {hubconf.path(conf['token_file']).read_text().strip()}"]
        except OSError:
            return False
    url = f"{(conf.get('base_url') or 'https://ntfy.sh').rstrip('/')}/{conf['topic']}"
    return _curl(*headers, "--data-binary", "@-", url, data=text[:4000]) == "200"


def _email(text: str) -> bool:
    from email.message import EmailMessage
    conf = hubconf.get("alerts.email") or {}
    if not conf.get("host") or not conf.get("to"):
        return False
    msg = EmailMessage()
    msg["Subject"] = f"{names.current('product')}:" + text.splitlines()[0][:80] if text else names.current("product")
    msg["From"], msg["To"] = conf.get("from") or conf.get("user") or names.current("product"), conf["to"]
    msg.set_content(text)
    try:
        password = hubconf.path(conf["password_file"]).read_text().strip() if conf.get("password_file") else None
        cls = smtplib.SMTP_SSL if conf.get("ssl") else smtplib.SMTP
        if _reaches_out(cls):
            return False
        with cls(conf["host"], int(conf.get("port") or (465 if conf.get("ssl") else 587)), timeout=20) as s:
            if not conf.get("ssl"):
                s.starttls()
            if conf.get("user") and password:
                s.login(conf["user"], password)
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        return False


def _macos(text: str) -> bool:
    if not hubconf.get("alerts.macos"):
        return False
    if _reaches_out(subprocess.run):
        return False
    script = f"display notification {json.dumps(text[:200])} with title \"{names.current('product')}\""
    return subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True).returncode == 0


CHANNELS = {"discord": (_discord, lambda: _creds() is not None), "ntfy": (_ntfy, lambda: bool(hubconf.get("alerts.ntfy"))),
            "email": (_email, lambda: bool(hubconf.get("alerts.email"))), "macos": (_macos, lambda: bool(hubconf.get("alerts.macos")))}


def _assistant_on() -> bool:
    return bool(hubconf.get("alerts.assistant") and hubconf.get("assistant.api"))


def _assistant(notice: dict) -> bool:
    """One notice about a keyed problem to the assistant. Only a 200 means it has it; anything else is tried again, with
    the same `at`, which the assistant takes as the same notice."""
    try:
        token = hubconf.p("assistant.ingest_token").read_text().strip()
    except (OSError, AttributeError):
        return False
    u = urllib.parse.urlsplit(hubconf.get("assistant.api"))
    body = json.dumps({"v": 1, "source": hubconf.hub_project(), **notice}, ensure_ascii=False)
    return _curl("-X", "POST", "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
                 "--data-binary", "@-", f"{u.scheme}://{u.netloc}/api/alerts", data=body) == "200"


# channels that take a notice with a key (once), not free text (send)
KEYED = {"assistant": (_assistant, _assistant_on)}


def configured() -> list[str]:
    return [n for n, (_send, on) in (*CHANNELS.items(), *KEYED.items()) if on()]


def send(text: str, notice: dict | None = None, plain: bool = True) -> bool:
    """Send on every channel set up; success only when all of them went out (one failing is recorded as a failure, and
    the status page turns red). None set up = False. notice: for the channels that need a key; without one they are
    skipped. plain=False: only the keyed channels (a recovery: the assistant closes its item; nothing is pushed to the
    owner's phone or inbox for it). With plain=False and no keyed channel set up, there is nothing to send: True."""
    if _held():
        return False
    results = []
    for n in configured():   # every channel is tried: a broken one must not silence the rest
        if n in KEYED:
            if notice:
                results.append(KEYED[n][0](notice))
        elif plain:
            results.append(CHANNELS[n][0](text))
    if not plain and not results and configured():
        return True
    return bool(results) and all(results)


def _log(key: str, state: str, ok: bool, letter: str | None = None) -> None:
    """One line per notice tried, kept (how many reached the owner, of which kind, and when: what tells whether the
    alerts got quieter). A log that can't be written changes nothing; a test never writes the live one."""
    if ("unittest" in sys.modules and LOG == _LIVE_LOG) or not configured():   # no channel: retried every tick, nothing told
        return
    try:
        with LOG.open("a") as f:
            f.write(json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"), "key": key, "state": state,
                                "sent": ok, **({"letter": letter} if letter else {})}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def probe() -> bool:
    """Whether the alert channels themselves work: Discord calls /users/@me (no message sent); other channels have no
    check that sends nothing, so being set up counts as working. No channel at all = False."""
    chans = configured()
    if not chans:
        return False
    if "discord" in chans:
        c = _creds()
        return _curl("-H", f"Authorization: Bot {c[0]}", f"{API}/users/@me") == "200"
    return True


def _copy(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(f"{path.stem}.copy.json")


def _load(path) -> dict:
    """The state. One that can't be read goes on from the copy _save keeps beside it, when that is of the same save (a
    version from before the copy writes the file alone); SENT without one from the log."""
    try:
        return _dict(path)
    except FileNotFoundError:   # nothing yet, or cleared on purpose
        return {}
    except (OSError, ValueError):
        pass
    try:
        if abs(path.stat().st_mtime - _copy(path).stat().st_mtime) <= SAME_SAVE_S:
            return _dict(_copy(path))
    except (OSError, ValueError):
        pass
    return _told_by_log() if path == SENT else {}


SAME_SAVE_S = 60


def _dict(path: pathlib.Path) -> dict:
    d = json.loads(path.read_text())
    if not isinstance(d, dict):
        raise ValueError(path.name)
    return d


def _save(path, data: dict) -> bool:
    """Whether it was written; never raises: what was sent was sent, whatever happens to the record of it."""
    text = json.dumps(data, ensure_ascii=False, indent=1)
    try:
        tick_record.save(path, text)
        tick_record.save(_copy(path), text)
    except OSError as e:
        try:
            print(f"alert: {path.name} can't be written ({e})", file=sys.stderr)
        except OSError:
            pass
        return False
    return True


def _told_by_log() -> dict:
    """SENT as the log has it: the keys whose last notice was a problem, told at that time. Which problem the log doesn't
    say: "unknown", taken as the one there is now (once), so what was told is not told again all at once."""
    if "unittest" in sys.modules and LOG == _LIVE_LOG:
        return {}
    out: dict[str, dict] = {}
    try:
        lines = LOG.read_text(errors="replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            rec = json.loads(line)
            key, at, state = rec["key"], rec["at"], rec["state"]
            datetime.datetime.fromisoformat(at)
            if not rec["sent"] or not isinstance(key, str):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if state in ("resolved", "letter-resolved"):
            out.pop(key, None)
        elif state in ("letter", "letter-update"):
            was = out.get(key) or {}
            out[key] = {"unknown": True, "problems": ["?"], "at": at, "to": route(key), "opened": was.get("opened") or at,
                        "letter": rec["letter"] if isinstance(rec.get("letter"), str) else was.get("letter")}
        else:
            out[key] = {"unknown": True, "problems": ["?"], "at": at,
                        **({"escalated": True, "to": route(key)} if route(key) != "owner" else {})}
    return out


MARK = "alerts-told."   # a marker's name: MARK, the key, its problem's hash, "owner"/"escalated"/a letter's time, dots between


def _hash(ident: list) -> str:
    return hashlib.sha256(json.dumps(ident, ensure_ascii=False).encode()).hexdigest()[:16]


def _mark_key(key: str) -> str:
    """key as it stands in a marker's name; one too long for a file name as its start and a hash."""
    q = urllib.parse.quote(key, safe="")
    return q if len(q) <= 150 else f"{q[:100]}%-{_hash([key])}"


def _markers(key: str | None = None) -> list[tuple[str, str, str, pathlib.Path, float]]:
    """(key, hash, kind, file, its date) of the notices told while SENT couldn't be written; a key's newest first."""
    out = []
    for p in SENT.parent.glob(MARK + "*"):
        parts = p.name[len(MARK):].rsplit(".", 2)
        if len(parts) == 3 and (key is None or parts[0] == _mark_key(key)):
            try:
                out.append((urllib.parse.unquote(parts[0]), parts[1], parts[2], p, p.stat().st_mtime))
            except OSError:
                continue
    return sorted(out, key=lambda m: m[4], reverse=True)


def _letter_id(key: str, opened: str) -> str:
    return f"alert-{''.join(c if c.isascii() and c.isalnum() else '-' for c in key)[:60]}-{opened}"


def _record(key: str, record: dict) -> None:
    """Remember what was just told about key: in SENT (read again: sending takes a while, and other runs write it too), else
    as a marker."""
    old = [m[3] for m in _markers(key)]
    if not _save(SENT, {**_load(SENT), key: record}):
        opened = str(record.get("letter") or "")[-14:]
        kind = "escalated" if record.get("escalated") else opened if record.get("to") and opened.isdigit() else "owner"
        mark = SENT.with_name(f"{MARK}{_mark_key(key)}.{_hash(sorted(record.get('ids', record['problems'])))}.{kind}")
        try:
            mark.touch()
            at = datetime.datetime.fromisoformat(record["at"]).timestamp()
            os.utime(mark, (at, at))
        except OSError:   # not even that: told again at the next run, rather than never
            return
        old = [p for p in old if p != mark]
    for p in old:
        try:
            p.unlink()
        except OSError:
            pass


def _time(rec: dict, key: str) -> str | None:
    """A time in a record (local, as all of them are); None when it has none or it can't be read."""
    try:
        t = datetime.datetime.fromisoformat(rec[key])
        return rec[key] if t.tzinfo is None else t.astimezone().replace(tzinfo=None).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def _last(key: str, problems: list[str], ids: list[str] | None, ident: list[str]) -> dict | None:
    """What was last told about key: SENT's record, or the marker that stands for it; None when nothing was. A field that
    can't be used is left out."""
    last = _load(SENT).get(key)
    if isinstance(last, list):   # old format: only the list of problems
        last = {"problems": last, "at": None}
    if not isinstance(last, dict):
        last = None
    marks = _markers(key)
    # SENT written after the marker (by a version that doesn't know them): that is the newer word
    if marks and last and (said := _time(last, "at")) and datetime.datetime.fromisoformat(said).timestamp() > marks[0][4] + 1:
        for m in marks:
            try:
                m[3].unlink()
            except OSError:   # it stays void by its date
                pass
        marks = []
    now_rec = {"problems": sorted(problems), **({"ids": ident} if ids is not None else {})}
    if marks:
        _key, said, kind, _path, when = marks[0]
        last = {"at": datetime.datetime.fromtimestamp(when).isoformat(timespec="seconds"),
                **(now_rec if said == _hash(ident) else {"problems": [] if said == _hash([]) else ["?"]})}
        if kind == "escalated":
            last |= {"escalated": True, "to": route(key)}
        elif kind != "owner":
            try:
                opened = datetime.datetime.strptime(kind, "%Y%m%d%H%M%S").isoformat(timespec="seconds")
                last |= {"to": route(key), "letter": _letter_id(key, kind), "opened": opened}
            except ValueError:
                pass
        _record(key, last)   # SENT may be writable again: then the marker goes
    elif last and last.get("unknown"):
        last = {k: v for k, v in last.items() if k != "unknown"} | (now_rec if problems else {})
        if problems:
            _record(key, last)
    if last is None:
        return None
    for k in ("problems", "ids"):
        if k in last and not (isinstance(last[k], list) and all(isinstance(x, str) for x in last[k])):
            del last[k]
    last.setdefault("problems", [])
    return {**last, "at": _time(last, "at"), **({"opened": _time(last, "opened")} if "opened" in last else {})}


def record_failure(key: str, text: str, notice: dict | None = None) -> None:
    if _held() or not configured():   # no channel: not a failed send (the setup reminder covers it)
        return
    failed = _load(FAILED)
    failed[key] = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "text": text.splitlines()[0][:200],
                   **({"notice": notice} if notice else {})}
    _save(FAILED, failed)


def clear_failure(key: str) -> None:
    failed = _load(FAILED)
    if failed.pop(key, None) is not None:
        _save(FAILED, failed)


def failures() -> dict:
    """Alerts that could not be sent and have not been resent successfully yet (for the status page). With no channel
    set up, none: what was recorded before applied to channels that are gone."""
    return {k: v for k, v in _load(FAILED).items() if isinstance(v, dict)} if configured() else {}


ESCALATE = datetime.timedelta(hours=24)   # an agent's letter not settled by then: the owner hears of it, once
# keys whose problem the platform's agents can fix: a letter to brunna, not the owner (docs/design/alert-routing.md).
# Anything not here stays the owner's: logins, credentials, safety refusals, what watches the mailbox itself, and every
# key a new caller makes until it is listed.
PLATFORM_KEYS = ("runtime-held-", "runtime-checks-", "runtime-install-", "runtime-latest-", "runtime-daily-",
                 "runtime-job-", "session-version", "session-unusable-", "completion-tools:", "claude-git-filter")
# not here on purpose: git-fence (what it finds in a project's git is a security finding) and every safety refusal
# (memory-sync-refused, memory-copy-refused, tool-servers): the owner's
JOB_KEY = "scheduler-job:"
BROKEN_KEY = "scheduler-broken:"   # + a project (or module) with a job the scheduler can't use
# the platform's jobs that keep the mailbox running: their failure can't go through it
OWNER_JOBS = ("hubbus-bridge",)


def route(key: str) -> str:
    """Who is told first: "owner", or the project whose agents can fix it (the platform's own: hubconf.hub_project()). The
    owner whenever that can't be told: an alert about what it is told from must not be lost on it."""
    try:
        return _route(key)
    except Exception:
        return "owner"


def _route(key: str) -> str:
    # the owner's own file; a registry that can't be read is what says whose the rest is
    if key in (BROKEN_KEY + "(schedule.yaml)", BROKEN_KEY + "(registry)"):
        return "owner"
    if key.startswith((JOB_KEY, BROKEN_KEY)):
        job = key[len(JOB_KEY if key.startswith(JOB_KEY) else BROKEN_KEY):]
        project, _sep, name = job.partition("/")
        if name in OWNER_JOBS:
            return "owner"
        docs = hubconf.projects()
        if project in docs and project != hubconf.hub_project() and docs[project].get("type") != "platform":
            return project
        return hubconf.hub_project()   # brunna's own jobs and the modules'
    if key.startswith(PLATFORM_KEYS):
        return hubconf.hub_project()
    return "owner"


ALREADY_CLOSED = "already closed"   # hubbus's refusal to take back a letter its recipient closed (run in English)


def _hubbus(*args: str, out: list[str] | None = None) -> bool:
    """One mailbox call as the platform (from the hub's folder, in English so a refusal can be read). A test never
    reaches the real bus."""
    if _reaches_out(subprocess.run):
        return False
    env = dict(os.environ)
    names.env_set(env, "LANG_UI", "en")
    try:
        r = subprocess.run([sys.executable, str(hubconf.ROOT / "bin" / "hubbus"), *args], cwd=hubconf.ROOT,
                           capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    if out is not None:
        out.append(r.stdout + r.stderr)
    return r.returncode == 0


def _letter_text(to: str, problems: list[str]) -> str:
    return (_("The platform found a problem for {project} to fix (an alert; the owner is not told yet):", project=to)
            + "\n" + "\n".join(f"- {p}" for p in problems) + "\n"
            + _("When it's fixed the platform sees it and closes this letter itself. Not fixed within a day, it goes to "
                "the owner."))


def _withdraw(letter: str | None) -> bool:
    """Take the letter back; True also when it is closed already (its recipient handled it: nothing to take back)."""
    if not letter:
        return True
    out: list[str] = []
    ok = _hubbus("done", letter, "--withdraw", "--note", _("fixed: the platform no longer sees the problem"), out=out)
    return ok or ALREADY_CLOSED in "".join(out)


MAX_UPDATES = 3   # of what a letter lists, in its thread: a notice starts no run, and a project that renames its jobs stops


def _more(key: str, to: str, problems: list[str], last: dict, letter: str, names: list[str], now: datetime.datetime) -> None:
    """What was added to the names a letter lists, and what is gone from them: a notice in its thread, never a new letter."""
    had, done = last.get("names"), last.get("updates", 0)
    if not isinstance(had, list) or not isinstance(done, int) or done >= MAX_UPDATES or sorted(names) == sorted(map(str, had)):
        return

    def some(xs: set) -> str:
        return ", ".join([str(x)[:60] for x in sorted(xs, key=str)[:10]] + (["…"] if len(xs) > 10 else []))
    added, gone = set(names) - set(had), set(had) - set(names)
    text = [_("This letter's problem has changed:")] + ([_("new in it: {names}", names=some(added))] if added else []) \
        + ([_("no longer in it: {names}", names=some(gone))] if gone else []) + [_("now: {problem}", problem=p) for p in problems]
    ok = _hubbus("send", "--from", hubconf.hub_project(), "--to", to, "--type", "info", "--reply-to", letter,
                 "--text", "\n".join(text))
    _log(key, "letter-update", ok, letter)
    if ok:
        _record(key, {**last, "problems": sorted(problems), "names": sorted(names), "updates": done + 1})


def _agent(key: str, to: str, problems: list[str], ids, ident, last_ident, last: dict | None,
           now: datetime.datetime, names: list[str] | None = None) -> bool:
    """alert.once for a problem the platform's agents fix: a request letter to that project instead of the owner's
    channels; changes go to its thread as info (no new request: one open letter per key, a project's dispatch budget is
    small); a day unsettled → the owner, once, and from then on as any owner alert; fixed → the letter is taken back.
    Returns False when the owner must be told instead (the letter couldn't be sent, or it is time to escalate)."""
    last = last or {}
    letter = last.get("letter") if last.get("problems") else None
    if ident == last_ident:
        if not problems:
            clear_failure(key)
            return True
        opened = last.get("opened")
        if opened and now - datetime.datetime.fromisoformat(opened) >= ESCALATE:
            return False
        if names is not None and letter:
            _more(key, to, problems, last, letter, names, now)
        return True
    record = {"problems": sorted(problems), **({"ids": ident} if ids is not None else {}),
              "at": now.isoformat(timespec="seconds"), "to": to,
              **({"names": sorted(names)} if names is not None and problems else {})}
    if problems and letter and last.get("opened") \
            and now - datetime.datetime.fromisoformat(last["opened"]) >= ESCALATE:
        return False   # a list that keeps changing is still unsettled: a day on, the owner
    if problems and letter:
        ok = _hubbus("send", "--from", hubconf.hub_project(), "--to", to, "--type", "info", "--reply-to", letter,
                     "--text", _letter_text(to, problems))
        state = "letter-update"
        record |= {"letter": letter, "opened": last.get("opened")}
    elif problems:
        letter = _letter_id(key, f"{now:%Y%m%d%H%M%S}")
        ok = _hubbus("send", "--from", hubconf.hub_project(), "--to", to, "--type", "request", "--id", letter,
                     "--text", _letter_text(to, problems))
        state = "letter"
        record |= {"letter": letter, "opened": record["at"]}
    else:
        # taken back; one its recipient already closed can't be, and is gone either way. Any other failure: tried again
        # next run (the record keeps its problems until it goes)
        ok, state = _withdraw(last.get("letter")), "letter-resolved"
    _log(key, state, ok, letter if problems else None)
    if not ok:
        return state != "letter"   # a failed update or take-back is tried again; a letter that never went: the owner
    _record(key, record)
    clear_failure(key)
    return True


def open_keys(prefix: str) -> list[str]:
    """The keys under this prefix whose problems were last sent and not resolved since (a caller that splits one alert
    into many finds the ones it must close)."""
    keys = {k for k, v in _load(SENT).items() if isinstance(v, dict) and v.get("problems")}
    for key, said, _kind, _path, _when in reversed(_markers()):   # told while SENT couldn't be written: newer than it
        keys = keys - {key} if said == _hash([]) else keys | {key}
    return sorted(k for k in keys if k.startswith(prefix))


def once(key: str, problems: list[str], ok_text: str, ids: list[str] | None = None, names: list[str] | None = None) -> bool:
    """Send what changed about one alert. Returns whether these problems are with whoever must hear them: sent now, or
    told before (open and unchanged, a reminder included, or a letter that stands). False when they couldn't be
    delivered (no channel, a failed send, a test's held state): a check then exits as broken, not with FOUND.
    names: what one problem lists when its id stays the same whatever that is (a project's jobs): while it is with a
    project's agents, what is added to them and what is gone is said in its letter's thread, MAX_UPDATES times."""
    if ids is not None and len(ids) != len(problems):
        raise ValueError(f"alert.once({key!r}): {len(ids)} ids for {len(problems)} problems")
    if _held():
        return False
    now = datetime.datetime.now()
    # what the problems are: their ids when the caller names them, else their wording (a record from before ids
    # compares by wording once, then carries them)
    ident = sorted(ids) if ids is not None else sorted(problems)
    last = _last(key, problems, ids, ident)
    last_ident = sorted((last or {}).get("ids") if ids is not None and "ids" in (last or {})
                        else (last or {}).get("problems", []))
    to = route(key)
    # the owner's already: escalated, or told before routing (a record with problems and no "to")
    escalated = bool((last or {}).get("escalated")) or bool(to != "owner" and (last or {}).get("problems")
                                                             and "to" not in (last or {}))
    if to != "owner" and not escalated:
        if _agent(key, to, problems, ids, ident, last_ident, last, now, names):
            return True
        # the owner is told: the letter couldn't go, or a day passed unsettled. From now on it is the owner's alert
        escalated = True
        last_ident = []   # what follows sends it as new
    if ident == last_ident:
        if not problems:
            clear_failure(key)  # the earlier one was not sent and the problem has fixed itself: nothing to resend
            return True
        at = (last or {}).get("at")
        if at and now - datetime.datetime.fromisoformat(at) < REMIND:
            return True
        text = _("⏰ Still not resolved (last reminder: {at}):", at=at[:16] if at else _("earlier")) + "\n" \
            + "\n".join(problems)
    elif problems:
        text = "⚠️ " + "\n".join(problems)
        if to != "owner":
            text = "⚠️ " + _("{project} has not fixed this (the platform's agents were told first):", project=to) \
                + "\n" + "\n".join(problems)
    else:
        text = "✅ " + ok_text
    # a retry is the same notice, to a channel that can tell (`at`): its time stays with the failure until it has gone out
    wanted = sorted(problems)
    unsent = (failed_one := _load(FAILED).get(key)) and isinstance(failed_one, dict) and failed_one.get("notice") or {}
    notice_at = unsent["at"] if unsent.get("at") and unsent.get("ids", unsent.get("problems")) == ident \
        else now.astimezone().isoformat(timespec="seconds")
    notice = {"key": key, "at": notice_at, "state": "open" if problems else "resolved"}
    if problems:
        more = len(problems) - 1
        notice["text"] = _("{first} (and {n} more)", first=problems[0], n=more) if more else problems[0]
        if more:
            notice["detail"] = "\n".join(problems)
    else:
        notice["text"] = ok_text
    # a recovery is news only to whoever keeps the item (the assistant closes it); it isn't pushed to the owner (11-18)
    ok = send(text, notice, plain=bool(problems))
    _log(key, "reminder" if ident == last_ident else notice["state"], ok)
    if ok:
        _record(key, {"problems": wanted, **({"ids": ident} if ids is not None else {}),
                      "at": now.isoformat(timespec="seconds"),
                      **({"escalated": True, "to": to, "letter": (last or {}).get("letter")} if escalated and problems else {})})
        if escalated and not problems:
            _withdraw((last or {}).get("letter"))
        clear_failure(key)
    else:
        record_failure(key, text, {"problems": wanted, **({"ids": ident} if ids is not None else {}), "at": notice_at})
    return ok or ident == last_ident   # a reminder that didn't go: they were told before (the failure shows red)
