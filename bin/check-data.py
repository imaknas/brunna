#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The projects' declared data outside their folders (registry data:, docs/design/project-data.md), checked daily
(platform job brunna/data):
  - each entry resolves to a place it may be (bin/project_data.py, the same rules the backup applies) and exists;
  - the size of what the backup takes of it (excludes applied, symlinks not followed), kept per day in
    .state/project-data.json: more than double and over 1 GB more than a week before is reported (something that can
    be made again landed in it without an exclude);
  - undeclared data: a folder named exactly like a registered project (four characters or more, any case) directly
    under ~, ~/.cache or ~/.local/share, or one level down in a plain folder of ~, over 50 MB, inside no workspace,
    runtime, declared entry, backup.include entry or platform folder. That project gets one request letter per path,
    asked once. Shared download caches (Hugging Face, Ollama, uv, pip, npm) are downloads, never asked about.
Whether last night's backup ran is the backup's own alert, not this one's.
Problems go to each project through alert.once (key project-data:<project>, routed to the project first); exit
alert.FOUND (4) when some were found and delivered, 1 when they couldn't be.

Usage: check-data.py [--dry-run]   (--dry-run: print, send nothing, write no state)
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import alert  # noqa: E402
import backup  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import project_data  # noqa: E402

_ = i18n.domain(__file__)

STATE = hubconf.STATE / "project-data.json"
GB = 1 << 30
ASK_MIN = 50 << 20
KEEP_DAYS = 30
SCAN_PARENTS = ("", ".cache", ".local/share")
DOWNLOAD_CACHES = (".cache/huggingface", ".ollama", ".cache/uv", ".cache/pip", ".npm", ".cache/torch")
# home folders never looked into: the owner's (project_data.DENY_HOME) and the ones macOS guards (reading one from a
# job blocks on its access check: the first scheduled runs hung until the 30-minute limit, 11-26)
NEVER_SCAN = set(project_data.DENY_HOME) | {"Pictures", "Music", "Movies", "Public", "Applications", "Library"}


