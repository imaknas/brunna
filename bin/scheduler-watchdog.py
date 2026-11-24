#!/usr/bin/env python3
"""Scheduler watchdog: when scheduler.py stops (launchd job gone, tick stuck, python broken) or its ticks don't complete
(each one crashes), post to Discord.

scheduler.py is the only trigger for the librarian, weekly-reflect, mirror-backup and every muninn-* job; when it
stops nothing gets called: the assistant's web server is a separate launchd job, so the PWA still opens, it just
stops updating. So this script deliberately depends neither on the scheduler nor on the assistant:

- its own launchd job (local.brunna-scheduler-watchdog, every 5 minutes)
- the system's /usr/bin/python3, standard library only: if the platform's Python (bin/py) breaks and stops the
  scheduler, this one keeps going
- notifications go through Discord REST (bot token, curl), not through the assistant

The check: scheduler.py writes .state/scheduler-heartbeat at the start of every tick and around every job; it alerts
only when that hasn't been updated for more than STALE_MIN minutes, or when .state/scheduler-tick.json (bin/tick_record.py)
says UNFINISHED ticks in a row began and didn't complete and none has for STALE_MIN minutes; two checks in a row (no false
alarm on the round right after boot/recovery). A long tick is one running tick and 0 unfinished, so it raises nothing; no
tick record (a version from before it) raises nothing either. After that it reminds every REMIND_H hours, and posts a
"recovered" message when it recovers.

Its own state (scheduler-watchdog.json) is a convenience: one that can't be read starts over, and one that can't be
written (a full disk, where it is needed most) means no second check is waited for, with the time of the last alert kept
as the date of an empty file beside it.

Settings, in the platform's secrets directory (outside the repo; `bin/hubconf.py dir secrets`):
  discord-token          bot token (can share the same bot with other projects)
  discord-alert-channel  the channel id to post to
Without them it fails loudly in the log (exit 1), rather than pretend to be watching.

Usage: scheduler-watchdog.py          check once (for launchd)
       scheduler-watchdog.py --test   post a test message, to confirm the token/channel settings are right
"""
from __future__ import annotations   # the system python3 (3.9 on macOS) runs this: no 3.10 syntax at run time

import datetime
import json
import os
import pathlib
import stat
import subprocess
import sys
import time

# a copy of the pairs this script uses from bin/names.py (current spelling first): it imports no platform module, so it
# can't read the table; tests/test_name_pairs.py compares this copy with it
NAMES = {"config_file": ("brunna.json", "hub.json"), "unit": ("local.brunna", "local.agent-hub")}


def env(x: str) -> str:
    """names.env: the old spelling when set (an outside override), else the new one."""
    return os.environ.get(f"AGENT_HUB_{x}") or os.environ.get(f"BRUNNA_{x}") or ""


# same as hubconf.ROOT
HUB = pathlib.Path(env("ROOT") or pathlib.Path(__file__).resolve().parents[1])


def _instance(sub: str, legacy: pathlib.Path) -> pathlib.Path:
    """The instance directory's layout (same rules as hubconf, docs/design/new-user.md 3.1). Deliberately imports no
    platform module: this must still run when the platform is broken (checked by tests/test_python_floor.py)."""
    if (v := env({"config": "CONFIG", "secrets": "SECRETS"}[sub])):
        return pathlib.Path(os.path.expanduser(v))
    home = env("HOME") or ((HUB / ".hub-home").read_text().strip() if (HUB / ".hub-home").is_file() else "")
    return pathlib.Path(os.path.expanduser(home)) / sub if home else legacy


CONFIG_DIR, SECRETS_DIR = _instance("config", HUB), _instance("secrets", HUB / ".secrets")
# the settings file, hub.json or brunna.json: whichever exists (hubconf.config_path)
SETTINGS = next((CONFIG_DIR / n for n in NAMES["config_file"] if (CONFIG_DIR / n).exists()),
                CONFIG_DIR / NAMES["config_file"][0])


class i18n:
    """bin/i18n.py's lookup, small enough to keep here (this script imports nothing of the platform): English msgids,
    locales/<hub.json language.ui>/scheduler-watchdog.json for the translation"""
    @staticmethod
    def domain(file):
        lang = env("LANG_UI")
        if not lang:
            try:
                lang = (json.loads(SETTINGS.read_text()).get("language") or {}).get("ui") or "en"
            except (OSError, ValueError):
                lang = "en"
        try:
            code = pathlib.Path(file).resolve().parents[1]   # the catalogs ship with the code, wherever the hub's state is
            cat = json.loads((code / "locales" / lang / "scheduler-watchdog.json").read_text()) if lang != "en" else {}
        except (OSError, ValueError):
            cat = {}

        def _(msgid, **kw):
            text = cat.get(msgid, msgid)
            if "{settings}" in text:   # the settings file's name as this instance has it (bin/i18n.py NAMES)
                kw = {"settings": SETTINGS.name, **kw}
            return text.format(**kw) if kw else text
        return _


