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

Updates (step 2b), other projects calling it (step 3) and its questions to you (step 4) aren't built yet.
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
ALLOW_LOCAL = svc.names.ENV_NEW + "SERVICE_ALLOW_LOCAL"   # tests: a local path or file:// URL may be fetched


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


def _sandboxed(name: str, phase: str, cwd: pathlib.Path, words: list[str], timeout: int | None = None) -> int:
    """Run in the service's sandbox, in a process group of its own, killed whole when the command ends (or times out):
    nothing it started in the background outlives it (a setup's leftover would keep writing its code folder)."""
    import signal
    cmd = [sys.executable, "-I", "-B", str(BIN / "sandboxed-run"), "--installed", name, "--phase", phase,
           "--cwd", str(cwd), "--", *words]
    p = subprocess.Popen(cmd, start_new_session=True)
    try:
        return p.wait(timeout=timeout)
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()


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
    allow_local = os.environ.get(ALLOW_LOCAL) == "1"
    stage = pathlib.Path(__import__("tempfile").mkdtemp(prefix="service-add-", dir=hubconf.private_tmp()))
    try:
        return _add(url, ref, yes, allow_local, stage)
    finally:
        _rmtree(stage)


def _add(url: str, ref: str | None, yes: bool, allow_local: bool, stage: pathlib.Path) -> int:
    say(_("Fetching {url} …", url=url))
    try:
        commit = service_fetch.fetch(url, ref, stage / "repo.git", allow_local=allow_local)
        text = service_fetch.manifest(stage / "repo.git", commit)
    except service_fetch.FetchError as e:
        return err(_("not added: {why}", why=e))
    doc, problems = service_manifest.parse(text)
    problems = problems or service_manifest.problems(doc)
    if problems:
        err(_("not added: its {file} has problems:", file=service_manifest.FILE))
        for p in problems:
            err(f"  {p}")
        return 1
    name = doc["name"]
    if (why := svc.clash(name)):
        return err(_("not added: the name {name} is taken ({why})", name=name, why=why))
    show_asks(doc)
    if not yes and not _ask_yes(_("Grant this and install {name}?", name=name)):
        return err(_("not added"))
    scripts = False
    values: dict[str, str] = {}
    for s in doc.get("secrets") or []:
        how = f" ({s['how']})" if s.get("how") else ""
        if not sys.stdin.isatty() and not s.get("optional"):
            return err(_("not added: {secret} is required and there is no terminal to ask it on; run add at your "
                         "terminal", secret=s["name"]))
        if not sys.stdin.isatty():
            continue
        v = getpass.getpass(_("{secret}{how}{opt}: ", secret=s["name"], how=how,
                              opt=_(" (Enter to skip)") if s.get("optional") else ""))
        if v:
            values[s["name"]] = v
        elif not s.get("optional"):
            return err(_("not added: {secret} is required", secret=s["name"]))
    f = svc.folders(name, commit)
    base = f["base"]
    try:
        base.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        return err(_("not added: {path} already exists", path=base))
    ok = False
    try:
        shutil.move(str(stage / "repo.git"), str(f["repo"]))   # private_tmp may be on another volume
        service_fetch.checkout(f["repo"], commit, f["code"])
        for d in ("cache", "state", "home", "tmp"):
            f[d].mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = {"v": 1, "name": name, "url": url, "ref": ref if ref and not service_fetch.HEX.match(ref) else None,
                 "commit": commit, "interface": doc["interface"],
                 "run": {"cli": doc["cli"]["run"], "mcp": doc["mcp"]["run"], "setup": list(doc.get("setup") or [])},
                 "granted": {"models": doc.get("models") or {}, "secrets": [s["name"] for s in doc.get("secrets") or []],
                             "principal": bool(doc.get("principal")), "scripts": scripts,
                             "network": list(doc.get("network") or [])},
                 "added": datetime.date.today().isoformat()}
        for secret, v in values.items():
            _write_secret(name, secret, v)
        # the entry is written before setup so sandboxed-run can find it; removed again if setup fails
        _write_entry(entry)
        for c in entry["run"]["setup"]:
            say(_("setup: {cmd}", cmd=c))
            try:
                code = _sandboxed(name, "setup", f["code"], ["/bin/sh", "-c", c], timeout=SETUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                return err(_("not added: setup took longer than {m} minutes", m=SETUP_TIMEOUT // 60))
            if code:
                return err(_("not added: setup failed ({cmd}, exit {code})", cmd=c, code=code))
        ok = True
    except (service_fetch.FetchError, OSError) as e:
        return err(_("not added: {why}", why=e))
    finally:
        if not ok:
            _entry_path(name).unlink(missing_ok=True)
            _rmtree(base)
    _commit(name, f"feat(services): add {name} at {commit[:12]}")
    say(_("{name} is installed ({commit}). Try it: brunna service run {name} -- --help", name=name, commit=commit[:12]))
    return 0


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
    _commit(name, f"feat(services): remove {name}")
    say(_("{name} removed", name=name) + ("" if purge else _(" (its state and secrets kept: --purge removes them)")))
    return 0


def cmd_list(args: list[str]) -> int:
    es = svc.entries()
    if "--json" in args:
        print(json.dumps(list(es.values()), ensure_ascii=False, indent=1))
        return 0
    if not es:
        say(_("No installed services."))
    for name, e in es.items():
        say(f"{name}  {e.get('ref') or '-'}  {e['commit'][:12]}  interface {e['interface']}  {e['url']}")
    for p in svc.check():
        err(p)
    return 0


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
    e = svc.entries().get(name)
    if not e:
        return err(_("{name} is not an installed service", name=name))
    try:
        words = shlex.split(e["run"]["cli"])
    except ValueError as x:
        return err(_("{name}'s CLI command can't be parsed ({err})", name=name, err=x))
    f = svc.folders(name, e["commit"])
    return _sandboxed(name, "run", f["code"], [*words, *rest])


VERBS = {"add": cmd_add, "remove": cmd_remove, "list": cmd_list, "secret": cmd_secret, "run": cmd_run}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help") or argv[0] not in VERBS:
        print(_(__doc__))
        return 0 if argv[:1] in ([], ["-h"], ["--help"], ["help"]) else 2
    if argv[0] != "list" and (why := guard()):
        return err(why)
    return VERBS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
