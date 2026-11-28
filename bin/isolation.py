"""Running a sandboxed project's own services and command jobs isolated, whatever the OS: the neutral front that
bin/sandboxed-run, render-services and the scheduler's job list use. It picks the adapter for this machine; each
adapter turns the neutral policy (policy.py) into its OS's mechanism:
  macOS   seatbelt.py   Seatbelt profile (seatbelt.service_profile), sandbox-exec
  Linux   landlock.py   Landlock rules + a seccomp filter on the sandboxed-run process before it execs
Elsewhere, or when the adapter can't work on this machine (no sandbox-exec, a kernel without Landlock ABI 4), there is
no fallback: can_sandbox() says why, render-services doesn't render the service, sandboxed-run exits 78 (fail closed;
never unsandboxed). What each adapter enforces and can't: docs/design/capabilities.md.

The backend interface (what a module needs to be an adapter here):
  can_sandbox() → None | why not              whether it can work on this machine
  guarantees() → set[str]                     which of GUARANTEES it enforces on this machine
  exec_service(pol, dirs, port, argv, entry)  replace this process with argv under the policy (raises OSError when it
                                              can't; nothing was run). entry (Entry, a service's own environment and
                                              log; None = none) is applied inside: no process outside the sandbox has
                                              it, and no path of it is looked at or opened outside
  confined_now(marked) → bool                 optional: whether this process runs inside this adapter's kind of
                                              sandbox (marked: started through sandboxed-run); missing or unsure = no
An adapter that doesn't guarantee every one of REQUIRED, or names a guarantee not in GUARANTEES, is refused like one
that can't work here. tests/test_sandboxed_services.py runs the same probe through every adapter on its own OS and
checks each guarantee it declares (Conformance): a new backend is added by passing that, not by review alone.

  sandboxed(doc) → bool                       whether this project's services and command jobs go through it
  wrap(project, cwd, argv, port) → argv       the command through bin/sandboxed-run, the platform's Python isolated (-I)
  wrap_service(project, cwd, svc, words) → argv
                                              a declared service through it: its entry is read by sandboxed-run
  wrapped_project(argv) → name | None         the project a command runs this hub's sandboxed-run for
  normal_service(svc), service_digest(svc), service_problem(svc)
                                              one service entry in its normal form, what of it the unit stands for, and
                                              why it can't be used (render-services and sandboxed-run read it alike)
  as_project(project, cwd, argv) → argv       argv as the project: wrapped when it is sandboxed, as is otherwise (the
                                              platform's git in project repos goes only through this: project_git.py,
                                              with a timeout and an output cap)
  confined_now(marked) → bool                 whether this process runs in a sandbox that holds: the adapter's check
                                              and the platform's state and secrets unreadable (reads_blocked)
  run_as_project(project, doc, argv, guard)   for a platform command that runs a sandboxed project's own commands
                                              (brunna test): None when it may go on here (confined, or the project
                                              isn't sandboxed), else (argv, env) to run instead, or SandboxRefused
  adapter() → (name, module) | None           this machine's adapter
  can_sandbox() → None | why not              the adapter's own reason, or a missing/unknown guarantee
  guarantees() → list[str]                    what this machine's adapter guarantees (empty when it can't sandbox)
  exec_service(pol, dirs, port, argv, entry)  through the adapter, after can_sandbox() and unmet(pol) (a policy asking
                                              for a guarantee the adapter lacks, NEEDS: refused, never run without it)
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shlex
import sys
import unicodedata
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402

ADAPTERS = {"darwin": "seatbelt", "linux": "landlock"}

# every guarantee an adapter may declare; the names are the rows of capabilities.md's "What each OS enforces" table
GUARANTEES = {
    "own-writes-only": "writes only to its own folders, tool caches and the temp area (not the bus)",
    "isolated-reads": "other projects, platform state and credentials can't be read",
    "declared-port-only": "only the port the service declares can be bound",
    "localhost-inbound-only": "connections are accepted only through localhost",
    "keychain-blocked": "the Keychain and launchd can't be reached",
    "unix-sockets-blocked": "unix-domain sockets can't be created (no D-Bus, no systemd user manager)",
    "signals-blocked": "the owner's other processes can't be signalled",
    "runtime-files-locked": "what its sessions load settings, hooks and tools from can't be written (policy protect)",
    "git-dirs-fenced": "for a project whose git goes through the platform (policy git_fence), no git dir can be written "
                       "or made anywhere: no .git, no HEAD that isn't a folder (docs/design/git-broker.md)",
    "no-mounts": "nothing can be mounted, attached as a disk or unmounted: no file system it built or reached appears "
                 "over a folder, where its write rules don't reach (docs/design/capabilities.md, \"11-15: no mounts\")",
}
# what a policy asks for that only some adapters can give: a policy carrying it is refused where the adapter lacks it
NEEDS = {"git_fence": "git-dirs-fenced"}
# what a sandboxed service or job must never get past, whatever the backend
# (no-mounts: a mount puts files over a folder past own-writes-only, so that guarantee means nothing without it)
REQUIRED = ("own-writes-only", "isolated-reads", "declared-port-only", "no-mounts")


def in_system_tmp(path) -> bool:
    """Whether this is in a temp area some sandbox writes (any adapter's, whichever this machine has): the same uid, so every sandboxed
    project's own processes can write it."""
    import landlock
    import seatbelt
    real = pathlib.Path(os.path.realpath(path))
    return any(real.is_relative_to(os.path.realpath(t)) for t in [*seatbelt.TMP, *landlock.TMP])


def adapter():
    name = next((m for p, m in ADAPTERS.items() if sys.platform.startswith(p)), None)
    if not name:
        return None
    import importlib
    return name, importlib.import_module(name)


def can_sandbox() -> str | None:
    a = adapter()
    if not a:
        return f"there is no sandbox adapter for this system ({sys.platform})"
    if (why := a[1].can_sandbox()):
        return why
    has = set(a[1].guarantees())
    if (unknown := sorted(has - GUARANTEES.keys())):
        return f"the {a[0]} adapter declares unknown guarantees: {', '.join(unknown)}"
    if (missing := [g for g in REQUIRED if g not in has]):
        return f"the {a[0]} adapter doesn't guarantee {', '.join(missing)}"
    return None


def guarantees() -> list[str]:
    """What this machine's adapter guarantees, in GUARANTEES order; empty when it can't sandbox here."""
    if can_sandbox():
        return []
    has = set(adapter()[1].guarantees())
    return [g for g in GUARANTEES if g in has]


def sandboxed(doc: dict) -> bool:
    """The project's own services and command jobs run isolated (bin/sandboxed-run). hub.json's
    service_sandbox_exempt holds projects back temporarily (owner's decision, until they're fixed)."""
    enf = doc.get("enforce")
    return isinstance(enf, dict) and enf.get("sandbox") is True \
        and doc.get("name") not in (hubconf.get("service_sandbox_exempt", []) or [])


def _run(options: list[str], argv: list[str]) -> list[str]:
    """The platform's Python, isolated (-I: no PYTHON* variable, user site or current folder; -B: no bytecode written),
    running sandboxed-run. Whatever the caller's environment, only the platform's code runs before the sandbox."""
    py, script = hubconf.launch(hubconf.ROOT / "bin" / "sandboxed-run")
    return [py, "-I", "-B", script, *options, "--", *argv]


def wrap(project: str, cwd, argv: list[str], port: int | None = None) -> list[str]:
    """argv → the same command through bin/sandboxed-run."""
    return _run(["--project", project, "--cwd", str(cwd), *(["--port", str(int(port))] if port else [])], argv)


def wrap_service(project: str, cwd, svc: dict, words: list[str]) -> list[str]:
    """A sandboxed project's declared service: words as the entry has them, and what sandboxed-run needs to read the
    rest of the entry itself (its name, and the digest of what the unit stands for). Raises ValueError where the
    entry can't be used."""
    svc = normal_service(svc)
    if (why := service_problem(svc)):
        raise ValueError(why)
    port = (svc.get("expose") or {}).get("local_port")
    return _run(["--project", project, "--service", svc["name"], "--cwd", str(cwd),
                 *(["--port", str(int(port))] if port else []), "--digest", service_digest(svc)], words)


def wrapped_project(argv: list[str]) -> str | None:
    """The project argv runs this hub's sandboxed-run for, when argv is one this platform writes: its options exactly as
    wrap_service has them now, or as a service's unit had them before (no -I -B, no service). None for anything else,
    whatever it runs."""
    isolated = argv[1:3] == ["-I", "-B"]
    rest = argv[3:] if isolated else argv[1:]
    ours = {str(root / "bin" / "sandboxed-run") for root in (hubconf.ROOT, hubconf.MAIN)}
    if not rest or rest[0] not in ours or not os.path.isabs(argv[0]) or not os.path.basename(argv[0]).startswith("python"):
        return None
    rest, got = rest[1:], {}
    for flag in ("--project", "--service", "--cwd", "--port", "--digest"):
        if rest[:1] == [flag] and len(rest) > 1:
            got[flag], rest = rest[1], rest[2:]
    shape = {"--project", "--cwd", *(("--service", "--digest") if isolated else ())}
    return got["--project"] if rest[:1] == ["--"] and len(rest) > 1 and set(got) - {"--port"} == shape else None


# ---------- a declared service's entry ----------

ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def has_control(text: str, allow: str = "") -> bool:
    """A character no unit, command or message carries: a control character (C0, DEL, C1), a line or paragraph separator,
    a noncharacter, a surrogate; what reads a unit (XML, systemd, this platform) doesn't agree on them."""
    return any(c not in allow and (unicodedata.category(c) in ("Cc", "Zl", "Zp", "Cs")
                                   or 0xFDD0 <= ord(c) <= 0xFDEF or ord(c) & 0xFFFE == 0xFFFE) for c in text)


def _whole(v):
    """v as the whole number int() makes of it; v itself when it makes none (validation then says so)."""
    try:
        return int(v) if isinstance(v, (int, float, str)) else v
    except (ValueError, OverflowError):
        return v


def normal_service(svc: dict) -> dict:
    """A service entry in one form, whether YAML or the JSON index gave it: an environment value that is a number, a
    date, true/false or null is the text it has always been written as, a number written as text is the number, a field
    left empty isn't there. What fits no form is left as it is, for validation to refuse."""
    out = {k: v for k, v in svc.items()
           if v or k not in ("label", "interval_s", "throttle_s", "env", "cwd", "log", "expose")}
    run = out.get("run")
    if isinstance(run, str) and any(c in run for c in "\n\t"):   # a folded or block YAML scalar: the words are the same
        try:
            words = shlex.split(run)
            run = shlex.join(words) if not any(has_control(w) for w in words) else run
        except ValueError:
            pass
        out["run"] = run
    env = out.get("env")
    if isinstance(env, dict):
        out["env"] = {(k if isinstance(k, str) else str(k)): v if isinstance(v, (str, dict, list)) else str(v)
                      for k, v in env.items()}
    if isinstance(out.get("name"), int) and not isinstance(out["name"], bool):
        out["name"] = str(out["name"])
    for k in ("interval_s", "throttle_s"):
        if k in out:
            out[k] = _whole(out[k])
    if isinstance(out.get("expose"), dict):
        out["expose"] = {k: _whole(v) if k in ("local_port", "tailnet_https") else v
                         for k, v in out["expose"].items() if v is not None and v != ""}
    if "keepalive" in out and not isinstance(out["keepalive"], (dict, list)):
        out["keepalive"] = bool(out["keepalive"])
    return out


def service_digest(svc: dict) -> str:
    """What a sandboxed service's unit stands for without naming it: its command, environment and log. The unit carries
    it, so a change of any of them changes the unit; sandboxed-run compares it with the entry it reads."""
    svc = normal_service(svc)
    text = json.dumps([svc.get("run"), svc.get("env") or {}, svc.get("log")], sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def service_problem(svc: dict, allow: str = "\t\n") -> str | None:
    """Why this (normal) entry's command, environment or log can't be used. Checked where they are used, not only by the
    registry (which allows no tab or newline either): an entry accepted under older rules stays the last valid one."""
    run, env, log = svc.get("run"), svc.get("env") or {}, svc.get("log")
    if not isinstance(run, str) or not run.strip() or has_control(run, allow):
        return "run is not a command without control characters"
    if not isinstance(env, dict):
        return "env is not a mapping"
    for k, v in env.items():
        if not ENV_KEY.fullmatch(k):
            if allow:   # accepted before: left out (usable_env), not a reason to take the service down
                continue
            return f"env: {k!r} is not a variable name (letters, digits and _)"
        if not isinstance(v, str) or has_control(v, allow):
            return f"env: {k} is not text without control characters"
    if log is not None and (not isinstance(log, str) or has_control(log, allow)):
        return "log is not a path without control characters"
    return None


def usable_env(svc: dict) -> dict[str, str]:
    """A (normal) service's environment without the names no shell variable can have."""
    return {k: v for k, v in (svc.get("env") or {}).items() if ENV_KEY.fullmatch(k)}


class Entry(NamedTuple):
    """What of a service's entry is applied inside its sandbox. if_exists: (variable, path, value): value instead when
    path names something, seen from inside."""
    env: dict[str, str]
    if_exists: tuple[tuple[str, str, str], ...] = ()
    log: str | None = None


def as_project(project: str | None, cwd, argv: list[str]) -> list[str]:
    """argv with the project's authority: through bin/sandboxed-run when the project is sandboxed (which refuses, exit
    78, where this machine can't sandbox), unchanged otherwise (an unsandboxed project, an unregistered folder or the
    platform itself (None) has the user's authority already). A name the registry has and its index doesn't (a stale index) may be
    sandboxed: it goes through bin/sandboxed-run too, which refuses a project it doesn't know."""
    doc = hubconf.projects().get(project) if project else None
    if project and doc is None and project in hubconf.roster():
        return wrap(project, cwd, argv)
    return wrap(project, cwd, argv) if doc and sandboxed({**doc, "name": project}) else argv


def readable_platform_folder() -> pathlib.Path | None:
    """The platform's state folder or secrets folder when this process can open it (the first that opens), else None."""
    for p in (hubconf.STATE, hubconf.secrets_dir()):
        try:
            fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            continue
        os.close(fd)
        return pathlib.Path(p)
    return None


def reads_blocked() -> bool:
    """The REQUIRED isolated-reads guarantee, probed from inside: the platform's state and its secrets folder can't be
    opened. True only when at least one of them exists and neither opens (one missing tells nothing)."""
    blocked = False
    for p in (hubconf.STATE, hubconf.secrets_dir()):
        try:
            fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY)
        except PermissionError:
            blocked = True
            continue
        except OSError:
            continue
        os.close(fd)
        return False
    return blocked


def confinement_gap(marked: bool) -> str:
    """Why confined_now(marked) says no, for a refusal to say: the adapter can't see its sandbox, or the platform's
    state can be read from inside it. The second happens where the sandbox can't hide that folder, e.g. one inside the
    temp area every sandboxed project may write (Landlock grants that area whole: on Linux a platform state kept there is
    never hidden)."""
    a = adapter()
    fn = getattr(a[1], "confined_now", None) if a else None
    if not (fn and fn(marked)):
        return "it was started through sandboxed-run but its sandbox can't be seen from inside"
    if (p := readable_platform_folder()):
        return f"the platform's own folder {p} can be read from inside its sandbox, which can't hide it there"
    return "it was started through sandboxed-run but its sandbox can't be confirmed from inside"


def confined_now(marked: bool = False) -> bool:
    """Whether this process already runs inside a sandbox that holds a project's isolation, not merely inside some
    sandbox (a permissive profile, a desktop app's, a container): the adapter sees its own kind of sandbox, and the
    platform's state and secrets can't be read from here (reads_blocked). marked: this process was started through
    sandboxed-run (its mark is set); an adapter whose own check can't tell its sandbox from others' (Landlock) says yes
    only then."""
    a = adapter()
    fn = getattr(a[1], "confined_now", None) if a else None
    return bool(fn and fn(marked)) and reads_blocked()


# what a wrapped run keeps of its starter's environment (git's own for a hook, the platform's settings); nothing else of
# the starter's, so a token in the owner's shell never reaches a project's tier (r2 B3, review F2)
KEEP_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "HOME", "TERM", "GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")