_ = i18n.domain(__file__)
HEARTBEAT = HUB / ".state" / "scheduler-heartbeat"
TICK_RECORD = HUB / ".state" / "scheduler-tick.json"
STATE_FILE = HUB / ".state" / "scheduler-last-run.json"
WATCH_STATE = HUB / ".state" / "scheduler-watchdog.json"
STATE_DIR = HUB / ".state"
ALERTED, RECOVERED = "scheduler-watchdog.alerted", "scheduler-watchdog.recovered"   # marks()
SCHED_LOG = HUB / ".logs" / "scheduler.log"
LOG = HUB / ".logs" / "scheduler-watchdog.log"
TOKEN = SECRETS_DIR / "discord-token"
CHANNEL = SECRETS_DIR / "discord-alert-channel"
LABELS = [u + "-scheduler" for u in NAMES["unit"]]   # this release's first; an instance not switched yet has the other
RESUME_GRACE_MIN = 20  # after a resume (Linux): the scheduler's 15-minute timer gets one full interval and a margin
STALE_MIN = 35  # a tick every 15 minutes; two missed ticks plus a few minutes of drift
UNFINISHED = 2   # ticks in a row that began and didn't complete (one may be a reboot)
REMIND_H = 6
CALL_S = 30   # what it runs (launchctl, systemctl, ps, curl, osascript) is given this long: an alert waits for none of them
SLOT_MIN = 5.05   # one check, every 5 minutes; systemd starts it up to a second late


def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line)
    except OSError:   # a full disk: the alert still has to go out, and stderr is a file on the same disk
        try:
            os.write(2, line.encode("utf-8", "replace"))   # not sys.stderr: what it holds back is tried again at the exit
        except OSError:
            pass


def last_tick():
    """The time of the last tick: the heartbeat, falling back to the state file's modification time (older
    scheduler). Not the newer of the two: schedulectl run writes the state file too, which would hide the fact that
    the scheduler has actually stopped."""
    for p in (HEARTBEAT, STATE_FILE):
        if (at := mtime(p)) is not None:   # nor one that may not be looked at: no tick is known, which is told
            return datetime.datetime.fromtimestamp(at)
    return None


