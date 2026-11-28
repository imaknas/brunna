#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""brunna service: installing someone else's service (docs/design/service-ecosystem.md, step 2a). The owner's
terminal only: every verb that changes something refuses inside a sandbox, a coding agent's session or a linked
worktree of the hub (bin/owner_terminal.py), since what it grants is the owner's to grant.

  brunna service add <url> [--ref <tag|branch|commit>] [--yes]
      fetch the repository (nothing of it runs as you), check its brunna-service.yaml, show what it asks for and ask
      you to grant it, ask each required secret, check out the code and run its setup in the sandbox; only then is
      its entry written (<config>/services/<name>.yaml) and committed. --yes grants what it asks without the question
      (secrets are still asked).
  brunna service remove <name> [--purge]
      its entry, fetched commits, code and caches; --purge also its state and secrets.
  brunna service list [--json]
  brunna service secret <name> <SECRET>
      set one of its granted secrets; the value is read from stdin (never an argument).
  brunna service run <name> [-- <args…>]
      its CLI with the arguments, in its sandbox, as you (the caller it is told is owner).
  brunna service update <name> [--ref <tag|branch|commit>] [--yes]
      take the newest release tag (or the ref asked for): what it asks beyond what was granted is shown and asked;
      the new version is set up beside the installed one and replaces it only if setup passed.
  brunna service callers <name> <project>… | --all
      who may call it from their sessions (through brunna mcp); --all: every registered project (the default).
  brunna service update --all
      the platform job brunna/service-updates: every service that follows release tags; a new version that asks for
      more, a tag that moved, or a failing setup is an alert, and the installed version keeps running.

