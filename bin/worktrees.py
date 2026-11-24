"""Overview of every project's git worktrees (for the status page) and their cleanup (for bin/prune-worktrees).
Standard library only. Every git command in a project's repo (and every deletion in it) runs as that project
(project_git.py: through bin/sandboxed-run when it is sandboxed); its facts come back as data.

One worktree = one parallel session's branch. Computed here: the branch, how long ago the last commit was, whether it's
merged into main, whether it's clean, which files it changed (compared with main), and two or more branches changing
the same file (to know early about a collision, without declaring paths up front).
Also the number of recent commits made directly on main without the merge gate (merge-branch): information only, never
blocking.

Automatic cleanup (auto_prune, run nightly by the platform job brunna/prune-worktrees; since 2026-10-24): only
touches "merged and clean" worktrees in a coding agent's own worktree folder in the project (the runtimes' worktree_maker,
where the platform makes a sandboxed project's too). One the user made elsewhere is theirs, and so are the main checkout
and a worktree on the base branch, wherever they are (project-git-job.py lists neither).
  Clean = git says so, with untracked files shown whatever the config says, and no file there that git is told not to look
  at (skip-worktree, assume-unchanged): an edit in one shows in no status.
  Idle days = whichever is the most recent of the branch's last commit, the worktree's admin folder and index (what
  project-git-job.py calls last) and that worktree's session transcript (~/.claude/projects/<slug>/*.jsonl): looking only
  at commits would count "chatting yesterday, no commit for three days" as idle for three days.
  Level 1 (idle ≥ 2 days): only deletes dependencies that can be rebuilt: `.venv` (with uv.lock or pyproject.toml
    next to it), `node_modules` (with a lock file next to it; without a lock file it's not deleted, since a reinstall
    isn't guaranteed identical). Folder, branch and files all stay, and the session can still be resumed;
    the marker is written in that worktree's git directory (.git/worktrees/<name>/brunna-deps-pruned, which
    doesn't make the worktree dirty), and worktree-memory.sh reminds how to rebuild when a session starts. One muninn
    worktree is about 190 MB, 90% of it .venv.
  Level 2 (idle ≥ 14 days, and no process has its cwd inside): `git worktree remove` (without --force), **the branch
    stays** (it's only a ref; if a session that was never archived needs resuming, it can be rebuilt from it). If even
    one ignored file isn't on the REGENERABLE list (e.g. .env, build output), the whole worktree is skipped with the
    reason listed in the result: no guessing what can be thrown away. The links a session's start puts there (_links) are
    not counted: what they lead to stays. A settings file it writes there is: it may hold the user's own, so a worktree a
    session of such a runtime started in is kept, and a person's `prune-worktrees` looks into it. Whether a process is in
    it is asked again right before it's removed.
  **If any process has its cwd in the worktree, neither level touches it at all** (and when that can't be found out, no
  worktree is touched): an idle session that wasn't
  archived keeps a process around (tested 10-24: the claude that Desktop pushed over), and it may just be that the
  owner is busy and hasn't come back to it yet. So this only cleans orphan worktrees with "no session open at all"
  (10-24: 35 of 37 were like that). A child session that is done but not archived looks the same as a paused session,
  and this can't tell them apart: that has to be decided from the session list (startedBy), not here.
  The `prune-worktrees --yes` a person runs: merged and clean → deleted along with the branch, whoever made it and however
  old, unless a process is in it or it holds ignored files that can't be rebuilt; every one kept is listed with why. A
  settings file a session's start wrote is no reason when nothing in it is the user's own: without what the platform wrote,
  in any version, and without what the main checkout's file holds, it is empty (_own_settings). What is left is printed.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import time
import unicodedata

import gitdirs
import hubconf
import i18n
import nofollow
import project_git
import runtime_ports

_ = i18n.domain(__file__)

DEPS_IDLE_DAYS = 2
REMOVE_IDLE_DAYS = 14
# dependency folder → it's deleted only with one of these "rebuild recipes" next to it
DEPS = {".venv": ("uv.lock", "pyproject.toml"), "node_modules": ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")}
REBUILD = {".venv": "uv sync", "node_modules": "npm ci"}
# level 2: remove the whole worktree only when the ignored things are just these (names)
LINK = ".agent-memory"   # what worktree-memory.sh links into a worktree besides the runtimes' local-only files
REGENERABLE = {".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".DS_Store"}
MARKER = "agent-hub-deps-pruned"
UNGATED_DAYS = 7


def facts(project: str | None, repo: pathlib.Path) -> dict:
    """One repo's worktrees and recent main commits, gathered by project-git-job.py as the project (the platform runs
    no git in a project's repo: project_git.py). {"unavailable": why} when they can't be had (e.g. the sandbox
    refused): said so, never shown as "no worktrees"."""
    try:
        d = project_git.data(project, repo, "worktrees", repo, UNGATED_DAYS)
    except project_git.Unavailable as e:
        return {"unavailable": str(e)}
    d = d if isinstance(d, dict) else {}
    base = d.get("base") if isinstance(d.get("base"), str) else None
    wts = []
    for w in d.get("worktrees") if isinstance(d.get("worktrees"), list) else []:
        if not (isinstance(w, dict) and isinstance(w.get("path"), str) and isinstance(w.get("branch"), str)):
            continue
        last = w.get("last")
        wts.append({"path": w["path"], "branch": w["branch"], "base": base, "merged": w.get("merged") is True,
                    "age_days": round((time.time() - last) / 86400, 1) if isinstance(last, int) else None,
                    "clean": w.get("clean") is True, "exists": w.get("exists") is True,
                    "hidden": project_git.strs(w.get("hidden")),
                    "files": project_git.strs(w.get("files"))})
    return {"worktrees": wts, "main": project_git.strs(d.get("main"))}


def ungated_main_commits(shas: list[str], project: str) -> int:
    """The number of commits on main in the last UNGATED_DAYS days (shas) that didn't come in through merge-branch
    (counted only, never blocked)."""
    gated = set()
    bus = hubconf.bus_db()
    if shas and bus.exists():
        for (pl,) in sqlite3.connect(bus, timeout=10).execute(
                "SELECT payload FROM events WHERE type='merge.done' AND source=?", (project,)):
            gated |= set(json.loads(pl).get("commits") or [])
    return sum(1 for s in shas if s not in gated)


def summary(project: str, f: dict) -> dict:
    wts = f.get("worktrees", [])
    touched: dict[str, list[str]] = {}
    for w in wts:
        if not w["merged"]:
            for p in w["files"]:
                touched.setdefault(p, []).append(w["branch"])
    return {"worktrees": wts, "overlaps": {p: bs for p, bs in touched.items() if len(bs) > 1},
            "ungated_main_7d": ungated_main_commits(f.get("main", []), project),
            **({"unavailable": f["unavailable"]} if "unavailable" in f else {})}


def overview() -> dict[str, dict]:
    out = {}
    for name in sorted(hubconf.projects()):
        repo = hubconf.workspace(name)
        if not repo or not (repo / ".git").exists():
            continue
        out[name] = summary(name, facts(name, repo))
    if hubconf.hub_project() not in out:
        out[hubconf.hub_project()] = summary(hubconf.hub_project(), facts(None, hubconf.ROOT))
    return out


def as_project(name: str) -> str | None:
    """Who runs git in this project's repo: the project, or the platform in its own checkout."""
    return None if name == hubconf.hub_project() else name


