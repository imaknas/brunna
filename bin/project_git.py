#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The platform's git work in project repos (docs/design/projects-and-repos.md section 4, "Platform git in project
repos"): the platform runs git only in repos it owns (the memory bare repos, the librarian's memory copies, its own
checkout); in a project's repo every git command runs as that project, through project-git-job.py started with
isolation.as_project (sandboxed when the project is). What comes back is data on its stdout (never a file in the
project's folder, which could be a symlink or a FIFO): JSON parsed here with types checked, or a bundle fetched into a
repo the platform owns with transfer.fsckObjects on. Every job has a timeout (its whole process group is killed: a
`[include] path=<fifo>` in the repo's config hangs any git) and an output cap. Nothing derived from the output is used
for a write outside the project's own job.

  job(project, cwd, op, *args, input, out, timeout, cap) → Result
                                                            one op of project-git-job.py as the project: returncode,
                                                            stdout (bytes; streamed into `out` when given), stderr
  data(project, cwd, op, *args) → object                    its JSON output; raises Unavailable (sandbox refused,
                                                            git failed, malformed output)
  fetch_bundle(bare, bundle, branch) → None | why not       fast-forward <bare>'s branch to the bundle's HEAD, every new commit
                                                            holding only what a memory branch may (memory_paths)
  memory_project(dir) → (name, doc) | None                  whose .agent-memory this is
  project_at(dir) → (name, doc) | None                      whose workspace holds this folder (by path, no git)
  run(project, cwd, argv, …) → Result                       any argv as the project, bounded like job
  sync_memory(name, doc, memdir) → None | why not           a sandboxed project's memory sync (commit + merge + bundle
                                                            as the project; the platform fetches the bundle). A bundle
                                                            refused for what it holds is an alert (once, reminded daily),
                                                            cleared by the next clean sync
  sync_memory_of(name, ws) → None | why not                 a platform script's sync of a project's memory, either kind
  memory_remote(memdir) → (remote | None, why not)          the remote an unsandboxed project's notes go to: the memory branch's, when
                                                            it is the platform's memory repo by its URL
  code_remotes_with_notes(name, doc) → [remote]             remotes of the project's that are known here to have the memory branch and
                                                            aren't the platform's

CLI (git-sync-memory.sh, sync-all-memory.sh):
  project_git.py sandboxed-project <name>   exit 0 when this registered project is sandboxed, 3 when not (any
                                      other exit: it couldn't tell)
  project_git.py run-as <name> <dir> -- <argv…>
                                      a shell hook's work as the project <dir> belongs to (HUB_AS_PROJECT=1 set, stdin
                                      and output passed through); exit 3 when it isn't sandboxed
  project_git.py memory-remote <dir>  memory_remote's name on stdout; exit 8 (memory_paths.REMOTE_BAD) with the reason on stderr
  project_git.py sync-memory [--lock] <name>
                                      sync_memory for that project (exit 1 with the reason on stderr); --lock: the job
                                      takes with-lock.sh's lock (a caller under with-lock.sh holds it already)
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import gitdirs  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import names  # noqa: E402
import isolation  # noqa: E402
import memory_paths  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402

_ = i18n.domain(__file__)

JOB = BIN / "project-git-job.py"
MAX_JSON = 8 << 20          # facts
MAX_BUNDLE = 4 << 30        # a bundle of unpushed work or memory
TIMEOUT_S = 120
BUNDLE_TIMEOUT_S = 1800
TIMED_OUT, TOO_BIG = 124, 125
INCOMING = names.repo_ref("incoming")
NOT_SANDBOXED = 3
COULD_NOT_TELL = 5   # the name isn't in the registry's index (a stale one, say): it may be sandboxed, so nothing runs as the user
EX_SANDBOX = 78   # sandboxed-run refused (no sandbox on this machine): the work is not done, never done unsandboxed


class Unavailable(Exception):
    pass


class Result(NamedTuple):
    returncode: int
    stdout: bytes
    stderr: str


def job(project: str | None, cwd, op: str, *args, **kw) -> Result:
    """One op of project-git-job.py as the project (run's keywords)."""
    return run(project, cwd, [*hubconf.launch(JOB), op, *map(str, args)], **kw)


def run(project: str | None, cwd, argv: list[str], input: str | bytes | None = None, out=None, timeout: float = TIMEOUT_S,
        cap: int | None = None, env: dict | None = None) -> Result:
    """Run argv as the project. Over `timeout` seconds or `cap` bytes of stdout (default MAX_JSON), the whole process
    group is killed (returncode TIMED_OUT / TOO_BIG); stdout goes into the binary file `out` when given."""
    cap = MAX_JSON if cap is None else cap
    argv = isolation.as_project(project, cwd, argv)
    p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, env=job_env(env))
    if input is not None:
        p.stdin.write(input if isinstance(input, bytes) else input.encode())
        p.stdin.close()
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ, "out")
    sel.register(p.stderr, selectors.EVENT_READ, "err")
    got, err, n, why = [], bytearray(), 0, None
    deadline = time.monotonic() + timeout
    while sel.get_map() and why is None:
        left = deadline - time.monotonic()
        if left <= 0:
            why = (TIMED_OUT, f"timed out after {timeout:g}s")
            break
        for key, _mask in sel.select(left):
            chunk = os.read(key.fileobj.fileno(), 1 << 16)
            if not chunk:
                sel.unregister(key.fileobj)
            elif key.data == "err":
                err += chunk[:max(0, 65536 - len(err))]
            else:
                n += len(chunk)
                if n > cap:
                    why = (TOO_BIG, f"output over {cap} bytes")
                    break
                if out is not None:
                    out.write(chunk)
                else:
                    got.append(chunk)
    if why is None:
        try:   # stdout closed is not the end: a process left behind may still run
            rc = p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            why = (TIMED_OUT, f"timed out after {timeout:g}s")
    if why:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        rc = p.wait()
    for fo in (p.stdout, p.stderr):
        fo.close()
    sel.close()
    return Result(why[0] if why else rc, b"".join(got), why[1] if why else err.decode(errors="replace"))