def kept_env(environ=None) -> dict[str, str]:
    import names
    environ = os.environ if environ is None else environ
    prefixes = tuple({names.env_names(x)[i][:-len(x)] for x in ("STATE", "TEST_CALLER") for i in (0, 1)})
    return {k: v for k, v in environ.items() if k in KEEP_ENV or k.startswith(prefixes)}


class SandboxRefused(Exception):
    """A sandboxed project's commands can't be run in its sandbox here; they are not run at all."""


def run_as_project(project: str, doc: dict, argv: list[str], guard: str) -> tuple[list[str], dict] | None:
    """A platform command whoever starts it (the owner's commit hook, the owner's terminal, a session) that runs a
    sandboxed project's own commands (its test tiers, its `cmd:` inputs, its lockfile installs) never runs them with the
    starter's authority (docs/design/git-config-fence.md, r2 B3). None: go on in this process (the project isn't
    sandboxed, or this process already runs in a sandbox that holds: confined_now). Otherwise (argv, env) to run
    instead: the same command through sandboxed-run, in a fixed environment (kept_env) carrying guard, the mark the
    inner run is known by. Raises SandboxRefused when this machine can't sandbox, or when a marked run still can't see
    its sandbox (never a loop, never unsandboxed). A run inside some other sandbox is wrapped too; on macOS that fails
    in sandboxed-run (a profile can't be applied inside another), which the caller reports as a refusal."""
    marked = bool(os.environ.get(guard))
    if not sandboxed({**doc, "name": project}) or confined_now(marked):
        return None
    if marked:
        raise SandboxRefused(confinement_gap(marked))
    if (why := can_sandbox()):
        raise SandboxRefused(why)
    return wrap(project, os.getcwd(), argv), {**kept_env(), guard: "1"}