A project whose registry entry consumes it calls its tools from its sessions through brunna mcp (unsandboxed projects
only so far); its questions to you aren't built yet.
"""
from __future__ import annotations

import datetime
import getpass
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import installed_services as svc  # noqa: E402
import service_fetch  # noqa: E402
import service_manifest  # noqa: E402

_ = i18n.domain(__file__)

BIN = pathlib.Path(__file__).resolve().parent
SETUP_TIMEOUT = 1800


def say(msg: str) -> None:
    print(msg, flush=True)


def err(msg: str) -> int:
    print(msg, file=sys.stderr, flush=True)
    return 1


def guard() -> str | None:
    import owner_terminal
    why = owner_terminal.refusal()
    if why:
        return {"sandbox": _("brunna service runs only at your own terminal, not inside a sandbox"),
                "session": _("brunna service runs only at your own terminal, not inside a coding agent's session"),
                "worktree": _("brunna service runs only from the hub's main checkout, not a linked worktree")}[why]
    return None


def _rmtree(path: pathlib.Path) -> None:
    """Remove a folder without following links (setup may have made some)."""
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _commit(name: str, message: str) -> None:
    rel = f"services/{name}.yaml"
    subprocess.run([str(BIN / "commit-hub-files.sh"), message, rel], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


GROUPS: set[int] = set()   # process groups started here, killed on SIGTERM too (the scheduler's timeout)


def _on_term(signum, frame):
    import signal
    for g in list(GROUPS):
        try:
            os.killpg(g, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise SystemExit(128 + signum)   # finally blocks run: a half-made version is removed


def _sandboxed(name: str, phase: str, cwd: pathlib.Path, words: list[str], timeout: int | None = None,
               commit: str | None = None) -> int:
    """Run in the service's sandbox, in a process group of its own, killed whole when the command ends (or times out,
    or this process is told to stop): nothing it started in the background outlives it (a setup's leftover would keep
    writing its code folder)."""
    import signal
    cmd = [sys.executable, "-I", "-B", str(BIN / "sandboxed-run"), "--installed", name, "--phase", phase,
           *(["--commit", commit] if commit else []), "--cwd", str(cwd), "--", *words]
    p = subprocess.Popen(cmd, start_new_session=True)
    GROUPS.add(p.pid)
    try:
        return p.wait(timeout=timeout)
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        GROUPS.discard(p.pid)


def show_asks(m: dict) -> None:
    say(_("{name}: {what}", name=m["name"], what=m["what"]))
    say(_("  interface version {v}", v=m["interface"]))
    for k, d in (m.get("models") or {}).items():
        say(_("  model calls through your models: {name} ({tier})", name=k, tier=d["tier"]))
    for s in m.get("secrets") or []:
        kind = _("optional") if s.get("optional") else _("required")
        say(_("  secret {name} ({kind}): {what}", name=s["name"], kind=kind, what=s["what"]))
    if m.get("network"):
        say(_("  says it reaches: {hosts}", hosts=", ".join(m["network"])))
    say(_("  outbound network is not confined: it can reach any host, and send what it is given"))
    if m.get("principal"):
        say(_("  asks to put questions and tasks to you (not used until that step is built)"))
    say(_("  it can't read your projects, credentials or the platform's state; it writes only its own folders"))


def _ask_yes(prompt: str) -> bool:
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _write_secret(name: str, secret: str, value: str) -> None:
    d = svc.folders(name)["secrets"]
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    path = d / secret
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
    os.chmod(path, 0o600)


def _secret_set(name: str, secret: str) -> bool:
    import stat
    try:
        st = os.lstat(svc.folders(name)["secrets"] / secret)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0


TIERS = ("fast", "standard", "strong")


def asks_more(entry: dict, m: dict) -> list[str]:
    """What the manifest m asks beyond what the entry was granted (each a line to show)."""
    g = entry.get("granted") or {}
    out = []
    for k, d in (m.get("models") or {}).items():
        had = (g.get("models") or {}).get(k)
        if not had or TIERS.index(d["tier"]) > TIERS.index(had.get("tier", "fast")):
            out.append(_("a model call {name} at {tier}", name=k, tier=d["tier"]))
    for s in m.get("secrets") or []:
        if s["name"] not in (g.get("secrets") or []):
            out.append(_("a secret {name}", name=s["name"]))
        elif not s.get("optional") and not _secret_set(entry["name"], s["name"]):
            out.append(_("the secret {name}, now required, which isn't set", name=s["name"]))
    if m.get("principal") and not g.get("principal"):
        out.append(_("putting questions and tasks to you"))
    for h in m.get("network") or []:
        if h not in (g.get("network") or []):
            out.append(_("reaching {host}", host=h))
    if m["interface"] != entry.get("interface"):
        out.append(_("interface version {old} → {new} (what calls it may break)", old=entry.get("interface"),
                     new=m["interface"]))
    return out


def _granted(m: dict) -> dict:
    return {"models": m.get("models") or {}, "secrets": [s["name"] for s in m.get("secrets") or []],
            "principal": bool(m.get("principal")), "network": list(m.get("network") or [])}


def _entry(m: dict, url: str, ref: str | None, commit: str, granted: dict, follow: str, tags: dict,
           previous: str | None, added: str) -> dict:
    return {"v": 1, "name": m["name"], "what": m["what"], "url": url, "ref": ref if ref and not service_fetch.HEX.match(ref) else None,
            "commit": commit, "interface": m["interface"],
            "run": {"cli": m["cli"]["run"], "mcp": m["mcp"]["run"], "setup": list(m.get("setup") or [])},
            "granted": granted, "data": list(m.get("data") or []),
            "secrets_required": [s["name"] for s in m.get("secrets") or [] if not s.get("optional")],
            "follow": follow, "tags": tags, "previous": previous, "added": added}


def _ask_secrets(m: dict, only: set[str] | None = None) -> dict[str, str] | str:
    """Ask the required (and offer the optional) secrets on the terminal; why not, as a string, when it can't."""
    values: dict[str, str] = {}
    for s in m.get("secrets") or []:
        if only is not None and s["name"] not in only:
            continue
        how = f" ({s['how']})" if s.get("how") else ""
        if not sys.stdin.isatty() and not s.get("optional"):
            return _("{secret} is required and there is no terminal to ask it on; run it at your terminal",
                     secret=s["name"])
        if not sys.stdin.isatty():
            continue
        v = getpass.getpass(_("{secret}{how}{opt}: ", secret=s["name"], how=how,
                              opt=_(" (Enter to skip)") if s.get("optional") else ""))
        if v:
            values[s["name"]] = v
        elif not s.get("optional"):
            return _("{secret} is required", secret=s["name"])
    return values