def _why_kept(project: str | None, w: dict, cwds: list[str] | None) -> str | None:
    """Why a person's prune keeps this merged worktree, or None."""
    p = pathlib.Path(w["path"])
    if not w["exists"]:
        return _("its folder is not there")
    if not w["clean"]:
        if w.get("hidden"):
            return _("git is told not to look at {files}, so edits in them can't be seen", files=", ".join(w["hidden"]))
        return _("it has changes that are not committed, or git can't tell")
    if cwds is None:
        return _("whether a process is working in it could not be found out")
    if _in_use(p, cwds):
        users = hubconf.cwd_users(p)
        return _("a process is working in it: {users}", users=", ".join(users[:3])) if users else _("a process is working in it")
    unknown = _unknown_ignored(p, project)
    if unknown is None:
        return _("its ignored files could not be listed")
    # a settings file with nothing of the user's is no reason for a person, who is asked each time (the nightly one leaves it)
    own = _own_settings(p) if unknown else {}
    shown = []
    for e in unknown:
        left = [f for f in _files_under(p, e) if own.get(f) != []]
        if len(left) == 1 and left[0] in own:
            shown.append(f"{left[0]} ({', '.join(own[left[0]][:5])})")   # the settings in it that are the user's
        elif left:
            shown.append(e)
    if shown:
        return _("ignored files not on the rebuildable list: {files}", files=", ".join(shown[:3]) + ("…" if shown[3:] else ""))
    return None


