"""Linux's service manager: the launchd plists the platform produces (render-services' render(), the scheduler and
watchdog templates in launchd/) → systemd user units (~/.config/systemd/user/), plus loading, status and unloading.
macOS keeps using launchd; this is used only when hubconf.service_manager() is systemd.

Mapping (these are all the plist fields the platform uses):
  ProgramArguments → ExecStart (each argument double-quoted; % and $ escaped by systemd's rules)
  WorkingDirectory, EnvironmentVariables → WorkingDirectory=, Environment=
  StandardOutPath / StandardErrorPath → StandardOutput=append: / StandardError=append:
  KeepAlive → Restart=always (RestartSec = ThrottleInterval, 10 s if unset), WantedBy=default.target
  StartInterval (without KeepAlive) → a Type=oneshot service + a timer (OnActiveSec 1 s = runs once on load, like
                                    RunAtLoad; OnUnitActiveSec = the interval), KillMode=process: see below
  RunAtLoad, no interval, no KeepAlive → a plain service, WantedBy=default.target
A tick that starts work meant to outlive it (the scheduler's background jobs; hubbus dispatch's triage run, background
fork and mailbox-route call) leaves the tick's process group with a new session, which is what launchd asks: it kills
what is left in the job's process group when the job exits. systemd goes by the unit's cgroup, which a new session
doesn't leave: with the default KillMode=control-group a oneshot service that ends kills everything it started. So an
interval unit kills only its main process (KillMode=process), as launchd does with a run that left its group. A run that
stays in the tick's group isn't killed for it, which is the one way the two differ; the platform starts none it means to
die with the tick. Not a transient scope per run (systemd-run --scope): every place that starts one would have to know the
service manager, and one that forgot would be killed silently.
To keep running after logout: `loginctl enable-linger <user>` is needed (doctor says so).
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import unicodedata

import hubconf
import i18n

_ = i18n.domain(__file__)

UNIT_DIR = pathlib.Path.home() / ".config" / "systemd" / "user"


def _line(s) -> str:
    """s, which goes into one line of a unit: a control character or a line separator in it (one may begin another line)
    is refused."""
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp", "Cs") for c in str(s)):
        raise ValueError(_("a control character can't be in a unit: {text!r}", text=str(s)[:80]))
    return str(s)


def _q(s: str) -> str:
    """One ExecStart argument / Environment value: double-quoted; \\ and " escaped; % becomes %% (specifier); $ in
    ExecStart becomes $$."""
    return '"' + _line(s).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


_ARG = re.compile(r'"((?:[^"\\]|\\.)*)"( |(?=\n)|\Z)', re.S)
_TIMER = re.compile(r"\[Unit\]\nDescription=[^\n]*\n\n\[Timer\]\nOnActiveSec=\d+\nOnUnitActiveSec=\d+\nAccuracySec=1\n\n"
                    r"\[Install\]\nWantedBy=timers\.target\n")


def written(text: str, label: str) -> tuple[list[str], str | None] | None:
    """(the command, the working directory) of a .service this module wrote for label; None for any other text. Read as
    it was written, up to there: nothing later in the file, which older versions let an environment value reach, matters."""
    head = [f"[Unit]\nDescription=agent-hub {label}\n\n[Service]\nType={t}\nExecStart=" for t in ("oneshot", "simple")]
    start = next((h for h in head if text.startswith(h)), None)
    if start is None:
        return None
    at, argv = len(start), []
    while (m := _ARG.match(text, at)):
        argv.append(re.sub(r"\\(.)", r"\1", m.group(1), flags=re.S).replace("$$", "$").replace("%%", "%"))
        at = m.end()
        if not m.group(2):
            break
    line = text[at + 1:].split("\n", 1)[0] if text[at:at + 1] == "\n" else ""
    cwd = line.removeprefix("WorkingDirectory=")
    return (argv, cwd if cwd != line else None) if argv else None


def written_timer(text: str) -> bool:
    return bool(_TIMER.fullmatch(text))


def target(plist: dict) -> str:
    """The one unit to enable: with an interval and not always running → the timer; otherwise → the service."""
    return f"{plist['Label']}.timer" if plist.get("StartInterval") and not plist.get("KeepAlive") else f"{plist['Label']}.service"


def files(plist: dict) -> dict[str, str]:
    """plist → {file name: content}. The content depends only on the plist (the same plist gives the same result every
    time); render-services compares content to decide whether to reload."""
    label = _line(plist["Label"])
    timer = target(plist).endswith(".timer")
    lines = ["[Unit]", f"Description=agent-hub {label}", "", "[Service]",
             f"Type={'oneshot' if timer else 'simple'}",
             "ExecStart=" + " ".join(_q(a).replace("$", "$$") for a in plist["ProgramArguments"])]
    if plist.get("WorkingDirectory"):
        lines.append(f"WorkingDirectory={_line(plist['WorkingDirectory'])}")
    for k, v in sorted((plist.get("EnvironmentVariables") or {}).items()):
        lines.append(f"Environment={_q(f'{k}={v}')}")
    if plist.get("StandardOutPath"):
        lines.append(f"StandardOutput=append:{_line(plist['StandardOutPath'])}")
    if plist.get("StandardErrorPath"):
        lines.append(f"StandardError=append:{_line(plist['StandardErrorPath'])}")
    if plist.get("KeepAlive"):
        lines += ["Restart=always", f"RestartSec={int(plist.get('ThrottleInterval') or 10)}"]
    if timer:   # a tick: what it started to outlive it isn't killed when it ends (module header)
        lines.append("KillMode=process")
    if not timer:
        lines += ["", "[Install]", "WantedBy=default.target"]
    out = {f"{label}.service": "\n".join(lines) + "\n"}
    if timer:
        out[f"{label}.timer"] = "\n".join([
            "[Unit]", "Description=" + _("brunna {label} (every {secs} s)", label=label, secs=int(plist['StartInterval'])),
            "",
            "[Timer]", "OnActiveSec=1" if plist.get("RunAtLoad") else f"OnActiveSec={int(plist['StartInterval'])}",
            f"OnUnitActiveSec={int(plist['StartInterval'])}", "AccuracySec=1", "",
            "[Install]", "WantedBy=timers.target"]) + "\n"
    return out


def installed(label: str) -> dict[str, str]:
    """The unit files currently installed for this label (none = empty)."""
    out = {}
    for suffix in (".service", ".timer"):
        f = UNIT_DIR / f"{label}{suffix}"
        if f.exists():
            out[f.name] = f.read_text()
    return out


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})


def _checked(r: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    """r, when the manager did it. Raises with what it said otherwise: one that can't be reached did nothing."""
    if r.returncode != 0:
        raise RuntimeError(_("systemctl {args} failed: {error}", args=" ".join(map(str, r.args if isinstance(r.args, (list, tuple))
                                                                               else [r.args]))[:200],
                             error=r.stderr.strip()[:200] or f"exit {r.returncode}"))
    return r


