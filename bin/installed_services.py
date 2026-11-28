"""Installed services (docs/design/service-ecosystem.md, step 2a): their entries, folders and names.

An installed service is someone else's program that `brunna service add` fetched from its repository, set up and
granted what its manifest (`brunna-service.yaml`, bin/service_manifest.py) asked for. It is not a registry entry: it
has no workspace, memory or merge gate, and every walker of the registry would take it for a project. Its entry is
`<config>/services/<name>.yaml` (hubconf.services_dir()):

  v: 1
  name: <the manifest's name>
  what: <the manifest's what>
  url: <the repository: https, ssh, or (tests) a local path>
  ref: <the tag or branch asked for> | null (a commit was)
  commit: <40 hex>
  interface: <the manifest's interface major at that commit>
  run: {cli: <command>, mcp: <command>, setup: [<command>…]}   copied from the manifest at add (never read back from
                                                                  code/, which setup can rewrite)
  granted: {models: {<name>: {tier}}, secrets: [<NAME>…], principal: bool, network: [<host>…]}
  data: [{path, what, backup?, why?}]     the manifest's, relative to state/ (step 2b)
  secrets_required: [<NAME>…]             the granted secrets not marked optional
  follow: tags | none                     tags: updated to newer release tags by brunna/service-updates
  tags: {<tag>: <commit>}                 every release tag installed, with the commit it named then
  previous: <40 hex> | null               the commit before, kept for processes still running in it
  callers: [<project>…]                   who may call it (step 3); not set = every registered project
  added: YYYY-MM-DD

Its folders, all under <services_root>/<name>/ (folders()): repo.git (the fetched commits), code/<commit>/ (read at
run time, written in setup), cache/ (tool caches and downloads), state/ (what it keeps; STATE_ENV below),
secrets/ (one file per granted secret). Standard library + PyYAML.
"""
from __future__ import annotations

import pathlib
import re
import sys
import unicodedata

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import names  # noqa: E402

_ = i18n.domain(__file__)

NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,47}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SECRET = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
TOP = {"v", "name", "url", "ref", "commit", "interface", "run", "granted", "added", "data", "secrets_required",
       "follow", "tags", "previous", "callers", "what"}
GRANTED = {"models", "secrets", "principal", "network"}
RELEASE = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
CAP = 20 << 30   # what a service's state/ may hold before the backup skips it and check-data says so
IDEA = re.compile(r"idea-[0-9]+\Z")
# what the service gets as its environment names (part of the service contract, docs/service-authors.md)
STATE_ENV = names.ENV_NEW + "SERVICE_STATE"
CALLER_ENV = names.ENV_NEW + "CALLER"


def fold(name: str) -> str:
    """A name as the disk compares it (APFS is case-insensitive by default)."""
    return unicodedata.normalize("NFC", name).casefold()


def root() -> pathlib.Path:
    return hubconf.p("services_root", "~/services")


def folders(name: str, commit: str | None = None) -> dict[str, pathlib.Path]:
    base = root() / name
    out = {"base": base, "repo": base / "repo.git", "code_root": base / "code", "cache": base / "cache",
           "state": base / "state", "secrets": base / "secrets", "home": base / "state" / "home",
           "tmp": base / "cache" / "tmp"}
    if commit:
        out["code"] = base / "code" / commit
    return out