def real_git() -> str | None:
    """The git a project's job runs, resolved here by its absolute path: sandboxed-run puts the platform's git wrapper
    first on PATH for a project whose git goes through the platform (enforce.git: platform), and the wrapper refuses
    what the platform's own ops need (bundle, worktree…)."""
    wrapper = (hubconf.ROOT / "bin" / "git-wrapper").resolve()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d) or pathlib.Path(d).resolve() == wrapper or pathlib.Path(d).name == "git-wrapper":
            continue
        g = os.path.join(d, "git")
        if os.path.isfile(g) and os.access(g, os.X_OK):
            return g
    return None


IDENTITY = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


def job_env(extra: dict | None = None) -> dict[str, str]:
    """A job's environment, built, never the caller's: whoever starts it (the owner's terminal, a hook, a service) may
    hold tokens a project's code would see (git status runs the repo's fsmonitor in the job). isolation.kept_env, as
    isolation.run_as_project, plus the git identity a commit made here needs (memory-sync), the real git's path and
    what the caller passes."""
    env = {**isolation.kept_env(), **{k: os.environ[k] for k in IDENTITY if k in os.environ}}
    if (g := real_git()):
        names.env_set(env, "GIT_BIN", g)
    return {**env, **(extra or {})}


def data(project: str | None, cwd, op: str, *args, **kw):
    """One op's JSON (run's keywords: input, timeout…); Unavailable when it failed or printed something else."""
    r = job(project, cwd, op, *args, **kw)
    if r.returncode == EX_SANDBOX:
        raise Unavailable(f"{project}: the sandbox refused ({r.stderr.strip()[-200:]})")
    if r.returncode != 0:
        raise Unavailable(f"{project}: {op} failed ({r.stderr.strip()[-200:]})")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise Unavailable(f"{project}: {op} output is not JSON") from None


