#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""Registry services -> launchd plists (~/Library/LaunchAgents/<label>.plist).

Each project declares its long-running programs in projects/<name>.yaml; the platform generates the plists by the
rules here and does bootout/bootstrap only when the content changed (nothing changed: nothing happens, no service is
restarted for no reason). The platform's own core (scheduler, watchdog, status
page) is not here: those are version-controlled plists under launchd/ -- the scheduler cannot reload itself.

Whose a unit file is (ours()): units_record() (.state/service-units.json) has the sha256 of each file this script wrote;
while one is being replaced, the old's and the new's. A unit is a plain file directly in the unit folder (never a link,
never a path a label spells). It is the platform's when it is as recorded, or exactly what would be written now, or text
this script writes (its own serialization, key set and order) that runs this hub's sandboxed-run. Once, on the first run
after a version without the record, also one that version lists, in that same text, that runs in a registered project's
folder and carries a mark of the platform's (the CLI folder, its log folder, the project's own label); and, for a
sandboxed project's own label, one nothing can read. Any other file is never unloaded, written or deleted, by a run or at
uninstall: the service isn't applied and the owner is told. `--adopt <label>` (asked at a terminal, a copy kept) lets a
wanted service's label take such a file over.

A run writes every changed file first (all of it beside, then in place), then takes out what is no longer wanted, then
loads what changed: a service whose label changes is stopped only when the new one is in place. One that has to stop
whatever happens (a sandboxed project's unit that isn't its skeleton) does, and that is said.

One service never stops another: each is rendered, serialized and applied behind its own guard, and what fails is told.

service fields (validated by registry.py):
  name                 required; label defaults to local.<project>.<name>
  label                keep an old label (e.g. other scripts restart it by label). A sandboxed project's isn't used: its
                       label is always the default
  run                  command; the first word is resolved to an absolute path, other arguments that are files
                       existing under cwd become absolute paths too
  cwd                  runtime (default when there is a runtime) | workspace | hub
  keepalive: true      KeepAlive (restart when it dies); without it: runs once / per interval_s
  interval_s           StartInterval (run every so many seconds)
  throttle_s           ThrottleInterval
  env                  environment variables; a value that is a relative path existing under cwd becomes an absolute
                       path. When run's first word is a command name (node) and no PATH is given: it is looked up
                       by hubconf.LAUNCHD_PATH, which is also written into the plist (the nvm default version's bin
                       first -- node is managed only by nvm, see hubconf.nvm_bin). Run by path (bin/x.sh) with no
                       PATH: the service manager's own, through a shell that adds to it at start (cli_last); $PLATFORM_PATH
                       in an env value is replaced by hubconf.LAUNCHD_PATH. Every service's PATH, its own included, ends
                       with hubconf.cli_folder(), which holds only the platform's `brunna` wrapper (written here and by
                       install-cli), so a service finds brunna by name and nothing else it finds changes
                       (docs/design/project-agent-calls.md D)
  log                  one file (stdout and stderr both go there, relative to cwd); without it:
                       <hub>/.logs/<label without local.>-{stdout,stderr}.log. A sandboxed project's is opened inside its
                       sandbox; its unit always has the platform's, where what happens before goes
  expose               {tailnet_https, local_port}: declaration only, tailscale serve is not run automatically --
                       the status page reminds you to run it by hand. For a sandboxed project local_port is the only
                       port its service may listen on