def incomplete():
    """(how many ticks in a row began and didn't complete, since when) when that is enough to alert, else None: the rule
    of bin/tick_record.py. Nothing recorded, or nothing readable, says nothing."""
    try:
        rec = json.loads(TICK_RECORD.read_text())
        n = rec["unfinished"]
        since = datetime.datetime.fromisoformat(rec.get("completed") or rec["since"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(n, int) or n < UNFINISHED:
        return None
    since = datetime.datetime.fromtimestamp(since.timestamp())   # local, whichever way it was written
    return (n, since) if minutes_since(since) > STALE_MIN else None


def completed_since(alert, now):
    """Whether a tick has completed since the alert, by the tick record; True where that can't be told (no record: the
    heartbeat is all there is; a time in the future: the clock was set back)."""
    try:
        rec = json.loads(TICK_RECORD.read_text())
        if rec.get("unfinished"):
            return False
        done = datetime.datetime.fromisoformat(rec["completed"]).timestamp()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return True
    return alert is None or not done < int(alert) <= now.timestamp()


def minutes_since(when):
    """As times, not readings of the clock: daylight saving moves those by an hour."""
    return (datetime.datetime.now().timestamp() - when.timestamp()) / 60


def load_state():
    """What the last check left; {} when there is none or it can't be read. A field that can't be used is left out."""
    try:
        st = json.loads(WATCH_STATE.read_text())
    except (OSError, ValueError):
        st = {}
    st = dict(st) if isinstance(st, dict) else {}
    for key in ("last_alert", "resumed_at"):
        if moment(st, key) is None:
            st.pop(key, None)
    for key, kind in (("stale_checks", int), ("sleep_gap", (int, float)), ("since", str)):
        if key in st and not isinstance(st[key], kind):
            del st[key]
    if (when := newest(RECOVERED)) is not None and when >= max(newest(ALERTED) or 0, mtime(WATCH_STATE) or 0):
        for key in ("alerting", "last_alert", "last_alert_at", "stale_checks", "since"):
            st.pop(key, None)
    elif (when := newest(ALERTED)) is not None:
        st["alerting"] = True
        note(st, "last_alert", when)
    return st


def mtime(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def newest(name):
    """When the mark was last set, in whichever of its places; None without one."""
    return max([t for t in map(mtime, marks(name)) if t is not None], default=None)


def note(st, key, at):
    """A moment in the state: the local reading, which every version of this script reads, and the time beside it, which
    daylight saving doesn't make ambiguous."""
    st[key], st[key + "_at"] = datetime.datetime.fromtimestamp(at).isoformat(timespec="seconds"), at


def moment(st, key):
    """That moment as a time; None when there is none or it can't be read. The reading alone when it isn't of the same
    moment: another version wrote it."""
    try:
        at = st.get(key + "_at")
        if isinstance(at, (int, float)) and datetime.datetime.fromtimestamp(at).isoformat(timespec="seconds") == st[key]:
            return at
        return datetime.datetime.fromisoformat(st[key]).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def second_place(make):
    """A folder outside .state for the marks when that can't be written: in the platform's private temp area
    (hubconf.private_tmp, in the secrets directory, which no sandboxed project can reach: the system's temp folder is
    theirs to write, as the same user, so a mark there could be planted and silence this). Made (make) 0700, and used
    only when it is a real folder, ours, that nobody else can enter. None without one."""
    d = SECRETS_DIR / "tmp" / "scheduler-watchdog"
    try:
        if make:
            for p in (d.parent, d):
                try:
                    os.mkdir(str(p), 0o700)
                except FileExistsError:
                    pass
        s = os.lstat(str(d))
        if stat.S_ISDIR(s.st_mode) and s.st_uid == os.getuid() and not s.st_mode & 0o077:
            return d
    except OSError:
        pass
    return None


def marks(name, make=False):
    """Where a mark (ALERTED, RECOVERED: empty files, their date says when) may be while WATCH_STATE can't be written."""
    d = second_place(make)
    return [STATE_DIR / name] + ([d / name] if d else [])


def clear_marks():
    for p in marks(ALERTED) + marks(RECOVERED):
        try:
            p.unlink()
        except OSError:
            pass
    d = second_place(False)
    if d:
        try:
            d.rmdir()   # only an empty one goes
        except OSError:
            pass


def save_state(st):
    """Whether it could be written (beside, synced, renamed: never cut)."""
    tmp = WATCH_STATE.with_name(WATCH_STATE.name + ".tmp")
    try:
        WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
        with open(str(tmp), "w") as f:
            f.write(json.dumps(st, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(WATCH_STATE))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        log(_("the watchdog's state can't be written: {error}", error=e))
        return False
    clear_marks()
    return True


def mark(name, at):
    """Set a mark to a moment, in the first place that takes it (the newest of them all counts). Whether it was set."""
    for p in marks(name, make=True):
        try:
            p.touch()
            os.utime(str(p), (at, at))
        except OSError:
            continue
        return True
    return False


def can_mark():
    """Whether a mark can be set at all: asked before something is sent that must be remembered."""
    for p in marks("scheduler-watchdog.probe", make=True):
        try:
            p.touch()
            p.unlink()
            return True
        except OSError:
            pass
    return False


def in_slot(since):
    """With nothing that can be remembered anywhere (read-only disks): whether this check is the one in REMIND_H hours that
    tells. Counted from when it stopped, so the first one is the first check past STALE_MIN."""
    if since is None:   # never ticked: the clock's own hours
        return datetime.datetime.now().timestamp() / 60 % (REMIND_H * 60) < SLOT_MIN
    past = minutes_since(since) - STALE_MIN
    return past >= 0 and past % (REMIND_H * 60) < SLOT_MIN


def remember(st, recovered, now):
    """What matters of a state that can't be written, as a mark, which needs no space: when the last alert went out, so the
    next check doesn't send it again; or that it is over, so that isn't told again either."""
    if st.get("alerting") and (at := moment(st, "last_alert")) is not None:
        mark(ALERTED, at)
    elif recovered:
        mark(RECOVERED, now.timestamp())


def keep_trying(text):
    """The one check that tells, when nothing it did can be remembered, and no channel could send: this process is what
    remembers, so it stays and tries again every minute until it is out, the scheduler is back, or the next slot comes."""
    stopped = last_tick()
    for _i in range(int(REMIND_H * 60 - 2 * SLOT_MIN)):
        time.sleep(60)
        if last_tick() != stopped:
            return False
        if send(text):
            return True
    return False


def details():
    """diagnose(), which must not keep an alert from going out."""
    try:
        return diagnose()
    except Exception as e:
        return repr(e)


def diagnose():
    uid = os.getuid()
    label = LABELS[0]
    # Linux: the scheduler is a systemd user timer (bin/systemd_units.py; this script deliberately imports no platform
    # module)
    if sys.platform != "darwin":
        r = subprocess.run(["systemctl", "--user", "status", "--no-pager", f"{label}.timer"], capture_output=True, text=True, timeout=CALL_S)
        lines = [_("`systemctl --user status {label}.timer`:\n```\n{out}\n```", label=label,
                   out=(r.stdout or r.stderr).strip()[:600]),
                 _("restart: `systemctl --user restart {label}.timer`", label=label)]
        if SCHED_LOG.exists():
            tail = SCHED_LOG.read_text(errors="replace").splitlines()[-3:]
            lines.append(_("last lines of scheduler.log:\n```\n{tail}\n```", tail="\n".join(x[:180] for x in tail)))
        return "\n".join(lines)
    for label in LABELS:   # the one launchd has, else this release's
        r = subprocess.run(["/bin/launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, text=True, timeout=CALL_S)
        if r.returncode == 0:
            break
    else:
        label = LABELS[0]
    if r.returncode != 0:
        lines = [_("{label} is not in launchd (booted out?) → `schedulectl start`", label=label)]
    else:
        info = {}
        for l in r.stdout.splitlines():  # the first one is the job's own (endpoints etc. have states further down)
            k, eq, v = l.partition("=")
            if eq:
                info.setdefault(k.strip(), v.strip())
        lines = [f"launchd:state={info.get('state', '?')} pid={info.get('pid', '-')} "
                 f"last exit={info.get('last exit code', '?')}"]
        ps = subprocess.run(["/bin/ps", "-axo", "pid,etime,command"], capture_output=True, text=True, timeout=CALL_S).stdout
        stuck = [l.strip() for l in ps.splitlines() if "bin/scheduler.py" in l and "watchdog" not in l]
        if stuck:
            lines.append(_("scheduler.py is still running (stuck in a job?): {proc}", proc=stuck[0][:120]))
        lines.append(_("restart: `launchctl kickstart -k gui/{uid}/{label}`", uid=uid, label=label))
    if SCHED_LOG.exists():
        tail = SCHED_LOG.read_text(errors="replace").splitlines()[-3:]
        lines.append(_("last lines of scheduler.log:\n```\n{tail}\n```", tail="\n".join(t[:180] for t in tail)))
    return "\n".join(lines)


def hub_alerts() -> dict | None:
    """hub.json's alerts section; None when hub.json can't be read."""
    try:
        return json.loads(SETTINGS.read_text()).get("alerts") or {}
    except (OSError, ValueError):
        return None


def conf_path(value: str) -> pathlib.Path:
    """hubconf.path's rules, kept here: .secrets/<name> is the secrets directory, ~ expands, relative is the config
    directory."""
    if value == ".secrets" or value.startswith(".secrets/"):
        return SECRETS_DIR / value[len(".secrets") + 1:]
    p = pathlib.Path(os.path.expanduser(value))
    return p if p.is_absolute() else CONFIG_DIR / p


def curl(*args, data=None) -> str:
    """curl is built into the system, no Homebrew needed. Returns the HTTP status ("" when curl itself fails)."""
    r = subprocess.run(["/usr/bin/curl", "-sS", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}", *args],
                       input=data, capture_output=True, text=True, timeout=CALL_S)
    return r.stdout.strip()


def send_discord(conf, text) -> bool:
    try:
        token = conf_path(conf.get("discord_token_file") or ".secrets/discord-token").read_text().strip()
        channel = conf_path(conf.get("discord_channel_file") or ".secrets/discord-alert-channel").read_text().strip()
    except OSError:
        return False
    return curl("-H", f"Authorization: Bot {token}", "-H", "Content-Type: application/json", "--data-binary", "@-",
                f"https://discord.com/api/v10/channels/{channel}/messages",
                data=json.dumps({"content": text[:1990]})) == "200"


def send_ntfy(conf, text) -> bool:
    n = conf.get("ntfy") or {}
    headers = ["-H", "Title: brunna"]
    if n.get("token_file"):
        try:
            headers += ["-H", f"Authorization: Bearer {conf_path(n['token_file']).read_text().strip()}"]
        except OSError:
            return False
    return curl(*headers, "--data-binary", "@-", f"{(n.get('base_url') or 'https://ntfy.sh').rstrip('/')}/{n['topic']}",
                data=text[:4000]) == "200"


def send_email(conf, text) -> bool:
    import smtplib
    from email.message import EmailMessage
    e = conf.get("email") or {}
    msg = EmailMessage()
    msg["Subject"] = "brunna: " + (text.splitlines()[0][:80] if text else "")
    msg["From"], msg["To"] = e.get("from") or e.get("user") or "brunna", e["to"]
    msg.set_content(text)
    try:
        password = conf_path(e["password_file"]).read_text().strip() if e.get("password_file") else None
        cls = smtplib.SMTP_SSL if e.get("ssl") else smtplib.SMTP
        with cls(e["host"], int(e.get("port") or (465 if e.get("ssl") else 587)), timeout=20) as s:
            if not e.get("ssl"):
                s.starttls()
            if e.get("user") and password:
                s.login(e["user"], password)
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        return False


def send_macos(conf, text) -> bool:
    script = f"display notification {json.dumps(text[:200])} with title \"brunna\""
    return subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=CALL_S).returncode == 0


def channels(conf) -> list[tuple[str, object]]:
    """The channels hub.json alerts sets up (the same ones as bin/alert.py, which this deliberately doesn't import:
    it must still send when the platform's Python is broken). Discord with no files configured falls back to the
    secrets directory's default names, as before."""
    out = []
    if all(mtime(conf_path(conf.get(k) or d)) is not None for k, d in (
            ("discord_token_file", ".secrets/discord-token"), ("discord_channel_file", ".secrets/discord-alert-channel"))):
        out.append(("discord", send_discord))
    if (conf.get("ntfy") or {}).get("topic"):
        out.append(("ntfy", send_ntfy))
    if (conf.get("email") or {}).get("host") and (conf.get("email") or {}).get("to"):
        out.append(("email", send_email))
    if conf.get("macos") and sys.platform == "darwin":
        out.append(("macos", send_macos))
    return out


def send(text):
    """Send on every channel set up. True when at least one went out: the point is that someone hears; each failure is
    logged."""
    conf = hub_alerts() or {}
    sent = False
    for name, fn in channels(conf):
        try:
            ok = fn(conf, text)
        except Exception as e:   # one broken channel must not silence the others
            log(type(e).__name__)   # not its text: it may hold a token
            ok = False
        if ok:
            sent = True
        else:
            log(_("{channel} send failed", channel=name))
    if not sent:
        log(_("failed: no alert channel could send"))
    return sent


def alerts_configured() -> bool:
    a = hub_alerts()
    return True if a is None else bool(a)   # hub.json unreadable: keep watching with whatever channel there is


def resumed(st: dict, now: datetime.datetime) -> None:
    """Linux: systemd timers resume their remaining time after a suspend, so this check can run before the scheduler's
    first tick after waking. The gap between CLOCK_BOOTTIME (counts suspended time) and CLOCK_MONOTONIC (doesn't) grows
    by the time spent asleep: when it grew since the last check, record the resume. macOS has no CLOCK_BOOTTIME; there
    launchd fires missed intervals on wake, so nothing is needed."""
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    if boottime is None:
        return
    gap = time.clock_gettime(boottime) - time.clock_gettime(time.CLOCK_MONOTONIC)
    woke = st.get("sleep_gap") is not None and gap - st["sleep_gap"] > 60
    st["sleep_gap"] = gap
    # only once: with a gap that can't be saved every check would find the same sleep again, and wait for ever
    if woke and save_state(st):
        note(st, "resumed_at", now.timestamp())


def in_resume_grace(st: dict, tick, now: datetime.datetime) -> bool:
    """Right after a resume, until the scheduler has ticked or RESUME_GRACE_MIN have passed: no alarm."""
    at = moment(st, "resumed_at")
    if at is None:
        return False
    if (tick is not None and tick.timestamp() >= at) or now.timestamp() - at >= RESUME_GRACE_MIN * 60:
        st.pop("resumed_at", None)
        st.pop("resumed_at_at", None)
        return False
    return True


def main():
    if sys.argv[1:] == ["--test"]:
        ok = send(_("🧪 brunna scheduler watchdog test message (settings are correct)") + "\n" + details())
        print(_("sent") if ok else _("could not send, see {log}", log=LOG))
        return 0 if ok else 1

    if not alerts_configured():
        # no alerts section in hub.json = the owner chose no alerts (the default for new users): still check and log,
        # not treated as a failure
        tick = last_tick()
        if tick and (found := incomplete()):
            log(_("no alerts set up ({settings} has no alerts), only logging: the scheduler's ticks don't complete ({n} in "
                  "a row, none since {since})", n=found[0], since=found[1]))
        stale = tick is None or minutes_since(tick) > STALE_MIN
        if stale:
            log(_("no alerts set up ({settings} has no alerts), only logging: the scheduler's last tick was {tick}; it has "
                  "stopped", tick=tick) if tick else
                _("no alerts set up ({settings} has no alerts), only logging: the scheduler has never ticked"))
        return 0
    if not channels(hub_alerts() or {}):
        # printed to the service's stderr log every 5 minutes, exit 1; the status page also flags "watchdog not set up"
        print(_("scheduler-watchdog: {settings} alerts has no channel this watchdog can send on (Discord, ntfy, email, "
                "macOS); no alert can be sent if the scheduler stops"), file=sys.stderr)
        return 1

    now = datetime.datetime.now()
    st = load_state()
    tick = last_tick()
    resumed(st, now)
    age_min = minutes_since(tick) if tick else None
    stale = age_min is None or age_min > STALE_MIN
    found = None if stale else incomplete()   # it ticks, but the ticks don't complete
    when = f"{tick:%m-%d %H:%M}" if tick else _("never")

    recovered = False
    if not stale and not found:
        in_resume_grace(st, tick, now)   # drops a resume that is over
        sleep = {k: st[k] for k in ("sleep_gap", "resumed_at", "resumed_at_at") if k in st}   # the next check's, whatever this one found
        if st.get("alerting") and not completed_since(moment(st, "last_alert"), now):
            pass   # it ticks again; whether a tick completes isn't known yet
        elif st.get("alerting") and not (save_state(st) or can_mark() or in_slot(None)):
            pass   # nor can a recovery be remembered as told: in its slot
        elif st.get("alerting"):
            if send(_("✅ The brunna scheduler is back (last tick {when}; it had been stopped since about {since})",
                      when=when, since=st.get("since", "?"))):
                log(_("recovered, notified"))
                st, recovered = sleep, True
        else:
            st, recovered = sleep, True
    elif stale and in_resume_grace(st, tick, now):
        log(_("the machine just woke up; waiting for the scheduler's first tick before judging it"))
    else:
        st["stale_checks"] = st.get("stale_checks", 0) + 1
        st.setdefault("since", f"{found[1]:%m-%d %H:%M}" if found else when)
        last = moment(st, "last_alert")
        # one in the future (the clock was set back) says nothing
        due = (last is None or not 0 <= now.timestamp() - last < REMIND_H * 3600) \
            if st.get("alerting") else st["stale_checks"] >= 2 or not save_state(st)
        # told only if it can be remembered as told, or in the one check in REMIND_H hours that needs no memory
        memory = due and (save_state(st) or can_mark())
        if due and (memory or in_slot(found[1] if found else tick)):
            age = _("{minutes:.0f} minutes", minutes=age_min) if age_min is not None else _("a long time")
            prefix = "🚨" if not st.get("alerting") else _("⏰ Still down:")
            text = (_("{prefix} The brunna scheduler starts, but its ticks don't complete: {n} in a row, none since {since}. "
                      "Jobs may not be running (the backup too); .logs/scheduler.log and .logs/scheduler-stderr.log say why.",
                      prefix=prefix, n=found[0], since=f"{found[1]:%m-%d %H:%M}")
                    if found else
                    _("{prefix} The brunna scheduler has not ticked for {age} (last tick {when}). The librarian, "
                      "the assistant's jobs, reflect and the upload jobs have all stopped.", prefix=prefix, age=age, when=when)
                    if tick else
                    _("{prefix} The brunna scheduler has not ticked for {age} (no tick recorded). The librarian, "
                      "the assistant's jobs, reflect and the upload jobs have all stopped.", prefix=prefix, age=age)) \
                + "\n" + details()
            if send(text) or (not memory and keep_trying(text)):
                st["alerting"] = True
                note(st, "last_alert", now.timestamp())
                log(_("the scheduler's ticks don't complete ({n} in a row), notified", n=found[0]) if found else
                    _("the scheduler stopped {age} ago, notified", age=age))
            else:
                return 1
    if not save_state(st):
        remember(st, recovered, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
