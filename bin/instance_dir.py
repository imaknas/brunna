"""The instance directory's folder: which folders init may take for one, and what `brunna uninstall --purge` takes out of it.

A folder is an instance when its config/ holds the platform's settings (brunna.json, or hub.json from before the rename).
init makes one only in a folder that is new, empty, or left empty by an init that stopped; it marks it (MARK). In a marked
folder, and in the default one (~/.brunna, ~/.agent-hub), everything under PARTS was made through brunna and goes whole;
whatever else is in it stays. Any other instance was put, by an earlier version, in a folder that may have held the user's
own config/ or data/ before: nothing in it is removed, and what there is brunna's is named for its owner to remove."""
from __future__ import annotations

import json
import os
import pathlib
import pwd
import re
import shutil
import subprocess

import hubconf
import i18n
import names

_ = i18n.domain(__file__)

PARTS = ("runtimes", "tools", "data", "secrets", "config")   # config last: a removal that stops can be run again
MARK = f".{names.current('product')}-instance"
NOISE = {".DS_Store"}
SETTINGS_KEYS = {"v", "owner", "registry_dir", "modules", "runtime"}   # one of them: it is the platform's settings file

# what of config/ its first commit holds, beside the registry
CONFIG_FILES = [*names.all("config_file"), "models.env", "mirror-list.txt", "services.tsv", "schedule.yaml"]
# what of the data an old layout keeps in the hub's own folder
DATA_FILES = ["digest", "reflect", "todo.md", "index.md", "review-log.md", "openclaw-migration-status.md", "legacy-paths.tsv"]
BUS_FILE = re.compile(r"(bus\.db(\.corrupt-\d{8}-\d{4}|\.migrating|\.moved)?(-wal|-shm|-journal)?|dispatch\.lock|hubbus\.log)")
MEMORY_HEAD = "ref: refs/heads/agent-memory"


# ---------- init ----------

def settings(home) -> pathlib.Path | None:
    """The settings file that makes this folder an instance, or None. One that is there but can't be read counts: it is
    never written over (broken)."""
    for f in names.resolve_both(pathlib.Path(home) / "config", "config_file"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            return f
        if isinstance(d, dict) and SETTINGS_KEYS & d.keys():
            return f
    return None


def broken(f: pathlib.Path) -> str | None:
    """Why a settings file can't be used, or None."""
    try:
        json.loads(f.read_text())
    except (OSError, ValueError) as e:
        return str(e)
    return None


def _bare(folder: pathlib.Path) -> bool:
    """No file anywhere in it: what an init that stopped before its settings leaves."""
    return folder.is_dir() and not folder.is_symlink() and not any(
        n not in NOISE for _d, _dirs, files in os.walk(folder) for n in files)


def problem(home) -> str | None:
    """Why init can't keep its settings, notes and keys in this folder, or None: a new or empty folder, or an instance."""
    real = pathlib.Path(os.path.realpath(home))
    for user in {os.path.realpath(pathlib.Path.home()), os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)}:
        if pathlib.Path(user).is_relative_to(real):
            return _("{path} is your home folder or contains it; name a folder of its own, such as {example}", path=home,
                     example="~/." + names.current("product"))
    hub = pathlib.Path(os.path.realpath(hubconf.MAIN))
    if real.is_relative_to(hub):
        return _("{path} is inside the hub's folder ({hub}): removing the hub would take it along; name a folder of its own, such "
                 "as {example}", path=home, hub=hub, example="~/." + names.current("product"))
    if not os.path.lexists(home):
        return None
    if not real.is_dir():
        return _("{path} is not a folder", path=home)
    if (f := settings(home)):
        why = broken(f)
        return None if why is None else _(
            "{path} holds an earlier install's settings, but {file} can't be read ({why}). Put it right (git -C {config} log "
            "shows its history), then run init again", path=home, file=f.name, why=why, config=f.parent)
    try:
        there = [p for p in real.iterdir() if p.name not in NOISE and p.name != MARK]
    except OSError as e:
        return f"{home}: {e.strerror}"
    marked = (real / MARK).is_file()   # what an uninstall left beside brunna's own is no reason to refuse its folder
    if any(not _bare(p) if p.name in PARTS else not marked for p in there):
        return _("{path} already holds other things; name a new or an empty folder", path=home)
    return None


def mark(home) -> None:
    (pathlib.Path(home) / MARK).write_text(
        "This folder is brunna's: uninstall --purge removes " + ", ".join(reversed(PARTS)) + " in it, and nothing else.\n")