def unmet(pol: dict) -> str | None:
    """Why this machine's adapter can't apply this policy whole (it asks for a guarantee the adapter doesn't give: the
    git fence on Linux, where Landlock can't deny a name inside a writable tree), else None. Such a policy is never
    applied without it: the command isn't run (fail closed)."""
    has = set(guarantees())
    missing = [g for k, g in NEEDS.items() if pol.get(k) and g not in has]
    if missing:
        name = (adapter() or ("none",))[0]
        return f"its policy needs {', '.join(missing)}, which the {name} adapter can't give on this system"
    return None


def exec_service(pol: dict, dirs: list, port: int | None, argv: list[str], entry: Entry | None = None):
    if (why := can_sandbox() or unmet(pol)):
        raise OSError(0, why)
    adapter()[1].exec_service(pol, dirs, port, argv, entry)


def exec_installed(pol: dict, argv: list[str]):
    """An installed service (policy.service_policy) under its own read model: the adapter's installed mode, in this
    process's environment, which the caller has built from nothing (docs/design/service-ecosystem.md, step 2a)."""
    if (why := can_sandbox()):
        raise OSError(0, why)
    impl = adapter()[1]
    if not hasattr(impl, "exec_installed"):
        raise OSError(0, "this sandbox adapter has no mode for installed services")
    impl.exec_installed(pol, argv)