def strs(x) -> list[str]:
    return [s for s in x if isinstance(s, str)] if isinstance(x, list) else []


def owned_git(repo, *args) -> subprocess.CompletedProcess:
    """git in a bare repo the platform owns. Named with --git-dir, never entered with -C: the owner's
    safe.bareRepository=explicit (the git broker's precondition) refuses a bare repo git finds on its own."""
    return subprocess.run(["git", "--git-dir", str(repo), *args], capture_output=True, text=True)


REFUSED = "refused:"   # how fetch_bundle's refusal for what a bundle holds begins


def refusal_alert(name: str, why: str | None) -> None:
    """A sync refused for what it holds stops a project's notes until someone rewrites its branch: the owner hears of it once, is reminded
    every 24 hours, and hears when it syncs clean again. A broken alert doesn't change the outcome."""
    try:
        import alert
        alert.once(f"memory-sync-refused:{name}", [_("{project}: its memory notes don't reach the platform: {why}", project=name,
                                                     why=memory_paths.one_line(memory_paths.scrub(why), 600))] if why else [],
                   _("{project}: its memory notes sync again", project=name), ids=["memory-sync-refused"] if why else [])
    except Exception as e:
        print(f"project_git: the alert was not sent: {e!r}", file=sys.stderr)


def fetch_bundle(bare: pathlib.Path, bundle: pathlib.Path, branch: str) -> str | None:
    """Fast-forward <bare>'s refs/heads/<branch> to the bundle's HEAD. The bundle is untrusted data: objects are
    checked (fsck), anything but a fast-forward is refused (the next round merges first), and so is a new commit holding what a memory
    branch may not (memory_paths): git plumbing gets past the job's commit of memory/ and STATE.md, and agents run as the user in clones
    of this branch. Nothing of it lands, and the reason goes where any other failed sync's does. A later commit removing the path
    doesn't clear it: those commits have to go from the project's branch."""
    fsck = ["-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true"]
    r = owned_git(bare, *fsck, "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(bundle), f"+HEAD:{INCOMING}")
    if r.returncode != 0:
        return f"fetching the bundle failed: {r.stderr.strip()[-300:]}"
    try:
        new = owned_git(bare, "rev-parse", "--verify", "-q", INCOMING).stdout.strip()
        old = owned_git(bare, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").stdout.strip()
        if old and owned_git(bare, "merge-base", "--is-ancestor", old, new).returncode != 0:
            return f"not a fast-forward of {branch} (the bare repo moved on; the next round merges it first)"
        if (bad := memory_paths.new_commit_problems(bare, old or None, new)):
            return (f"{REFUSED} new commits on {branch} hold what a memory branch may not: {'; '.join(bad)}. It holds "
                    f"{', '.join(memory_paths.TOP)} and {memory_paths.TREE}/**, plain files; those commits have to go from the branch")
        r = owned_git(bare, "update-ref", f"refs/heads/{branch}", new, old)
        return None if r.returncode == 0 else f"update-ref failed: {r.stderr.strip()[-300:]}"
    finally:
        owned_git(bare, "update-ref", "-d", INCOMING)


def memory_project(d) -> tuple[str, dict] | None:
    real = pathlib.Path(d).resolve()
    for name, doc in hubconf.projects().items():
        ws = hubconf.path((doc.get("paths") or {}).get("workspace"))
        if ws and (ws / ".agent-memory").resolve() == real:
            return name, {**doc, "name": name}
    return None


def project_at(d) -> tuple[str, dict] | None:
    """The registered project whose workspace holds this folder (its worktrees under it included); by path only, no
    git (a repo's config can hang even rev-parse)."""
    real = pathlib.Path(d).resolve()
    best = None
    for name, doc in hubconf.projects().items():
        ws = hubconf.path((doc.get("paths") or {}).get("workspace"))
        if ws and real.is_relative_to(ws.resolve()) and (best is None or len(ws.parts) > len(best[2].parts)):
            best = (name, {**doc, "name": name}, ws)
    return best[:2] if best else None


def sandbox_of(cwd) -> tuple[str, dict] | None:
    """The sandboxed project this folder is under, the nearest, as a hook's cwd names it or as it resolves (gitdirs.holder). As
    named comes first: a folder a sandboxed project owns can be swapped for a link after the runtime took it as the session's
    folder, and a hook's input still names it, so what it resolves to says nothing about whose session this is."""
    docs = {name: {**doc, "name": name} for name, doc in hubconf.projects().items()}
    roots = {name: ws for name, doc in docs.items()
             if (ws := hubconf.path((doc.get("paths") or {}).get("workspace"))) and isolation.sandboxed(doc)}
    name = gitdirs.holder(cwd, roots)
    return (name, docs[name]) if name else None


def holds(doc: dict, folder) -> bool:
    """Whether this project's workspace really holds the folder, resolved (a sandbox_of by name may not)."""
    ws = hubconf.path((doc.get("paths") or {}).get("workspace"))
    return ws is None or gitdirs.holder(pathlib.Path(folder).resolve(), {"": ws}) is not None


def registered_main(main) -> bool:
    """Whether this folder is the checkout of the hub or of a registered project that isn't sandboxed."""
    m = pathlib.Path(main).resolve()
    return m == hubconf.MAIN.resolve() or any(
        (ws := hubconf.path((d.get("paths") or {}).get("workspace"))) and ws.resolve() == m and not isolation.sandboxed({**d, "name": n})
        for n, d in hubconf.projects().items())


class Scope(NamedTuple):
    memory_ok: bool           # git in its project's memory, as the user
    folder_ok: bool           # git in this very folder, as the user
    memdir: pathlib.Path      # that memory, from the same look


def scope_of(folder) -> Scope:
    """What a hook may run as the user in this folder, from one look at its pointers: looking twice lets them change in between.

    memory_ok: git reads the repo's config and runs what it names, so yes only for what isn't a stranger's: a registered project that
    isn't sandboxed, the hub, or a linked worktree of one of those that it confirms (a repo a stranger made confirms itself, so its
    being registered is what counts). Everything else is no, not a list of bad cases.
    folder_ok: memory_ok, and a folder only its owner can write, since a .git file and all beside it can change between a look and
    the git. In nothing a sandboxed project can write; then a registered project's, or a linked worktree under its main checkout's
    folder or under workspace_root (no sandbox writes there but its own folder, policy.py). Anywhere else, the temp folder
    included, it is no.
    memdir: a confirmed linked worktree's memory is its main checkout's, whatever sits beside it, others their own."""
    found, at = gitdirs.dirs(folder), project_at(folder)
    is_confirmed = bool(found) and gitdirs.confirmed(found)
    memdir = found.common.parent / ".agent-memory" if is_confirmed else pathlib.Path(folder) / ".agent-memory"
    if at:
        memory_ok = not isolation.sandboxed(at[1])
    else:
        memory_ok = bool(found) and (found.top == hubconf.MAIN.resolve() or (is_confirmed and registered_main(found.common.parent)))
    folder_ok = False
    if memory_ok and not sandbox_of(folder) and not gitdirs.holder(pathlib.Path(folder).resolve(), sandbox_writes()):
        folder_ok = bool(at) or (bool(found) and gitdirs.holder(found.top, {"main": found.common.parent,
                                                                            "root": hubconf.p("workspace_root", "~/workspace")}) is not None)
    return Scope(memory_ok, folder_ok, memdir)


def user_git_ok(folder) -> bool:
    return scope_of(folder).memory_ok


def sandbox_writes() -> dict:
    """What the sandboxed projects' own processes can write besides the temp folder: {path: path}, for gitdirs.holder."""
    out = {}
    for name, doc in hubconf.projects().items():
        doc = {**doc, "name": name}
        if isolation.sandboxed(doc):
            paths = doc.get("paths") or {}
            d = registry.dirs(doc) if paths.get("workspace") else {}
            out.update({str(p): p for p in (d.get("workspace"), d.get("runtime"), *map(hubconf.path, doc.get("allow_write") or [])) if p})
    return out


def folder_git_ok(folder) -> bool:
    return scope_of(folder).folder_ok


def memory_dir(folder) -> pathlib.Path:
    return scope_of(folder).memdir


def sync_memory_of(name: str, ws) -> str | None:
    """A platform script's sync of this project's memory: as the project when it is sandboxed (with-lock.sh's own rev-parse reads the
    repo's config, which can hang on a FIFO), otherwise the hook's path. Returns why it didn't sync (the next hourly sync tries again),
    and so for a name the registry's index doesn't have."""
    doc = hubconf.projects().get(name)
    if doc is None:
        return f"{name} is not in the registry's index; whether it is sandboxed can't be told, memory not synced"
    mem = pathlib.Path(ws) / ".agent-memory"
    if isolation.sandboxed({**doc, "name": name}):
        return sync_memory(name, doc, mem, lock=True)
    import memory_health
    rc, said = memory_health.sync(mem, name)
    if said:
        print(memory_paths.quoted(said, memory_paths.SAID), file=sys.stderr)
    return None if rc == 0 else f"{name}: the memory sync failed ({memory_paths.quoted(said, memory_paths.SAID)})"


MEMORY_REMOTES = ("memory", "mini")   # what onboarding and recover call it (hubpaths.sh MEMORY_REMOTE), and an older checkout


def _url_parts(url: str) -> tuple[str, str]:
    """(host, path) of a git URL, whatever its spelling: a local path has no host; user, port and the host's case don't tell
    one repo from another."""
    m = re.match(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/]*)(/.*)?$", url)
    if m:
        host = m[2].rpartition("@")[2].partition(":")[0].casefold()
        if m[1].casefold() == "file":
            return "", m[3] or ""
        return host, (m[3] or "").removeprefix("/") if (m[3] or "").startswith("/~") else (m[3] or "")
    host, sep, path = url.partition(":")
    if url.startswith("/") or not sep or "/" in host:
        return "", url
    return host.rpartition("@")[2].casefold(), path


def _memory_url_ok(url: str, name: str | None, docs: dict) -> bool:
    """Whether this is the platform's memory repo: the project's own, or one under the memory repos' folder that is no other
    project's (an older name's); on the machine that only reaches the hub machine over ssh, a memory repo there."""
    own = [policy.memory_bare(name, docs[name])] if name in docs else []
    others = {os.path.realpath(policy.memory_bare(n, d)) for n, d in docs.items() if n != name}
    root = os.path.realpath(hubconf.p("git_origin", "~/git-origin"))
    host, path = _url_parts(url)
    path = path.rstrip("/")
    if not host:
        if not path.startswith("/"):
            return False
        real = os.path.realpath(path)
        return real in {os.path.realpath(p) for p in own} \
            or (os.path.dirname(real) == root and real.endswith("-memory.git") and real not in others)
    return host == hubconf.host("server").rpartition("@")[2].casefold() and path.endswith("-memory.git")


def _urls(cwd, remote: str, *flags) -> list[str]:
    r = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "--all", *flags, remote], capture_output=True, text=True, timeout=30)
    return r.stdout.splitlines() if r.returncode == 0 else []


