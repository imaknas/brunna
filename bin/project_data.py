"""A project's data outside its folder (registry `data:`, docs/design/project-data.md): which paths a project may
declare, what the backup takes of them, and restic's exclude rules in Python (one matcher for backup.covers() and the
daily check, so all three agree with what restic does).

Two layers of path rules (blind review, 11-26):
  text_problems(): the validator's, from the entry's text and the project's own document only (a registry file must
    validate the same on every machine, and a refused file freezes the project's whole entry);
  entries(): where the disk is read (the backup, bin/check-data.py): each path resolved, the same place rules on the
    resolved path, and no overlap with another project's entry or the owner's backup.include. An entry that fails is
    skipped (its problem said), the rest go on.
Standard library only.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402

_ = i18n.domain(__file__)

FIELDS = {"path", "what", "backup", "why", "exclude"}
# no glob, bracket, brace or control character: <path>/<pattern> becomes a restic exclude pattern
PLAIN = re.compile(r"[^\x00-\x1f\x7f()\[\]{}*?]+")
# the owner's tool, credential and personal folders under home: never a project's data, nor around one
# (the coding agents' own folders come from their adapters: runtime_ports.user_controls())
DENY_HOME = (".ssh", ".gnupg", "Library", ".config", ".aws", ".docker", ".kube", ".netrc",
             ".local/state", ".nvm", ".password-store", "Documents", "Desktop", "Downloads")
EXTERNAL = pathlib.Path("/Volumes")


def _home() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser("~"))


def expand(raw) -> pathlib.Path | None:
    """`~/…` or absolute, with no `..` step: the path, normalised; None for anything else."""
    if not isinstance(raw, str) or not raw.startswith(("~/", "/")) or not PLAIN.fullmatch(raw):
        return None
    p = pathlib.Path(os.path.expanduser(raw))
    if ".." in p.parts:
        return None
    return pathlib.Path(os.path.normpath(p))


def _within(p: pathlib.Path, root: pathlib.Path) -> bool:
    return p == root or root in p.parents


def _own_folders(name: str, doc: dict) -> list[pathlib.Path]:
    ws = (doc.get("paths") or {}).get("workspace")
    out = [hubconf.path(ws)] if ws else []
    root = hubconf.p("services_root")
    if doc.get("runtime") is not None and root:
        out.append(root / name)
    return [pathlib.Path(os.path.normpath(p)) for p in out if p]


def _platform_roots() -> list[pathlib.Path]:
    out = [hubconf.ROOT, hubconf.instance_home(), hubconf.config_dir(), hubconf.data_dir(), hubconf.secrets_dir(),
           hubconf.bus_db().parent, hubconf.p("memory_copies"), hubconf.p("git_origin"), hubconf.STATE]
    try:
        import runtime_ports
        out += [pathlib.Path(os.path.expanduser(d)) for d in runtime_ports.user_controls()[0]]
    except Exception:   # noqa: BLE001 (no adapter to ask: the fixed list still holds)
        pass
    return [pathlib.Path(os.path.normpath(p)) for p in out if p]


def place_problem(name: str, doc: dict, p: pathlib.Path) -> str | None:
    """Why this path may not be this project's data, or None. The same rules for the text and the resolved path."""
    home = _home()
    if not ((home in p.parents) or (EXTERNAL in p.parents and len(p.relative_to(EXTERNAL).parts) > 1)):
        return _("it must be under the home folder (not the home folder itself) or on an external disk under /Volumes")
    own = _own_folders(name, doc)
    if any(p == o for o in own):
        return _("it is the project's whole folder: declare a folder inside it (the backup never takes whole workspaces)")
    if not (any(o in p.parents for o in own) or name.lower() in (x.lower() for x in p.parts)):
        return _("it must be inside the project's own folder, or have a folder named {project} in it", project=name)
    for root in _platform_roots():
        if _within(p, root) or _within(root, p):
            return _("it is inside, or holds, a folder of the platform's ({root})", root=root)
    for key in ("services_root", "workspace_root"):
        root = hubconf.p(key, "~/workspace" if key == "workspace_root" else None)
        root = pathlib.Path(os.path.normpath(root)) if root else None
        # under the services or workspace root only inside its own runtime or workspace (not another project's)
        if root and (_within(root, p) or (root in p.parents and not any(o in p.parents for o in own))):
            return _("it is inside, or holds, the folder every project's {kind} is in ({root})",
                     kind=_("services") if key == "services_root" else _("workspace"), root=root)
    for rel in DENY_HOME:
        d = home / rel
        if _within(p, d) or _within(d, p):
            return _("it is inside, or holds, a folder of the owner's that no project's data is in ({folder})",
                     folder="~/" + rel)
    return None