def prune(project: str | None, yes: bool) -> int:
    n = 0
    found = overview()
    cwds = _cwds_in_use()   # after the overview, which takes a while
    for name, info in found.items():
        if project and name != project:
            continue
        repo = hubconf.ROOT if name == hubconf.hub_project() else hubconf.workspace(name)
        if info.get("unavailable"):
            print(_("{project}: its worktrees could not be read ({why}); none touched", project=name,
                    why=info["unavailable"][:200]))
        for w in info["worktrees"]:
            if not w["merged"]:
                print(_("Kept (not merged yet): {project} {branch}", project=name, branch=w['branch']))
                continue
            kw = dict(project=name, branch=w['branch'], path=w['path'])
            if why := _why_kept(as_project(name), w, cwds):
                print(_("Kept: {project} {branch} ({path}): {why}", why=why, **kw))
                continue
            n += 1
            kw["days"] = w["age_days"] if w["age_days"] is not None else "?"
            if not yes:
                print(_("Can delete: {project} {branch} ({path}, untouched for {days} {days:day|days})", **kw))
                continue
            print(_("Deleting: {project} {branch} ({path}, untouched for {days} {days:day|days})", **kw))
            r = project_git.job(as_project(name), repo, "worktree-remove", repo, w["path"])
            if r.returncode != 0:   # its branch stays with it
                print(_("{label}: git worktree remove failed: {err}", label=f"{name} {w['branch']}", err=r.stderr.strip()[:200]))
            else:
                project_git.job(as_project(name), repo, "branch-delete", repo, w["branch"])
    if n and not yes:
        print(_("(only listed; once you are sure no session is still open in them, add --yes to delete them)"))
    return 0


def _cwds_in_use() -> list[str] | None:
    """Every process's cwd (hubconf.process_cwds: lsof or /proc). An idle Desktop session also has a process sitting
    in its worktree. None: it can't be found out. There is always this process, so a listing without it failed (lsof not
    there, killed, stopped halfway), and nothing it doesn't list says nobody is there."""
    procs = hubconf.process_cwds()
    return [cwd for _pid, _name, cwd in procs] if any(pid == os.getpid() for pid, _name, _cwd in procs) else None


def _in_use(p: pathlib.Path, cwds: list[str]) -> bool:
    """Whether one of the folders is in p, however it is spelled (a link on the way, Unicode forms, case): taking two names
    for one folder only ever keeps a worktree."""
    def key(x) -> str:
        return unicodedata.normalize("NFC", str(x)).casefold()
    roots = {key(p), key(os.path.realpath(p))}
    return any(k == r or k.startswith(r + "/") for k in map(key, cwds) for r in roots)


def _agent_folders(ws: pathlib.Path) -> list[pathlib.Path]:
    """Where the installed runtimes' own worktrees are in this project (worktree_maker's place for one). A runtime that
    makes none there has none, and no runtime has no folder: nothing is cleaned up by itself then."""
    out = []
    for name in runtime_ports.installed():
        try:
            rt = runtime_ports.load(name, ports=("worktree_maker",))
        except runtime_ports.Unsupported:
            continue
        out.append(pathlib.Path(rt.worktree_location(ws, "x")[0]).parent)
    return out


def _an_agents(p: pathlib.Path, folders: list[pathlib.Path]) -> bool:
    real = os.path.realpath(p.parent)
    return any(real == os.path.realpath(f) for f in folders)


def _links(p: pathlib.Path) -> set[str]:
    """What worktree-memory.sh links into a worktree, where it is a link: to the main checkout's memory folder and its
    local-only files. Removing one loses nothing, and the next session's start makes it again."""
    import session_worktree
    return {rel for rel in (LINK, *session_worktree.platform_entries()) if (p / rel).is_symlink()}