def memory_remote(memdir) -> tuple[str | None, str | None]:
    """git runs in the project's repo: only where it may run as the user. In a worktree of the project's repo, which shares
    its remotes, by the platform's names for it; a clone of the memory repo (.git a folder) calls it what it likes. By the
    URL in both."""
    r = subprocess.run(["git", "-C", str(memdir), "config", "--get", f"branch.{memory_paths.BRANCH}.remote"], capture_output=True,
                       text=True, timeout=30)
    remote = r.stdout.strip()
    found = memory_project(memdir)
    urls = [*_urls(memdir, remote, "--push"), *_urls(memdir, remote)] if remote else []
    named = remote in MEMORY_REMOTES or (pathlib.Path(memdir) / ".git").is_dir()
    if named and urls and all(_memory_url_ok(u, found[0] if found else None, hubconf.projects()) for u in urls):
        return remote, None
    shown = ", ".join(json.dumps(memory_paths.one_line(memory_paths.scrub(u), 120)) for u in dict.fromkeys(urls)) or "-"
    return None, _("the notes in {path} are not pushed: their branch {branch} follows {remote} ({url}), which is not the platform's "
                   "memory repo. Set it back with: git -C '{path}' config branch.{branch}.remote {memory} (its URL is {own})",
                   path=memdir, branch=memory_paths.BRANCH, remote=json.dumps(memory_paths.one_line(memory_paths.scrub(remote), 60)) if remote else "-",
                   url=shown, memory=MEMORY_REMOTES[0], own=policy.memory_bare(*found) if found else "-")