def daemon_reload() -> None:
    _checked(systemctl("daemon-reload"))


def install(plist: dict) -> subprocess.CompletedProcess:
    """Write the unit files and load them."""
    label, want = plist["Label"], files(plist)
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    for name in set(installed(label)) - set(want):   # e.g. it used to have an interval and now runs all the time
        systemctl("disable", "--now", "--", name)
        (UNIT_DIR / name).unlink()
    for name, text in want.items():
        hubconf.write_atomic(UNIT_DIR / name, text)
    return load(plist)


def load(plist: dict) -> subprocess.CompletedProcess:
    """The unit files as they are now: daemon-reload, enable --now the one to run; if it is a service, restart it (to
    pick up the new content). The first step that fails."""
    r = systemctl("daemon-reload")
    r = r if r.returncode else systemctl("enable", "--now", "--", target(plist))
    if r.returncode == 0 and target(plist).endswith(".service"):
        r = systemctl("restart", "--", f"{plist['Label']}.service")
    return r


def remove(label: str) -> None:
    for name in installed(label):
        systemctl("disable", "--now", "--", name)
        (UNIT_DIR / name).unlink(missing_ok=True)
    systemctl("daemon-reload")


RUNS = ("active", "activating", "reloading", "deactivating", "refreshing", "maintenance")
RUNS_NOT = ("inactive", "failed", "unknown")