def _left_over(mine, main) -> list[str]:
    """The keys of one settings value that aren't in the other: dotted, a list's by the list's key."""
    def walk(x, y, at: str) -> list[str]:
        if isinstance(x, dict):
            return [k for key, v in x.items() for k in walk(v, y.get(key) if isinstance(y, dict) else None, f"{at}.{key}" if at else key)]
        if isinstance(x, list):
            return [at] if any(i not in (y if isinstance(y, list) else []) for i in x) else []
        return [] if x == y and y is not None else [at]
    return walk(mine, main, "")


def _own_settings(p: pathlib.Path) -> dict[str, list[str]]:
    """{a runtime's settings file in this worktree, a plain file: the keys in it that are the user's own}: without what the
    platform wrote into it (its runtime takes its hooks, whichever version's, and its memory entries out of a copy) and
    without what the main checkout's file holds, the same way. A file whose runtime can't take its own out, or that
    can't be read, isn't in it. A local-only file of another kind counts only when it is the main checkout's, byte for byte."""
    out: dict[str, list[str]] = {}
    found = gitdirs.dirs(p)
    main = found.common.parent if found else None
    for name in runtime_ports.installed():
        try:
            rt = runtime_ports.load(name, ports=("instructions",))
        except runtime_ports.Unsupported:
            continue
        install, remove = getattr(rt, "install_worktree", None), getattr(rt, "remove_lifecycle", None)
        for rel in rt.local_ignores() if main else ():
            try:
                mine = nofollow.read(p, rel, cap=1 << 20)
            except OSError:
                continue
            if mine is None:
                continue
            try:
                theirs = nofollow.read(main, rel, cap=1 << 20)
            except OSError:
                theirs = None
            if mine == theirs:
                out[rel] = []
            elif callable(install) and callable(remove):
                try:
                    with tempfile.TemporaryDirectory(prefix="hub-worktree-", dir=hubconf.private_tmp()) as t:
                        out[rel] = _left_over(*(_without_platform(pathlib.Path(t) / d, rel, raw, install, remove)
                                                for d, raw in (("wt", mine), ("main", theirs))))
                except Exception:   # not what the runtime reads (not JSON): the user's
                    pass
    return out


def _without_platform(d: pathlib.Path, rel: str, raw: bytes | None, install, remove):
    """The settings as an object, what the platform wrote taken out: its memory entries (for no main checkout there are
    none, and the ones for any other place go), then its hooks."""
    if raw is None:
        return {}
    f = d / "ws" / rel
    f.parent.mkdir(parents=True)
    f.write_bytes(raw)
    (d / "none").mkdir()
    install(d / "ws", d / "none")
    remove(d / "ws")
    return json.loads(f.read_text()) if f.exists() else {}


def _files_under(p: pathlib.Path, entry: str) -> list[str]:
    """The entry itself, or the files in it when it is a folder (git names a folder with nothing else in it as one)."""
    if (p / entry).is_symlink() or not (p / entry).is_dir():
        return [entry]
    return [str((pathlib.Path(root) / n).relative_to(p)) for root, dirs, files in os.walk(p / entry)
            for n in files + [d for d in dirs if (pathlib.Path(root) / d).is_symlink()]]


def _transcript_age_days(path: str) -> float | None:
    import sessions  # the sessions on this machine, whichever agent made them (none: only the folder's own time)
    last = sessions.last_activity(path)
    return round((time.time() - last.timestamp()) / 86400, 1) if last else None


def _idle_days(w: dict) -> float | None:
    ages = [a for a in (w.get("age_days"), _transcript_age_days(w["path"])) if a is not None]
    return min(ages) if ages else None


def _unknown_ignored(p: pathlib.Path, project: str | None) -> list[str] | None:
    """Things that are ignored and not in REGENERABLE (level 2 skips when it meets one); None when the project's job
    couldn't say."""
    try:
        entries = project_git.strs(project_git.data(project, p, "ignored", p))
    except project_git.Unavailable:
        return None
    links = _links(p)
    return [e for e in entries if pathlib.PurePath(e).name not in REGENERABLE and e not in links]