def entry_problems(doc, stem: str | None = None) -> list[str]:
    """What is wrong with an entry (as read from its file)."""
    if not isinstance(doc, dict):
        return [_("the entry must be a mapping")]
    errs = [_("unknown field {key}", key=k) for k in doc if k not in TOP]
    if doc.get("v") != 1 or isinstance(doc.get("v"), bool):
        errs.append(_("v must be 1"))
    name = doc.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        errs.append(_("name must be lowercase letters, digits and -, 2 to 48 characters"))
    elif stem is not None and stem != name:
        errs.append(_("the file is named {stem} but the entry is {name}", stem=stem, name=name))
    if not isinstance(doc.get("url"), str) or not doc["url"] or doc["url"].startswith("-"):
        errs.append(_("url must be the repository's address"))
    if doc.get("ref") is not None and not (isinstance(doc["ref"], str) and doc["ref"]):
        errs.append(_("ref must be a tag or branch name, or null"))
    if not isinstance(doc.get("commit"), str) or not COMMIT.match(doc["commit"]):
        errs.append(_("commit must be 40 hex characters"))
    if not isinstance(doc.get("interface"), int) or isinstance(doc.get("interface"), bool) or doc["interface"] < 1:
        errs.append(_("interface must be a whole number from 1"))
    run = doc.get("run")
    if not (isinstance(run, dict) and set(run) <= {"cli", "mcp", "setup"} and all(
            isinstance(run.get(k), str) and run[k] for k in ("cli", "mcp"))
            and isinstance(run.get("setup", []), list) and all(isinstance(c, str) and c for c in run.get("setup", []))):
        errs.append(_("run must be {{cli, mcp, setup}}: commands"))
    g = doc.get("granted")
    if not isinstance(g, dict) or set(g) - GRANTED:
        errs.append(_("granted must be a mapping of models, secrets, principal, network"))
    else:
        if not isinstance(g.get("models", {}), dict):
            errs.append(_("granted.models must be a mapping"))
        if not (isinstance(g.get("secrets", []), list) and all(isinstance(s, str) and SECRET.match(s)
                                                               for s in g.get("secrets", []))):
            errs.append(_("granted.secrets must be a list of environment names"))
        if not isinstance(g.get("principal", False), bool):
            errs.append(_("granted.{key} must be true or false", key="principal"))
        if not (isinstance(g.get("network", []), list) and all(isinstance(h, str) for h in g.get("network", []))):
            errs.append(_("granted.network must be a list of hosts"))
    if doc.get("follow", "none") not in ("tags", "none"):
        errs.append(_("follow must be tags or none"))
    tags = doc.get("tags", {})
    if not (isinstance(tags, dict) and all(isinstance(k, str) and RELEASE.match(k) and isinstance(v, str)
                                           and COMMIT.match(v) for k, v in tags.items())):
        errs.append(_("tags must map release tags to commits"))
    if "callers" in doc and not (isinstance(doc["callers"], list) and all(isinstance(c, str) and c
                                                                            for c in doc["callers"])):
        errs.append(_("callers must be a list of project names"))
    if doc.get("previous") is not None and not (isinstance(doc["previous"], str) and COMMIT.match(doc["previous"])):
        errs.append(_("previous must be 40 hex characters or null"))
    req = doc.get("secrets_required", [])
    if not (isinstance(req, list) and all(isinstance(s, str) for s in req)
            and set(req) <= set((g or {}).get("secrets", []) if isinstance(g, dict) else [])):
        errs.append(_("secrets_required must list granted secrets"))
    data = doc.get("data", [])
    if not isinstance(data, list):
        errs.append(_("data must be a list"))
    else:
        import service_manifest
        probe = {"v": 1, "name": "ab", "what": "x", "interface": 1, "cli": {"run": "a"}, "mcp": {"run": "b"},
                 "data": data}
        errs += service_manifest.problems(probe)
    return errs


def release(tag: str) -> tuple[int, int, int] | None:
    m = RELEASE.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def own_folder(d: pathlib.Path, base: pathlib.Path, make: bool = True) -> str | None:
    """d is a real folder of this user's, not a link, with no link from base down to it (made one level at a time
    when missing and make is true); None when it is, else why not. A service can't write base/, so a link on the way
    could only be one it made in a folder it writes: state/ swapped for a link would lead the next run's profile, the
    owner's mkdir or the backup into a folder of the owner's."""
    import os
    import stat
    d = pathlib.Path(d)
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        if not make:
            return _("{path} is missing", path=d)
        if d != base and (why := own_folder(d.parent, base)):
            return why
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        except OSError as e:
            return _("{path} can't be made ({err})", path=d, err=e.strerror)
        st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        return _("{path} is not a folder (a link or a file where its folder belongs)", path=d)
    if st.st_uid != os.getuid():
        return _("{path} is not this user's", path=d)
    if d != base:
        if (why := own_folder(d.parent, base, make=False)):
            return why
        if os.path.realpath(d) != os.path.join(os.path.realpath(d.parent), d.name):
            return _("{path} doesn't lead where it should", path=d)
    return None


