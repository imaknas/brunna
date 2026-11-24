#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""Stage two: each project's sandbox + permission rules, generated from the project registry and written into the
coding agent's local settings file in the project (runtime_claude.settings_path; a managed file).

What to write: policy.py computes the neutral policy, runtime_claude.settings() translates it into Claude Code's
format; this script only writes the file, records, and detects drift.

Written only for projects registered with `enforce: {sandbox: true}`; for other projects it only computes what it
would write (--report). Not applied to the platform itself (brunna).
What is written:
  sandbox (the whole key is managed by the platform): enabled, failIfUnavailable, allowUnsandboxedCommands: false,
    filesystem.allowWrite (its own runtime, the bus directory, the registry's allow_write, and its push request
    folder, made here first: docs/design/platform-push.md),
    filesystem.denyRead (other projects' workspace/runtime, raw memory notes, the platform's secrets, credentials,
    the hub's platform state: what its services are denied, docs/design/capabilities.md 11-08),
    network.allowedDomains (platform defaults + the registry's network)
  env: the variables the platform sets (its own tool caches, policy.py env); other variables are untouched
  the entries the platform adds to permissions.deny / permissions.allow (other entries are untouched -- an
  interactive session's "always allow" is written there too):
    deny  Read/Edit other projects' workspace/runtime, Edit the platform/bare repos/memory copies/the instance/what
          runs as the user (policy deny_write), Read/Edit the raw notes in memory copies, the platform's secrets, its
          platform state and credentials, Edit the runtime's user-level control files, the Keychain command
    allow the mailbox CLI, the merge gate (the only channels across the boundary)
  Which entries were written last time is recorded in .state/rendered-settings/<project>.json; next time those are
  removed first and then added again, so nothing piles up.

What isn't the platform's in such a file stays only when it is of a kind the runtime takes whatever its value
(runtime_claude.carried: Claude Code loads nothing of a file with one value it doesn't take, the sandbox included).
Everything else (its own `sandbox` key and hooks, other settings, an env variable or a rule the platform also sets, a
file that can't be read, a link) is kept first and said to the owner: the file as it was before the platform's first
write in RENDERED/saved/<project>/before.json, with the workspace it is of, and any other content about to be lost in a
file there named by its content (a worktree's: in worktrees/), the KEEP_MAX newest. Never in the project's folder (a
sandboxed project could write what is put back later, and the tool reads settings there), never over an earlier copy.
Turning the sandbox off puts back what was there before (unapply). A file that can't be read is told to the owner until
its copy (*.invalid.json) is deleted.

Each project is on its own: whatever goes wrong with one is said (exit 1) and the next goes on.

The same file goes into each repo in the project's folder (look, write_nested), since a session opened in one reads that
folder's files only. What the project's own files add is told, once (tell_once), never taken away. A settings file that
changes under a live session is taken in only while it is the platform's (config_changed). All three, and what is and
isn't claimed of them: docs/design/capabilities.md, "A project's own settings files".

Drift detection: the actual file differs from what it should be (a session changed its own settings, or someone
edited it by hand) -> print it, exit 1 (platform job failed -> status page), and write it back right away.

Usage: render-project-settings.py [--report] [--only <project>]
       render-project-settings.py --worktree <folder>   (worktree-memory.sh: a sandboxed project's worktree at a session's
                                                         start; session_worktree.make renders one it makes through
                                                         render_worktree before the tool gets its path)
       render-project-settings.py --renamed <old> <new> <workspace>   (rename-project.sh: what it keeps of a project)
Also an applier (appliers.py): plan()/apply(), used by `bin/hub config plan/apply`.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import names  # noqa: E402
import i18n  # noqa: E402
import nofollow  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402
import runtime_claude  # noqa: E402

_ = i18n.domain(__file__)

RENDERED = hubconf.STATE / "rendered-settings"


def desired(name: str, doc: dict, index: dict) -> tuple[dict, list[str], list[str]]:
    """(sandbox, deny rules, allow rules): the neutral policy (policy.py) translated into Claude Code settings
    (runtime_claude.py), for its sessions: the only place policy session_write (the push request folder) is rendered."""
    return runtime_claude.session_settings(policy.policy(name, doc, index))


def desired_env(name: str, doc: dict, index: dict) -> dict[str, str]:
    """The environment its sessions get (its own tool caches)."""
    return runtime_claude.session_env(policy.policy(name, doc, index))


def settings_path(doc: dict) -> pathlib.Path | None:
    ws = registry.dirs(doc)["workspace"]
    return runtime_claude.settings_path(ws) if ws else None


def shown(x) -> str:
    """x as a line anyone can print or send (a name a project gave a file needn't be text)."""
    return str(x).encode("utf-8", "backslashreplace").decode()


def line(x, n: int = 300) -> str:
    """x within one line of a message, at most n characters of it (a name or a value a project chose)."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+", " ", shown(x))[:n]


def key(x) -> str:
    return json.dumps(x, sort_keys=True)


def same(a, b) -> bool:
    """Equal as JSON (in Python 1 == True)."""
    return key(a) == key(b)


UNREACHED = (OSError, ValueError, RuntimeError)   # settings_rel's too: the folder is somewhere else now, or a loop of links


def raw_settings(doc: dict, folder: pathlib.Path | None = None) -> bytes | None:
    try:
        return nofollow.read(registry.dirs(doc)["workspace"], settings_rel(doc, folder))
    except UNREACHED:
        return None


def read_settings(doc: dict, folder: pathlib.Path | None = None) -> tuple[dict, str | None, bytes | None]:
    """(content, why it couldn't be read, its bytes), of the workspace's file or of another session folder's. The file is in the
    project's folder, where a sandboxed project's job can put a link (e.g. to the user-level settings) or a FIFO: it is
    read without following the one or waiting on the other (nofollow.py); what isn't a plain file counts as empty and is
    replaced by the next write."""
    try:
        raw = nofollow.read(registry.dirs(doc)["workspace"], settings_rel(doc, folder))
    except UNREACHED as e:
        return {}, line(e), None
    try:
        return (parse(raw) if raw else {}), None, raw
    except ValueError as e:
        return {}, line(e), raw


MAX_DEPTH = 100   # no settings are nested like that; what reads, compares and writes them recurses


def parse(raw: bytes) -> dict:
    """The settings in raw; ValueError when they aren't a JSON object in UTF-8 (json reads UTF-16 and UTF-32 too, the tool
    doesn't) that the platform could write back."""
    try:
        got = json.loads(raw.decode("utf-8-sig"))
    except RecursionError:
        raise ValueError("nested too deeply") from None
    if not isinstance(got, dict):
        raise ValueError("not a JSON object")
    level, depth = [got], 0
    while level := [v for x in level for v in (x.values() if isinstance(x, dict) else x) if isinstance(v, (dict, list))]:
        if (depth := depth + 1) > MAX_DEPTH:
            raise ValueError("nested too deeply")
    json.dumps(got, ensure_ascii=False, allow_nan=False).encode()   # NaN, 1e999, a lone surrogate: each a ValueError
    return got


def part(d: dict, key: str) -> dict:
    return d[key] if isinstance(d.get(key), dict) else {}


def rules(d: dict, key: str) -> list[str]:
    """permissions.<key> of these settings: the entries that are text."""
    got = part(d, "permissions").get(key)
    return [r for r in got if isinstance(r, str)] if isinstance(got, list) else []


def write_settings(doc: dict, content: dict, folder: pathlib.Path | None = None) -> None:
    """Replaces the file (a link or a FIFO there is replaced, never written through), in the workspace or in another of
    its session folders (a worktree). Always walked down from the workspace, link by link refused: a session folder is the
    project's to swap for a link into another project, whenever it was last looked at. One that resolves elsewhere raises."""
    put(registry.dirs(doc)["workspace"], settings_rel(doc, folder), as_file(content))


def as_file(content: dict) -> bytes:
    return (json.dumps(content, indent=2, ensure_ascii=False) + "\n").encode()


def put(ws: pathlib.Path, rel: pathlib.Path, data: bytes) -> None:
    """data as this file, keeping a file there from being read by more people than now (never written by them)."""
    st = nofollow.lstat(ws, rel)
    nofollow.replace(ws, rel, data, 0o600 | (st.st_mode & 0o044) if st and stat.S_ISREG(st.st_mode) else None)


def settings_rel(doc: dict, folder: pathlib.Path | None = None) -> pathlib.Path:
    """The settings file of the workspace, or of another of its session folders, relative to the workspace."""
    ws = registry.dirs(doc)["workspace"]
    rel = settings_path(doc).relative_to(ws)
    return rel if folder is None else pathlib.Path(folder).resolve().relative_to(ws.resolve()) / rel


def saved_json(f: pathlib.Path, doc: dict | None = None) -> dict:
    """One of the platform's own files under RENDERED; {} when it is missing, can't be read, or is another workspace's (a
    project's name used again, or given to another)."""
    try:
        got = json.loads(f.read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    ws = str(registry.dirs(doc)["workspace"]) if doc else None
    return got if isinstance(got, dict) and (doc is None or got.get("workspace", ws) == ws) else {}


def another_workspaces(f: pathlib.Path, doc: dict) -> bool:
    return bool(saved_json(f)) and not saved_json(f, doc)


def known(name: str, doc: dict) -> dict:
    """What the last look through the project's folder found (look): {"nested": its repos, "settings": its plain folders
    with settings files, "others": the folders whose .git isn't a real repo's, "leaving": the ones that were
    repos and still hold the platform's file, "told": what the owner has been told of, "repos": the repos among it, for good}."""
    got = saved_json(known_dir() / f"{name}.json", doc)
    texts = lambda x: [v for v in x if isinstance(v, str)] if isinstance(x, list) else []   # noqa: E731
    return {k: texts(got.get(k)) for k in ("nested", "settings", "others", "leaving", "told", "repos")}


def known_dir() -> pathlib.Path:
    return RENDERED / "known"


def remember(name: str, doc: dict, **what) -> None:
    f = known_dir() / f"{name}.json"
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    kept = saved_json(f, doc)   # with the record, where the sandbox is going off (unapply_nested)
    hubconf.write_atomic(f, json.dumps({**kept, **known(name, doc), **what, "workspace": str(registry.dirs(doc)["workspace"])},
                                       indent=1), mode=0o600)   # ASCII: a folder's name needn't be text


def reach(doc: dict) -> dict:
    """What the project's sessions may write and reach, for what a settings file would add (runtime_claude.carried,
    foreign_controls): from its registry entry alone, so the same whether its sandbox is on or going off."""
    d = registry.dirs(doc)
    return {"workspace": d["workspace"], "network": policy.DOMAINS + list(doc.get("network") or []),
            "write": [*([d["runtime"]] if d["runtime"] else []), *(doc.get("allow_write") or []), *policy.session_write(doc.get("name") or "", doc)]}


def last_record(name: str, doc: dict) -> dict:
    """What the platform wrote last time (with no workspace in it: before it carried one)."""
    got = saved_json(RENDERED / f"{name}.json", doc)
    texts = lambda x: isinstance(x, list) and all(isinstance(v, str) for v in x)   # noqa: E731
    return got if texts(got.get("deny", [])) and texts(got.get("allow", [])) and isinstance(got.get("env", {}), dict) else {}


# ---------- whose a setting is ----------
# One rule for every file of a project's (its workspace's, its worktrees'): what isn't the platform's (users) and isn't
# of a kind the runtime takes whatever its value (runtime_claude.carried) isn't in the settings while the sandbox is on.
# It is kept first, said to the owner, and what was there before the sandbox comes back when it goes off.

NOTHING = object()


def minus(a, b):
    """What a has that b doesn't (NOTHING when that is nothing): in objects and lists, entry by entry."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = {k: r for k, v in a.items() if (r := minus(v, b[k]) if k in b else v) is not NOTHING}
        return out or NOTHING
    if isinstance(a, list) and isinstance(b, list):
        have = {key(x) for x in b}
        return [x for x in a if key(x) not in have] or NOTHING
    return NOTHING if same(a, b) else a


def plus(a, b):
    """a with what b has (minus's inverse)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return {**a, **{k: plus(a[k], v) if k in a else v for k, v in b.items()}}
    if isinstance(a, list) and isinstance(b, list):
        have = {key(x) for x in a}
        return a + [x for x in b if key(x) not in have]
    return b


def users(cur: dict, prev: dict, was: dict) -> dict:
    """cur without what the platform wrote last time (prev, its record), and with what was there before the sandbox (was)
    that its writing took the place of."""
    fixed = runtime_claude.SESSION_FIXED
    out = {k: v for k, v in cur.items() if k not in ("sandbox", "hooks") and not (k in fixed and same(fixed[k], v))}
    if (hooks := runtime_claude.own_hooks(cur)):
        out["hooks"] = hooks
    for kind in ("deny", "allow"):
        mine = set(prev.get(kind, []))
        back = [r for r in rules(was, kind) if r in mine]
        if kind in part(cur, "permissions") and isinstance(cur["permissions"][kind], list):
            out["permissions"] = {**out["permissions"], kind: [r for r in cur["permissions"][kind] if not (isinstance(r, str) and r in mine)]}
        if back:
            out["permissions"] = plus(part(out, "permissions"), {kind: back})
    mine = prev.get("env") or {}
    env = {**{k: v for k, v in part(cur, "env").items() if k not in mine}, **{k: v for k, v in part(was, "env").items() if k in mine}}
    if env or "env" in out and isinstance(out["env"], dict):
        out["env"] = env
    return {k: v for k, v in out.items() if k != "env" or v != {}}


def going(cur: dict, new: dict, prev: dict, record: dict) -> dict:
    """What of cur isn't the platform's and isn't in new, which is about to replace it."""
    mine = users(cur, prev, {})
    if "sandbox" in cur and not same(cur["sandbox"], prev.get("sandbox")) and not same(cur["sandbox"], record["sandbox"]):
        mine["sandbox"] = cur["sandbox"]
    changed = {k: v for k, v in part(cur, "env").items() if k in (prev.get("env") or {}) and not same(v, prev["env"][k])}
    if changed:
        mine["env"] = {**part(mine, "env"), **changed}
    rest = minus(mine, new)
    return {} if rest is NOTHING else rest


def named(rest: dict) -> str:
    """The settings in rest, by name."""
    out = []
    for k, v in rest.items():
        out += [f"{k}.{i}" for i in v] if k in ("permissions", "env", "sandbox", "hooks") and isinstance(v, dict) else [k]
    return line(", ".join(line(x, 60) for x in out[:20]))


# ---------- what is kept ----------

READ_CAP = 1 << 20   # nofollow.read's, with which the platform reads settings
RENDER_CAP = READ_CAP - (1 << 16)   # what it writes can be read back, a worktree's own entries included
KEEP_CAP = 4 << 20   # a file bigger than this gets a second name in its folder instead of a copy
KEEP_MAX = 20        # of one folder's copies, the newest stay
WORKTREES_MAX = 50   # worktrees whose copies are kept; those that are gone make room first
LASTING = (".invalid.json", ".kept.json")   # what the owner hasn't dealt with, and what nothing puts back: these stay too
BEFORE, UNTOLD = "before.json", "untold.json"


def saved_dir(name: str) -> pathlib.Path:
    return RENDERED / "saved" / name


def before(name: str, doc: dict) -> tuple[dict, dict]:
    """What was there before the sandbox: (its settings, the note of it: {"workspace", "raw" (base64) or "link"});
    ({}, {}) when there was nothing."""
    note = saved_json(saved_dir(name) / BEFORE, doc)
    try:
        raw = base64.b64decode(note.get("raw") or "")
        return (parse(raw) if raw else {}), note
    except (ValueError, TypeError):   # it couldn't be read: `raw` is put back as it was
        return {}, note


def retire_before(name: str) -> None:
    """The note of what was there before (put back, another workspace's, or of a run that stopped halfway) under another
    name: no longer what comes back."""
    f = saved_dir(name) / BEFORE
    if f.exists():
        os.rename(f, f.with_name(f"before-{secrets.token_hex(6)}.json"))


def keep_before(name: str, doc: dict, alone: bool) -> None:
    """The workspace's file as it is, which the platform hasn't written: what comes back when the sandbox goes off (alone:
    all of it, nothing of it is carried meanwhile)."""
    f, ws, rel = saved_dir(name) / BEFORE, registry.dirs(doc)["workspace"], settings_rel(doc)
    retire_before(name)
    if (st := nofollow.lstat(ws, rel)) is None:
        return
    note = {"workspace": str(ws), **({"alone": True} if alone else {})}
    if stat.S_ISLNK(st.st_mode):
        note["link"] = nofollow.readlink(ws, rel)
    elif stat.S_ISREG(st.st_mode):
        note["raw"] = base64.b64encode(nofollow.read(ws, rel, KEEP_CAP) or b"").decode()
    else:
        return
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    hubconf.write_atomic(f, json.dumps(note), mode=0o600)   # ASCII: a link's target needn't be text


def keep_copy(name: str, doc: dict, kind: str = ".json", folder: pathlib.Path | None = None) -> str:
    """The settings file as it is now, for the owner to read: where the copy is. It never stops the sandbox from being
    written: a copy that can't be made (the disk with the platform's state is full, the file can't be read) gives the
    file itself a second name in its folder, which takes no space and which the tool doesn't read."""
    ws, rel = registry.dirs(doc)["workspace"], settings_rel(doc, folder)
    try:
        raw = nofollow.read(ws, rel, KEEP_CAP) or b""
        tree = folder is not None and folder.parent in (ws / runtime_claude.WORKTREES, ws.resolve() / runtime_claude.WORKTREES)
        unique = hashlib.sha256(os.fsencode(rel)).hexdigest()[:16]   # a repo's: by its path, whatever others are called
        d = saved_dir(name) if folder is None else saved_dir(name) / "worktrees" / folder.name if tree else saved_dir(name) / "repos" / unique   # a limit each
        if folder is not None and not d.exists() and d.parent.is_dir() and len(kept := list(d.parent.iterdir())) >= WORKTREES_MAX:
            there = {hashlib.sha256(os.fsencode(settings_rel(doc, ws / x))).hexdigest()[:16] for x in known(name, doc)["nested"] if nofollow_dir(ws, x)}
            gone = sorted((x for x in kept if (not os.path.lexists(folder.parent / x.name) if tree else x.name not in there)), key=lambda x: x.stat().st_mtime)
            shutil.rmtree(gone[0])   # none whose worktree is gone: the file itself stays beside, under another name
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = d / (hashlib.sha256(raw).hexdigest()[:16] + kind)
        others = sorted((f for f in d.iterdir() if f.is_file() and f.name not in (dest.name, BEFORE, UNTOLD)), key=lambda f: f.stat().st_mtime)
        waiting = [f for f in others if f.name.endswith(LASTING) and folder is None]
        if kind != ".json" and folder is None and len(waiting) >= KEEP_MAX and not dest.exists():
            raise OSError(f"{len(waiting)} copies wait for the owner")
        if dest.exists():
            os.utime(dest)
        else:
            hubconf.write_atomic(dest, raw, mode=0o600 | (nofollow.lstat(ws, rel).st_mode & 0o777))
        passing = [f for f in others if f not in waiting]
        for f in passing[:max(0, len(passing) + (kind == ".json" or folder is not None) - KEEP_MAX)]:
            f.unlink()
        return shown(dest)
    except Exception as e:   # noqa: BLE001
        try:
            return shown(ws / rel.with_name(nofollow.link(ws, rel, f"{rel.name}.kept")))
        except Exception as e2:   # noqa: BLE001
            return shown(_("nowhere: no copy could be made ({why})", why=f"{e}; {e2}"))


def note(name: str, text: str) -> None:
    """Something the owner hears of once (tell): tried again until it has gone."""
    f = saved_dir(name) / UNTOLD
    try:
        texts = [t for t in saved_json(f).get("texts", []) if isinstance(t, str)]
        if text not in texts:
            f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            hubconf.write_atomic(f, json.dumps({"texts": [*texts, text][-KEEP_MAX:]}), mode=0o600)
    except Exception as e:   # noqa: BLE001 (it is in the job's output as well)
        print(shown(_("{project}: the owner could not be told about its settings file ({why})", project=name, why=e)), file=sys.stderr)


def tell(name: str, unreadable: str | None = None, off: bool = False) -> None:
    """The owner hears of what note() holds, once; and of a settings file that couldn't be read, with where its content
    is, until they have put back what should stay and deleted the copy. unreadable: why one that is still there can't be
    read (unapply leaves it)."""
    import alert
    ok, f = _("{project}: its agent settings file is in order", project=name), saved_dir(name) / UNTOLD
    problems = [_("{project}: its agent settings file couldn't be read (e.g. it wasn't valid JSON), so the sandbox settings "
                  "replaced it. What was in it is in {file}: put back what should stay, then delete that file", project=name, file=c)
                for c in sorted(saved_dir(name).glob("*.invalid.json"))]
    ids = [c.name for c in sorted(saved_dir(name).glob("*.invalid.json"))]
    if unreadable:
        problems.append(_("{project}: its sandbox is off, but its agent settings file can't be read ({why}), so the sandbox settings "
                          "the platform wrote are still in it: put the file right", project=name, why=unreadable))
        ids.append("unreadable")
    try:
        key = f"project-settings:{name}"
        if problems or alert.open_keys(key):
            alert.once(key, problems, ok, ids=ids)
        key, texts = f"project-settings-kept:{name}", saved_json(f).get("texts", [])
        if texts and alert.once(key, texts, ok, ids=[hashlib.sha256(shown(t).encode()).hexdigest()[:12] for t in texts]):
            f.unlink()
        if off and not texts and alert.open_keys(key):
            alert.once(key, [], ok, ids=[])
    except Exception as e:   # noqa: BLE001 (the sandbox is written; the next run tries again)
        print(shown(_("{project}: the owner could not be told about its settings file ({why})", project=name, why=e)), file=sys.stderr)


def keep_what_goes(name: str, doc: dict, cur: dict, new: dict, prev: dict, record: dict, unreadable: str | None,
                   folder: pathlib.Path | None = None) -> str | None:
    """Before new replaces the settings file (the workspace's, or a worktree's): what of it isn't the platform's and isn't
    in new is kept and noted for the owner. What is said of it, or None."""
    ws, rel = registry.dirs(doc)["workspace"], settings_rel(doc, folder)
    st = nofollow.lstat(ws, rel)
    # the first write here: not one the platform rendered whose record is gone, which is never the user's to get back
    rec = RENDERED / f"{name}.json"
    upgraded = folder is None and from_before(rec)
    first = folder is None and (not rec.exists() or another_workspaces(rec, doc)) and not runtime_claude.platform_rendered(cur) \
        and not (unreadable and before(name, doc)[1])   # a lost record: what was there before is known
    if first:
        try:
            keep_before(name, doc, bool(record.get("alone") or unreadable))
        except Exception as e:   # noqa: BLE001 (a copy is still made below)
            print(shown(_("{project}: what comes back when its sandbox goes off could not be kept ({why})", project=name, why=e)), file=sys.stderr)
    if st is None:
        return None
    if stat.S_ISLNK(st.st_mode):
        if folder is not None:   # to the main checkout's file, as worktrees' used to be
            return None
        target = line(nofollow.readlink(ws, rel), 1024)
        text = _("{project}: its agent settings file was a link to {target}; while the sandbox is on, a file of its own is in its place "
                 "(the target isn't changed, nor read). Turning the sandbox off puts the link back", project=name, target=target) if first \
            else _("{project}: its agent settings file was turned into a link to {target} while the sandbox was on; the sandbox settings "
                   "replaced it (the target isn't changed)", project=name, target=target)
        note(name, text)
        return text
    rest = going(cur, new, prev, record)
    if not stat.S_ISREG(st.st_mode) or not (unreadable or rest):
        return None
    where = keep_copy(name, doc, ".invalid.json" if unreadable else ".kept.json" if upgraded else ".json", folder)
    if unreadable and folder is None:   # tell() says it until it is dealt with
        return _("what was there is kept in {file}", file=where)
    what = _("everything in it ({why})", why=unreadable) if unreadable else named(rest)
    if folder is not None and folder.parent not in (ws / runtime_claude.WORKTREES, ws.resolve() / runtime_claude.WORKTREES):
        text = _("{project}: the sandbox settings of its repo {folder} replaced {what}; kept in {file}", project=name,
                 folder=line(rel.parent.parent, 200), what=what, file=where)
    elif folder is not None:
        text = _("{project}: the sandbox settings of its worktree {worktree} replaced {what}; kept in {file}", project=name,
                 worktree=line(folder.name, 100), what=what, file=where)
    elif first:
        text = _("{project}: the sandbox settings don't have {what}, which its agent settings file had; kept in {file}, and back in it "
                 "when the sandbox is turned off", project=name, what=what, file=where)
    else:
        text = replaced(name, what, where, upgraded)
    note(name, text)
    return text


def from_before(rec: pathlib.Path) -> bool:
    """A record from before it carried its workspace: what stood next to the sandbox was carried over, whatever it was, and
    nothing says since when."""
    return bool(got := saved_json(rec)) and "workspace" not in got


def replaced(name: str, what: str, where: str, upgraded: bool) -> str:
    return _("{project}: the sandbox settings no longer have {what}, which its agent settings file had next to them; they don't come "
             "back by themselves when the sandbox is turned off: {file} has them", project=name, what=what, file=where) if upgraded else \
        _("{project}: the sandbox settings replaced {what}, put in its agent settings file while the sandbox was on; kept in "
          "{file}", project=name, what=what, file=where)


def compose(name: str, doc: dict, index: dict) -> tuple[pathlib.Path, dict, dict, dict, dict, str | None, bytes | None] | None:
    """Pure computation, no writes: (settings file, current content, intended content, the record of what the platform
    wrote last time, the record to keep this time, why the file can't be read or None, its bytes). None when there is no
    workspace."""
    path = settings_path(doc)
    if not path:
        return None
    sandbox, deny, allow = desired(name, doc, index)
    cur, unreadable, raw = read_settings(doc)
    prev, was = last_record(name, doc), before(name, doc)[0]
    env = desired_env(name, doc, index)
    # the platform's hooks only: a sandboxed project's own processes can't write this file (policy protect), and a
    # hook found here that the platform didn't write would run outside its sandbox: it is removed
    fixed = {"hooks": runtime_claude.platform_hooks(), **runtime_claude.session_fixed()}

    def rendered(mine: dict) -> dict:
        new = {k: v for k, v in mine.items() if k not in ("permissions", "env")}
        # one order whatever the file had: the user's, then the platform's (the same on the next run, so it is the last)
        perms = {kind: [r for r in rules(mine, kind) if r not in own] + list(own) for kind, own in
                 (("deny", dict.fromkeys(deny)), ("allow", dict.fromkeys(allow)))}
        new |= {"sandbox": sandbox, "permissions": {**perms, **{k: v for k, v in part(mine, "permissions").items() if k not in perms}}, **fixed}
        if (e2 := {**{k: v for k, v in part(mine, "env").items() if k not in env}, **env}):
            new["env"] = e2
        return new
    # what the user had made it too big to be read back, or couldn't be read: it all comes back at off instead
    alone = bool(prev.get("alone"))
    # a file that can't be read: what was there before the sandbox counts instead
    new = rendered(runtime_claude.carried(users(was, {}, {}) if unreadable else users(cur, prev, was), reach(doc)))
    record = {"sandbox": sandbox, "deny": deny, "allow": allow, "env": env, "hooks": fixed["hooks"],
              "fixed": {k: v for k, v in fixed.items() if k != "hooks"},
              "workspace": str(registry.dirs(doc)["workspace"])}
    if len(as_file(new)) > RENDER_CAP:
        new, alone = rendered({}), True
    if alone:
        record["alone"] = True
    return path, cur, new, prev, record, unreadable, raw


def foreign_controls(ws, project: str | None = None, reach: dict | None = None, folders=(), bare=(), rendered=None) -> list[str]:
    """What in the folder any protected runtime's sessions would load that the platform didn't put there (each adapter's
    foreign_controls; runtime_ports.protected_runtimes, whether installed or not: one planted before the protection came
    is found too; docs/design/runtime-codex.md, Step 4 r2, review S5). The skills `brunna skills sync` installed are the
    platform's while each is exactly as installed (skills_sync.owned; docs/design/skills-sync.md). reach, folders, bare,
    rendered: what its sessions may reach, the other folders in it they may be opened in, for an adapter that takes them."""
    import inspect
    import runtime_ports
    import skills_sync
    owned = skills_sync.owned(project, ws) if project else None
    out = []
    for name in runtime_ports.protected_runtimes():
        try:
            rt = runtime_ports.load(name, ports=("control",))
        except runtime_ports.Unsupported:
            continue
        more = {"reach": reach, "folders": folders, "bare": bare, "rendered": rendered} if "reach" in inspect.signature(rt.foreign_controls).parameters else {}
        out += [w for w in rt.foreign_controls(ws, owned, **more) if w not in out]
    return out


def write_project(name: str, doc: dict, index: dict, report: bool) -> list[str]:
    """Returns the drift (where the actual file differs from what it should be). report=True only computes, no
    writes."""
    ws, unseen, bare, hidden = registry.dirs(doc)["workspace"], [], [], []
    if not report:
        push_folder(name)
        unseen, bare, hidden = look(name, doc)
    for _try in range(TRIES):
        c = compose(name, doc, index)
        if not c:
            return []
        path, cur, new, prev, record, unreadable, raw = c
        sandbox, deny, allow = record["sandbox"], record["deny"], record["allow"]
        drift = [_("the settings file can't be read ({why}); it is replaced", why=unreadable)] if unreadable else []
        if prev:  # drift only makes sense once it has been written
            if not same(cur.get("sandbox"), prev.get("sandbox")):
                drift.append(_("sandbox was changed"))
            for kind in ("deny", "allow"):
                missing = set(prev.get(kind, [])) - set(rules(cur, kind))
                if missing:
                    drift.append(_("permissions.{key} is missing {n} {n:rule|rules} (e.g. {example})", key=kind, n=len(missing),
                                   example=sorted(missing)[0]))
            if any(not same(part(cur, "env").get(k), v) for k, v in (prev.get("env") or {}).items()):
                drift.append(_("env was changed"))
        # what the platform wrote last time (prev) is not drift either: a platform update that changes its own hooks
        if cur.get("hooks") and not same(cur["hooks"], record["hooks"]) and not same(cur["hooks"], prev.get("hooks")):
            drift.append(_("hooks the platform didn't write were removed"))
        if report:
            drift += foreign_drift(foreign(ws, name, doc))
            print(_("{project}: would write sandbox (allowWrite {write}, denyRead {read}, {domains} {domains:domain|domains}), "
                    "{deny} deny {deny:rule|rules}, {allow} allow {allow:rule|rules} → {path}", project=name,
                    write=len(sandbox["filesystem"]["allowWrite"]), read=len(sandbox["filesystem"]["denyRead"]),
                    domains=len(sandbox["network"]["allowedDomains"]), deny=len(deny), allow=len(allow), path=path))
            return [shown(d) for d in drift]
        if raw == as_file(new):
            break
        drift += kept_first(name, doc, cur, new, prev, record, unreadable)
        if raw_settings(doc) == raw:   # else the tool has written it meanwhile (a rule it was told to remember): again
            break
    written = True
    renders(name, doc, record)
    coming(name, record)
    if raw != as_file(new):
        try:
            write_settings(doc, new)
        except (OSError, ValueError) as e:   # e.g. the folder it is in is itself a link: nothing written through it
            drift.append(_("the settings file could not be written ({why})", why=e))
            written = False
    for folder in runtime_claude.session_folders(ws)[1:]:   # its worktrees get the same settings
        # plus its own .agent-memory, which a worktree reaches through a link
        drift += write_worktree(name, doc, folder, runtime_claude.worktree_memory(new, ws), prev, record)
    for rel in known(name, doc)["nested"]:   # and the repos in its folder: a session opened in one reads its files only
        drift += write_nested(name, doc, rel, new, prev, record)
    leave(name, doc, prev or record, runtime_claude.carried(users(new, record, {}), reach(doc)))
    if written:   # the record is what is in the file: without it the next run takes the file for the user's, as it is
        hubconf.write_atomic(RENDERED / f"{name}.json", json.dumps(record, ensure_ascii=False, indent=1))
        coming(name, None)
    theirs = foreign(ws, name, doc)
    new = tell_once(name, doc, theirs, unseen, bare, hidden)
    tell(name)
    drift += foreign_drift(theirs) + [x for x in hidden if x in new]   # a folder that is too big is, every time: told, then no failure
    return [shown(d) for d in drift]


# ---------- the folders of a project its sessions may be opened in ----------
# Its worktrees are where the tool makes them (session_folders). A repo anywhere else in its folder (a clone, a submodule,
# a worktree of either: anything with a .git in it, an empty one too) has settings of its own: a session opened there
# loads none of the workspace's. The platform writes the same file into a real repo (a .git folder with a HEAD); into one
# whose .git is a file, or a folder with none, it writes nothing (the file would keep a session from removing what it
# otherwise can), and the owner is told of it. Found by a walk of the project's own (as it,
# in its sandbox, following no link), so what it says is data: names to look at again, never to trust.

WALK_FOLDERS, WALK_SECONDS, NESTED_MAX, WALK_MOST = 100_000, 30, 200, 5000   # (WALK_MOST: of each kind, what the walk lists)
REPOS_TOLD = 2000   # of the repos told, the latest are known


def look(name: str, doc: dict) -> tuple[list[str], list[str], list[str]]:
    """The walk, its result known for what is rendered from it. (repos, and other folders with a .git, not told of before,
    why not everything could be looked at or covered: what is known to be a real repo stays, whatever the walk says)."""
    import project_git
    ws, was, why = registry.dirs(doc)["workspace"], known(name, doc), []
    try:
        got = project_git.data(name, ws, "session-folders", ws, str(WALK_FOLDERS), str(WALK_SECONDS), str(WALK_MOST),
                               *runtime_claude.SETTINGS_FILES, timeout=WALK_SECONDS * 2)
        if not isinstance(got, dict):
            raise ValueError("its answer isn't what was asked")
    except Exception as e:   # noqa: BLE001 (no sandbox here, a job that died: the folders known so far)
        got = {}
        why.append(line(f"{type(e).__name__}: {e}", 200))
    if isinstance(got.get("over"), str):
        why.append(line(got["over"], 200))
    trees, odd = {f.relative_to(ws).as_posix() for f in runtime_claude.session_folders(ws)}, []

    def rels(key: str) -> list[str]:
        """Folders in the workspace, whatever characters their names have; what can't be one is said."""
        out = []
        for x in project_git.strs(got.get(key)):
            p = pathlib.PurePosixPath(x)
            try:
                fits = 0 < len(os.fsencode(x)) <= 4000
            except UnicodeError:   # a lone surrogate that stands for no byte
                fits = False
            if not fits or p.is_absolute() or ".." in p.parts or "\0" in x or p.as_posix() != x:
                odd.append(x)
            elif x not in trees and p.parts[0] != ".agent-memory":
                out.append(x)
        return out
    found, settings, others = rels("repos"), rels("settings"), rels("others")
    if odd:
        why.append(_("a folder it can't name: {folder}", folder=line(odd[0], 100)))
    real = [x for x in dict.fromkeys([*was["nested"], *was["leaving"]]) if x in found or real_repo(ws, x) is not False]   # until it is confirmed that it is none
    nested = real + sorted(set(found) - set(real))[:max(0, NESTED_MAX - len(real))]
    uncovered = len(set(found) | set(real)) - len(nested)
    if why:   # not all of it was looked at
        settings += [x for x in was["settings"] if x not in settings]
        others += [x for x in was["others"] if x not in others and x not in nested]
    leaving = sorted({*was["leaving"], *was["nested"]} - set(nested))   # no real repo any more, or not there (leave)
    others, marked = sorted(others)[:NESTED_MAX], set(was["repos"])   # one that comes and goes is told once
    new, bare = [x for x in nested if x not in marked], [x for x in others if f"{x}/.git" not in marked]
    remember(name, doc, nested=nested, settings=sorted(settings)[:NESTED_MAX], others=others, leaving=leaving, off=None,
             repos=[*was["repos"], *new, *(f"{x}/.git" for x in bare)][-REPOS_TOLD:])
    hidden = [with_key(_("not all of its folder could be looked through for repos, where a session would have no sandbox settings ({why})", why=x),
                       "not looked through: " + re.sub(r"\d+", "#", x)) for x in why]
    if uncovered:
        hidden.append(with_key(_("{n} {n:repo|repos} in its folder {n:has|have} no sandbox settings, as more than {most} aren't covered: a session "
                                 "opened there has none", n=uncovered, most=NESTED_MAX), "not covered"))
    return new, bare, hidden


def real_repo(ws, rel: str) -> bool | None:
    """Whether there is a real repo (a .git folder with a HEAD) here; None when it can't be told."""
    at = pathlib.PurePosixPath(rel)
    try:
        if nofollow.lstat(ws, at) is None:
            return False
        dot = nofollow.lstat(ws, at / ".git")
        return dot is not None and stat.S_ISDIR(dot.st_mode) and nofollow.lstat(ws, at / ".git" / "HEAD") is not None
    except NotADirectoryError:   # it, or a folder above, is a file or a link now
        return False
    except OSError:
        return None


def nofollow_dir(ws, rel) -> bool:
    try:
        return nofollow.is_dir(ws, pathlib.PurePosixPath(rel))
    except OSError:
        return False


def nested_before(name: str, rel: str) -> pathlib.Path:
    """Where what stood in a repo's settings file before the platform's first write there is kept."""
    return saved_dir(name) / "nested" / (hashlib.sha256(os.fsencode(rel)).hexdigest()[:16] + ".json")


def write_nested(name: str, doc: dict, rel: str, target: dict, prev: dict, record: dict) -> list[str]:
    """The sandbox settings in a repo in the project's folder, as in a worktree; what stood there is kept, and the repo's
    own git is told not to list the file. What went wrong."""
    import project_git
    ws, folder = registry.dirs(doc)["workspace"], registry.dirs(doc)["workspace"] / rel
    try:
        file, first = settings_rel(doc, folder), nested_before(name, rel)
        st = nofollow.lstat(ws, file)
        at = read_settings(doc, folder)
        if at[2] is not None and at[2] == as_file(target):
            return []
        if st is not None and not first.exists() and not wrote(name, doc, at[0], prev or record) and not (not prev and runtime_claude.platform_rendered(at[0])):
            note = {"workspace": str(ws), "folder": rel}
            if stat.S_ISLNK(st.st_mode):
                note["link"] = nofollow.readlink(ws, file)
            elif stat.S_ISREG(st.st_mode):
                note["raw"] = base64.b64encode(nofollow.read(ws, file, KEEP_CAP) or b"").decode()
            first.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            hubconf.write_atomic(first, json.dumps(note), mode=0o600)
    except (OSError, ValueError) as e:   # e.g. a link on the way: nothing is written through it
        return [_("the settings file in {folder} could not be written ({why})", folder=line(rel, 200), why=line(e))]
    wrong = write_worktree(name, doc, folder, target, prev, record)
    if not wrong:
        project_git.job(name, ws, "exclude", folder, "/" + settings_path(doc).relative_to(ws).as_posix())   # a .git that is no repo: no status to keep clean
    return [f"{line(rel, 200)}: {w}" for w in wrong]


def clear_nested(doc: dict, rel: pathlib.PurePosixPath) -> None:
    """The settings file the platform wrote in this repo goes, and its folder when nothing else is in it (the worktree it
    is in is being removed)."""
    ws = registry.dirs(doc)["workspace"]
    if nofollow.lstat(ws, rel) is None:
        return
    file = rel / settings_path(doc).relative_to(ws)
    got = nofollow.read(ws, file)
    if got is not None and (wrote(doc["name"], doc, parse(got), last_record(doc["name"], doc)) or runtime_claude.platform_rendered(parse(got))):
        nofollow.remove(ws, file)
        if nofollow.entries(ws, file.parent) == []:
            nofollow.remove_dir(ws, file.parent)


def sandbox_key(settings: dict) -> str | None:
    return hashlib.sha256(key(settings["sandbox"]).encode()).hexdigest()[:24] if isinstance(settings.get("sandbox"), dict) else None


def renders(name: str, doc: dict, add: dict | None = None) -> list[str]:
    """Which sandboxes the platform ever wrote for this project (add: one more): a file of an earlier one, in a repo that
    was away or a copy put back, is the platform's, not the user's. Kept when the sandbox goes off."""
    f = RENDERED / "renders" / f"{name}.json"
    got = [x for x in saved_json(f, doc).get("sandboxes") or [] if isinstance(x, str)]
    if add and (k := sandbox_key(add)) not in got:
        got = [*got, k][-RENDERS_MAX:]
        try:   # the settings are written whether or not this can be noted: without it an old file is kept as the user's
            f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            hubconf.write_atomic(f, json.dumps({"workspace": str(registry.dirs(doc)["workspace"]), "sandboxes": got}), mode=0o600)
        except OSError as e:
            print(shown(_("{project}: which sandbox was written could not be noted ({why})", project=name, why=e)), file=sys.stderr)
    return got


RENDERS_MAX = 500


def wrote(name: str, doc: dict, at: dict, record: dict) -> bool:
    """Whether the settings in a repo are the platform's by its records (a sandbox it wrote), not by how they look."""
    return bool(record.get("sandbox")) and same(at.get("sandbox"), record["sandbox"]) or sandbox_key(at) in renders(name, doc)


def out_of(name: str, doc: dict, rel: str, prev: dict, theirs: dict) -> bool:
    """The settings file the platform wrote in this repo goes, or is what it was before (theirs: the user's own settings in
    the workspace's file, which the repos' had too). Whether that is done: what couldn't be read or written is tried again,
    and the note of what was there is kept until then."""
    ws = registry.dirs(doc)["workspace"]
    first, folder = nested_before(name, rel), ws / rel
    note = saved_json(first, doc)
    try:
        if nofollow.lstat(ws, pathlib.PurePosixPath(rel)) is None:
            first.unlink(missing_ok=True)   # gone: what stood there isn't another repo's of that name
            return True
        file = settings_rel(doc, folder)
        at, why, _raw = read_settings(doc, folder)
        if why is not None:
            raise ValueError(why)
        if wrote(name, doc, at, prev) or runtime_claude.platform_rendered(at):   # an older render too; what is the user's in it is kept all the same
            mine, gone = offed(at, prev, {}, doc)
            if gone:
                note_kept(name, rel, gone, keep_copy(name, doc, ".json", folder))
            if prev.get("sandbox") and not same(at.get("sandbox"), prev["sandbox"]):   # of an earlier render, whose rules aren't known any more
                keep_copy(name, doc, ".json", folder)
                mine = {}
            mine = minus(mine, theirs)
            mine = {} if mine is NOTHING else {k: v for k, v in mine.items() if v != {}}
            raw = base64.b64decode(note.get("raw") or "")
            if note.get("link"):
                nofollow.symlink(ws, file, note["link"])
                first.unlink(missing_ok=True)
                return True
            was = {}
            if raw:
                with contextlib.suppress(ValueError):
                    was = parse(raw)
                if wrote(name, doc, was, prev):   # a render of the platform's, however it came to be kept: never put back
                    was, raw = {}, b""
            if raw:
                if minus(mine, was) is NOTHING:
                    put(ws, file, raw)
                else:
                    write_settings(doc, plus(mine, was), folder)
            elif mine:
                write_settings(doc, mine, folder)
            else:
                nofollow.remove(ws, file)
                if nofollow.entries(ws, file.parent) == []:
                    nofollow.remove_dir(ws, file.parent)
        first.unlink(missing_ok=True)
        return True
    except UNREACHED as e:
        print(shown(_("{project}: the sandbox settings in {folder} could not be taken out ({why})", project=name, folder=line(rel, 200), why=line(e))),
              file=sys.stderr)
        return False


def leave(name: str, doc: dict, prev: dict, theirs: dict) -> None:
    """What was a repo and isn't one any more (its .git a file now, or gone) isn't left with the platform's file, which the
    next render wouldn't keep right."""
    was = known(name, doc)["leaving"]
    if was and (left := [rel for rel in was if not out_of(name, doc, rel, prev, theirs)]) != was:
        remember(name, doc, leaving=left)


def unapply_nested(name: str, doc: dict, prev: dict, theirs: dict) -> None:
    """The sandbox goes off: the platform's file out of each repo in the project's folder; what is left is tried again at
    the next run, by the record as it is now."""
    f = known_dir() / f"{name}.json"
    prev = saved_json(f, doc).get("off") or prev
    k = known(name, doc)
    left = [rel for rel in dict.fromkeys([*k["nested"], *k["leaving"]]) if not out_of(name, doc, rel, prev, theirs)]
    if left:
        remember(name, doc, nested=left, leaving=[], off=prev)
    else:
        f.unlink(missing_ok=True)
    coming(name, None)


def note_kept(name: str, rel: str, gone: dict, where: str) -> None:
    note(name, _("{project}: the sandbox settings of its repo {folder} replaced {what}; kept in {file}", project=name,
                 folder=line(rel, 200), what=named(gone), file=where))


TOLD_SHOWN = 20   # of one notice, the entries written out


def tell_once(name: str, doc: dict, theirs: list[str], repos: list[str], bare: list[str], hidden: list[str]) -> list[str]:
    """The owner hears once of each thing the project's own files make its sessions run or let them reach (theirs), of each
    repo found in its folder (bare: the ones the platform writes nothing into), and that not all of it could be looked through; of one that went away and came back, again. What is new."""
    told = known(name, doc)["told"]
    said = [*theirs, *hidden]
    new = [t for t in said if t.key not in told]
    lines = [line(t, 400) for t in new]
    if repos:
        lines.append(_("a session opened in one of these repos in its folder has the sandbox settings from now on: {folders}", folders=line(", ".join(line(x, 120) for x in repos), 600)))
    if bare:
        lines.append(_("a session opened in one of these folders has no settings of the platform's, no sandbox among them (their .git "
                       "is a file or no real repo): {folders}", folders=line(", ".join(line(x, 120) for x in bare), 600)))
    if lines:
        more = len(lines) - TOLD_SHOWN
        note(name, _("{project}: look at this before you open a session in it: {what}", project=name,
                     what="; ".join(lines[:TOLD_SHOWN]) + (_("; and {n} more", n=more) if more > 0 else "")))
    keys = sorted({t.key for t in said})
    if sorted(told) != keys:
        remember(name, doc, told=keys)
    return new


# ---------- a settings file that changes under a live session (runtime_claude.CHANGE_HOOK) ----------

def coming(name: str, record: dict | None) -> None:
    """What is about to be written, on record until it is the record: a session that takes the file in meanwhile is right."""
    f = RENDERED / "coming" / f"{name}.json"
    try:
        if record is None:
            f.unlink(missing_ok=True)
            return
        f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        hubconf.write_atomic(f, json.dumps(record, ensure_ascii=False), mode=0o600)
    except OSError as e:
        print(shown(f"{name}: {e}"), file=sys.stderr)


def ours(got: dict, record: dict, doc: dict) -> bool:
    """Whether these settings are what the platform wrote (record), with nothing next to it but what it would leave there."""
    fixed = {"sandbox": record.get("sandbox"), "hooks": record.get("hooks"), **part(record, "fixed")}   # as it was written, not as here
    if not record.get("sandbox") or any(not same(got.get(k), v) for k, v in fixed.items()):
        return False
    if any(not set(record.get(k) or []) <= set(rules(got, k)) for k in ("deny",)) \
            or any(not same(part(got, "env").get(k), v) for k, v in (record.get("env") or {}).items()):
        return False
    mine, memory = users(got, record, {}), runtime_claude.memory_access(registry.dirs(doc)["workspace"])[0]
    if memory and isinstance((dirs := part(mine, "permissions").get("additionalDirectories")), list):   # a worktree's, the platform's own
        mine = {**mine, "permissions": {**mine["permissions"], "additionalDirectories": [d for d in dirs if d not in memory]}}
    return minus(mine, runtime_claude.carried(mine, reach(doc))) is NOTHING


def config_changed(raw: str) -> str | None:
    """Why a live session of a sandboxed project is not to take in its settings file that changed (it goes on with what it
    has), or None: the file is still the platform's, or it isn't such a session's."""
    import project_git
    change = runtime_claude.changed_settings(raw)
    if change is None:
        return None
    found = project_git.sandbox_of(os.path.dirname(change[1]))   # whose file it is, not where the session is
    if not found:
        return None
    name, doc = found
    ws = registry.dirs(doc)["workspace"]
    why = _("this session's settings file was replaced by something the platform didn't write, so it isn't taken in: the "
            "session goes on with the settings it has ({file})", file=line(change[1]))
    try:
        rel = pathlib.PurePath(os.path.normpath(change[1]))
        rel = rel.relative_to(ws) if rel.is_relative_to(ws) else rel.relative_to(ws.resolve())
        if rel.name != settings_path(doc).name:
            return why
        got = parse(nofollow.read(ws, rel) or b"")
    except (OSError, ValueError):
        return why
    return None if rendered_here(name, doc, got) else why


TRIES = 5   # a file that changes under the platform that often is replaced as it is then


def kept_first(name: str, doc: dict, cur: dict, new: dict, prev: dict, record: dict, unreadable: str | None, *folder) -> list[str]:
    """keep_what_goes; nothing about what was there keeps the sandbox from being written."""
    try:
        kept = keep_what_goes(name, doc, cur, new, prev, record, unreadable, *folder)
        return [kept] if kept else []
    except Exception as e:   # noqa: BLE001
        return [_("what was there could not be kept ({why})", why=e)]


def write_worktree(name: str, doc: dict, folder: pathlib.Path, target: dict, prev: dict, record: dict) -> list[str]:
    """target as a worktree's own settings file, what else is in it kept first. What went wrong; what was kept is on stderr."""
    for _try in range(TRIES):
        cur, unreadable, raw = read_settings(doc, folder)
        if raw == as_file(target):
            return []
        for text in kept_first(name, doc, cur, target, prev, record, unreadable, folder):
            print(shown(text), file=sys.stderr)
        if raw_settings(doc, folder) == raw:
            break
    try:
        write_settings(doc, target, folder)
    except UNREACHED as e:
        return [_("the settings file could not be written ({why})", why=e)]
    return []


def foreign(ws, name: str, doc: dict) -> list[str]:
    """foreign_controls, each within a line (its key kept); after the sandbox is written, whatever its own files are."""
    k = known(name, doc)
    try:
        got = foreign_controls(ws, name, reach(doc), [*k["nested"], *k["settings"]], k["others"], lambda at: rendered_here(name, doc, at))
        return [with_key(line(what, 400), getattr(what, "key", "") or str(what)) for what in got]
    except Exception as e:   # noqa: BLE001
        return [with_key(_("what its own files make its sessions run could not be checked ({why})", why=f"{type(e).__name__}: {e}"[:200]), "unchecked")]


class Said(str):
    """A line the owner is told once, by its key."""
    key = ""


def with_key(text: str, key: str) -> Said:
    out = Said(text)
    out.key = hashlib.sha256(shown(key).encode()).hexdigest()[:16]
    return out


def rendered_here(name: str, doc: dict, got: dict) -> bool:
    """Whether these settings are what the platform wrote, or is about to."""
    return any(r and ours(got, r, doc) for r in (last_record(name, doc), saved_json(RENDERED / "coming" / f"{name}.json", doc)))


def foreign_drift(theirs: list[str]) -> list[str]:
    """What foreign says, as one thing the job says of the project."""
    if not theirs:
        return []
    more = len(theirs) - TOLD_SHOWN
    return [_("its own files would make its sessions run or reach something the platform didn't give them: {what}",
              what="; ".join(theirs[:TOLD_SHOWN]) + (_("; and {n} more", n=more) if more > 0 else ""))]


def push_folder(name: str) -> None:
    """Its push request folder, made by the platform before any session settings name it (docs/design/platform-push.md
    r2 K1): a session can't make it (the root is the platform's), and one it swapped for a link is made again."""
    import push_queue
    try:
        push_queue.ensure_folder(name, log=lambda m: print(m, file=sys.stderr))
    except Exception as e:   # noqa: BLE001 (its sessions push less; the sandbox is written all the same)
        print(_("{project}: its push request folder could not be made ({why})", project=name, why=e), file=sys.stderr)


def unapply(name: str, doc: dict) -> None:
    """enforce turned off in the registry: remove the sandbox and the rules the platform wrote (roll back), and put back
    what was there before its first write: everything the sandbox settings didn't have (users, carried). A file that can't
    be read is left as it is (nothing in it can be told apart), said, and tried again; one that isn't a plain file any
    more has nothing of the platform's in it."""
    rec, path = RENDERED / f"{name}.json", settings_path(doc)
    if not path:
        return
    ws, rel = registry.dirs(doc)["workspace"], settings_rel(doc)
    prev = last_record(name, doc)
    try:
        st = nofollow.lstat(ws, rel)
    except OSError:   # a folder on the way is a link, or no such workspace: read_settings says so
        st = None
    plain = st is not None and stat.S_ISREG(st.st_mode)
    cur, unreadable, _raw = read_settings(doc)
    was_on = rec.exists() or runtime_claude.platform_rendered(cur)
    theirs = {} if unreadable is not None else runtime_claude.carried(users(cur, prev, before(name, doc)[0]), reach(doc))
    if rec.exists() and (another_workspaces(rec, doc) or st is not None and not plain):
        rec.unlink()
    if not rec.exists() and not runtime_claude.platform_rendered(cur):
        # (a lost record: what was there before is known, so the sandbox was on, and this may still hold it)
        lost = unreadable if plain and before(name, doc)[1] else None
        if not lost:
            retire_before(name)
    else:
        prev = prev or written_now(name, doc)
        if (lost := unreadable) is not None:
            print(_("{project}: enforce is off, but its settings file can't be read ({why}); left as it is", project=name, why=unreadable))
        else:
            off_main(name, doc, cur, prev)
            rec.unlink(missing_ok=True)
            retire_before(name)
            print(_("{project}: enforce is off, removed the platform's sandbox/rules", project=name))
    for folder in runtime_claude.session_folders(ws)[1:]:   # each on its own; one left is tried again (platform_rendered)
        try:
            off_worktree(name, doc, folder, prev)
        except Exception as e:   # noqa: BLE001
            print(shown(_("{project}: the sandbox settings of its worktree {worktree} could not be taken out ({why})", project=name,
                          worktree=line(folder.name, 100), why=e)), file=sys.stderr)
    if known(name, doc)["nested"] or known(name, doc)["leaving"]:
        unapply_nested(name, doc, prev or written_now(name, doc), theirs)
    if was_on or lost or saved_dir(name).is_dir():
        tell(name, lost, off=not lost)


def off_main(name: str, doc: dict, cur: dict, prev: dict) -> None:
    ws, rel = registry.dirs(doc)["workspace"], settings_rel(doc)
    was, at_first = before(name, doc)
    mine, gone = offed(cur, prev, was, doc)
    if gone:   # put in since the platform's last write
        upgraded = from_before(RENDERED / f"{name}.json")
        note(name, replaced(name, named(gone), keep_copy(name, doc, ".kept.json" if upgraded else ".json"), upgraded))
    raw, anything = base64.b64decode(at_first.get("raw") or ""), minus(mine, {"permissions": {"deny": [], "allow": []}}) is not NOTHING
    if at_first.get("link"):   # what came since is the plain file's: kept, where the owner finds it
        if anything:
            note(name, _("{project}: its settings file is a link to {target} again; what was added since is kept in {file}", project=name,
                         target=line(at_first["link"], 1024), file=keep_copy(name, doc)))
        nofollow.symlink(ws, rel, at_first["link"])
    elif raw and not was and raw.strip() != b"{}" and not anything:
        put(ws, rel, raw)   # it couldn't be read, and there is nothing of the user's since: as it was
        for f in saved_dir(name).glob("*.invalid.json"):   # back where the user keeps it: nothing to put back by hand
            if f.read_bytes() == raw:
                f.unlink()
    elif cur or was:
        theirs = {**was, "hooks": runtime_claude.own_hooks(was)}
        rest = minus(theirs, {} if prev.get("alone") or at_first.get("alone") else runtime_claude.carried(theirs, reach(doc)))
        back = plus(with_session_hooks(mine, cur), {} if rest is NOTHING else rest)
        write_settings(doc, {k: v for k, v in back.items() if k != "hooks" or v})


def off_worktree(name: str, doc: dict, folder: pathlib.Path, prev: dict) -> None:
    at, why, _raw = read_settings(doc, folder)
    if why or not (runtime_claude.platform_rendered(at) or "sandbox" in at and same(at["sandbox"], prev.get("sandbox"))):
        return   # not the platform's
    mine, gone = offed(at, prev or written_now(name, doc), {}, doc)
    if gone:
        note(name, _("{project}: the sandbox settings of its worktree {worktree} replaced {what}; kept in {file}", project=name,
                     worktree=line(folder.name, 100), what=named(gone), file=keep_copy(name, doc, ".json", folder)))
    write_settings(doc, with_session_hooks(mine, at), folder)


def offed(cur: dict, prev: dict, was: dict, doc: dict) -> tuple[dict, dict]:
    """(what of cur stays when the sandbox goes off, what doesn't: put in since the platform's last write)."""
    mine = runtime_claude.carried(users(cur, prev, was), reach(doc))
    rendered = runtime_claude.platform_rendered(cur)   # its sandbox is the platform's even when no record says so
    return mine, going(cur, mine, prev, {"sandbox": cur.get("sandbox") if rendered and not prev.get("sandbox") else None})


def with_session_hooks(mine: dict, cur: dict) -> dict:
    """mine with the session hooks any project has, as cur has them."""
    hooks = runtime_claude.session_hooks_of(cur)
    return {**mine, "hooks": hooks} if hooks else mine


def renamed(old: str, new: str, ws: str) -> None:
    """rename-project.sh, on the hub machine: the record and what is kept move to the new name and the new workspace; what
    an earlier project of that name left stays, under another."""
    if saved_dir(old).is_dir():
        if saved_dir(new).exists():
            os.rename(saved_dir(new), saved_dir(new).with_name(f"{new}.earlier-{secrets.token_hex(4)}"))
        os.rename(saved_dir(old), saved_dir(new))
    moved = str(pathlib.Path(ws).expanduser())
    for f, to in ((RENDERED / f"{old}.json", RENDERED / f"{new}.json"), (saved_dir(new) / BEFORE, saved_dir(new) / BEFORE),
                  (known_dir() / f"{old}.json", known_dir() / f"{new}.json"),
                  (RENDERED / "renders" / f"{old}.json", RENDERED / "renders" / f"{new}.json"), *((x, x) for x in (saved_dir(new) / "nested").glob("*.json"))):
        if f != to and f.exists():
            os.rename(f, to)
        if (got := saved_json(to)):
            hubconf.write_atomic(to, json.dumps({**got, "workspace": moved}, indent=1), mode=0o600)


def written_now(name: str, doc: dict) -> dict:
    """What the platform would write today, for a record that can't be read."""
    try:
        doc = {**doc, "enforce": {**(doc.get("enforce") or {}), "sandbox": True}}
        index = registry.refresh()
        _sandbox, deny, allow = desired(name, doc, index)
        return {"deny": deny, "allow": allow, "env": desired_env(name, doc, index)}
    except Exception as e:   # noqa: BLE001 (the sandbox key and the hooks still go)
        print(_("{project}: which rules the platform wrote can't be told ({why})", project=name, why=e), file=sys.stderr)
        return {}


GATE_MARK = names.gate_mark() + " (managed; registry gate: strict)"
GATE_HOOK_HEAD = GATE_MARK + """
# No direct commit or merge commit on main/master in the main checkout; merge-branch fast-forwards, which does not run this hook.
b=$(git symbolic-ref --short -q HEAD)
case "$b" in main|master) ;; *) exit 0 ;; esac
[ -n "$MERGE_GATE" ] && exit 0
"""


# the branch, as the hook's shell expands it: braced, since a translation may put a non-ASCII character right after it
# ($b followed by a full-width full stop, U+3002, makes some shells read its first byte as part of the name,
# dropping the branch and leaving broken UTF-8)
BRANCH = "${b}"


def gate_hook() -> str:
    """the hook's text; the line it prints when it refuses a commit is a message ({branch} is filled in by the shell)"""
    return GATE_HOOK_HEAD + 'echo "' + _(
        "This project has the merge gate on (gate: strict): no direct commits to {branch}. Work in your own "
        "worktree/branch and finish with brunna merge-branch.", branch=BRANCH) + '" >&2\nexit 1\n'


MARK = names.gate_mark()   # every hook the platform writes starts with it; others are the project's own
MARKS = tuple(names.gate_mark(s) for s in names.all("marker"))   # either spelling is the platform's
HOOKS = ("pre-commit", "pre-merge-commit", "commit-msg", "pre-push")


def wanted_hooks(doc: dict) -> dict[str, tuple[str, str]]:
    """hook -> (its text, which registry field wants it). gate: strict refuses commits on main; commit_gate runs the
    commit tiers on a branch and the merge tiers on main through the main checkout's runner (gate code from main,
    docs/design/test-waiting.md A1); commits: conventional checks commit titles; the platform's own repo gets a
    pre-push check of the messages it publishes. One pre-commit serves gate and
    commit_gate (strict wins on main: no commit there at all)."""
    import shlex
    strict, cg = doc.get("gate") == "strict", doc.get("commit_gate") is True
    py, hub = shlex.quote(str(hubconf.MAIN / "bin" / "py")), hubconf.MAIN / "bin"
    out = {}
    if strict and not cg:
        out["pre-commit"] = ("#!/bin/sh\n" + gate_hook(), "gate strict")
    elif cg:
        refuse = ('  main|master) [ -n "$MERGE_GATE" ] || { echo "' + _(
            "This project has the merge gate on (gate: strict): no direct commits to {branch}. Work in your own "
            "worktree/branch and finish with brunna merge-branch.", branch=BRANCH) + '" >&2; exit 1; } ;;\n') if strict else "  main|master) moment=merge ;;\n"
        out["pre-commit"] = ("#!/bin/sh\n" + MARK + " (managed; registry commit_gate)\n"
                             "# a commit on a branch runs the commit tiers, one onto main the merge tiers\n"
                             "b=$(git symbolic-ref --short -q HEAD)\nmoment=commit\ncase \"$b\" in\n" + refuse +
                             "esac\nexec " + py + " " + shlex.quote(str(hub / "test-runner.py")) +
                             ' --index --when "$moment"\n', "commit_gate")
    if strict:
        out["pre-merge-commit"] = ("#!/bin/sh\n" + gate_hook(), "gate strict")
    if doc.get("name") == hubconf.hub_project():
        # the platform's history is public: no pushed commit or tag message names a private project, however it was
        # made (cherry-pick, revert, --no-verify skip the commit-msg hook; bin/private_names.py)
        out["pre-push"] = ("#!/bin/sh\n" + MARK + " (managed; the platform's own repo)\nexec " + py + " " +
                           shlex.quote(str(hub / "private_names.py")) + ' --pre-push "$@"\n', "the platform's own repo")
    if doc.get("commits") == "conventional":
        out["commit-msg"] = ("#!/bin/sh\n" + MARK + " (managed; registry commits: conventional)\nexec " + py + " " +
                             shlex.quote(str(hub / "commit_title.py")) + ' --file "$1"\n', "commits: conventional")
    return out


def gate(name: str, doc: dict, report: bool, quiet: bool = False, remove_all: bool = False) -> list[tuple[str, str]]:
    """The platform's git hooks for a repo (wanted_hooks): installed when its registry entry wants them, updated when
    their text changed, removed when it no longer does (only those carrying the platform's marker). A project with
    its own core.hooksPath (e.g. husky) gets nothing installed, only detection. Returns what would be done (report) /
    was done: (add|update|remove, hook)."""
    import project_git
    say = (lambda *_args: None) if quiet else print
    ws = registry.dirs(doc)["workspace"]
    if not ws or not (ws / ".git").is_dir():
        return []
    want = {} if remove_all else wanted_hooks(doc)   # remove_all: agenthub uninstall
    # the repo's .git is the project's: what is there is read, and the hooks written, by its own job (a symlink
    # planted in .git/hooks then reaches only what the project can write itself; the job replaces it rather than
    # writing through it)
    try:
        st = project_git.data(name, ws, "hooks", ws, *HOOKS)
    except project_git.Unavailable as e:
        say(_("{project}: the gate hooks could not be checked ({why})", project=name, why=e))
        return []
    st = st if isinstance(st, dict) else {}
    hp = st.get("hooks_path") if isinstance(st.get("hooks_path"), str) else ""
    texts = st.get("hooks") if isinstance(st.get("hooks"), dict) else {}
    acts = []
    if name == hubconf.hub_project() and hp and pathlib.Path(ws, hp).resolve() == (hubconf.MAIN / "githooks").resolve():
        # the hub's tracked githooks/ from before its gate moved into its git dir (step 2.3): the platform's own, so
        # replaced, not detected around
        acts.append(("remove", "core.hooksPath"))
        if not report:
            subprocess.run(["git", "-C", str(ws), "config", "--unset", "core.hooksPath"], capture_output=True)
            say(_("{project}: core.hooksPath {path} unset; the platform's hooks go into the git dir", project=name, path=hp))
        hp = ""
    if want and hp:
        say(_("{project}: the platform's hooks are not installed (the project uses its own core.hooksPath={path}); "
              "detecting only", project=name, path=hp))
        return acts
    for hook in HOOKS:
        text = texts.get(hook) if isinstance(texts.get(hook), str) else None
        mine = text is not None and any(text.startswith("#!/bin/sh\n" + m) for m in MARKS)
        if hook in want:
            body, why = want[hook]
            if text is not None and not mine:
                say(_("{project}: .git/hooks/{hook} is already the project's own hook; not overwriting it (the gate only "
                      "detects)", project=name, hook=hook))
                continue
            if text == body:
                continue
            acts.append(("update" if mine else "add", hook))
            if not report:
                project_git.job(name, ws, "hook-write", ws, hook, input=body)
                say((_("{project}: updated {hook} ({why})", project=name, hook=hook, why=why) if mine else
                     _("{project}: installed {hook} ({why})", project=name, hook=hook, why=why)))
        elif mine:
            acts.append(("remove", hook))
            if not report:
                project_git.job(name, ws, "hook-remove", ws, hook)
                say(_("{project}: the registry no longer wants {hook}; removed it", project=name, hook=hook))
    return acts


# ---------- applier (appliers.py) ----------
# A Change uses only neutral words (agent-settings:<project>, hook:<project>/<name>); the runtime's settings format
# stays in runtime_claude.

EXTERNAL = False


# ---------- the agent's session hooks (the runtime's lifecycle: `hub-event <event>`) ----------
# A hook names the hub where it was when it was written (hubconf.hub_bin). A project's are written again at each of its
# session starts (sync-local-setup → hub-event install), but only while they still reach the hub: once the hub has
# moved, the old ones quietly do nothing and nothing in a session ever rewrites them. So the applier keeps them too:
# `agenthub config apply` after a move (the scheduler and the CLI need writing again then anyway) points every one at
# the hub's new place. The hub's own sessions get theirs only here: its memory isn't a checkout of its own, so the
# session-start setup that installs a project's skips it. A sandboxed project's are part of its settings above.

def lifecycle_folder(name: str, doc: dict) -> pathlib.Path | None:
    """Where this registry entry's sessions get the platform's hooks from the applier, or None."""
    if name == hubconf.hub_project():
        return hubconf.MAIN
    if (doc.get("enforce") or {}).get("sandbox"):
        return None
    ws = registry.dirs(doc)["workspace"]
    return ws if ws and (ws / ".agent-memory").is_dir() else None   # onboarded on this machine


def lifecycle(index: dict, apply: bool, only: str | None = None) -> list:
    """The session hooks that are not what installing them now would write: installed when apply; the Changes."""
    from appliers import Change
    import runtime_ports
    try:
        rt = runtime_ports.load(ports=("lifecycle",))
    except runtime_ports.Unsupported:   # the runtime has no session events (doctor's feature table says so)
        return other_lifecycles(index, apply, only)
    out = []
    for name, doc in sorted(index["projects"].items()):
        ws = lifecycle_folder(name, doc)
        if not ws or (only and name != only):
            continue
        try:
            if rt.install_lifecycle(ws, check=True):
                if apply:
                    rt.install_lifecycle(ws)
                out.append(Change("update", f"agent-settings:{name}", _("the platform's session hooks")))
        except Exception as e:   # noqa: BLE001 (e.g. its settings aren't valid JSON, in any way: left as they are, said)
            print(_("{project}: its session hooks could not be checked ({why})", project=name, why=e), file=sys.stderr)
    return out + other_lifecycles(index, apply, only)


CAGED_NEEDS = {"control", "confine"}


def other_lifecycles(index: dict, apply: bool, only: str | None = None) -> list:
    """The session hooks of the installed runtimes other than runtime.default (hub.json runtime.installed;
    docs/design/mailbox-runtimes.md A1), same rules as lifecycle(). In a sandboxed project only a runtime with control
    and confine gets them, written by the platform (its hook files are then in the project's protected paths, and its
    runs can be confined); one without them is refused there, fail closed: never installed, and hub-event does nothing
    for one found anyway (doctor says which). Claude Code's hooks in a sandboxed project are part of its settings
    (compose)."""
    from appliers import Change
    import runtime_ports
    out = []
    for rname in runtime_ports.installed():
        if rname == runtime_ports.default_name():
            continue
        try:
            rt = runtime_ports.load(rname, ports=("lifecycle",))
        except runtime_ports.Unsupported:
            continue
        caged_ok = CAGED_NEEDS <= runtime_ports.capabilities(rname)
        for name, doc in sorted(index["projects"].items()):
            if only and name != only:
                continue
            ws = lifecycle_folder(name, doc)
            if ws is None and (doc.get("enforce") or {}).get("sandbox") and caged_ok and rt is not runtime_claude:
                w = registry.dirs(doc)["workspace"]
                ws = w if w and (w / ".agent-memory").is_dir() else None
            if not ws:
                continue
            try:
                if rt.install_lifecycle(ws, check=True):
                    if apply:
                        rt.install_lifecycle(ws)
                    out.append(Change("update", f"agent-settings:{name}",
                                      _("the platform's session hooks ({runtime})", runtime=rname)))
            except Exception as e:   # noqa: BLE001 (as in lifecycle)
                print(_("{project}: its session hooks could not be checked ({why})", project=name, why=e),
                      file=sys.stderr)
    return out


@contextlib.contextmanager
def alone(name: str, failed: list[str]):
    """What is done for one project: whatever goes wrong is said and counted, and the next one is done."""
    try:
        yield
    except Exception as e:   # noqa: BLE001
        failed.append(shown(f"{type(e).__name__}: {e}")[:200])
        print(_("⚠️ {project}: its settings or hooks could not be written ({why})", project=name, why=failed[-1]), file=sys.stderr)


def _changes(index: dict) -> list:
    from appliers import Change
    out = lifecycle(index, apply=False)
    for name, doc in sorted(index["projects"].items()):
        failed: list[str] = []
        with alone(name, failed):
            out += _project_changes(name, doc, index)
        out += [Change("update", f"agent-settings:{name}", why) for why in failed]   # never a plan that looks done
    return out


def _project_changes(name: str, doc: dict, index: dict) -> list:
    from appliers import Change
    want = wanted_hooks(doc)
    out = [Change(op, f"hook:{name}/{hook}", want.get(hook, ("", _("not wanted")))[1])
           for op, hook in gate(name, doc, report=True, quiet=True)]
    if name == hubconf.hub_project():   # the platform's own repo: its hooks only, no sandbox settings
        return out
    if not (doc.get("enforce") or {}).get("sandbox"):
        there = settings_path(doc) and settings_path(doc).exists()
        if there and ((RENDERED / f"{name}.json").exists() or runtime_claude.platform_rendered(read_settings(doc)[0])):
            out.append(Change("remove", f"agent-settings:{name}", _("enforce is off: remove the platform's sandbox/rules")))
        return out
    c = compose(name, doc, index)
    if not c:
        return out
    _path, cur, new, prev, record, _unreadable, raw = c
    if raw != as_file(new) or not same(prev, record):
        what = [w for w, changed in (("sandbox", not same(new.get("sandbox"), cur.get("sandbox"))),
                                     (_("permission rules"), not same(new.get("permissions"), cur.get("permissions"))),
                                     (_("record"), not same(prev, record))) if changed]
        out.append(Change("add" if not cur else "update", f"agent-settings:{name}", _(", ").join(what)))
    return out


def plan(index: dict | None = None) -> list:
    return _changes(index or registry.refresh())


def apply(index: dict | None = None) -> list:
    index = index or registry.refresh()
    todo = _changes(index)
    lifecycle(index, apply=True)
    for name, doc in sorted(index["projects"].items()):
        with alone(name, []):
            gate(name, doc, report=False, quiet=True)
        if name == hubconf.hub_project():
            continue
        with alone(name, []):
            if (doc.get("enforce") or {}).get("sandbox"):
                write_project(name, doc, index, report=False)
            else:
                unapply(name, doc)
    return todo


def render_worktree(name: str, folder) -> pathlib.Path:
    """A sandboxed project's settings written into one of its session folders (a worktree), as a plain file the project
    can't write there itself: what its main checkout gets (compose), with its own .agent-memory as a working directory.
    Returns the file written; raises when it can't be (session_worktree.make then takes the worktree back: fail closed)."""
    index = registry.refresh()
    doc = index["projects"][name]
    push_folder(name)
    c = compose(name, doc, index)
    if not c:
        raise ValueError(_("{project} has no workspace on this machine", project=name))
    ws = registry.dirs(doc)["workspace"]
    # its own .agent-memory as a working directory, notes and STATE.md only (runtime_claude.worktree_memory): the
    # worktree reaches it through a link, and every write there would ask. Never a folder that is a link
    target = runtime_claude.worktree_memory(c[2], ws)
    if (wrong := write_worktree(name, doc, pathlib.Path(folder), target, c[3], c[4])):
        raise OSError(shown(wrong[0]))
    return pathlib.Path(folder) / settings_path(doc).relative_to(ws)


def worktree(folder: str) -> int:
    """At a worktree session's start (worktree-memory.sh, before anything runs as the project): the platform's settings
    for a sandboxed project written into that worktree again. A worktree the platform made (session_worktree.make) has
    had them since it was made; this covers one made any other way, and settings changed since."""
    import isolation
    import project_git
    found = project_git.project_at(folder)
    if not found or not isolation.sandboxed(found[1]):
        return 0
    try:
        render_worktree(found[0], folder)
    except Exception as e:   # said into the session (worktree-memory.sh prints it): no sandbox settings here
        print(_("⚠️ This worktree's sandbox settings could not be written ({why}); don't run commands in this session, "
                "ask the owner to look at it", why=e))
        return 1
    return 0


def usage() -> str:
    return _("usage: render-project-settings.py [--report] [--only <project>]\n"
             "       render-project-settings.py --worktree <folder>\n"
             "       render-project-settings.py --renamed <old> <new> <workspace>")


def main() -> int:
    argv = sys.argv[1:]
    if argv in (["-h"], ["--help"]):
        print(usage())
        return 0
    wt = len(argv) == 2 and argv[0] == "--worktree"
    if len(argv) == 4 and argv[0] == "--renamed" and not hubconf.worktree_lookup():
        renamed(*argv[1:])
        return 0
    if not wt and not hubconf.known_args(argv, {"--report"}, {"--only"}):
        print(usage(), file=sys.stderr)
        return 2
    if hubconf.worktree_lookup() and "--report" not in argv:
        # it would write every sandboxed project's settings with this copy's paths (11-13)
        print(_("render-project-settings: {root} is a linked worktree of the hub; it writes projects' settings only "
                "from the main checkout ({main}); --report works here", root=hubconf.ROOT, main=hubconf.MAIN),
              file=sys.stderr)
        return 1
    if wt:
        return worktree(argv[1])
    report = "--report" in argv
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    index = registry.refresh()
    bad = 0
    for c in lifecycle(index, apply=not report, only=only):
        name = c.target.split(":", 1)[1]
        print(_("{project}: would write the platform's session hooks again", project=name) if report else
              _("{project}: the platform's session hooks written again", project=name))
    for name, doc in sorted(index["projects"].items()):
        if only and name != only:
            continue
        failed: list[str] = []
        with alone(name, failed):
            gate(name, doc, report)
        if name == hubconf.hub_project():   # the platform's own repo: its hooks only, no sandbox settings
            bad += bool(failed)
            continue
        on, drift = bool((doc.get("enforce") or {}).get("sandbox")), []
        with alone(name, failed):
            if on:
                drift = write_project(name, doc, index, report=report)
            elif report:
                write_project(name, doc, index, report=True)
            else:
                unapply(name, doc)
        bad += bool(failed or drift)
        with alone(name, failed):
            if drift:
                print(_("⚠️ {project}'s settings drifted (report mode: not written back): {drift}", project=name,
                        drift="; ".join(drift)) if report else
                      _("⚠️ {project}'s settings drifted (written back): {drift}", project=name, drift="; ".join(drift)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