def _load() -> dict:
    try:
        d = json.loads(STATE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def grew(history: dict, today: str, now_bytes: int) -> bool:
    """More than double and over 1 GB more than the newest record at least a week old."""
    week = (datetime.date.fromisoformat(today) - datetime.timedelta(days=7)).isoformat()
    older = sorted(d for d in history if d <= week)
    if not older:
        return False
    before = history[older[-1]]
    return now_bytes > 2 * before and now_bytes - before > GB


def _taken(projects: dict, entries: list) -> list[pathlib.Path]:
    """Folders a found one may not be inside (or around): workspaces, runtimes, declared, the owner's include, the
    platform's, the shared download caches."""
    home = pathlib.Path(os.path.expanduser("~"))
    out = [e.path for e in entries if e.path] + backup.owner_included() + project_data._platform_roots()
    for name, doc in projects.items():
        out += project_data._own_folders(name, doc)
    out += [hubconf.p("services_root"), hubconf.p("workspace_root", "~/workspace")]
    out += [home / x for x in DOWNLOAD_CACHES]
    return [pathlib.Path(os.path.realpath(p)) for p in out if p]


def candidates(projects: dict, entries: list) -> list[tuple[str, pathlib.Path]]:
    """(project, folder) for undeclared folders named like a project."""
    home = pathlib.Path(os.path.expanduser("~"))
    names = {n.lower(): n for n in projects if len(n) >= 4}
    taken = _taken(projects, entries)
    parents = [home / x for x in SCAN_PARENTS]
    try:
        parents += [c for c in home.iterdir() if c.name not in NEVER_SCAN and not c.name.startswith(".")
                    and c.is_dir() and not c.is_symlink()]
    except OSError:
        pass
    seen, out = set(), []
    for parent in parents:
        if parent.name in NEVER_SCAN:
            continue
        try:
            kids = [c for c in parent.iterdir() if c.is_dir() and not c.is_symlink()]
        except OSError:
            continue
        for c in kids:
            project = names.get(c.name.lower())
            real = pathlib.Path(os.path.realpath(c))
            if not project or real in seen:
                continue
            seen.add(real)
            if any(real == t or t in real.parents or real in t.parents for t in taken):
                continue
            if project_data.size(real, [], cap=ASK_MIN) > ASK_MIN:
                out.append((project, real))
    return out


def ask_text(project: str, path: pathlib.Path) -> str:
    return _("The platform found {path} (over 50 MB), which looks like {project}'s data but isn't declared. If it is "
             "yours, declare it in your registry entry's data: (path, what, and either the exclude patterns of what "
             "can be made again, or backup: false with a why if all of it can be downloaded or made again), with a "
             "manifest.update. Until then the nightly backup doesn't take it. If it isn't yours, reply saying so.",
             path=path, project=project)


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    projects = hubconf.projects()
    entries = project_data.entries(projects, include=backup.owner_included())
    today = datetime.date.today().isoformat()
    state = _load()
    sizes = state.setdefault("sizes", {})
    asked = state.setdefault("asked", {})
    problems: dict[str, list[str]] = {}
    ids: dict[str, list[str]] = {}   # one stable id per problem: a size that changes daily is the same problem

    def problem(project: str, pid: str, text: str) -> None:
        problems.setdefault(project, []).append(text)
        ids.setdefault(project, []).append(pid)

    for e in entries:
        where = f"data {e.raw}"
        if e.problem:
            problem(e.project, f"skipped:{e.raw}", _("{where}: skipped by the backup: {why}", where=where, why=e.problem))
            continue
        if not e.path.exists():
            problem(e.project, f"missing:{e.raw}", _("{where}: missing (moved? change the declaration)", where=where))
            continue
        if not e.backup:
            print(_("{project}: {path} declared, not backed up", project=e.project, path=e.raw))
            continue
        unreadable: list[str] = []
        n = project_data.size(e.path, e.patterns(), unreadable=unreadable)
        if unreadable:
            problem(e.project, f"unreadable:{e.raw}",
                    _("{where}: {count} {count:path|paths} in it can't be read, so the backup misses them (first: "
                      "{first}): make them readable, or exclude them", where=where, count=len(unreadable),
                      first=unreadable[0]))
        hist = sizes.setdefault(f"{e.project}:{e.path}", {})
        if grew(hist, today, n):
            problem(e.project, f"grew:{e.raw}",
                    _("{where}: what the backup takes of it grew to {gb} GB in a week: did something that can be made "
                      "again land in it without an exclude?", where=where, gb=round(n / GB, 1)))
        else:   # a ballooned size is not kept as the baseline, so the warning stays until it is dealt with
            hist[today] = n
        for d in [d for d in hist if d < (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()]:
            del hist[d]
        print(_("{project}: {path} backed up, {mb} MB", project=e.project, path=e.raw, mb=round(n / (1 << 20))))
    # installed services (docs/design/service-ecosystem.md, step 2b): the backup takes each one's state/ unless it is
    # over the cap or isn't its own real folder; say so here, where it is seen
    # (their own alert key, service-data:<name>, the owner's: no project's agent can shrink a service's state)
    import installed_services
    service_problems: dict[str, list[str]] = {}
    for svc_name, se in sorted(installed_services.entries().items()):
        f = installed_services.folders(svc_name)
        if not f["state"].exists():
            continue
        if (why := installed_services.own_folder(f["state"], f["base"], make=False)):
            service_problems[svc_name] = [_("service {name}: not backed up: {why}", name=svc_name, why=why)]
            continue
        skip = [f"{f['state']}/{d['path'].rstrip('/')}" for d in se.get("data") or [] if d.get("backup") is False]
        if project_data.size(f["state"], skip, cap=installed_services.CAP) > installed_services.CAP:
            service_problems[svc_name] = [_("service {name}: its state is over {gb} GB, so the backup leaves it out",
                                            name=svc_name, gb=installed_services.CAP >> 30)]
    live = {f"{e.project}:{e.path}" for e in entries if e.path}
    for k in [k for k in sizes if k not in live]:
        del sizes[k]
    found = candidates(projects, entries)
    for project, path in found:
        if str(path) in asked:
            continue
        print(_("{project}: undeclared data that looks like its: {path}", project=project, path=path))
        if not dry:
            lid = "data-ask-" + hashlib.sha1(str(path).encode()).hexdigest()[:12]
            if alert._hubbus("send", "--from", hubconf.hub_project(), "--to", project, "--type", "request", "--id", lid,
                             "--text", ask_text(project, path)):
                asked[str(path)] = today
    if dry:
        for p, probs in problems.items():
            for x in probs:
                print(f"⚠️ {p}: {x}")
        return 0
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    told = True
    for svc_name, probs in sorted(service_problems.items()):
        for x in probs:
            print(f"⚠️ {x}")
        told &= alert.once(f"service-data:{svc_name}", probs, _("service {name}'s state is backed up again",
                                                                 name=svc_name))
    for key in alert.open_keys("service-data:"):
        if key.split(":", 1)[1] not in service_problems:
            told &= alert.once(key, [], _("service {name}'s state is backed up again", name=key.split(":", 1)[1]))
    for key in alert.open_keys(alert.DATA_KEY):
        if key[len(alert.DATA_KEY):] not in problems:
            told &= alert.once(key, [], _("{project}'s declared data is fine again", project=key[len(alert.DATA_KEY):]))
    for project, probs in sorted(problems.items()):
        for x in probs:
            print(f"⚠️ {project}: {x}")
        told &= alert.once(alert.DATA_KEY + project, probs,
                           _("{project}'s declared data is fine again", project=project), ids=ids[project])
    print(_("Checked {count} declared {count:folder|folders}; {problems} {problems:project|projects} with problems",
            count=len(entries), problems=len(problems)))
    if not problems and not service_problems:
        return 0 if told else 1
    return alert.FOUND if told else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