def code_remotes_with_notes(name: str, doc: dict) -> list[str]:
    """The names of the project's remotes known here to have its memory branch that aren't the platform's memory repo, whichever way
    that is spelled. Raises Unavailable when the project's job couldn't say. The names are the project's own text."""
    ws = hubconf.path((doc.get("paths") or {}).get("workspace"))
    got = data(name, ws, "memory-remotes", ws, memory_paths.BRANCH)
    docs, own = hubconf.projects(), policy.memory_bare(name, doc).name
    out = []
    for remote, urls in sorted(got.items()) if isinstance(got, dict) else ():
        urls = strs(urls)   # none known: nothing to say
        # the platform's by its names for it and the repo's own name (a host it isn't known under any more) is not a code remote
        if not all(_memory_url_ok(u, name, docs) for u in urls) and not (
                remote in MEMORY_REMOTES and all(os.path.basename(_url_parts(u)[1].rstrip("/")) == own for u in urls)):
            out.append(remote)
    return out


def sync_memory(name: str, doc: dict, memdir, branch: str = "agent-memory", lock: bool = False) -> str | None:
    """lock: the job takes with-lock's lock itself (the hourly sync; a hook's call already holds it). How it went is recorded
    (memory_health)."""
    import memory_health
    why, rc = _sync_memory(name, doc, memdir, branch, lock)
    memory_health.record(name, why and memory_paths.scrub(why), rc, memdir)
    return why and memory_paths.scrub(why)


