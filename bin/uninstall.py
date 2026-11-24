#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""Take brunna off this machine: `brunna uninstall [--apply] [--purge] [--yes]`.

Without --apply it only lists what it would do. Each part is undone by the code that put it there (render-services,
render-project-settings, the runtime adapters); this only runs them in order:
  1. the scheduler and its watchdog, when their unit is this hub's (the same labels on another hub are left alone)
  2. the background services render-services generated (and only those: services.tsv entries it doesn't own stay)
  3. in each connected project: the platform's block in the rules file, its hooks in the agent's settings, the sandbox
     and rules it wrote, its git hooks (through project_git, as the project). The project's code is never touched
  4. user level, only on the owner's own yes at the terminal (--yes doesn't count): brunna's MCP server registered
     with each AI app, and the runtime's user-level hooks for this hub
  5. the `brunna` command it put on PATH
Notes stay: the instance directory (settings, data, keys), the memory repos, each project's .agent-memory, and the bus.
--purge removes those too (notes can't be brought back), after a question of its own whose answer is no unless you type
yes; --yes answers both questions. Your own files in the instance directory and the bus's folder stay, and an instance in
a folder that wasn't made for it (an earlier version took any) is left whole, with the grants in it: what in it is
brunna's is named, and yours to remove. Never removed: the projects' code, nvm, node, the AI agents, uv, git. The hub
folder itself is removed last, by you: the command is printed at the end, unless the folder holds what was kept.

What can't be removed is said, the rest goes on, and the exit code is 1. Refused from a linked worktree, and while a
scheduler tick runs (it holds the scheduler's lock while it works).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import plistlib
import shlex
import subprocess
import sys
import time

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402
import names  # noqa: E402
import i18n  # noqa: E402
import init_dialogue  # noqa: E402
import instance_dir  # noqa: E402
import memlock  # noqa: E402
import user_file  # noqa: E402

_ = i18n.domain(__file__)
CORE = tuple(names.unit_label(s, sp) for sp in names.all("unit") for s in names.CORE_UNITS)
# a line that is the begin or the end of the platform's block, in either spelling (docs/design/rename-brunna.md)
MARKS = {line: (s, i) for s in names.all("marker") for i, line in enumerate(names.block(s))}


def _load(script: str):
    spec = importlib.util.spec_from_file_location(script.replace("-", "_"), BIN / f"{script}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ours(text: str) -> bool:
    """Whether a unit's text runs this hub (its bin/ named in it)."""
    return str(hubconf.MAIN / "bin") in text


def core_units() -> list[tuple[str, object]]:
    """(what, undo) for the scheduler and watchdog of this hub; undo is None where no service manager can be reached."""
    out = []
    reachable = hubconf.service_manager()
    sm = reachable or ("launchd" if sys.platform == "darwin" else "systemd")
    for label in CORE:
        if sm == "systemd":
            import systemd_units
            units = systemd_units.installed(label)
            if units and any(ours(t) for t in units.values()):
                out.append((label, (lambda label=label: systemd_units.remove(label)) if reachable else None))
        elif sm == "launchd":
            path = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            try:
                plist = plistlib.loads(path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if ours(" ".join(map(str, plist.get("ProgramArguments") or []))):
                def undo(label=label, path=path):
                    subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
                    path.unlink(missing_ok=True)
                out.append((label, undo if reachable else None))
    return out


def strip_text(text: str) -> tuple[str, bool]:
    """(text without the platform's blocks, whether their begin and end lines pair). Only a line that is nothing but a mark
    counts. When they don't pair, what lies between them can't be told from the user's own lines: the text as it is."""
    lines = text.splitlines(keepends=True)
    blocks, begin = [], None
    for i, line in enumerate(lines):
        if not (mark := MARKS.get(line.strip())):
            continue
        spelling, is_end = mark
        if is_end != (begin is not None) or (is_end and begin[1] != spelling):
            return text, False
        if is_end:
            blocks.append((begin[0], i))
        begin = None if is_end else (i, spelling)
    if begin is not None:
        return text, False
    if not blocks:
        return text, True
    nl = "\r\n" if lines[blocks[0][0]].endswith("\r\n") else "\n"
    edges = [0, *(i for a, b in blocks for i in (a, b + 1)), len(lines)]
    between = ["".join(lines[a:b]) for a, b in zip(edges[::2], edges[1::2])]   # the user's, around the blocks
    between[1:] = [s.lstrip("\r\n") for s in between[1:]]
    between[:-1] = [s.rstrip("\r\n") for s in between[:-1]]
    kept = [s for s in between if s]
    return (nl * 2).join(kept) + (nl if kept and not between[-1] else ""), True


def strip_block(path: pathlib.Path, apply: bool, follow: bool = True) -> bool:
    """The platform's managed block out of a rules file, the rest as the user has it (its mode, its line ends); a file left
    empty goes. Whether there was one; ValueError when its begin and end lines don't pair, and the file is left as it is.
    follow: a link stays a link, and what it points to is edited (not for a sandboxed project, which could have put it
    there: left alone)."""
    if not path.is_file() or (path.is_symlink() and not follow):
        return False
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    rest, paired = strip_text(text)
    if not paired:
        raise ValueError(_("{file} is left as it is: its {product}:begin and :end lines don't pair; remove the platform's "
                           "part by hand", file=path, product=names.current("marker")))
    if rest != text and apply:
        if rest.strip() or path.is_symlink():
            user_file.replace(path, rest)
        else:
            path.unlink()
    return rest != text


def project_parts(name: str, doc: dict, apply: bool) -> tuple[list[str], list[str]]:
    """What is (or would be) taken out of one project, and what can't be, each with why: one part's failure is no reason to
    leave the others."""
    import isolation
    import registry
    import runtime_ports
    ws = registry.dirs(doc)["workspace"]
    if not ws or not ws.is_dir():
        return [], []
    done: list[str] = []
    left: list[str] = []

    def part(what: str, fn) -> None:
        try:
            if fn():
                done.append(what)
        except runtime_ports.Unsupported:
            pass
        except Exception as e:   # noqa: BLE001 (a file this can't read or write: said, and the next part)
            left.append(_("{what}: {why}", what=what, why=e))

    for rname in runtime_ports.installed():
        try:
            f = runtime_ports.load(rname, ports=("instructions",)).instructions_file(ws)
            part(_("the platform's block in {file}", file=f.name),
                 lambda f=f: strip_block(f, apply, follow=not isolation.sandboxed(doc)))
        except runtime_ports.Unsupported:
            pass
        try:
            rt = runtime_ports.load(rname, ports=("lifecycle",))
        except runtime_ports.Unsupported:
            continue
        remove = getattr(rt, "remove_lifecycle", None)
        if remove is None:
            done.append(_("{runtime}'s hooks for brunna stay: it can't remove them; remove them by hand", runtime=rname))
        else:
            part(_("{runtime}'s hooks for brunna", runtime=rname), lambda: remove(ws, check=not apply))
    rps = _load("render-project-settings")

    def unrender() -> bool:
        if not (rps.RENDERED / f"{name}.json").exists():
            return False
        if why := rps.read_settings(doc)[1]:   # written anyway, it would lose what is in it
            raise ValueError(f"{rps.settings_path(doc)}: {why}")
        if apply:
            rps.unapply(name, doc)
        return True

    part(_("the sandbox and rules brunna wrote"), unrender)
    try:
        hooks = rps.gate(name, doc, report=not apply, quiet=True, remove_all=True)
        stay = [h for _op, h in rps.gate(name, doc, report=True, quiet=True, remove_all=True)] if apply else []
    except Exception as e:   # noqa: BLE001
        hooks, stay = [], []
        left.append(_("{what}: {why}", what=_("its git hooks"), why=e))
    done += [_("git hook {hook}", hook=h) for _op, h in hooks if h not in stay]
    left += [_("git hook {hook} is still there", hook=h) for h in stay]
    return done, left


def project_repo(name: str, ws: pathlib.Path, *args: str):
    """git in a project's repo, as the project, with the repo's hooks and fsmonitor off."""
    import project_git
    return project_git.run(name, ws, [project_git.real_git() or "git", "-c", "core.hooksPath=/dev/null", "-c",
                                      "core.fsmonitor=", "-C", str(ws), *args])


def notes_branch(name: str, ws: pathlib.Path) -> bool:
    return (ws / ".git").exists() and project_repo(name, ws, "show-ref", "--verify", "--quiet",
                                                   "refs/heads/agent-memory").returncode == 0


def notes(name: str, doc: dict) -> pathlib.Path | None:
    """The project's notes folder, when it or their branch is there."""
    import registry
    ws = registry.dirs(doc)["workspace"]
    return ws / ".agent-memory" if ws and ((ws / ".agent-memory").exists() or notes_branch(name, ws)) else None


def remove_notes(name: str, folder: pathlib.Path) -> str | None:
    """The notes worktree, their branch (what was never sent included: the purge was agreed to) and the remote they went to,
    removed as the project; why they couldn't be, or None."""
    import project_git
    ws = folder.parent

    def why(r) -> str:
        return r.stderr.strip()[-200:] or _("exit {code}", code=r.returncode)

    if folder.exists():
        r = project_git.job(name, ws, "worktree-remove", str(ws), str(folder), "--force")
        if folder.exists():
            return why(r)
    if notes_branch(name, ws) and (r := project_repo(name, ws, "branch", "-q", "-D", "agent-memory")).returncode != 0:
        return why(r)
    url = project_repo(name, ws, "remote", "get-url", "memory").stdout.decode(errors="replace").strip()
    ours = [f"{hubconf.p('git_origin', '~/git-origin')}/", *([f"{h}:~/git-origin/"] if (h := hubconf.host("server")) else [])]
    if url.endswith("-memory.git") and url.startswith(tuple(ours)):   # with its tracking ref; another's is left
        r = project_repo(name, ws, "remote", "remove", "memory")
        return None if r.returncode == 0 else why(r)
    return None


def command(*args: str) -> str:
    """One to run after this: the command on PATH may be gone by then."""
    return shlex.join([str(hubconf.MAIN / "bin" / names.current("cli")), *args])


def purge_paths() -> list[pathlib.Path]:
    """Data that only --purge removes, beside the projects' notes: the memory repos and copies of the connected projects,
    the bus, and the instance directory last (it says which projects there are)."""
    out = []
    for name in sorted(hubconf.projects() or {}):
        for p in (hubconf.p("git_origin", "~/git-origin") / f"{name}-memory.git",
                  hubconf.p("memory_copies", "~/.memory-copies") / name):
            if p.is_dir() and p not in out:
                out.append(p)
    home = hubconf.instance_home()
    if hubconf.BUS_DIR.is_dir() and not goes_with(home, hubconf.BUS_DIR):
        out.append(hubconf.BUS_DIR)
    if home and home.is_dir():
        out.append(home)
    return out


def goes_with(home: pathlib.Path | None, p: pathlib.Path) -> bool:
    """Whether it is in an instance directory of its own: that removal's, or what it names as left."""
    return bool(home) and instance_dir.own(home) and instance_dir.within(p, home)


def left_whole(p: pathlib.Path) -> bool:
    """Whether nothing at this path is removed: an instance, or a bus's folder, that wasn't made for it, and whatever is inside
    such an instance."""
    home = hubconf.instance_home()
    return bool(home and not instance_dir.own(home) and instance_dir.within(p, home)) \
        or (p == hubconf.BUS_DIR and not instance_dir.own_bus(p))


def purge_one(p: pathlib.Path) -> tuple[list[str], list[str]]:
    """One of purge_paths: (what couldn't be removed, what is left because it isn't brunna's)."""
    if p == hubconf.instance_home():
        return instance_dir.purge(p)
    if p == hubconf.BUS_DIR:
        return instance_dir.purge_bus(p)
    if p.parent == hubconf.p("memory_copies", "~/.memory-copies"):
        if not instance_dir.is_memory_copy(p):
            return [], [str(p)]
        with memlock.hold(p, "uninstall") as got:   # a memory copy goes holding its writer lock (memlock)
            if not got:
                return [memlock.busy_message(p) + " " + _("(left; run {command} again)", command=command("uninstall", "--apply", "--purge"))], []
            return instance_dir.remove(p), []
    return (instance_dir.remove(p), []) if instance_dir.is_memory_bare(p) else ([], [str(p)])


def typed_yes(question: str) -> bool:
    """For what can't be undone: no unless yes is typed (Enter is no, and so is no terminal)."""
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cli_wrappers() -> list[pathlib.Path]:
    """install-cli's wrappers of this hub, under either name (the one from before the rename too)."""
    import upgrade
    files = [pathlib.Path.home() / ".local" / "bin" / name for name in names.all("cli")]
    return [f for f in files if (hub := upgrade.own_wrapper(f)) and os.path.realpath(hub) == os.path.realpath(hubconf.MAIN)]


def mcp_tools() -> tuple[list[tuple[str, list[list[str]]]], object]:
    """(label, unregister commands) for each AI app that has one and is installed, and the environment to run it in."""
    spec = importlib.util.spec_from_loader("agenthub_cli", importlib.machinery.SourceFileLoader("agenthub_cli",
                                                                                                str(BIN / "agenthub")))
    ah = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ah)
    # a registration under either of the platform's names (one made before the rename keeps the old one)
    return [(info["label"], [ah.mcp_argv(info["mcp_unregister"], server) for server in names.all("cli")])
            for info in ah.agent_presets().values() if info.get("mcp_unregister") and ah.agent_found(info)], ah.agent_env


def still_there(rs, label: str) -> bool:
    """Whether a service that was to be removed is still there: its file, or loaded."""
    if hubconf.service_manager() == "systemd":
        import systemd_units
        return bool(systemd_units.installed(label))
    for _try in range(50):   # unloading may take a moment after it was asked for
        if rs.running(os.getuid(), label) is None:
            break
        time.sleep(0.1)
    else:
        return True
    return (rs.AGENTS / f"{label}.plist").exists()


def kept_inside_the_hub() -> list[str]:
    """The user's that is inside the hub folder, so removing that would take it along."""
    import registry
    hub = pathlib.Path(os.path.realpath(hubconf.MAIN))
    home = hubconf.instance_home()
    # the old layout keeps everything in the hub's own folder
    legacy = [] if home else [
        *(hubconf.config_file(f) for f in instance_dir.CONFIG_FILES), *(hubconf.data_file(f) for f in instance_dir.DATA_FILES),
        hubconf.secrets_dir(), *(f for f in hubconf.registry_dir().glob("*.yaml") if f.name != hubconf.platform_registry().name)]
    there = [home, *legacy, hubconf.p("git_origin", "~/git-origin"),
             hubconf.p("memory_copies", "~/.memory-copies"), *([hubconf.BUS_DIR] if hubconf.BUS_DIR.is_dir() else []),
             *(registry.dirs(d)["workspace"] for n, d in (hubconf.projects() or {}).items() if n != hubconf.hub_project())]
    return sorted({str(p) for p in there if p and p.exists() and pathlib.Path(os.path.realpath(p)).is_relative_to(hub)})


def main(argv: list[str]) -> int:
    flags = set(argv)
    unknown = flags - {"--apply", "--purge", "--yes", "-h", "--help"}
    if unknown or flags & {"-h", "--help"}:
        print(_(__doc__), file=sys.stderr if unknown else sys.stdout)
        return 2 if unknown else 0
    apply, purge, yes = "--apply" in flags, "--purge" in flags, "--yes" in flags
    if apply and hubconf.ROOT != hubconf.MAIN:
        print(_("brunna uninstall: run it from the hub's main checkout ({main}), not a linked worktree; nothing was "
                "done", main=hubconf.MAIN), file=sys.stderr)
        return 2
    import runtime_ports
    projects = {n: d for n, d in sorted((hubconf.projects() or {}).items()) if n != hubconf.hub_project()}
    core = core_units()
    rs = _load("render-services")
    services = rs.uninstall(apply=False)
    per_project = {n: project_parts(n, d, apply=False) for n, d in projects.items()}
    tools, agent_env = mcp_tools()
    user = [label for label, _cmd in tools]
    runtime_user = [line for name in runtime_ports.installed()
                    for line in (getattr(runtime_ports._module(name), "user_cleanup", lambda apply: [])(False))]
    wrappers = cli_wrappers()
    project_notes = [(n, f) for n, d in projects.items() if (f := notes(n, d))] if purge else []
    data = purge_paths() if purge else []
    home = hubconf.instance_home()
    whole = bool(home) and instance_dir.own(home)   # before anything is removed, its mark included
    stay_whole = {p for p in [*data, *(f for _n, f in project_notes)] if left_whole(p)}

    print(_("brunna uninstall {what}:", what=_("removes") if apply else _("would remove (nothing is done without "
                                                                              "--apply)")))
    for label in [label for label, _undo in core] + services:
        print("  " + _("service {label}", label=label))
    for name, (parts, stay) in per_project.items():
        for line in parts:
            print("  " + _("{project}: {part}", project=name, part=line))
        for line in stay:
            print("  ⚠️ " + _("{project}: {part}", project=name, part=line))
    for label in user:
        print("  " + _("brunna in {tool}'s tools (asked first)", tool=label))
    for line in runtime_user:
        print("  " + _("{what} (asked first)", what=line))
    for wrapper in wrappers:
        print("  " + str(wrapper))
    if data or project_notes:
        print(_("And, asked on its own, your notes and data, which can't be brought back:"))
        for name, _folder in project_notes:
            print("  " + _("{project}: {part}", project=name, part=_("its notes (.agent-memory)")))
        for p in data:
            print("  " + (_("{path} (not a folder made for it: left whole, what in it is brunna's is named)", path=p)
                          if p in stay_whole else str(p)))

    def say_kept() -> None:
        paths = ", ".join(str(k) for k in (home, hubconf.p("git_origin", "~/git-origin")) if k)
        print(_("Kept: your settings, notes and keys ({paths}) and each project's .agent-memory. --purge removes them "
                "too.", paths=paths))

    if not purge:
        say_kept()
    print(_("Never touched: your projects' code, nvm, node, the AI agents, uv, git."))
    if not apply:
        return 0

    if not yes:
        question = _("Remove all of the above but the notes and data?") if purge else _("Remove all of the above?")
        if not init_dialogue.Terminal().consent("uninstall", question):
            print(_("Nothing was done."))
            return 1
        if purge and not typed_yes(_("Remove your settings, notes, keys and mail too? They can't be brought back.")):
            purge, project_notes, data = False, [], []
            say_kept()
    upgrade = _load("upgrade")
    fd = upgrade.scheduler_lock()
    if fd is None:
        print(_("brunna uninstall: a scheduler tick is running; try again in a minute (nothing was done)"),
              file=sys.stderr)
        return 1
    left: list[str] = []

    def stays(line: str) -> None:
        left.append(line)
        print("⚠️ " + line, flush=True)

    try:
        reachable = hubconf.service_manager()
        for label in [label for label, undo in core if not undo] + ([] if reachable else services):
            stays(_("{label} was not stopped or removed: {reason}", label=label, reason=hubconf.no_service_manager()))
        for label, undo in core:
            if undo:
                undo()
        removed = [label for label, undo in core if undo]
        if services and reachable:
            try:
                removed += rs.uninstall(apply=True)
            except OSError as e:
                stays(_("{what}: {why}", what=_("the background services"), why=e))
        for label in removed:
            if still_there(rs, label):
                stays(_("{label} could not be stopped or removed", label=label))
            else:
                print(_("✓ {label} stopped and removed", label=label))
        for name, doc in projects.items():
            done, stay = project_parts(name, doc, apply=True)
            for line in done:
                print(_("✓ {project}: {part}", project=name, part=line))
            for line in stay:
                stays(_("{project}: {part}", project=name, part=line))
        # user level: the owner's own yes at the terminal, each time; --yes doesn't stand in for it (AGENTS.md)
        term = init_dialogue.Terminal()
        for label, cmds in tools:
            if term.consent("mcp_unregister", _("Remove brunna from {tool}'s tools (a setting for your user)?",
                                                 tool=label)):
                # one registration under either name; the other name's command finds nothing to remove
                runs = [subprocess.run(cmd, env=agent_env(), capture_output=True, text=True) for cmd in cmds]
                r = next((x for x in runs if x.returncode == 0), runs[0])
                print(_("✓ removed from {tool}", tool=label) if r.returncode == 0 else
                      _("{tool}: {error}", tool=label, error=(r.stderr or r.stdout).strip()[:200]))
            else:
                print(_("Left in {tool}: remove it later with {command}", tool=label, command=" ".join(cmds[0])))
        for name in runtime_ports.installed():
            fn = getattr(runtime_ports._module(name), "user_cleanup", None)
            if fn and fn(False) and term.consent("user_cleanup", _("Remove {what} (a setting for your user)?",
                                                                    what="; ".join(fn(False)))):
                for line in fn(True):
                    print(_("✓ {what} removed", what=line))
        for wrapper in wrappers:
            if failed := instance_dir.remove(wrapper):
                stays(_("not removed: {what}", what=failed[0]))
            else:
                print(_("✓ {path} removed", path=wrapper))
        if purge and home and home.is_dir():   # the platform's grants are revoked at their providers before the instance goes
            import credentials
            if whole:
                for ok, line in credentials.revoke_all():
                    print(_("✓ {what}", what=line) if ok else f"⚠️ {line}")
            elif stored := sorted(f for b in credentials.bases() for f in b.glob("*/*.json")):
                print(_("The grants in {path} remain: before the hub goes, revoke each with {commands}", path=home,
                        commands="; ".join(command("credentials", "revoke", f.parent.name, f.stem) for f in stored)))
        before = len(left)
        for name, folder in project_notes:
            if folder in stay_whole:
                print(_("{path}: left", path=folder))
                continue
            if why := remove_notes(name, folder):
                stays(_("{project}: {part}", project=name,
                        part=_("{what}: {why}", what=_("its notes (.agent-memory)"), why=why)))
            else:
                print(_("✓ {project}: {part}", project=name, part=_("its notes (.agent-memory)")))
        for p in data:
            if p == home and len(left) > before:   # what says which projects there are goes when their notes have
                stays(_("{path} stays until the rest is gone", path=p))
                continue
            failed, kept = ([], []) if p in stay_whole and p not in (home, hubconf.BUS_DIR) else purge_one(p)
            for line in failed[:5]:
                stays(_("not removed: {what}", what=line))
            if len(failed) > 5:
                print("  " + _("and {n} more", n=len(failed) - 5))
            if p in stay_whole:
                kept = kept if p in (home, hubconf.BUS_DIR) else [str(p)]
                print(_("{path} wasn't made for this, and may hold files of yours: nothing in it was removed. Brunna's there, to "
                        "remove by hand: {kept}", path=p, kept=", ".join(kept)) if kept else
                      _("{path}: left", path=p))
            elif kept:
                print(_("{path}: left, not brunna's: {kept}", path=p, kept=", ".join(kept)))
            elif not failed:
                print(_("✓ {path} removed", path=p))
        if purge and (not home or (home in data and not instance_dir.settings(home))):   # one that isn't there is kept
            hubconf.POINTER.unlink(missing_ok=True)
    finally:
        os.close(fd)
    if left:
        print(_("Not all of it is gone (the ⚠️ lines above). Keep the hub folder ({hub}), put that right, and run {command} "
                "again.", hub=hubconf.MAIN, command=command("uninstall", "--apply", *(["--purge"] if purge else []))))
        return 1
    if inside := kept_inside_the_hub():
        print(_("The hub folder ({hub}) holds what was kept: {paths}. Move it out before you remove the folder.",
                hub=hubconf.MAIN, paths=", ".join(inside)))
        return 0
    print(_("Last step, yours: remove the hub folder with: rm -rf {hub}", hub=hubconf.MAIN))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