def secret_problems(name: str, entry: dict) -> list[str]:
    """The required secrets that aren't set: each must be a real, non-empty file of this user's, readable by no one
    else, in the service's secrets folder. An optional one isn't checked."""
    import os
    import stat
    out = []
    for secret in entry.get("secrets_required") or []:
        try:
            st = os.lstat(folders(name)["secrets"] / secret)
            ok = (stat.S_ISREG(st.st_mode) and st.st_size > 0 and st.st_uid == os.getuid()
                  and not st.st_mode & 0o077)
        except OSError:
            ok = False
        if not ok:
            out.append(_("{service}: secret {name} is not set: brunna service secret {service} {name}",
                         service=name, name=secret))
    return out


def lock_wait() -> float:
    """How long add, update, remove and run wait for the lock: a minute (tests set a shorter one)."""
    try:
        return float(names.env("SERVICE_LOCK_WAIT") or 60)
    except ValueError:
        return 60.0


class Locked(Exception):
    pass


def lock(name: str, shared: bool = False, wait: float | None = None):
    """The service's lock (<state>/services/<name>.lock, where no sandbox reads): exclusive for add, update and
    remove, shared for run, so a swap or remove never happens under a running CLI. Retried for `wait` seconds, then
    Locked. A context manager; the lock goes with its process."""
    import contextlib
    import fcntl
    import os
    import time

    @contextlib.contextmanager
    def held():
        d = hubconf.STATE / "services"
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            end = time.monotonic() + (lock_wait() if wait is None else wait)
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise Locked(_("{name} is busy (another add, update, remove or run holds it)", name=name))
                    time.sleep(0.5)
            yield
        finally:
            os.close(fd)
    return held()


def entries() -> dict[str, dict]:
    """Every installed service's valid entry, by name. An invalid one is left out (check() says why)."""
    out = {}
    d = hubconf.services_dir()
    if not d.is_dir():
        return out
    import yaml
    for f in sorted(d.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not entry_problems(doc, f.stem):
            out[doc["name"]] = doc
    return out


def check() -> list[str]:
    """Problems of every entry file, for `brunna config check`."""
    import yaml
    out = []
    d = hubconf.services_dir()
    for f in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            doc = yaml.safe_load(f.read_text())
        except (OSError, yaml.YAMLError) as e:
            out.append(f"services/{f.name}: {e}")
            continue
        out += [f"services/{f.name}: {e}" for e in entry_problems(doc, f.stem)]
    return out


def names_taken() -> set[str]:
    """Every installed service's name, folded."""
    d = hubconf.services_dir()
    return {fold(f.stem) for f in d.glob("*.yaml")} if d.is_dir() else set()


def clash(name: str, projects: dict | None = None) -> str | None:
    """Why `name` can't be an installed service's name here, or None. Compared folded."""
    f = fold(name)
    projects = hubconf.projects() if projects is None else projects
    taken: dict[str, str] = {}
    for p in projects:
        taken[fold(p)] = _("a registered project")
        for old in hubconf.former_names(p):
            taken.setdefault(fold(old), _("a registered project's former name"))
    for n in names.all("project"):
        taken.setdefault(fold(n), _("the platform's name"))
    for m in (hubconf.ROOT / "modules").glob("*.yaml"):
        taken.setdefault(fold(m.stem), _("a module's name"))
    for a in hubconf.owner_addresses() | {hubconf.party("principal_side")}:
        taken.setdefault(fold(a), _("an address of the owner or the assistant"))
    if f in taken:
        return taken[f]
    if IDEA.match(f):
        return _("the name of an idea sandbox")
    if f in names_taken():
        return _("an installed service")
    base = root()
    if base.is_dir() and any(fold(c.name) == f for c in base.iterdir()):
        return _("a folder of that name is already in the services folder")
    return None