def _sync_memory(name: str, doc: dict, memdir, branch: str, lock: bool) -> tuple[str | None, int]:
    bare = policy.memory_bare(name, doc)
    if not (bare / "objects").is_dir():
        return f"{name}: no local memory bare repo at {bare}", 1
    try:
        tmp = tempfile.TemporaryDirectory(prefix="hub-memory-", dir=hubconf.private_tmp(), ignore_cleanup_errors=True)
    except OSError as e:
        return f"{name}: no folder for the bundle ({e})", 1
    with tmp as d:
        bundle = pathlib.Path(d) / "memory.bundle"
        with bundle.open("wb") as fh:
            r = job(name, memdir, "memory-sync", memdir, bare, branch, *(["--lock"] if lock else []),
                    "--rules=" + ",".join(memory_paths.rules_names()), out=fh, timeout=BUNDLE_TIMEOUT_S, cap=MAX_BUNDLE)
        err = memory_paths.quoted(r.stderr, memory_paths.SAID)   # the project's own text
        if r.returncode == EX_SANDBOX:
            return f"{name}: the sandbox refused; memory not synced ({err})", 1
        if r.returncode != 0:
            return f"{name}: the memory sync job failed ({err})", r.returncode
        if r.stderr.strip():
            print(memory_paths.quoted(r.stderr, 300), file=sys.stderr)   # what the job left out of the commit, said once
        why = fetch_bundle(bare, bundle, branch) if bundle.stat().st_size else None
        if why is None or why.startswith(REFUSED):
            refusal_alert(name, why)
        return (f"{name}: {why}", 1) if why else (None, 0)


def by_name(project: str) -> tuple[str, dict] | None:
    doc = hubconf.projects().get(project)
    return (project, {**doc, "name": project}) if doc else None