def active(name: str) -> bool:
    """Whether systemd runs a unit. Raises when its answer isn't one (it can't be reached, it says nothing)."""
    r = systemctl("is-active", "--", name)
    said = r.stdout.strip()
    if said not in (*RUNS, *RUNS_NOT):
        raise RuntimeError(_("systemd doesn't say whether {unit} runs: {error}", unit=name,
                             error=(r.stderr.strip() or said)[:200] or f"exit {r.returncode}"))
    return said in RUNS


def stop(name: str, kill: bool = False) -> None:
    """Not running and not enabled any more (its links gone, an alias's too); what it is told to do by then is its file as
    it is now, so the caller makes that the platform's own first (inert() or a new unit, and daemon_reload()). kill: all it
    started ends too, whatever its own KillMode was. Raises unless systemd did each of it and says it doesn't run."""
    if kill:
        r = systemctl("kill", "-s", "KILL", "--", name)
        if "not loaded" not in r.stderr:   # then there is nothing of it
            _checked(r)
    _checked(systemctl("disable", "--now", "--", name))
    if active(name):
        raise RuntimeError(_("{unit} is still running: {error}", unit=name, error="systemctl disable --now"))


def inert(name: str) -> bytes:
    """What a unit file is made to say before it is taken out: nothing it said before is obeyed by then (an [Install]
    section's Also= names other units)."""
    body = "[Service]\nType=oneshot\nExecStart=/bin/true\n" if name.endswith(".service") else "[Timer]\nOnActiveSec=3650d\n"
    return f"[Unit]\nDescription=brunna {name}: being removed\n\n{body}".encode()


def state(label: str) -> str | None:
    """The same kind of answer as render-services' running(): not loaded = None; an always-running service → running /
    not running; a timer → whether it is scheduled (active)."""
    have = installed(label)
    if not have:
        return None
    unit = f"{label}.timer" if f"{label}.timer" in have else f"{label}.service"
    r = systemctl("is-active", "--", unit)
    return "running" if r.stdout.strip() == "active" else "not running"


def main_pid(label: str) -> int | None:
    """The pid systemd runs a service as (MainPID), None when it runs none."""
    r = systemctl("show", "-p", "MainPID", "--value", "--", f"{label}.service")
    try:
        pid = int(r.stdout.strip())
    except ValueError:
        return None
    return pid or None


def fragment(label: str) -> str | None:
    """The file systemd has a unit of this label from (None: none). Raises when its answer isn't one."""
    r = _checked(systemctl("show", "-p", "LoadState", "-p", "FragmentPath", "--", f"{label}.service"))
    said = dict(x.split("=", 1) for x in r.stdout.splitlines() if "=" in x)
    if not said.get("LoadState") or "FragmentPath" not in said:
        raise RuntimeError(_("systemd doesn't say what {unit} is", unit=f"{label}.service"))
    return said["FragmentPath"] or None


def linger() -> bool | None:
    """Whether user units keep running after logout (loginctl enable-linger); can't tell = None."""
    r = subprocess.run(["loginctl", "show-user", os.environ.get("USER") or str(os.getuid()), "-p", "Linger"],
                       capture_output=True, text=True)
    return None if r.returncode != 0 else r.stdout.strip() == "Linger=yes"