A sandboxed project's services (isolation.sandboxed: enforce.sandbox, not service_sandbox_exempt) run through
bin/sandboxed-run (isolation.py: Seatbelt on macOS, Landlock on Linux). Their unit is always the same skeleton: the
platform's Python isolated, sandboxed-run with the project's, the service's and its folder's names and a digest of its
run, env and log, the words of run as written (nothing of the project's is looked up outside), the platform's PATH and
log, the label local.<project>.<name>. sandboxed-run reads the rest from the registry and applies it inside. A unit of a
sandboxed project that isn't its skeleton is replaced, and when the skeleton can't be rendered (no adapter here, a name
no file can have) unloaded and removed: nothing a project says keeps an older one. Units are found by what they run, in
the unit folder itself, listed or not. On Linux the unit gets no sandboxing directives of its own: a systemd user unit silently drops them when user namespaces
are restricted (measured 10-31, docs/design/capabilities.md).

Safety net (2026-10-24: Homebrew's node was deleted, this script switched the plist to a `node` that could not be
found, and a LINE bot was down for a day and a half without anyone knowing):
  - an unsandboxed service's first word cannot be resolved -> leave the current plist alone (what runs keeps running), only
    report the problem
  - bootstrap failed, a keepalive service is not running, something that should be loaded is not -> report it
  Problems go to Discord (once per set, once more on recovery), exit 1 (the scheduler records job.failed, the status
  page turns red).
A platform service that keeps running the code of an older commit than the checkout's past the same limit is a problem
too (FOLLOWS_CODE, old_code; docs/design/service-code.md D5): each exits by itself once it has nothing in flight, and
this only says when that hasn't happened; it never restarts one for its code.
A service whose restart loses work in flight (WAIT_FOR_IDLE: cap-runner marks a running job lost) keeps its unit as it
is while it has some, by the record it keeps in the platform's state while it runs; the change is made on a run when it
has none, or after two hours beyond the longest agent call (three by default). A change that waits doesn't show in plan either, so a job ending between
`config apply`'s apply and its plan again can show it there once.

Usage: render-services.py [--dry-run] [--only <label>]
       render-services.py --adopt <label>     take a unit file this script didn't write over (the owner, at a terminal)
Also an applier (appliers.py): plan()/apply(), used by `bin/hub config plan/apply`.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import time
import sys
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import alert  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import isolation  # noqa: E402
import names  # noqa: E402
import registry  # noqa: E402

_ = i18n.domain(__file__)

AGENTS = pathlib.Path.home() / "Library" / "LaunchAgents"
# The PATH under launchd: commands are resolved by this (or by the service's own env PATH), not by the PATH of the
# shell running this script (the shell may have nvm and the like; launchd does not)
DEFAULT_PATH = hubconf.LAUNCHD_PATH
RENDERED = hubconf.STATE / "rendered-services.json"


def with_cli(path: str) -> str:
    """path with the platform's CLI folder last (docs/design/project-agent-calls.md D): `brunna` by name, and nothing
    found today found anywhere else."""
    return f"{path}:{hubconf.cli_folder()}" if path else str(hubconf.cli_folder())


def cli_last(args: list[str]) -> list[str]:
    """A service started by path, which sets no PATH, keeps the one its service manager gives it (launchd's, the systemd
    user manager's, whatever they are when it starts) and gets the CLI folder after it: a shell that sets PATH and execs
    it, so the unit itself names no PATH and doesn't change when the manager's does."""
    script = f'PATH="${{PATH:+$PATH:}}"{shlex.quote(str(hubconf.cli_folder()))}; export PATH; exec "$@"'
    return ["/bin/sh", "-c", script, "sh", *args]


# Services whose restart loses work in flight: their unit is changed only while they have none (cap-runner marks a
# running job lost when it restarts). (module or project, service name) → the record the running service keeps of it
# in the platform's state ({pid, jobs}), where no project writes; one whose pid isn't alive minds nothing.
WAIT_FOR_IDLE = {("sandbox", "cap-runner"): "cap-runner/active.json"}
# past this the change is made anyway: a job lasts at most 30 minutes, an agent call at most cap_runner.agent_max_timeout_s
# (1 hour) plus a minute, and each pool runs its queue one or two at a time: two hours beyond the longest agent call
WAIT_MARGIN_S = 2 * 3600


def wait_limit_s() -> int:
    return WAIT_MARGIN_S + int(hubconf.get("cap_runner.agent_max_timeout_s", 3600))
WAITED = hubconf.STATE / "render-services-waiting.json"   # label → when its change first waited
waiting: list[str] = []   # the last _desired()'s labels left as they are until idle (main says so)
overdue: list[str] = []   # the last _desired()'s labels changed although busy, after waiting wait_limit_s()


# The platform's long-running services and how each follows the platform's code (docs/design/service-code.md D5): the
# name of the record it keeps through code_watch (it exits by itself once the checkout has moved on and it has nothing in
# flight), or None for one that runs no platform code. By service name, for the platform's entry and the modules only.
# Hard-coded like WAIT_FOR_IDLE: a registry field would be read by the running code during the merge that adds it.
# tests/test_service_code.py fails when a keepalive service of the platform's is missing here
FOLLOWS_CODE = {"lease-runner": "lease-runner", "cap-runner": "cap-runner", "status-page": None}
OLD_CODE = hubconf.STATE / "render-services-old-code.json"   # label → when it was first seen on old code


def old_code(index: dict) -> list[str]:
    """Problems: a platform service that has run the code of a commit other than the checkout's for longer than
    wait_limit_s() since a run of this script first saw it, with what its record says holds it. Only a record whose pid
    is the one the service manager runs the service as counts: a service with no record (one started before code_watch,
    which writes none until its own restart), a record left by a process that has ended, or a pid reused after a reboot
    says nothing here; a service not running is reported anyway."""
    import code_watch
    rev = hubconf.code_rev()
    if rev is None:
        return []
    try:
        seen = json.loads(OLD_CODE.read_text())
        seen = seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    now, new, problems = time.time(), {}, []
    docs = [(n, d) for n, d in index["projects"].items() if d.get("type") == "platform"] + \
        sorted((index.get("modules") or {}).items())
    for name, doc in docs:
        for svc in doc.get("services") or []:
            if not (rec_name := FOLLOWS_CODE.get(svc["name"])):
                continue
            rec = code_watch.record(rec_name) or {}
            old, label = rec.get("rev"), label_of(name, svc)
            if not old or old == rev:
                continue
            try:
                if int(rec["pid"]) != manager_pid(os.getuid(), label):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            first = new[label] = float(seen.get(label) or now)
            if now - first >= wait_limit_s():
                # the same words on every run while it lasts: a changed wording is a new alert (alert.once)
                problems.append(_("{label} ({project}): still runs the code of {old} since {since}, the checkout is at "
                                  "{new}; it exits by itself once nothing is in flight ({why})", label=label,
                                  project=name, old=str(old)[:8], new=rev[:8],
                                  since=time.strftime("%m-%d %H:%M", time.localtime(first)),
                                  why=rec.get("draining") or _("it says nothing holds it: its check may have stopped")))
    if new != seen:
        try:
            hubconf.write_atomic(OLD_CODE, json.dumps(new))
        except OSError as e:
            problems.append(_("{path} could not be written: {why}", path=OLD_CODE, why=e.strerror or e))
    return problems


def busy(project: str, svc: dict) -> bool:
    rel = WAIT_FOR_IDLE.get((project, svc["name"]))
    if not rel:
        return False
    try:
        rec = json.loads((hubconf.STATE / rel).read_text())
        pid, jobs = int(rec["pid"]), int(rec["jobs"])
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return False   # no record, or its runner isn't running: nothing in flight to lose
    return jobs > 0


def _waited() -> dict:
    try:
        d = json.loads(WAITED.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def note_waiting() -> None:
    """After an apply: when each label still waiting first waited (kept), the others dropped."""
    old, now = _waited(), time.time()
    new = {label: old.get(label, now) for label in waiting}
    if new != old:
        hubconf.write_atomic(WAITED, json.dumps(new))


# What the last _desired() found besides what is wanted (cleared by it, as waiting and overdue are)
cannot: dict[str, str] = {}             # a sandboxed service's label that can't be rendered -> its project
renamed: dict[str, dict] = {}           # a label sandboxed projects' entries declare (not used) -> {project: the one used}
dropped: list[str] = []                 # what of an entry isn't used although the service runs
cages: set[str] = set()                 # the folders sandboxed projects write
service_of: dict[str, tuple] = {}       # label -> (project, service name)
folders: dict[str, set[str]] = {}       # a folder services run in -> the projects and modules it is of
confined: set[str] = set()              # the sandboxed projects
# and the last _changes()
blocked: set[str] = set()               # wanted labels that aren't in place and can't be put there
removals: dict[str, tuple] = {}         # label to remove -> (the label the service has now, whether it may wait for it, notice)
left: dict[str, str] = {}               # a label only an older version's list has -> its file, which isn't the platform's


def label_of(project: str, svc: dict, doc: dict | None = None) -> str:
    """The label a service has (registry.service_label); without doc, the project's entry as the index has it."""
    return registry.service_label(project, doc if doc is not None else hubconf.projects().get(project) or {}, svc)


def plain(doc: dict, svc: dict, label: str) -> tuple[dict, str | None]:
    """An unsandboxed service's (plist, problem): its entry as it says, in the project's own folder."""
    d = registry.dirs(doc)
    cwd = d[svc.get("cwd") or registry.default_cwd(doc)]
    words = shlex.split(svc["run"])
    first = words[0]
    search = with_cli(hubconf.expand_env((svc.get("env") or {}).get("PATH") or DEFAULT_PATH))
    if not first.startswith("/"):
        first = str(cwd / first) if "/" in first else (shutil.which(first, path=search) or first)
    problem = None if pathlib.Path(first).is_absolute() and os.access(first, os.X_OK) else \
        _("`{command}` not found (PATH={path})", command=words[0], path=search)
    args = [first] + [str(cwd / w) if not w.startswith(("/", "-")) and (cwd / w).exists() else w for w in words[1:]]
    path_started = "/" in words[0] and "PATH" not in (svc.get("env") or {})
    if path_started:
        args = cli_last(args)
    p: dict = {"Label": label, "ProgramArguments": args, "WorkingDirectory": str(cwd)}
    env = {k: (str(cwd / v) if isinstance(v, str) and not v.startswith("/") and (cwd / v).exists()
               else hubconf.expand_env(str(v))) for k, v in (svc.get("env") or {}).items()}
    if "PATH" in env:
        env["PATH"] = with_cli(env["PATH"])
    elif not path_started:
        # the command was found by the platform's PATH (node etc.); the process itself (npm's child processes) must
        # see it too
        env["PATH"] = with_cli(DEFAULT_PATH)
    if env:
        p["EnvironmentVariables"] = env
    if svc.get("log"):
        p["StandardOutPath"] = p["StandardErrorPath"] = str(cwd / svc["log"])
    return p, problem


def skeleton(project: str, doc: dict, svc: dict, label: str) -> tuple[dict, str | None]:
    """A sandboxed service's (plist, problem): nothing of its entry but the words of its command. Raises ValueError where
    the entry can't be used at all."""
    cwd = registry.dirs(doc).get(svc.get("cwd") or registry.default_cwd(doc))
    if cwd is None:
        raise ValueError(_("it has no folder to run in"))
    words = shlex.split(svc["run"])
    # no more than its sessions get (bin/sandboxed-run); where that can't be done it is not run at all
    why = isolation.can_sandbox()
    problem = _("{project} is sandboxed and {why}; its services are not run unsandboxed", project=project, why=why) if why \
        else None
    p: dict = {"Label": label, "ProgramArguments": isolation.wrap_service(project, cwd, svc, words)}
    if "/" not in words[0]:   # one started by path gets the service manager's own, as the platform's do
        p["EnvironmentVariables"] = {"PATH": with_cli(DEFAULT_PATH)}
    return p, problem


def render(project: str, doc: dict, svc: dict) -> tuple[dict, str | None]:
    """(plist, problem). problem = it can't be run as it is (the command's first word cannot be resolved, no sandbox
    here); the plist is then unusable. Raises on an entry no plist can be made of."""
    svc = isolation.normal_service(svc)
    label = label_of(project, svc, doc)
    if not registry.label_ok(label):
        raise ValueError(_("{label!r} is not a label", label=label))
    p, problem = skeleton(project, doc, svc, label) if registry.runs_sandboxed(doc, project) else plain(doc, svc, label)
    p["RunAtLoad"] = True
    if svc.get("keepalive"):
        p["KeepAlive"] = True
    if svc.get("interval_s"):
        p["StartInterval"] = int(svc["interval_s"])
    if svc.get("throttle_s"):
        p["ThrottleInterval"] = int(svc["throttle_s"])
    if "StandardOutPath" not in p:
        base = hubconf.LOGS / label.removeprefix("local.")
        p["StandardOutPath"], p["StandardErrorPath"] = f"{base}-stdout.log", f"{base}-stderr.log"
    return p, problem


# ---------- whose a unit file is (the header) ----------

def units_record() -> pathlib.Path:
    return RENDERED.with_name("service-units.json")


def recorded() -> dict:
    """{label: {"files": {file name: [sha256, …]}, "project": name, "service": name, "loading": [labels]?}}: what each unit
    file was written as; loading = it is in place but not loaded yet, and the labels what it replaced was loaded as."""
    try:
        d = json.loads(units_record().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in (d.items() if isinstance(d, dict) else ())
            if registry.label_ok(k) and isinstance(v, dict) and isinstance(v.get("files"), dict)}


@contextlib.contextmanager
def _locked():
    units_record().parent.mkdir(parents=True, exist_ok=True)
    with open(units_record().with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def record(label: str, entry: dict | None) -> None:
    """One label's record (None = none), read, changed and written under a lock: config apply and the scheduler's run may
    be at it at once."""
    with _locked():
        rec = recorded()
        if rec.get(label) == entry:
            return
        rec.pop(label, None)
        if entry:
            rec[label] = entry
        hubconf.write_atomic(units_record(), json.dumps(dict(sorted(rec.items())), indent=1))


def listed() -> set[str]:
    """The labels in the list a version without the record wrote (this one's ends in a newline, note_rendered)."""
    try:
        text = RENDERED.read_text()
        d = [] if text.endswith("\n") else json.loads(text)
    except (OSError, ValueError):
        return set()
    return {x for x in d if isinstance(x, str)} if isinstance(d, list) else set()


def _systemd() -> bool:
    return hubconf.service_manager() == "systemd"


def unit_folder() -> pathlib.Path:
    if _systemd():
        import systemd_units
        return systemd_units.UNIT_DIR
    return AGENTS


@contextlib.contextmanager
def _folder():
    fd = os.open(unit_folder(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def _read(fd: int | None, name: str) -> bytes | None:
    try:
        f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(f).st_mode):
            return None
        with open(f, "rb", closefd=False) as fh:
            return fh.read()
    except OSError:
        return None
    finally:
        os.close(f)


def suffixes() -> tuple[str, ...]:
    return (".service", ".timer") if _systemd() else (".plist",)


def listing() -> list[str]:
    """The names in the unit folder."""
    try:
        with _folder() as fd:
            return sorted(os.listdir(fd))
    except OSError:
        return []


def installed() -> dict[str, dict[str, bytes | None]]:
    """{label: {file name: content}} of the unit folder, its direct children only, by the names they have there; None for
    what isn't a plain file that can be read."""
    out: dict[str, dict[str, bytes | None]] = {}
    with contextlib.suppress(OSError), _folder() as fd:
        for name in sorted(os.listdir(fd)):
            for suffix in suffixes():
                if name.endswith(suffix):   # not splitext: "..plist" and "...plist" are units too
                    out.setdefault(name.removesuffix(suffix), {})[name] = _read(fd, name)
    return out


def _inside(path: str, folder: str) -> bool:
    return any(os.path.commonpath([x, y]) == y for x, y in ((os.path.normpath(path), os.path.normpath(folder)),
                                                            (os.path.realpath(path), os.path.realpath(folder))))


ODD_AT_MOST = 20


def _odd_store() -> pathlib.Path:
    return RENDERED.with_name("service-units-odd.json")


def _told_odd() -> dict[str, str]:
    try:
        d = json.loads(_odd_store().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, str)} if isinstance(d, dict) else {}


def odd(units: dict) -> dict[str, str]:
    """{path: a command to look at it}: what an older version may have left that nothing is done about automatically,
    because its label is no name or because it is a link a project writes the end of. Never handed to the service manager,
    never stopped, changed or removed: the owner is told. Found by looking at names and links only: what a label that is no
    name spells (one an older version lists, an entry declares or that was told before), a unit of the platform's form under
    such a name, and links in the unit folder (systemd: in its .wants, .requires and .upholds folders) into a sandboxed
    project's folder."""
    out: dict[str, str] = {}
    folder, old, told = unit_folder(), listed(), _told_odd()
    look = lambda path: f"ls -l -- {shlex.quote(registry.said(path, 400))}"   # noqa: E731
    for label in sorted(old | set(renamed)):
        if not registry.label_ok(label) and label and "\0" not in label:
            paths = [str(folder / f"{label}{s}") for s in suffixes()]
            here = next((p for p in paths if os.path.lexists(p)), None)
            if here or label in old:
                out[here or paths[0]] = look(here or paths[0]) if here or _systemd() else \
                    f"launchctl print {shlex.quote(registry.said(f'gui/{os.getuid()}/{label}', 400))}"
    for label, files in units.items():
        if not registry.label_ok(label):
            for n, data in files.items():
                if label in old or data is not None and sandboxed_for(as_written(label, n, data)):
                    out[str(folder / n)] = look(str(folder / n))
    within = [folder, *(folder / n for n in listing() if _systemd() and n.endswith((".wants", ".requires", ".upholds")))]
    for d in within:
        with contextlib.suppress(OSError):
            for n in sorted(os.listdir(d)):
                if os.path.islink(d / n) and any(_inside(os.path.join(d, os.readlink(d / n)), c) for c in cages):
                    out[str(d / n)] = look(str(d / n))
    out.update({path: command for path, command in told.items() if os.path.lexists(path)})
    return out


def odd_said(found: dict[str, str]) -> list[str]:
    """One line for each, at most ODD_AT_MOST."""
    lines = [_("{path}: an older version may have run it outside a sandbox; brunna does nothing about it. Look at it: {command}",
               path=registry.said(path, 400), command=command) for path, command in sorted(found.items())]
    more = len(lines) - ODD_AT_MOST
    return lines[:ODD_AT_MOST] + ([_("and {n} more like these in {folder} and the folders linked there", n=more,
                                     folder=unit_folder())] if more > 0 else [])


def content(plist: dict) -> dict[str, bytes]:
    """The files a plist is written as. Raises where it can't be written."""
    if _systemd():
        import systemd_units
        return {n: text.encode() for n, text in systemd_units.files(plist).items()}
    return {f"{plist['Label']}.plist": plistlib.dumps(plist)}


def loads(data: bytes | None) -> dict | None:
    try:
        d = plistlib.loads(data or b"")
    except Exception:   # noqa: BLE001 (any file may be there)
        return None
    return d if isinstance(d, dict) else None


def says(files: dict[str, bytes | None], plist: dict) -> bool:
    """Whether what is installed is what plist is written as (a plist by its content, however it is laid out)."""
    new = content(plist)
    return files == new or set(files) == set(new) and not _systemd() and \
        loads(files[f"{plist['Label']}.plist"]) == plist


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Written(NamedTuple):
    """A unit as this script wrote it."""
    argv: list[str]
    cwd: str | None
    label: str     # what it calls itself
    mark: bool     # it carries something only the platform writes


KEYS = {"Label", "ProgramArguments", "RunAtLoad", "StandardOutPath", "StandardErrorPath"}
MAY = {"WorkingDirectory", "EnvironmentVariables", "KeepAlive", "StartInterval", "ThrottleInterval"}
_HEAD, _TAIL = plistlib.dumps({"k": True}).decode().split("\t<key>k</key>\n\t<true/>\n")
_PAIR = re.compile(r"\t<key>([^<]*)</key>\n\t(?:<string>([^<]*)</string>|<(true|false)/>|<integer>(-?[0-9]+)</integer>|"
                   r"<array>\n((?:\t\t<string>[^<]*</string>\n)+)\t</array>|"
                   r"<dict>\n((?:\t\t<key>[^<]*</key>\n\t\t<string>[^<]*</string>\n)+)\t</dict>)\n")


def read_plist(data: bytes) -> dict | None:
    """A plist exactly as plistlib.dumps writes a service's, whatever characters its values hold (plistlib itself refuses
    to read some that launchd takes); None for any other file."""
    def plain(s: str) -> str:
        return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text, out = data.decode("utf-8", "surrogateescape"), {}
    if not text.startswith(_HEAD) or not text.endswith(_TAIL):
        return None
    at, end = len(_HEAD), len(text) - len(_TAIL)
    while at < end:
        m = _PAIR.match(text, at, end)
        if not m or plain(m.group(1)) in out:
            return None
        _key, string, flag, number, array, table = m.groups()
        out[plain(m.group(1))] = (
            plain(string) if string is not None else flag == "true" if flag else number if number else
            [plain(x) for x in re.findall(r"<string>([^<]*)</string>", array)] if array else
            {plain(k): plain(v) for k, v in re.findall(r"<key>([^<]*)</key>\n\t\t<string>([^<]*)</string>", table)})
        at = m.end()
    return out


def cli_script() -> str:
    return cli_last([])[2]


def without_shell(argv: list[str]) -> list[str]:
    return argv[4:] if argv[:4] == cli_last([]) else argv


def as_written(label: str, name: str, data: bytes) -> Written | None:
    """data, when it is a unit as this script writes one for label (the file's; a plist's own may differ)."""
    if name.endswith(".service"):
        import systemd_units
        w = systemd_units.written(data.decode("utf-8", "surrogateescape"), label)
        return w and Written(w[0], w[1], label, True)
    d = read_plist(data)
    if d is None or not KEYS <= set(d) <= KEYS | MAY or d["RunAtLoad"] is not True or not isinstance(d["Label"], str) \
            or not isinstance(d["ProgramArguments"], list):
        return None
    cli, env = str(hubconf.cli_folder()), d.get("EnvironmentVariables") or {}
    base = hubconf.LOGS / d["Label"].removeprefix("local.")
    mark = (d["StandardOutPath"], d["StandardErrorPath"]) == (f"{base}-stdout.log", f"{base}-stderr.log") \
        or str(env.get("PATH", "")).split(":")[-1] == cli or without_shell(d["ProgramArguments"]) != d["ProgramArguments"]
    return Written(d["ProgramArguments"], d.get("WorkingDirectory"), d["Label"], mark)


def sandboxed_for(w: Written | None) -> str | None:
    """The project a unit of the platform's runs sandboxed-run for."""
    return isolation.wrapped_project(without_shell(w.argv)) if w else None


def _was_rendered(label: str, name: str, data: bytes, w: Written | None, owner: str | None) -> bool:
    """Whether a unit an older version lists under label is that version's (the header's "once")."""
    if w is None:
        nothing_reads_it = b"ExecStart=" not in data if name.endswith(".service") else loads(data) is None
        return nothing_reads_it and any(label.startswith(f"local.{p}.") for p in confined)
    of = folders.get(w.cwd or "", set())
    return w.label == label and (owner in of or bool(of) and (w.mark or any(label.startswith(f"local.{p}.") for p in of)))


def ours(label: str, files: dict[str, bytes | None], rec: dict, old: set[str], plist: dict | None = None,
         owner: str | None = None) -> bool:
    """Whether every file installed under label is the platform's (the header). plist: what is wanted there; owner: the
    project whose service it is."""
    if plist and files and says(files, plist):
        return True
    shas, whole = (rec.get(label) or {}).get("files") or {}, False
    for name, data in sorted(files.items()):   # a .service before its .timer
        if data is None:
            return False
        good = sha(data) in (shas.get(name) or ())
        if not good and name.endswith(".timer"):
            import systemd_units
            good = systemd_units.written_timer(data.decode("utf-8", "replace")) and \
                (whole or label in old)
        elif not good:
            w = as_written(label, name, data)
            good = bool(sandboxed_for(w)) or label in old and _was_rendered(label, name, data, w, owner)
        if not good:
            return False
        whole = True
    return bool(files)


def entry_of(label: str, files: dict[str, bytes | None]) -> dict:
    project, service = service_of.get(label) or ("", "")
    return {"files": {n: [sha(d)] for n, d in files.items() if d is not None}, "project": project, "service": service}


def _unload(uid: int, name: str) -> None:
    """One label out of launchd. Raises while launchd still has it, or doesn't say."""
    was = loaded_from(uid, name)
    r = launchctl("bootout", f"gui/{uid}/{name}")
    for _wait in range(40):   # bootout is asynchronous
        now = loaded_from(uid, name)
        if now is None or now and now != was:   # one that doesn't say where from may be it still
            return
        time.sleep(0.5)
    raise RuntimeError(_("{label} is still loaded: {error}", label=name, error=r.stderr.strip()[:200] or "launchctl bootout"))


def _print(uid: int, label: str) -> str | None:
    """What launchd says of a label; None when it says it hasn't it. Raises when it says neither."""
    r = launchctl("print", f"gui/{uid}/{label}")
    if r.returncode == 0 and "state = " in r.stdout:
        return r.stdout
    if r.returncode == NOT_FOUND:
        return None
    raise RuntimeError(_("launchd doesn't say whether it has {label}: {error}", label=label,
                         error=(r.stderr.strip() or r.stdout.strip())[:200] or f"exit {r.returncode}"))


NOT_FOUND = 113   # launchctl print: "Could not find service"


def loaded_from(uid: int, label: str) -> str | None:
    """The file the service manager has this label from; "" when it has the label and doesn't say, None when it hasn't.
    Raises when its answer isn't one."""
    if _systemd():
        import systemd_units
        return systemd_units.fragment(label)
    said = _print(uid, label)
    return None if said is None else \
        next((x.split("=", 1)[1].strip() for x in said.splitlines() if x.strip().startswith("path = ")), "")


def _runs_without_a_file(label: str, names_: set[str] | None = None) -> set[str]:
    """The unit files of a label (names_, or any it may have) that aren't there any more although the manager goes on running
    what was loaded from them."""
    path = loaded_from(os.getuid(), label)
    if path and not _is(path, label):
        return set()   # another file's
    if not _systemd():
        return {f"{label}.plist"} if path else set()
    import systemd_units
    return {n for n in names_ or {f"{label}{s}" for s in suffixes()} if systemd_units.active(n)}


def _is(path: str | None, label: str) -> bool:
    f = unit_folder() / f"{label}{'.service' if _systemd() else '.plist'}"
    return path is not None and os.path.realpath(path) == os.path.realpath(f)


def bootstrap(uid: int, label: str, path: pathlib.Path) -> subprocess.CompletedProcess:
    """bootout is asynchronous: bootstrapping before the old process has ended gives `5: Input/output error` (and the
    service stays unloaded). Wait for it to disappear from launchd (at most 20 seconds), and retry a few times on
    failure."""
    import time
    for _wait in range(40):
        if _print(uid, label) is None:
            break
        time.sleep(0.5)
    for attempt in range(5):
        r = launchctl("bootstrap", f"gui/{uid}", str(path))
        if r.returncode == 0:
            return r
        time.sleep(2 * (attempt + 1))
    return r


def running(uid: int, label: str) -> str | None:
    """The state from launchctl print (running / not running / …); None when not loaded."""
    if hubconf.service_manager() == "systemd":
        import systemd_units
        return systemd_units.state(label)
    said = _print(uid, label)
    if said is None:
        return None
    for line in said.splitlines():
        if line.strip().startswith("state = "):
            return line.split("=", 1)[1].strip()
    return "unknown"


def manager_pid(uid: int, label: str) -> int | None:
    """The pid the service manager runs a service as; None when it runs none."""
    if hubconf.service_manager() == "systemd":
        import systemd_units
        return systemd_units.main_pid(label)
    r = launchctl("print", f"gui/{uid}/{label}")
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        if line.strip().startswith("pid = "):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)


# ---------- applier (appliers.py): plan only computes, does not touch launchd; apply writes plists + reloads;
# health checks and alerts stay in main ----------

EXTERNAL = False


def _desired(index: dict) -> tuple[dict[str, tuple[str, dict]], list[str], set[str], set[str]]:
    """(label -> (project, plist), problems, labels left alone because the command cannot be resolved, keepalive
    labels)."""
    problems: list[str] = []
    keep: set[str] = set()       # command cannot be resolved: the current plist is left alone
    keepalive: set[str] = set()  # should be running all the time
    for found in (waiting, overdue, cannot, renamed, service_of, folders, confined, dropped, cages):
        found.clear()
    waited, now, rec, units = _waited(), time.time(), recorded(), installed()
    claims: dict[str, list[tuple]] = {}   # folded label -> who wants it
    # services of enabled modules too
    docs = sorted(index["projects"].items()) + sorted((index.get("modules") or {}).items())
    for name, doc in docs:
        with contextlib.suppress(Exception):   # an entry its own services say the problem of
            if registry.runs_sandboxed(doc, name):
                confined.add(name)
                cages.update(str(v) for k, v in registry.dirs(doc).items() if v and k != "hub")
            for key, folder in registry.dirs(doc).items():
                if folder and (key != "hub" or registry.platform_doc(doc, name)):
                    folders.setdefault(str(folder), set()).add(name)
        for svc in doc.get("services") or []:
            label, sandboxed = None, False
            try:
                sandboxed = registry.runs_sandboxed(doc, name)
                svc = isolation.normal_service(svc)
                label = label_of(name, svc, doc)
                if sandboxed and isinstance(declared := svc.get("label"), str) and declared != label:
                    renamed.setdefault(declared, {})[name] = label
                if sandboxed and (bad := sorted(set(svc.get("env") or {}) - set(isolation.usable_env(svc)))):
                    dropped.append(_("{label} ({project}): env {keys}: not a variable's name (letters, digits and _); it isn't "
                                     "given to the service", label=label, project=name, keys=", ".join(map(repr, bad))[:200]))
                plist, problem = render(name, doc, svc)
                content(plist)
                platform = registry.platform_doc(doc, name)
                if not platform and label.casefold() in registry.core_labels():
                    raise ValueError(_("the label is the platform's own"))
            except Exception as e:   # noqa: BLE001 (one entry never stops the others)
                what = svc.get("name") if isinstance(svc, dict) else svc
                problems.append(_("{label} ({project}): it can't be rendered: {error}", label=label or repr(what)[:80],
                                  project=name, error=f"{e or type(e).__name__}"[:300]))
                if isinstance(label, str):
                    service_of.setdefault(label, (name, svc["name"]))
                    if sandboxed:
                        cannot[label] = name
                    else:
                        keep.add(label)
                continue
            claims.setdefault(label.casefold(), []).append((name, svc, label, plist, problem, sandboxed, platform))
    want: dict[str, tuple[str, dict]] = {}
    for claim in claims.values():
        if len(claim) > 1:   # the platform's; the one whose unit it is; the first name
            def rank(c):
                name, _svc, label, plist, _problem, _sb, platform = c
                files = units.get(label) or {}
                its = files and (says(files, plist) or name in {sandboxed_for(as_written(label, n, d))
                                                                     for n, d in files.items() if d is not None})
                return not platform, (rec.get(label) or {}).get("project") != name, not its, name
            claim.sort(key=rank)
        for name, _svc, label, *_rest in claim[1:]:
            problems.append(_("{label} ({project}): the label is {other}'s; this service is not run under it", label=label,
                              project=name, other=f"{claim[0][0]}/{claim[0][1]['name']}"))
        name, svc, label, plist, problem, sandboxed, _platform = claim[0]
        service_of[label] = (name, svc["name"])
        if sandboxed and problem:
            problems.append(_("{label} ({project}): {problem}", label=label, project=name, problem=problem))
            cannot[label] = name
            continue
        want[label] = (name, plist)
        if problem:
            problems.append(_("{label} ({project}): {problem}; the plist is left as it is (and not created if it is missing)",
                              label=label, project=name, problem=problem))
            keep.add(label)
        elif busy(name, svc):
            if now - float(waited.get(label) or now) < wait_limit_s():
                keep.add(label)
                waiting.append(label)
            else:
                overdue.append(label)
        if svc.get("keepalive"):
            keepalive.add(label)
    return want, problems, keep, keepalive


def _not_ours(label: str, project: str, path) -> str:
    return _("{label} ({project}): {path} is not a unit brunna wrote (or it was changed since); it is left as it is and "
             "this service is not applied. To have brunna take it over: bin/render-services.py --adopt {label}",
             label=label, project=project, path=path)


def _changes(want: dict, keep: set[str], only: str | None = None, problems: list[str] | None = None) -> list[tuple]:
    """(Change, the plist's current content, the plist to write); for remove the last two are None. What is installed
    and isn't the platform's is in none of them, and said in problems."""
    from appliers import Change
    out, uid, rec, old, units = [], os.getuid(), recorded(), listed(), installed()
    problems = [] if problems is None else problems
    for found in (blocked, removals, left):
        found.clear()
    problems += odd_said(odd(units)) if not only else []
    entries = listing()
    for label, (proj, plist) in sorted(want.items()):
        if (only and label != only) or label in keep:
            continue
        files, entry = units.get(label) or {}, rec.get(label) or {}
        try:
            path = None if files and _systemd() else loaded_from(uid, label)
            foreign = unit_folder() / min(files) if files and not ours(label, files, rec, old, plist, proj) else \
                path if path and not _is(path, label) else _variant(label, plist, entries, units, rec, old, keep)
            if foreign:
                blocked.add(label)
                problems.append(_not_ours(label, proj, foreign))
                continue
            if says(files, plist) and not entry.get("loading"):
                # the file is right but the service manager isn't running it (11-02: unloaded by something else for 14
                # hours while every check said "matches"): loading it again is the same change as writing it
                if _systemd() or running(uid, label) is not None:
                    continue
                detail = _(" not loaded")
            elif _systemd():
                new = content(plist)
                detail = _(" changed {keys}", keys=_(", ").join(sorted(n for n in set(files) | set(new)
                                                                        if files.get(n) != new.get(n))))
            else:
                cur = loads(files.get(f"{label}.plist")) or {}
                detail = _(" changed {keys}", keys=_(", ").join(sorted(k for k in set(cur) | set(plist)
                                                                        if cur.get(k) != plist.get(k))))
        except Exception as e:   # noqa: BLE001
            blocked.add(label)
            problems.append(_("{label} ({project}): it can't be rendered: {error}", label=label, project=proj,
                              error=f"{e or type(e).__name__}"[:300]))
            continue
        out.append((Change("update" if files else "add", f"service:{label}", proj + (":" + detail if files else "")),
                    (loads(files.get(f"{label}.plist")) or {}) if files and not _systemd() else None, plist))
    for label in sorted((set(units) | set(rec) | old) - set(want) - keep):
        if registry.label_ok(label) and (not only or label == only):   # any other: odd()
            _guarded(lambda: out.extend(_unwanted(label, units.get(label) or {}, rec, old, want, problems)), label, "", problems)
    # removals first: a service whose label changed (the rename, docs/design/rename-brunna.md) frees its port before
    # the new label binds it (its file is in place by then, perform())
    return sorted(out, key=lambda c: c[0].op != "remove")


def _variant(label: str, plist: dict, entries: list[str], units: dict, rec: dict, old: set[str], keep: set[str]):
    """An entry spelled the same as a wanted file but for case (its suffix too), when it isn't the platform's to remove: on
    a volume that folds case they are one file."""
    for n in content(plist):
        for e in entries:
            if e != n and e.casefold() == n.casefold():
                other = next((v for v, f in units.items() if e in f), None)
                if other is None or other in keep or not ours(other, units[other], rec, old):
                    return unit_folder() / e
    return None


def _unwanted(label: str, files: dict, rec: dict, old: set[str], want: dict, problems: list[str]) -> list[tuple]:
    """The removal of one installed or recorded label that isn't wanted, if it is the platform's."""
    from appliers import Change
    entry = rec.get(label) or {}
    if files and not ours(label, files, rec, old):
        if label in old or entry:
            left[label] = str(unit_folder() / min(files))
        return []
    if not files and not entry and not _runs_without_a_file(label):
        return []
    whose = {entry.get("project"), *(sandboxed_for(as_written(label, n, d)) for n, d in files.items() if d is not None)}
    renames = next((new for project, new in (renamed.get(label) or {}).items() if project in whose), None)
    now = next((s for s, of in service_of.items() if entry and of == (entry.get("project"), entry.get("service"))),
               None) or renames or next((s for s in names.unit_spellings(label) if s in want), None)
    # what may go on running meanwhile: an unsandboxed service's unit (a sandboxed one's has no other label to go to)
    safe = not renames and entry.get("project") not in confined
    if now in blocked and safe:
        problems.append(_("{label}: it stays until the service's new unit, {new}, is in place", label=label, new=now))
        return []
    why = _("{project}'s sandboxed unit can't be rendered", project=cannot[label]) if label in cannot else \
        _("a sandboxed project's label is not used; the service is now {label}", label=renames) if renames else \
        _("no longer in the registry")
    if now in blocked:
        why += _("; {new} can't be used, so the service doesn't run", new=now)
        problems.append(f"{label}: {why}")
    removals[label] = (now, safe, f"{label}: {why}" if renames else None)
    return [(Change("remove", f"service:{label}", why), None, None)]


def _settle(want: dict, only: str | None = None) -> bool:
    """Before anything is changed: the record of each of the platform's units that has none (on the first run with the
    record, after it was lost), whatever becomes of it in this run, so a step that fails leaves it known as the platform's.
    Whether that is so of all of them: only then is the list an older version wrote not needed any more."""
    rec, old, all_known = recorded(), listed(), True
    for label, files in sorted(installed().items()):
        has = (rec.get(label) or {}).get("files") or {}
        if (only and label != only) or all(d is not None and sha(d) in has.get(n, ()) for n, d in files.items()):
            continue
        try:
            project, plist = want.get(label) or (service_of.get(label, (None,))[0], None)
            if registry.label_ok(label) and ours(label, files, rec, old, plist, project):
                record(label, entry_of(label, files))
        except Exception:   # noqa: BLE001 (one label's record never stops a run: the next one writes it)
            all_known = False
    return all_known


def _guarded(what, label: str, project: str, problems: list[str]) -> bool:
    """what() with whatever goes wrong said; one unit never stops another."""
    try:
        what()
        return True
    except Exception as e:   # noqa: BLE001
        print("    " + _("⚠️ {error}", error=e))
        problems.append(_("{label} ({project}): it could not be applied: {error}", label=label, project=project,
                          error=f"{e or type(e).__name__}"[:300]))
        return False


def _write(fd: int, name: str, data: bytes) -> None:
    f = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644, dir_fd=fd)
    with open(f, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(f)


def _put(fd: int, name: str, data: bytes) -> None:
    tmp = f".{name}.{os.getpid()}.tmp"
    try:
        _write(fd, tmp, data)
        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=fd)


def place(label: str, plist: dict) -> None:
    """The files of a wanted unit, in place: each written whole beside first, then the record (what was there and what
    will be, so a run that ends in between is taken up by the next), then each put in its place. Nothing is unloaded."""
    unit_folder().mkdir(parents=True, exist_ok=True)
    new, files = content(plist), installed().get(label) or {}
    old = recorded().get(label) or {}
    service, timer = f"{label}.service", f"{label}.timer"
    # systemd goes on running what a file that was replaced said: a service that isn't a skeleton, or that is now an interval
    # unit (only its timer is enabled), is stopped before the new one is loaded, a sandboxed project's with all it started
    w = as_written(label, service, files[service]) if files.get(service) else None
    by = "kill" if service_of[label][0] in confined else "stop"
    stop = old.get("stop") or (by if _systemd() and service not in files and (old or label in listed())
                               and service in _runs_without_a_file(label) else None if service not in files else
                               "kill" if not (sandboxed_for(w) and w.argv[1:3] == ["-I", "-B"]) and service_of[label][0] in confined
                               else "stop" if timer in new and timer not in files else None)
    drop = sorted({*(old.get("drop") or ()), *files} - set(new))
    was = {label, *(old.get("loading") or ()), *_called(label, files)}
    with _folder() as fd:
        tmp = {n: f".{n}.{os.getpid()}.tmp" for n in new}
        try:
            for n, data in new.items():
                _write(fd, tmp[n], data)
            both = {n: [sha(d) for d in (files.get(n), new.get(n)) if d is not None] for n in {*files, *new}}
            record(label, {**entry_of(label, {}), "files": both, "loading": sorted(was),
                           **({"stop": stop} if stop else {}), **({"drop": drop} if drop else {})})
            for n in new:
                os.replace(tmp[n], n, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            for n in tmp.values():
                with contextlib.suppress(OSError):
                    os.unlink(n, dir_fd=fd)


def _called(label: str, files: dict[str, bytes | None]) -> set[str]:
    """What a label's plist calls itself, where that is a name; where it isn't, launchd may run it as that, and it is told
    (nothing is done about a label that is no name)."""
    out = set()
    for n, d in files.items():
        w = as_written(label, n, d) if n.endswith(".plist") and d is not None else None
        if w and registry.label_ok(w.label):
            out.add(w.label)
        elif w and (key := f"label {registry.said(w.label, 400)}") not in _told_odd():   # once: its file says it until it is replaced
            command = "launchctl print " + shlex.quote(registry.said(f"gui/{os.getuid()}/{w.label}", 400))
            notice_once(_("{path}: an older version may have run it outside a sandbox; brunna does nothing about it. Look at it: {command}",
                          path=unit_folder() / n, command=command))
            hubconf.write_atomic(_odd_store(), json.dumps({**_told_odd(), key: command}))
    return out


def _kill(label: str, name: str, data: bytes | None) -> bool:
    """Whether what a .service started must all end with it: a sandboxed project's unit that isn't its skeleton."""
    w = as_written(label, name, data) if data else None
    of = folders.get((w.cwd if w else None) or "", set())
    return bool(sandboxed_for(w)) and w.argv[1:3] != ["-I", "-B"] or bool(of & confined)


def bootout(uid: int, label: str, called: set[str]) -> None:
    """Unloads what the service manager loaded from label's own file, under the labels that file may call itself; under
    another one than the file's only when the manager says so. Raises while one is still loaded, or it doesn't say."""
    for name in sorted(called):
        path = loaded_from(uid, name)
        if path == "" and name != label:
            raise RuntimeError(_("launchd doesn't say whether it has {label}: {error}", label=name, error="no path"))
        if path == "" or path and _is(path, label):
            _unload(uid, name)


def load(uid: int, label: str, project: str, plist: dict, problems: list[str]) -> None:
    """A unit whose files are in place, loaded; what was loaded from its file before is unloaded first. Until the manager
    says it has it, the record keeps that it isn't (the next run loads it)."""
    entry = recorded().get(label) or {}
    if _systemd():
        import systemd_units
        if entry.get("stop"):
            systemd_units.daemon_reload()   # what it is stopped by is the new file: the old one's rules are not obeyed
            systemd_units.stop(f"{label}.service", kill=entry["stop"] == "kill")
        if entry.get("drop"):   # e.g. it used to have an interval and now runs all the time
            _take_out(label, {n: False for n in entry["drop"]})
        r, how = systemd_units.load(plist), "systemctl enable"
    else:
        bootout(uid, label, {label, *entry.get("loading", ())})
        r, how = bootstrap(uid, label, AGENTS / f"{label}.plist"), "bootstrap"
    if r.returncode != 0:
        error = r.stderr.strip()
        print("    " + (_("⚠️ bootstrap failed: {error}", error=error) if how == "bootstrap" else
                        _("⚠️ systemctl enable failed: {error}", error=error)))
        problems.append(_("{label} ({project}): bootstrap failed: {error}", label=label, project=project, error=error[:200])
                        if how == "bootstrap" else
                        _("{label} ({project}): systemctl enable failed: {error}", label=label, project=project,
                          error=error[:200]))
        return
    record(label, entry_of(label, {n: d for n, d in content(plist).items()}))


def _take_out(label: str, names_: dict[str, bool]) -> None:
    """systemd: files of a label (-> whether all it started ends too) made inert, then stopped, then deleted once systemd
    says they don't run. The record has what they were and the inert text meanwhile."""
    import systemd_units
    there, entry = installed().get(label) or {}, recorded().get(label) or entry_of(label, {})
    names_ = {n: kill for n, kill in names_.items() if there.get(n, b"") is not None}   # never what is a link or a folder now
    files = {**entry["files"], **{n: [*entry["files"].get(n, []), *(sha(d) for d in (there.get(n), systemd_units.inert(n)) if d)]
                                  for n in names_}}
    record(label, {**entry, "files": files, **({"stop": "kill"} if any(names_.values()) else {})})
    with _folder() as fd:
        for n in names_:   # one whose file is gone too: systemd disables only what has one
            _put(fd, n, systemd_units.inert(n))
    systemd_units.daemon_reload()
    for n in sorted(names_, reverse=True):   # the timer first: it starts the service
        systemd_units.stop(n, kill=names_[n])
    with _folder() as fd:
        for n in names_:
            os.unlink(n, dir_fd=fd)
    systemd_units.daemon_reload()


def remove(uid: int, label: str) -> None:
    """A unit of the platform's, unloaded and then, once the manager hasn't it any more, its files deleted."""
    files, entry = installed().get(label) or {}, recorded().get(label) or {}
    if _systemd():
        gone = set() if files else _runs_without_a_file(label, set(entry.get("files") or ()))
        _take_out(label, {n: n.endswith(".service") and (entry.get("stop") == "kill" or _kill(label, n, files.get(n)))
                          for n in {*files, *gone}})
    else:
        bootout(uid, label, {label, *entry.get("loading", ()), *_called(label, files)})
        with contextlib.suppress(FileNotFoundError), _folder() as fd:
            for n in files:
                os.unlink(n, dir_fd=fd)
    record(label, None)


def perform(todo: list[tuple], problems: list[str], tell=lambda ch, cur, plist: None) -> None:
    """The changes: every file in place, then the removals, then the loading (the header). Each behind its own guard."""
    uid, units, done = os.getuid(), {v.casefold(): v for v in installed()}, []
    steps = [(ch, cur, plist, ch.target.removeprefix("service:"), ch.detail.split(":")[0]) for ch, cur, plist in todo]

    def put(label, project, plist) -> None:
        if not _guarded(lambda: place(label, plist), label, project, problems):
            blocked.add(label)
    # a unit spelled the same but for case is removed first: on a volume that folds case they are one file
    late = {label for ch, _cur, _plist, label, _project in steps if units.get(label.casefold(), label) != label}
    for ch, _cur, plist, label, project in steps:
        if ch.op != "remove" and label not in late:
            put(label, project, plist)
    for ch, cur, plist, label, project in steps:
        if ch.op == "remove":
            now, safe, notice = removals.get(label) or (None, False, None)
            if now in blocked and safe:
                problems.append(_("{label}: it stays until the service's new unit, {new}, is in place", label=label, new=now))
                continue
            tell(ch, cur, plist)
            if _guarded(lambda: remove(uid, label), label, ch.detail, problems) and notice:
                done.append(notice)
            continue
        tell(ch, cur, plist)
        if label in late:
            put(label, project, plist)
        if label not in blocked:
            _guarded(lambda: load(uid, label, project, plist, problems), label, project, problems)
    for text in done:   # told once, whoever runs it
        _guarded(lambda: notice_once(text), text, "", problems)


def notices() -> pathlib.Path:
    return RENDERED.with_name("service-units-notices.json")


def _pending() -> list[str]:
    try:
        d = json.loads(notices().read_text())
    except (OSError, ValueError):
        return []
    return [x for x in d if isinstance(x, str)] if isinstance(d, list) else []


def notice_once(text: str) -> None:
    """Something to tell the owner once: kept until it was sent (deliver)."""
    with _locked():
        if text not in (texts := _pending()):
            hubconf.write_atomic(notices(), json.dumps([*texts, text], ensure_ascii=False))


def deliver() -> None:
    """Each kept notice, dropped once it was sent (with no channel to send on: once printed)."""
    for text in _pending():
        print(f"ℹ️ {text}")
        if alert.send(f"ℹ️ {text}") or not alert.configured():
            with _locked():
                hubconf.write_atomic(notices(), json.dumps([x for x in _pending() if x != text], ensure_ascii=False))


def _tell_odd() -> None:
    """What odd() finds, told once until it is gone (kept by path, so it stays known when the older list is not)."""
    found, told = odd(installed()), _told_odd()
    if new := {p: c for p, c in found.items() if p not in told}:
        for text in odd_said(new):
            notice_once(text)
    found |= {k: v for k, v in told.items() if k.startswith("label ")}   # _called()'s
    if found != told:
        hubconf.write_atomic(_odd_store(), json.dumps(found))


def finish(problems: list[str], known: bool) -> None:
    """After a whole run: the labels whose units are the platform's (what a version without the record reads), and, once,
    what an older version's list or the record names that isn't. known: whether nothing needs that list any more; without
    it it stays for the next run."""
    for label, path in sorted(left.items()):
        told = _("{label}: no service has this label any more, but {path} is not a unit brunna wrote (or it was changed "
                 "since); it is left as it is: unload and delete it yourself if it should go", label=label, path=path)
        known = _guarded(lambda: (notice_once(told), record(label, None)), label, "", problems) and known
    known = _guarded(_tell_odd, str(_odd_store()), "", problems) and known
    for path, write in ((WAITED, note_waiting), (notices(), deliver),
                        (RENDERED, lambda: hubconf.write_atomic(RENDERED, json.dumps(sorted(recorded())) + "\n"))):
        if path is RENDERED and not known:
            continue
        try:
            write()
        except OSError as e:
            problems.append(_("{path} could not be written: {why}", path=path, why=e.strerror or e))


def adopt(label: str) -> int:
    """Let a wanted service's label take over the unit file that is there and isn't the platform's: shown, asked, a copy
    kept; the next run replaces it. Never a label no service has: nothing is deleted on its account."""
    import owner_terminal
    want, _problems, keep, _keepalive = _desired(registry.refresh())
    files = installed().get(label) or {}
    if not registry.label_ok(label) or not files or any(d is None for d in files.values()):
        print(_("{label}: there is no unit file of this name to take over", label=label), file=sys.stderr)
        return 1
    if label not in want:
        print(_("{label}: no service has this label; nothing was done", label=label), file=sys.stderr)
        return 1
    if owner_terminal.refusal() or not sys.stdin.isatty():
        print(_("--adopt is the owner's, at a terminal of their own; nothing was done"), file=sys.stderr)
        return 1
    for n, data in files.items():
        print(f"--- {unit_folder() / n}\n{registry.said(data.decode('utf-8', 'replace'))}")
    aside = units_record().with_name("service-units-adopted")
    print(_("brunna's next run replaces it with {project}'s {service} (a copy stays in {folder})", folder=aside,
            project=want[label][0], service=service_of[label][1]))
    if input(_("Take it over? [yes/no]") + " ").strip().lower() != "yes":
        print(_("Nothing was done."))
        return 1
    aside.mkdir(parents=True, exist_ok=True)
    for n, data in files.items():
        (aside / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}").write_bytes(data)
    record(label, entry_of(label, files))
    print(_("{label}: taken over", label=label))
    return 0


def write_wrapper(problems: list[str]) -> None:
    """The `brunna` every service's PATH ends with (hubconf.cli_folder), written before any unit that names it."""
    try:
        if hubconf.write_cli_wrapper():
            print(_("wrote {path}", path=hubconf.cli_folder() / names.current("cli")))
    except OSError as e:
        problems.append(_("{path} could not be written: {why}", path=hubconf.cli_folder(), why=e.strerror or e))


def plan(index: dict | None = None) -> list:
    if hubconf.service_manager() is None:
        return []   # no service manager: nothing can be applied (doctor and main say so)
    want, _problems, keep, _keepalive = _desired(index or registry.refresh())
    return [c for c, _cur, _plist in _changes(want, keep)]


def apply(index: dict | None = None) -> list:
    if hubconf.service_manager() is None:
        return []
    want, problems, keep, _keepalive = _desired(index or registry.refresh())
    write_wrapper(problems)
    todo = _changes(want, keep, problems=problems)
    known = _settle(want)
    perform(todo, problems)
    finish(problems, known)
    return [c for c, _cur, _plist in todo]


def _installed_by_us() -> tuple[list[str], list[str]]:
    """(the labels whose units are the platform's, the files under its other labels, which aren't, and what odd() finds)."""
    if not hubconf.service_manager():   # nothing here can say what is loaded: the older list's are the platform's too
        return sorted(x for x in {*recorded(), *listed()} if registry.label_ok(x)), []
    try:
        want, _problems, keep, _keepalive = _desired(json.loads(hubconf.REGISTRY_INDEX.read_text()))
    except (OSError, ValueError, KeyError):
        want, keep = {}, set()
    rec, old, units, mine = recorded(), listed(), installed(), []
    others = sorted(odd(units))
    for label in sorted((set(rec) | old) - set(units)):
        with contextlib.suppress(RuntimeError):   # what it doesn't say is told by the removal, and the record kept
            if not registry.label_ok(label) or label not in rec and not _runs_without_a_file(label):
                continue
        mine.append(label)
    for label, files in sorted(units.items()):
        project, plist = want.get(label) or (None, None)
        if registry.label_ok(label) and ours(label, files, rec, old, plist, project):
            mine.append(label)
        elif label in {*want, *keep, *rec, *old}:
            others += [str(unit_folder() / n) for n in files]
    return mine, list(dict.fromkeys(others))


def uninstall(apply: bool) -> list[str]:
    """agenthub uninstall: every service unit that is the platform's (the header; and only those), unloaded and its file
    removed; the labels, with apply those that are gone. What the manager still has keeps its file and the record.
    left_behind() names the files under its labels that aren't its."""
    from appliers import Change
    labels, _others = _installed_by_us()
    if apply and labels and hubconf.service_manager():
        removals.clear()
        blocked.clear()
        perform([(Change("remove", f"service:{label}", ""), None, None) for label in labels], [])
        still, _others = _installed_by_us()
        labels = [x for x in labels if x not in still]
        if not still:
            RENDERED.unlink(missing_ok=True)
            units_record().unlink(missing_ok=True)
    return labels


def left_behind() -> list[str]:
    """The unit files under labels the platform knows that it didn't write (or that were changed since), and what
    odd() finds: uninstall leaves them, and should name them. Ask before uninstall(True)."""
    return _installed_by_us()[1]


def usage() -> str:
    return _("usage: render-services.py [--dry-run] [--only <label>]") + "\n" + \
        _("       render-services.py --adopt <label>   (a unit file brunna didn't write; asked at a terminal)")


def main() -> int:
    argv = sys.argv[1:]
    if argv in (["-h"], ["--help"]):
        print(usage())
        return 0
    if not hubconf.known_args(argv, {"--dry-run"}, {"--only", "--adopt"}) or ("--adopt" in argv and len(argv) != 2):
        print(usage(), file=sys.stderr)
        return 2
    if hubconf.worktree_lookup() and "--dry-run" not in argv:
        # units written from here would run the services from this copy
        print(_("render-services: {root} is a linked worktree of the hub; it changes services only from the main "
                "checkout ({main}); --dry-run works here", root=hubconf.ROOT, main=hubconf.MAIN), file=sys.stderr)
        return 1
    if hubconf.service_manager() is None:
        print(_("services: {reason}", reason=hubconf.no_service_manager()))
        return 0
    if "--adopt" in argv:
        return adopt(argv[1])
    dry = "--dry-run" in sys.argv
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    index = registry.refresh()
    uid = os.getuid()
    want, problems, keep, keepalive = _desired(index)
    if only:
        # either spelling of a platform label (recover-service passes the one services.tsv names)
        only = next((s for s in names.unit_spellings(only) if s in want), only)
    if not dry:
        write_wrapper(problems)
    todo = _changes(want, keep, only, problems)
    known = not dry and _settle(want, only)
    for label in waiting:
        if not only or label == only:
            print(_("{label}: has work in flight; its changes wait until it has none", label=label))
    for label in overdue:
        if not only or label == only:
            print(_("{label}: still has work in flight after waiting {hours:g} hours; changed now", label=label,
                    hours=wait_limit_s() / 3600))
    changed = [c for c in todo if c[0].op != "remove"]

    def tell(ch, cur, plist):
        label = ch.target.removeprefix("service:")
        if ch.op == "remove":
            print(("(dry-run) " if dry else "") +
                  (_("{label}: no longer in the registry, unloading", label=label) if ch.detail == _("no longer in the registry")
                   else _("{label}: {why}, unloading", label=label, why=ch.detail)))
            return
        print(("(dry-run) " if dry else "") + _("{label} ({project}): {op}", label=label, project=want[label][0],
                                                op=_("add") if ch.op == "add" else _("update")))
        for k in sorted(set(cur) | set(plist)) if cur is not None else ():
            if cur.get(k) != plist.get(k):
                print(f"    {k}: {cur.get(k)!r} → {plist.get(k)!r}")
    if dry:
        for step in todo:
            tell(*step)
    else:
        perform(todo, problems, tell)
    if not dry and not only:
        finish(problems, known)
    for label, new in sorted((label, new) for label, by in renamed.items() for new in by.values()):
        if not only or only in (label, new):
            print(_("{label}: a sandboxed project's label is not used; the service is {new}", label=label, new=new))
    if not changed:
        print(_("services: everything matches the registry"))
    if not dry and not only:
        for label in sorted(want):
            if any(label in x for x in problems):
                continue
            try:
                state = running(uid, label)
            except RuntimeError as e:
                problems.append(f"{label} ({want[label][0]}): {e}")
                continue
            if state is None:
                problems.append(_("{label} ({project}): not loaded in launchd", label=label, project=want[label][0]))
            elif label in keepalive and state != "running":
                problems.append(_("{label} ({project}): the keepalive service is not running (state = {state})", label=label,
                                  project=want[label][0], state=state))
        problems += old_code(index)
        alert.once("registry-ignored", sorted([*registry.ignored_before(index).values(), *dropped]),
                   _("No registry entry has a part that is ignored any more"))
        alert.once("render-services", problems, _("The background services are all back to normal (render-services)"))
    for x in [*dropped, *problems]:
        print(f"⚠️ {x}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