def text_problems(name: str, doc: dict) -> list[str]:
    """The validator's checks of `data:` (text only)."""
    data = doc.get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        return [_("data must be a list")]
    errs, seen = [], []
    for i, e in enumerate(data):
        where = f"data[{i}]"
        if not isinstance(e, dict):
            errs.append(_("{where} must be a mapping", where=where))
            continue
        errs += [_("{where}: unknown field {key}", where=where, key=k) for k in e if k not in FIELDS]
        if not (isinstance(e.get("what"), str) and e["what"].strip()):
            errs.append(_("{where}: what is required (one line: what this data is)", where=where))
        if "backup" in e and not isinstance(e["backup"], bool):
            errs.append(_("{where}: backup must be true/false", where=where))
        if e.get("backup") is False and not (isinstance(e.get("why"), str) and e["why"].strip()):
            errs.append(_("{where}: backup: false needs a why (why it can be made or downloaded again)", where=where))
        ex = e.get("exclude")
        if ex is not None and not (isinstance(ex, list) and all(isinstance(x, str) and x for x in ex)):
            errs.append(_("{where}: exclude must be a list of patterns", where=where))
        elif ex:
            for x in ex:
                if x.startswith("/") or ".." in x.split("/") or re.search(r"[\x00-\x1f\x7f]", x):
                    errs.append(_("{where}: exclude {pattern} must be relative to path, with no .. and no control "
                                  "character", where=where, pattern=x))
        p = expand(e.get("path"))
        if p is None:
            errs.append(_("{where}: path must be ~/… or absolute, with no .. and no glob, bracket, brace or control "
                          "character", where=where))
            continue
        why = place_problem(name, doc, p)
        if why:
            errs.append(_("{where}: path {path}: {why}", where=where, path=e["path"], why=why))
        for q in seen:
            if _within(p, q) or _within(q, p):
                errs.append(_("{where}: path {path} overlaps another entry of this project", where=where, path=e["path"]))
        seen.append(p)
    return errs


@dataclasses.dataclass
class Entry:
    project: str
    raw: str
    what: str
    backup: bool
    exclude: list[str]
    path: pathlib.Path | None        # resolved; None when the entry is skipped
    problem: str | None = None

    def patterns(self) -> list[str]:
        """The excludes as absolute restic patterns on the resolved path."""
        return [f"{self.path}/{x.rstrip('/')}" for x in self.exclude] if self.path else []


def entries(projects: dict[str, dict] | None = None, include: list[pathlib.Path] | None = None) -> list[Entry]:
    """Every project's declared data, resolved, with the use-time rules applied. include: the owner's
    backup.include (resolved), which no project's entry may overlap (restic's excludes apply to the whole run)."""
    projects = hubconf.projects() if projects is None else projects
    include = [] if include is None else [pathlib.Path(os.path.realpath(x)) for x in include]
    out: list[Entry] = []
    for name, doc in sorted(projects.items()):
        for e in doc.get("data") or []:
            if not isinstance(e, dict):
                continue
            ent = Entry(name, str(e.get("path")), str(e.get("what") or ""), e.get("backup", True) is not False,
                        [x for x in e.get("exclude") or [] if isinstance(x, str)], None)
            p = expand(e.get("path"))
            if p is None:
                ent.problem = _("the path is not one a project may declare")
            else:
                real = pathlib.Path(os.path.realpath(p))
                why = place_problem(name, doc, real)
                if why:
                    ent.problem = _("{path} leads to {real}: {why}", path=ent.raw, real=real, why=why) if real != p else why
                elif any(_within(real, f) or _within(f, real) for other, odoc in projects.items() if other != name
                         for f in _own_folders(other, odoc)):
                    ent.problem = _("it is inside, or holds, another project's folder")
                elif any(_within(real, x) or _within(x, real) for x in include):
                    ent.problem = _("it overlaps a folder the owner's backup settings list (backup.include): ask brunna "
                                    "to move that folder to this declaration")
                else:
                    ent.path = real
            out.append(ent)
    taken = [e for e in out if e.path]
    for e in taken:
        for o in taken:
            if o is not e and o.project != e.project and (_within(e.path, o.path) or _within(o.path, e.path)):
                e.problem = _("it overlaps {project}'s declared data ({path})", project=o.project, path=o.raw)
    for e in out:
        if e.problem:
            e.path = None
    return out


# ---------- restic's exclude rules ----------

def _match_parts(pat: list[str], parts: list[str]) -> bool:
    """Whether the pattern's components match the start of parts (a match on a folder takes what is below it)."""
    if not pat:
        return True
    if pat[0] == "**":
        return any(_match_parts(pat[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pat[0]) and _match_parts(pat[1:], parts[1:])


def excluded(path: str | pathlib.Path, pattern: str) -> bool:
    """Whether restic's --exclude <pattern> leaves this absolute path out: components matched one by one (`*` stays
    within one, `**` spans any number), a match on a folder takes everything below it, and a pattern that doesn't
    start with / may match starting at any folder."""
    parts = [x for x in str(path).split("/") if x]
    pat = [x for x in pattern.rstrip("/").split("/") if x]
    if not pat:
        return False
    if pattern.startswith("/"):
        return _match_parts(pat, parts)
    return any(_match_parts(pat, parts[i:]) for i in range(len(parts)))


def size(root: pathlib.Path, patterns: list[str], cap: int | None = None) -> int:
    """Bytes of what the backup would take under root (excludes applied, symlinks not followed); stops past cap."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not any(excluded(os.path.join(dirpath, d), x) for x in patterns)]
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if any(excluded(fp, x) for x in patterns):
                continue
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            total += st.st_size
            if cap is not None and total > cap:
                return total
    return total