def run_as_cli(project: str, d: str, argv: list[str]) -> int:
    """A shell hook's work as this project in the folder d, by the name the hook already has, never asked again of a folder that can
    have changed: stdin passed in, its stdout and stderr passed out. Exit 3 when it isn't sandboxed (the caller goes on itself), and
    COULD_NOT_TELL for a name the index doesn't have (the caller goes no further)."""
    found = by_name(project)
    if not found:
        return _not_in_index(project)
    if not isolation.sandboxed(found[1]):
        return NOT_SANDBOXED
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    r = run(found[0], d, argv, input=stdin, env={"HUB_AS_PROJECT": "1"})
    sys.stdout.buffer.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


UNCONFIRMED = 4   # worktree_scope: its .git names a registered repo that doesn't list it (moved by hand)


def worktree_scope(top: str, main: str, cwd: str | None = None) -> int:
    """Whether a linked worktree's hook goes on as the user (worktree-memory.sh, through `hub-event worktree-scope`). cwd: what the
    hook's input names; top and main are read from the folder, and pointer files under top say main, so neither decides alone.
    0: a sandboxed project's, as the session's folder names it, or as top or main is (a repo it writes is never run as the
    user, whoever names it); its name and its folder, resolved, are printed, as its registry entry says.
    NOT_SANDBOXED: the hook goes on in main's memory, so main has to be what it said when it was read: the top may pass now with
    a main read a moment ago from a pointer since rewritten. UNCONFIRMED, or 1: nothing runs."""
    cage = (sandbox_of(cwd) if cwd else None) or sandbox_of(top)
    at_main = None if cage else memory_project(pathlib.Path(main) / ".agent-memory")
    cage = cage or (at_main if at_main and isolation.sandboxed(at_main[1]) else None)
    if cage:
        print(f"{cage[0]}\t{hubconf.path((cage[1].get('paths') or {}).get('workspace')).resolve()}")
        return 0
    if user_git_ok(top):
        m, at_top = pathlib.Path(main).resolve(), project_at(top)
        at_m = project_at(m)
        return NOT_SANDBOXED if registered_main(m) or (at_top and at_m and at_top[0] == at_m[0]) else 1
    found = gitdirs.dirs(top)
    return UNCONFIRMED if found and registered_main(found.common.parent) and not gitdirs.confirmed(found) else 1


def _not_in_index(name: str) -> int:
    print(f"{name} is not in the registry's index, so whether it is sandboxed can't be told", file=sys.stderr)
    return COULD_NOT_TELL


def main(argv: list[str]) -> int:
    if argv[:1] == ["run-as"] and len(argv) >= 5 and argv[3] == "--":
        return run_as_cli(argv[1], argv[2], argv[4:])
    if argv[:1] == ["memory-remote"] and len(argv) == 2:
        remote, why = memory_remote(pathlib.Path(argv[1]))
        if why:
            print(why, file=sys.stderr)
            return memory_paths.REMOTE_BAD
        print(remote)
        return 0
    lock = "--lock" in argv
    argv = [a for a in argv if a != "--lock"]
    if len(argv) != 2 or argv[0] not in ("sandboxed-project", "sync-memory"):
        print("usage: project_git.py sandboxed-project <name> | sync-memory [--lock] <name>", file=sys.stderr)
        return 2
    found = by_name(argv[1])
    if not found:
        return _not_in_index(argv[1])
    if argv[0] == "sandboxed-project":
        return 0 if isolation.sandboxed(found[1]) else NOT_SANDBOXED
    if not isolation.sandboxed(found[1]):   # this path is a sandboxed project's job: anything else, the caller's
        print(f"{argv[1]} is no sandboxed registered project", file=sys.stderr)
        return NOT_SANDBOXED
    why = sync_memory(*found, hubconf.path((found[1].get("paths") or {}).get("workspace")) / ".agent-memory", lock=lock)
    if why:
        print(why, file=sys.stderr)
    return 1 if why else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