def _fetch_checked(url: str, ref: str | None, stage: pathlib.Path, allow_local: bool, expect: str | None = None,
                   tag: bool = False) -> tuple[str, dict]:
    commit = service_fetch.fetch(url, ref, stage, allow_local=allow_local, expect=expect, tag=tag)
    doc, problems = service_manifest.parse(service_fetch.manifest(stage, commit))
    problems = problems or service_manifest.problems(doc)
    if problems:
        raise service_fetch.FetchError(_("its {file} has problems: {problems}", file=service_manifest.FILE,
                                         problems="; ".join(problems)))
    return commit, doc


def _setup(name: str, commit: str, code: pathlib.Path, commands: list[str]) -> str | None:
    for c in commands:
        say(_("setup: {cmd}", cmd=c))
        try:
            rc = _sandboxed(name, "setup", code, ["/bin/sh", "-c", c], timeout=SETUP_TIMEOUT, commit=commit)
        except subprocess.TimeoutExpired:
            return _("setup took longer than {m} minutes", m=SETUP_TIMEOUT // 60)
        if rc:
            return _("setup failed ({cmd}, exit {code})", cmd=c, code=rc)
    return None


def _release_tags(url: str, allow_local: bool) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """The remote's release tags {tag: commit}, the problems (two spellings of one version naming different commits),
    and every tag it has."""
    remote = service_fetch.tags(url, allow_local)
    rel = {t: c for t, c in remote.items() if svc.release(t)}
    seen: dict[tuple, tuple[str, str]] = {}
    problems = []
    for t, c in sorted(rel.items()):
        v = svc.release(t)
        if v in seen and seen[v][1] != c:
            problems.append(_("tags {a} and {b} are one version but name different commits", a=seen[v][0], b=t))
        seen.setdefault(v, (t, c))
    return rel, problems, remote


def cmd_add(args: list[str]) -> int:
    url, ref, yes = None, None, False
    it = iter(args)
    for a in it:
        if a == "--ref":
            ref = next(it, None)
            if not ref:
                return err(_("--ref needs a tag, branch or commit"))
        elif a == "--yes":
            yes = True
        elif url is None and not a.startswith("--"):
            url = a
        else:
            return err(_("usage: brunna service add <url> [--ref <tag|branch|commit>] [--yes]"))
    if not url:
        return err(_("usage: brunna service add <url> [--ref <tag|branch|commit>] [--yes]"))
    allow_local = svc.names.env("SERVICE_ALLOW_LOCAL") == "1"
    stage = pathlib.Path(__import__("tempfile").mkdtemp(prefix="service-add-", dir=hubconf.private_tmp()))
    try:
        return _add(url, ref, yes, allow_local, stage)
    except (service_fetch.FetchError, svc.Locked, OSError) as e:
        return err(_("not added: {why}", why=e))
    finally:
        _rmtree(stage)


def _add(url: str, ref: str | None, yes: bool, allow_local: bool, stage: pathlib.Path) -> int:
    say(_("Fetching {url} …", url=url))
    follow, tags, expect = "none", {}, None
    if ref and svc.release(ref):   # follows release tags only when the remote says it is one
        rel, problems, _all = _release_tags(url, allow_local)
        if problems:
            return err(_("not added: {why}", why="; ".join(problems)))
        if ref in rel:
            follow, expect = "tags", rel[ref]
    commit, doc = _fetch_checked(url, ref, stage / "repo.git", allow_local, expect=expect, tag=follow == "tags")
    if follow == "tags":
        tags = {ref: commit}
    name = doc["name"]
    if (why := svc.clash(name)):
        return err(_("not added: the name {name} is taken ({why})", name=name, why=why))
    show_asks(doc)
    if not yes and not _ask_yes(_("Grant this and install {name}?", name=name)):
        return err(_("not added"))
    values = _ask_secrets(doc)
    if isinstance(values, str):
        return err(_("not added: {why}", why=values))
    with svc.lock(name):
        f = svc.folders(name, commit)
        try:
            f["base"].mkdir(parents=True, mode=0o700)
        except FileExistsError:
            return err(_("not added: {path} already exists", path=f["base"]))
        ok = False
        try:
            shutil.move(str(stage / "repo.git"), str(f["repo"]))   # private_tmp may be on another volume
            f["code_root"].mkdir(mode=0o700)
            service_fetch.checkout(f["repo"], commit, f["code"])
            for secret, v in values.items():
                _write_secret(name, secret, v)
            # the entry is written before setup so sandboxed-run can find it; removed again if setup fails
            entry = _entry(doc, url, ref, commit, _granted(doc), follow, tags, None,
                           datetime.date.today().isoformat())
            _write_entry(entry)
            if (why := _setup(name, commit, f["code"], entry["run"]["setup"])):
                return err(_("not added: {why}", why=why))
            ok = True
        finally:
            if not ok:
                _entry_path(name).unlink(missing_ok=True)
                _rmtree(f["base"])
    _commit(name, f"feat(services): add {name} at {commit[:12]}")
    say(_("{name} is installed ({commit}). Try it: brunna service run {name} -- --help", name=name, commit=commit[:12]))
    return 0


def _newer(entry: dict, rel: dict[str, str]) -> str | None:
    """The newest release tag above the installed one."""
    cur = svc.release(entry.get("ref") or "") or max((svc.release(t) for t in entry.get("tags") or {}), default=None)
    best = max(((svc.release(t), t) for t in rel), default=None)
    if best and (cur is None or best[0] > cur):
        return best[1]
    return None


def update_one(name: str, ref: str | None = None, yes: bool = False, auto: bool = False) -> list[str] | None:
    """Update one service; the problems (empty: current, or updated; None: not checked, its alert left as it is)."""
    allow_local = svc.names.env("SERVICE_ALLOW_LOCAL") == "1"
    entry = svc.entries().get(name)
    if not entry:
        return [_("{name} is not an installed service", name=name)]
    url = entry["url"]
    if auto and not url.startswith(("https://", "file://", "/")) and not os.environ.get("SSH_AUTH_SOCK"):
        say(_("{name}: an ssh address is checked only by hand (brunna service update {name}): the job has no ssh agent",
              name=name))
        return None
    rel, problems, every = _release_tags(url, allow_local)
    moved = [t for t, c in (entry.get("tags") or {}).items() if rel.get(t) != c]
    if moved and (auto or not ref):
        problems.append(_("tag {tags} no longer names what was installed (moved or deleted): nothing updated; "
                          "brunna service update {name} --ref <tag> takes a tag after you have looked",
                          tags=", ".join(moved), name=name))
    if problems:
        return problems
    if not auto and not ref and entry.get("follow") != "tags":
        return []   # pinned to a branch or commit: only an update with --ref moves it
    target = ref or _newer(entry, rel)
    if not target:
        return []
    is_tag = target in every
    if is_tag and target in (entry.get("tags") or {}) and rel.get(target) == entry["commit"]:
        return []
    checked = {k: entry.get(k) for k in ("commit", "granted", "tags", "follow")}
    stage = pathlib.Path(__import__("tempfile").mkdtemp(prefix="service-update-", dir=hubconf.private_tmp()))
    try:
        say(_("{name}: fetching {ref} …", name=name, ref=target))
        commit, doc = _fetch_checked(url, target, stage / "repo.git", allow_local,
                                     expect=every.get(target) if is_tag else None, tag=is_tag)
        if doc["name"] != name:
            return [_("{ref}'s manifest names {other}, not {name}", ref=target, other=doc["name"], name=name)]
        # how it follows from here: a release tag keeps following tags (a moved one, accepted by hand, replaces the
        # record); a branch, commit or other tag asked for by hand pins it (nothing moves it but another --ref)
        if is_tag and svc.release(target) is not None:
            follow = "tags"
            tags = {target: commit} if moved else {**(entry.get("tags") or {}), target: commit}
        else:
            follow, tags = "none", {}
        if commit == entry["commit"]:
            if not ref or (entry.get("follow"), entry.get("tags"), entry.get("ref")) == (follow, tags, target):
                return []
            with svc.lock(name):   # the same code, asked for by another name: only how it follows changes
                now = svc.entries().get(name)
                if now is None or {k: now.get(k) for k in checked} != checked:
                    return [_("{name} changed while it was being checked (removed, re-added or updated); run it "
                              "again", name=name)]
                _write_entry({**now, "ref": target if not service_fetch.HEX.match(target) else None,
                              "follow": follow, "tags": tags})
            _commit(name, f"feat(services): {name} follows {target}")
            say(_("{name} is current", name=name))
            return []
        more = asks_more(entry, doc)
        granted = entry["granted"]
        values: dict[str, str] = {}
        if more:
            if auto:
                return [_("{ref} asks for more than was granted: {more}; run brunna service update {name} to see and "
                          "grant it", ref=target, more="; ".join(more), name=name)]
            show_asks(doc)
            say(_("It asks for more than you granted: {more}", more="; ".join(more)))
            if not yes and not _ask_yes(_("Grant this and update {name}?", name=name)):
                return [_("not updated")]
            new = {s["name"] for s in doc.get("secrets") or []} - set(granted.get("secrets") or [])
            unset = {s["name"] for s in doc.get("secrets") or [] if not s.get("optional")
                     and not _secret_set(name, s["name"])}
            got = _ask_secrets(doc, only=new | unset)
            if isinstance(got, str):
                return [_("not updated: {why}", why=got)]
            values = got
            granted = _granted(doc)
        with svc.lock(name):
            now = svc.entries().get(name)   # read again under the lock: what was checked must still be what it is
            if now is None or {k: now.get(k) for k in checked} != checked:
                return [_("{name} changed while it was being checked (removed, re-added or updated); run it again",
                          name=name)]
            for secret, v in values.items():
                _write_secret(name, secret, v)
            f = svc.folders(name, commit)
            keep = {entry["commit"], entry.get("previous")}
            for c in list(f["code_root"].iterdir()) if f["code_root"].is_dir() else []:
                if c.name not in keep:
                    _rmtree(c)   # left by a run that was stopped
            ok = False
            try:
                service_fetch.checkout(stage / "repo.git", commit, f["code"])
                if (why := _setup(name, commit, f["code"], list(doc.get("setup") or []))):
                    return [_("{ref} wasn't taken: {why}", ref=target, why=why)]
                ok = True
            finally:
                if not ok:
                    _rmtree(f["code"])
            new_entry = _entry(doc, url, target, commit, granted, follow, tags, entry["commit"], entry["added"])
            _write_entry(new_entry)   # first: a failure after this leaves the new version's entry and code, working
            _rmtree(f["repo"])
            shutil.move(str(stage / "repo.git"), str(f["repo"]))
            for c in list(f["code_root"].iterdir()):
                if c.name not in (commit, entry["commit"]):
                    _rmtree(c)
        _commit(name, f"feat(services): update {name} to {target} ({commit[:12]})")
        say(_("{name} updated to {ref} ({commit})", name=name, ref=target, commit=commit[:12]))
        return []
    except (service_fetch.FetchError, svc.Locked, OSError) as e:
        return [_("{ref} wasn't taken: {why}", ref=target, why=e)]
    finally:
        _rmtree(stage)


def cmd_update(args: list[str]) -> int:
    if args == ["--all"]:
        import alert
        found = False
        told = True
        names = {n for n, e in svc.entries().items() if e.get("follow") == "tags"}
        for name in sorted(names):
            problems = update_one(name, auto=True)
            if problems is None:
                continue
            for p in problems:
                err(f"⚠️ {name}: {p}")
            found |= bool(problems)
            told &= alert.once(f"service-update:{name}", problems,
                               _("{name} is up to date again", name=name))
        for key in alert.open_keys("service-update:"):
            if key.split(":", 1)[1] not in names:   # removed, or no longer following tags
                told &= alert.once(key, [], _("{name} is up to date again", name=key.split(":", 1)[1]))
        if not found:
            return 0 if told else 1
        return alert.FOUND if told else 1
    ref, yes, rest = None, False, []
    it = iter(args)
    for a in it:
        if a == "--ref":
            ref = next(it, None)
        elif a == "--yes":
            yes = True
        else:
            rest.append(a)
    if len(rest) != 1 or (ref is not None and not ref):
        return err(_("usage: brunna service update <name> [--ref <tag|branch|commit>] [--yes] | --all"))
    problems = update_one(rest[0], ref=ref, yes=yes) or []
    for p in problems:
        err(p)
    if not problems:
        say(_("{name} is current", name=rest[0]))
    return 1 if problems else 0


def _entry_path(name: str) -> pathlib.Path:
    return hubconf.services_dir() / f"{name}.yaml"


def _write_entry(entry: dict) -> None:
    import yaml
    d = hubconf.services_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{entry['name']}.yaml.tmp"
    tmp.write_text(yaml.safe_dump(entry, sort_keys=False, allow_unicode=True))
    os.replace(tmp, _entry_path(entry["name"]))


def cmd_remove(args: list[str]) -> int:
    purge = "--purge" in args
    rest = [a for a in args if a != "--purge"]
    if len(rest) != 1:
        return err(_("usage: brunna service remove <name> [--purge]"))
    name = rest[0]
    if not svc.NAME.match(name):
        return err(_("{name} is not an installed service", name=name))
    if name not in svc.entries() and not _entry_path(name).exists():
        return err(_("{name} is not an installed service", name=name))
    try:
        with svc.lock(name):
            f = svc.folders(name)
            for k in ("repo", "code_root", "cache"):
                _rmtree(f[k])
            if purge:
                _rmtree(f["state"])
                _rmtree(f["secrets"])
                try:
                    f["base"].rmdir()
                except OSError:
                    pass
            _entry_path(name).unlink(missing_ok=True)
    except svc.Locked as e:
        return err(str(e))
    _commit(name, f"feat(services): remove {name}")
    say(_("{name} removed", name=name) + ("" if purge else _(" (its state and secrets kept: --purge removes them)")))
    return 0


def cmd_callers(args: list[str]) -> int:
    """Who may call a service: named projects, or --all (every registered project)."""
    if len(args) < 2 or not svc.NAME.match(args[0]):
        return err(_("usage: brunna service callers <name> <project>… | --all"))
    name, who = args[0], args[1:]
    try:
        with svc.lock(name):
            e = svc.entries().get(name)
            if not e:
                return err(_("{name} is not an installed service", name=name))
            if who == ["--all"]:
                e.pop("callers", None)
            else:
                unknown = [p for p in who if p not in hubconf.projects()]
                if unknown:
                    return err(_("not registered projects: {names}", names=", ".join(unknown)))
                e["callers"] = sorted(set(who))
            _write_entry(e)
    except svc.Locked as x:
        return err(str(x))
    _commit(name, f"feat(services): who may call {name}")
    say(_("{name} may be called by {who}", name=name,
          who=_("every registered project") if who == ["--all"] else ", ".join(sorted(set(who)))))
    return 0


def cmd_list(args: list[str]) -> int:
    es = svc.entries()
    if "--json" in args:
        print(json.dumps(list(es.values()), ensure_ascii=False, indent=1))
        return 0
    if not es:
        say(_("No installed services."))
    calls = _calls_this_week()
    for name, e in es.items():
        say(f"{name}  {e.get('ref') or '-'}  {e['commit'][:12]}  interface {e['interface']}  {e['url']}")
        _say_calls(calls.get(name))
    import service_index
    try:
        own = {n: s for n, s in service_index.services().items() if s.kind == "project"}
    except Exception:   # noqa: BLE001
        own = {}
    for name, s in sorted(own.items()):
        say(_("{name}  your project's service (interface {v}), runs with your rights in {path}", name=name,
              v=s.interface, path=s.cwd))
        _say_calls(calls.get(name))
    for p in svc.check():
        err(p)
    return 0


def _calls_this_week() -> dict[str, dict[str, int]]:
    """{service: {consumer: calls}} over the last 7 days (<state>/services/calls.jsonl)."""
    since = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    out: dict[str, dict[str, int]] = {}
    try:
        lines = (hubconf.STATE / "services" / "calls.jsonl").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and str(r.get("at", "")) >= since:
            per = out.setdefault(str(r.get("service")), {})
            per[str(r.get("consumer"))] = per.get(str(r.get("consumer")), 0) + 1
    return out


def _say_calls(per: dict[str, int] | None) -> None:
    if per:
        say(_("  calls this week: {calls}", calls=", ".join(f"{c} {n}" for c, n in sorted(per.items()))))


def cmd_secret(args: list[str]) -> int:
    if len(args) != 2:
        return err(_("usage: brunna service secret <name> <SECRET> (the value on stdin)"))
    name, secret = args
    e = svc.entries().get(name)
    if not e:
        return err(_("{name} is not an installed service", name=name))
    if secret not in e["granted"]["secrets"]:
        return err(_("{secret} is not one of {name}'s secrets", secret=secret, name=name))
    value = getpass.getpass(f"{secret}: ") if sys.stdin.isatty() else sys.stdin.read().rstrip("\n")
    if not value:
        return err(_("no value given"))
    _write_secret(name, secret, value)
    say(_("{secret} set for {name}", secret=secret, name=name))
    return 0


def cmd_run(args: list[str]) -> int:
    if not args:
        return err(_("usage: brunna service run <name> [-- <args…>]"))
    name, rest = args[0], args[1:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if not svc.NAME.match(name):
        return err(_("{name} is not an installed service", name=name))
    try:
        with svc.lock(name, shared=True):   # no swap or remove under a running CLI
            e = svc.entries().get(name)
            if not e:
                return err(_("{name} is not an installed service", name=name))
            try:
                words = shlex.split(e["run"]["cli"])
            except ValueError as x:
                return err(_("{name}'s CLI command can't be parsed ({err})", name=name, err=x))
            f = svc.folders(name, e["commit"])
            return _sandboxed(name, "run", f["code"], [*words, *rest])
    except svc.Locked as x:
        return err(str(x))


VERBS = {"add": cmd_add, "remove": cmd_remove, "list": cmd_list, "secret": cmd_secret, "run": cmd_run,
         "update": cmd_update, "callers": cmd_callers}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help") or argv[0] not in VERBS:
        print(_(__doc__))
        return 0 if argv[:1] in ([], ["-h"], ["--help"], ["help"]) else 2
    if argv[0] != "list" and (why := guard()):
        return err(why)
    import signal
    signal.signal(signal.SIGTERM, _on_term)
    return VERBS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