def _size_mb(p: pathlib.Path) -> float:
    r = subprocess.run(["du", "-sk", str(p)], capture_output=True, text=True)
    return round(int(r.stdout.split()[0]) / 1024, 1) if r.returncode == 0 and r.stdout else 0.0


def auto_prune(dry_run: bool) -> dict:
    """The nightly automatic cleanup (see the file header). Returns the result (also written to
    .state/worktree-prune.json for the status page)."""
    found = overview()
    in_use = _cwds_in_use()   # after the overview, which takes a while
    res = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "dry_run": dry_run,
           "deps": [], "removed": [], "skipped": [], "notes": [], "freed_mb": 0.0}
    if in_use is None:
        res["in_use_unknown"] = True
        res["notes"].append(_("who is working in which folder could not be found out; none was touched"))
    if any(info["worktrees"] for info in found.values()) and not _agent_folders(hubconf.MAIN):
        res["notes"].append(_("no installed coding agent keeps its worktrees in the projects, so none is cleaned up by itself "
                              "(`bin/prune-worktrees` lists them)"))
    for name, info in found.items():
        repo = hubconf.ROOT if name == hubconf.hub_project() else hubconf.workspace(name)
        if info.get("unavailable"):
            res["skipped"].append(_("{project}: its worktrees could not be read ({why}); none touched", project=name,
                                    why=info["unavailable"][:200]))
        if in_use is None or not info["worktrees"]:
            continue
        folders = _agent_folders(repo)
        for w in info["worktrees"]:
            p = pathlib.Path(w["path"])
            if not (w["exists"] and w["merged"] and w["clean"] and _an_agents(p, folders)):
                continue
            idle = _idle_days(w)
            label = f"{name} {w['branch']}"
            if idle is None or idle < DEPS_IDLE_DAYS:
                continue
            # a session open inside (even idle, the owner just not back yet) → don't touch it at all, either level
            if _in_use(p, in_use):
                continue
            if idle >= REMOVE_IDLE_DAYS:
                unknown = _unknown_ignored(p, as_project(name))
                if unknown is None:
                    res["skipped"].append(_("{label}: its ignored files could not be listed; kept", label=label))
                    continue
                if unknown:
                    res["skipped"].append(_("{label}: ignored files not on the rebuildable list ({files}…); kept",
                                            label=label, files=", ".join(unknown[:3])))
                else:
                    mb = _size_mb(p)
                    if not dry_run:
                        now = _cwds_in_use()   # minutes may have gone by since the first look
                        if now is None or _in_use(p, now):
                            continue
                        r = project_git.job(as_project(name), repo, "worktree-remove", repo, p)
                        if r.returncode != 0:
                            res["skipped"].append(_("{label}: git worktree remove failed: {err}", label=label,
                                                    err=r.stderr.strip()[:200]))
                            continue
                    res["removed"].append(_("{label} (idle {days} {days:day|days}, {mb} MB; branch kept)", label=label, days=idle,
                                            mb=mb))
                    res["freed_mb"] += mb
                    continue
            for dep, recipes in DEPS.items():
                d = p / dep
                if not d.is_dir() or d.is_symlink():
                    continue
                if not any((p / r).exists() for r in recipes):
                    continue
                mb = _size_mb(d)
                if not dry_run:
                    # deleted, and the marker written into its git dir, as the project (a symlink planted there only
                    # reaches what the project can write itself)
                    line = _("{date} {dep} → rebuild: {cmd}", date=res['at'][:10], dep=dep, cmd=REBUILD[dep])
                    r = project_git.job(as_project(name), p, "remove-dep", p, dep, MARKER, line,
                                        timeout=project_git.BUNDLE_TIMEOUT_S)
                    if r.returncode != 0:
                        res["skipped"].append(_("{label}: removing {dep} failed: {err}", label=label, dep=dep,
                                                err=r.stderr.strip()[:200]))
                        continue
                res["deps"].append(_("{label} {dep} (idle {days} {days:day|days}, {mb} MB)", label=label, dep=dep, days=idle, mb=mb))
                res["freed_mb"] += mb
    res["freed_mb"] = round(res["freed_mb"], 1)
    if not dry_run:
        f = hubconf.STATE / "worktree-prune.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res