# ---------- uninstall --purge ----------

def own(home) -> bool:
    """Whether everything under PARTS was made through brunna."""
    home = pathlib.Path(os.path.abspath(home))
    if not home.is_symlink() and home in {pathlib.Path.home() / f".{n}" for n in names.all("product")}:
        return True
    return (home / MARK).is_file()


def remove(path: pathlib.Path) -> list[str]:
    """A file, a link (never what it points to) or a folder whole; what couldn't be removed, each with why."""
    failed: list[str] = []
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, onexc=lambda _f, p, e: failed.append(f"{p}: {getattr(e, 'strerror', None) or e}"))
    except OSError as e:
        failed.append(f"{path}: {e.strerror or e}")
    return failed


def rmdir(folder: pathlib.Path) -> bool:
    """Remove an empty folder (the desktop's own leftovers don't count); whether it went."""
    try:
        if {p.name for p in folder.iterdir()} <= NOISE:
            for n in NOISE:
                (folder / n).unlink(missing_ok=True)
        folder.rmdir()
        return True
    except OSError:
        return False


def parts(home) -> list[str]:
    """What an instance directory holds that is brunna's."""
    return [str(pathlib.Path(home) / p) for p in reversed(PARTS) if os.path.lexists(pathlib.Path(home) / p)]


def purge(home) -> tuple[list[str], list[str]]:
    """Take brunna's out of an instance directory of its own, and remove it when nothing else is in it: (what couldn't be
    removed, what is left because it isn't brunna's). Nothing is touched in one that isn't (own)."""
    home = pathlib.Path(home)
    failed: list[str] = []
    kept: list[str] = []
    if not own(home):
        return failed, parts(home)
    for p in (home / part for part in PARTS):
        if failed and p.name == "config":
            break   # what names the projects and this folder goes when everything else has
        if p.is_symlink():
            kept.append(os.path.realpath(p))   # the link goes, what it points to stays
        if os.path.lexists(p):
            failed += remove(p)
    if failed:
        return failed, kept
    if others := sorted(str(home / n) for n in {p.name for p in home.iterdir()} - NOISE - {MARK}):
        return failed, kept + others
    failed += remove(home / MARK)
    if not rmdir(home):   # a link to it, or something that can't be removed: it is empty, and stays
        kept.append(str(home))
    return failed, kept


def own_bus(folder) -> bool:
    """Whether this is the folder the bus gets by default: in it, the bus's own files go. One named in the settings may be
    any folder of the user's."""
    folder = pathlib.Path(os.path.abspath(folder))
    return not folder.is_symlink() and folder in {
        *(pathlib.Path.home() / ".local" / "state" / f"{n}-bus" for n in names.all("product")), hubconf.STATE / "bus"}


def bus_files(folder) -> list[pathlib.Path]:
    return sorted(p for p in pathlib.Path(folder).iterdir() if BUS_FILE.fullmatch(p.name) and not p.is_dir())


def purge_bus(folder) -> tuple[list[str], list[str]]:
    """The bus's files out of its own folder, and the folder when that empties it: (failed, kept) as purge."""
    folder = pathlib.Path(folder)
    if not own_bus(folder):
        return [], [str(p) for p in bus_files(folder)]
    failed = [line for p in bus_files(folder) for line in remove(p)]
    return (failed, []) if failed or rmdir(folder) else ([], [str(folder)])


def within(path, folder) -> bool:
    return pathlib.Path(os.path.realpath(path)).is_relative_to(os.path.realpath(folder))


def is_memory_bare(path: pathlib.Path) -> bool:
    """A repo of notes: the memory branch, and nothing beside it but the platform's own refs."""
    if path.is_symlink() or not (path / "HEAD").is_file():
        return False
    r = subprocess.run(["git", "--git-dir", str(path), "for-each-ref", "--format=%(refname)"], capture_output=True, text=True)
    refs = [x for x in r.stdout.split() if not x.startswith(tuple(f"refs/{n}/" for n in names.all("repo_dir")))]
    return r.returncode == 0 and refs == ["refs/heads/agent-memory"]


def is_memory_copy(path: pathlib.Path) -> bool:
    """A librarian's copy: a checkout of the memory branch, never a project's own checkout of its code."""
    try:
        return not path.is_symlink() and (path / ".git" / "HEAD").read_text().strip() == MEMORY_HEAD
    except OSError:
        return False
