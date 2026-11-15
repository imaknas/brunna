#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""`brunna push`: the owner pushes a sandboxed project's branch to its registry remote, without running git in the
project's repo (docs/design/git-broker.md, "Owner-run push, built first (11-15)").

A sandboxed project can't reach the owner's credentials, so its pushes are the owner's. Run in the project's repo, the
owner's git reads that repo's `.git` (hooks, core.fsmonitor, core.sshCommand, credential.helper, filters…), which the
project can write: project-planted code run as the owner, outside every sandbox. Here instead:

  1. the project must be sandboxed (isolation.sandboxed), and its remote is the registry's paths.origin (owner-only):
     the only URL contacted. None, `(native)`, ext::/fd:: or a local path a sandbox can write is refused;
  2. the branch's commits are bundled AS THE PROJECT (project-git-job.py bundle-branch, through project_git.job): the
     only git that runs in the project's repo, sandboxed, its stdout a bundle in hubconf.private_tmp();
  3. the owner's git runs only in a bare repo the platform owns, `<state>/git-broker/<project>/remote.git` (init
     --bare --template=, config written here), always named with --git-dir, from that folder, with an environment
     built from scratch (KEEP_ENV: the owner's HOME, PATH, global config and agent, nothing else): ls-remote --symref
     and a fetch from paths.origin (hooks off, no submodules, no tags, fsck on), then the bundle, fetched through a
     staging bare repo so git's fsck checks every object it brings whatever the git version (a `.git` tree entry is
     refused there);
  4. policy: fast-forward only (no force of any kind), no tags, no deletes, one branch per call. The default branch
     (the remote's HEAD by ls-remote --symref; refused when that can't be told, e.g. an empty remote) is allowed: the
     owner runs this and sees the commits first. A pushed commit whose `.github` entry differs from the remote default
     branch's is refused unless the registry has enforce.git_ci: true, and then still listed first;
  5. the owner sees the range, each commit (short sha, author name and email, subject), the changed paths, `.github/`
     flagged and author emails the remote's recent history doesn't have flagged (the registry has no field for a
     project's email), all with control characters made visible, and confirms (y/N) unless --yes; with no terminal
     and no --yes it refuses;
  6. `push --no-verify <paths.origin> <sha>:refs/heads/<branch>` from the owned repo. The project's repo is never
     written: its remote-tracking ref stays where it was until its own next fetch;
  7. each push is a line in `<state>/git-broker/<project>/pushes.jsonl` (no bus event type fits one).

Exit: 0 pushed or nothing to push, 1 failed (git, network, the project's job), 2 refused (policy or usage).

    git_remote.py push <project> [<branch>] [--yes]
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import gitdirs  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import isolation  # noqa: E402
import names  # noqa: E402
import project_git  # noqa: E402

_ = i18n.domain(__file__)

OK, FAILED, REFUSED = 0, 1, 2
NOTHING, NO_BRANCH = 3, 4   # project-git-job.py bundle-branch
REMOTE = "refs/remotes/origin"
INCOMING = names.repo_ref("push-incoming")
MAX_COMMITS, MAX_PATHS, USUAL_DEPTH = 50, 100, 500
TIMEOUT_S = 1800
# the owner's environment, as far as their git needs it to reach the remote; nothing a project could have set for it
KEEP_ENV = ("HOME", "USER", "LOGNAME", "PATH", "SHELL", "TERM", "TZ", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
            "SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND",
            "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "XDG_CONFIG_HOME", "DISPLAY",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy")
# what every network step of the owner's git is run with (and the owned repo's own config says it again)
SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true",
        "-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never", "-c", "submodule.recurse=false")
OWNED_CONFIG = (("core.hooksPath", "/dev/null"), ("transfer.fsckObjects", "true"), ("fetch.fsckObjects", "true"),
                ("receive.fsckObjects", "true"), ("fetch.fsck.hasDotgit", "error"), ("protocol.ext.allow", "never"),
                ("gc.autoDetach", "false"), ("fetch.recurseSubmodules", "false"))
REFUSED_FLAGS = {"--force": "force", "-f": "force", "--tags": "tags", "--follow-tags": "tags", "--delete": "delete",
                 "-d": "delete", "--mirror": "mirror", "--all": "all", "--prune": "prune"}


class Refused(Exception):
    """A policy or usage refusal: nothing was pushed."""


class Failed(Exception):
    """git, the network or the project's job failed: nothing was pushed."""


def visible(text: str, lines: bool = False) -> str:
    """What the project (or the remote) wrote, safe to print on the owner's terminal: control characters are shown as
    escapes, so none can move the cursor or repaint what is above it. lines: git's own output keeps its line breaks."""
    return "".join(ch if ch.isprintable() or (lines and ch == "\n") else ch.encode("unicode_escape").decode("ascii")
                   for ch in text)


def owner_env() -> dict[str, str]:
    return {k: os.environ[k] for k in KEEP_ENV if k in os.environ}


def run_git(argv: list[str], cwd: pathlib.Path, env: dict[str, str], input: bytes | None = None,
            capture: bool = True) -> subprocess.CompletedProcess:
    """The one place the owner's git starts (tests record it). argv starts with git --git-dir <owned repo>."""
    return subprocess.run(argv, cwd=cwd, env=env, input=input, capture_output=capture, timeout=TIMEOUT_S)


class Owned:
    """A bare repo the platform owns; every git the owner's side runs is in it, named with --git-dir, from its folder."""

    def __init__(self, repo: pathlib.Path):
        self.repo = repo

    def git(self, *args: str, check: bool = False, input: bytes | None = None) -> subprocess.CompletedProcess:
        r = run_git(["git", "--git-dir", str(self.repo), *args], cwd=self.repo.parent, env=owner_env(), input=input)
        if check and r.returncode != 0:
            raise Failed(_("git {op} failed: {why}", op=next((a for a in args if not a.startswith("-") and "=" not in a), "?"),
                           why=visible(r.stderr.decode(errors="replace").strip()[-400:], lines=True)))
        return r

    def out(self, *args: str) -> str:
        r = self.git(*args)
        return r.stdout.decode(errors="replace") if r.returncode == 0 else ""

    def sha(self, rev: str) -> str | None:
        return self.out("rev-parse", "--verify", "-q", f"{rev}^{{commit}}").strip() or None

    def init(self) -> None:
        if not (self.repo / "HEAD").is_file():
            self.repo.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.git("init", "-q", "--bare", "--template=", check=True)
        for k, v in OWNED_CONFIG:
            self.git("config", k, v, check=True)


def broker_dir(project: str) -> pathlib.Path:
    """<state>/git-broker/<project>: the platform's, which no sandbox writes (taken at call time, so a test's state)."""
    return pathlib.Path(hubconf.STATE) / "git-broker" / project


def check_origin(origin: str | None) -> str:
    if not origin or origin == "(native)":
        raise Refused(_("the registry has no paths.origin for it: brunna push sends only to the remote the registry "
                        "names"))
    if origin.startswith("-") or re.match(r"^(ext|fd)::", origin):
        raise Refused(_("paths.origin {url} is not a remote brunna push contacts", url=origin))
    local = origin.removeprefix("file://") if origin.startswith(("/", "~", "file://")) else None
    if local is not None and gitdirs.holder(pathlib.Path(os.path.expanduser(local)), project_git.sandbox_writes()):
        raise Refused(_("paths.origin {url} is in a folder a sandboxed project writes", url=origin))
    return origin


def branch_arg(args: list[str]) -> tuple[str | None, bool]:
    """(the branch, or None for the checked-out one; --yes). Everything git push could do besides one fast-forward
    of one branch is refused here, with its reason."""
    yes, pos = False, []
    for a in args:
        if a in ("--yes", "-y"):
            yes = True
        elif a.split("=", 1)[0] in REFUSED_FLAGS or a.startswith("--force"):
            raise Refused(_("{flag}: brunna push makes only a fast-forward of one branch (no force, tags, deletes or "
                            "mirrors)", flag=a))
        elif a.startswith("-"):
            raise Refused(_("unknown option {flag}", flag=a))
        else:
            pos.append(a)
    if len(pos) > 1:
        raise Refused(_("one branch per call (got {n})", n=len(pos)))
    b = pos[0] if pos else None
    if b is not None:
        if b.startswith("+"):
            raise Refused(_("{arg}: a + refspec forces; brunna push makes fast-forwards only", arg=b))
        if ":" in b:
            raise Refused(_("{arg}: name one branch, not a refspec (no deletes, no other destination)", arg=b))
        if b.startswith("refs/tags/"):
            raise Refused(_("{arg}: brunna push pushes no tags", arg=b))
        if b.startswith("refs/") or b == "HEAD" or "@{" in b:
            raise Refused(_("{arg}: name a branch (as in refs/heads/<branch>), not a ref", arg=b))
    return b, yes


def valid_branch(owned: Owned, b: str) -> str:
    if b.startswith("-") or owned.git("check-ref-format", f"refs/heads/{b}").returncode != 0:
        raise Refused(_("{arg} is not a branch name", arg=visible(b)))
    return b


def remote_heads(owned: Owned, origin: str, branch: str) -> tuple[str, bool]:
    """(the remote's default branch, whether <branch> is on the remote), from ls-remote --symref; refused when the
    default branch can't be told (the remote is empty, or its HEAD names no branch)."""
    r = owned.git(*SAFE, "ls-remote", "--symref", "--", origin, "HEAD", f"refs/heads/{branch}")
    if r.returncode != 0:
        raise Failed(_("ls-remote {url} failed: {why}", url=origin,
                       why=visible(r.stderr.decode(errors="replace").strip()[-400:], lines=True)))
    default, born, has = None, False, False
    for line in r.stdout.decode(errors="replace").splitlines():
        left, _tab, name = line.partition("\t")
        if name == "HEAD" and left.startswith("ref: refs/heads/"):
            default = left.removeprefix("ref: refs/heads/")
        elif name == "HEAD":
            born = True   # a HEAD naming an unborn branch is advertised by newer git with no commit
        elif name == f"refs/heads/{branch}":
            has = True
    if not default or not born:
        raise Refused(_("the remote's default branch can't be told (ls-remote --symref shows no HEAD; an empty remote?): "
                        "its first push is not brunna push's"))
    return default, has


def fetch_remote(owned: Owned, origin: str, default: str, branch: str, has: bool) -> None:
    specs = [f"+refs/heads/{default}:{REMOTE}/{default}"]
    if has and branch != default:
        specs.append(f"+refs/heads/{branch}:{REMOTE}/{branch}")
    elif not has:
        owned.git("update-ref", "-d", f"{REMOTE}/{branch}")
    r = owned.git(*SAFE, "fetch", "-q", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--",
                  origin, *specs)
    if r.returncode != 0:
        raise Failed(_("fetching {url} failed: {why}", url=origin,
                       why=visible(r.stderr.decode(errors="replace").strip()[-400:], lines=True)))


def bundle_in(owned: Owned, project: str, ws: pathlib.Path, branch: str, have: list[str], tmp: pathlib.Path) -> str | None:
    """The branch as the project's job bundles it, in the owned repo at INCOMING after fsck; its sha, or None when the
    project has nothing the remote's tips don't reach."""
    bundle = tmp / "push.bundle"
    with bundle.open("wb") as fh:
        r = project_git.job(project, ws, "bundle-branch", ws, branch, *have, out=fh,
                            timeout=project_git.BUNDLE_TIMEOUT_S, cap=project_git.MAX_BUNDLE)
    why = visible(r.stderr.strip()[-300:])
    if r.returncode == NOTHING:
        return None
    if r.returncode == NO_BRANCH:
        raise Refused(_("{project} has no branch {branch}", project=project, branch=visible(branch)))
    if r.returncode == project_git.EX_SANDBOX:
        raise Failed(_("the sandbox refused the project's bundle job ({why})", why=why))
    if r.returncode != 0:
        raise Failed(_("the project's bundle job failed ({why})", why=why))
    # through a staging repo that borrows the owned repo's objects (the bundle's prerequisites), then into the owned
    # repo over file://: a fetch of a bundle checks objects only on newer git, a fetch over a transport on every git
    stage = Owned(tmp / "stage.git")
    stage.git("init", "-q", "--bare", "--template=", check=True)
    (stage.repo / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (stage.repo / "objects" / "info" / "alternates").write_text(str(owned.repo.resolve() / "objects") + "\n")
    r = stage.git(*SAFE, "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--", str(bundle),
                  f"+refs/heads/{branch}:refs/heads/incoming")
    if r.returncode != 0:
        raise Refused(_("the project's bundle was refused: {why}", why=visible(r.stderr.decode(errors="replace").strip()[-400:], lines=True)))
    r = owned.git(*SAFE, "-c", "protocol.file.allow=always", "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--",
                  f"file://{stage.repo.resolve()}", f"+refs/heads/incoming:{INCOMING}")
    if r.returncode != 0:
        raise Refused(_("the project's commits were refused by git's object checks: {why}",
                        why=visible(r.stderr.decode(errors="replace").strip()[-400:], lines=True)))
    return owned.sha(INCOMING)


def github_entry(owned: Owned, sha: str) -> str:
    """The .github entry of a commit's tree as `<mode> <type> <oid>`, or "" when there is none."""
    return owned.out("ls-tree", sha, "--", ".github").split("\t")[0].strip()


class Plan:
    def __init__(self, project, origin, branch, default, old, new, commits, n, paths, github, new_emails):
        self.project, self.origin, self.branch, self.default = project, origin, branch, default
        self.old, self.new, self.commits, self.n, self.paths = old, new, commits, n, paths
        self.github, self.new_emails = github, new_emails

    def lines(self) -> list[str]:
        out = []
        if self.github:
            out.append(_("!! .github/ differs from {default} on the remote: CI runs what it says, with the repo's secrets "
                         "(allowed by enforce.git_ci)", default=self.default))
        rng = f"{self.old[:12]}..{self.new[:12]}" if self.old else _("{sha} (a new branch on the remote)", sha=self.new[:12])
        out.append(_("push {project} {branch} → {url}", project=self.project, branch=self.branch, url=self.origin))
        out.append(_("  range: {range}, {n} {n:commit|commits}", range=rng, n=self.n))
        for short, name, email, subject in self.commits:
            mark = "  !!" if email in self.new_emails else "    "
            out.append(f"{mark}{short}  {visible(name)} <{visible(email)}>  {visible(subject)}")
        if self.n > len(self.commits):
            out.append(_("    … and {n} more", n=self.n - len(self.commits)))
        if self.new_emails:
            out.append(_("  !! author emails not in the remote's recent history: {emails}",
                         emails=", ".join(visible(e) for e in sorted(self.new_emails))))
        out.append(_("  changed paths:"))
        for status, path in self.paths[:MAX_PATHS]:
            out.append(f"  {'!!' if path == '.github' or path.startswith('.github/') else '  '}{status} {visible(path)}")
        if len(self.paths) > MAX_PATHS:
            out.append(_("    … and {n} more", n=len(self.paths) - MAX_PATHS))
        return out


def plan(owned: Owned, project: str, origin: str, branch: str, default: str, old: str | None, new: str) -> Plan:
    tips = [t for t in (owned.sha(f"{REMOTE}/{default}"), old) if t]
    log = owned.out("log", "--no-show-signature", "-z", f"--max-count={MAX_COMMITS}", "--format=%h%x1f%an%x1f%ae%x1f%s",
                    new, "--not", *tips)
    commits = [tuple(rec.split("\x1f", 3)) for rec in log.split("\0") if rec.strip()]
    commits = [c + ("",) * (4 - len(c)) for c in commits]
    n = int(owned.out("rev-list", "--count", new, "--not", *tips).strip() or 0)
    base = old or owned.out("merge-base", new, tips[0]).strip() or owned.out("hash-object", "-t", "tree", "/dev/null").strip()
    raw = owned.out("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", base, new).split("\0")
    paths = [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2) if raw[i]]
    usual = set(owned.out("log", "--no-show-signature", f"--max-count={USUAL_DEPTH}", "--format=%ae",
                          f"{REMOTE}/{default}").split())
    emails = {c[2] for c in commits}
    return Plan(project, origin, branch, default, old, new, commits, n, paths,
                github_entry(owned, new) != github_entry(owned, tips[0]), emails - usual if usual else set())


def confirm(yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise Refused(_("no terminal to confirm on: run it in a terminal, or add --yes"))
    try:
        answer = input(_("Push these? [y/N] "))
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise Refused(_("not pushed"))


def log(project: str, **rec) -> None:
    f = broker_dir(project) / "pushes.jsonl"
    with f.open("a") as fh:
        fh.write(json.dumps({"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "project": project,
                             **rec}) + "\n")


@contextlib.contextmanager
def locked(d: pathlib.Path):
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(d / "push.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(_("another brunna push of this project is running")) from None
        yield
    finally:
        os.close(fd)


def push(project: str, args: list[str]) -> int:
    branch, yes = branch_arg(args)
    doc = hubconf.projects().get(project)
    if doc is None:
        raise Refused(_("{project} is not in the registry", project=project))
    doc = {**doc, "name": project}
    if not isolation.sandboxed(doc):
        raise Refused(_("{project} is not sandboxed: push it with git as usual (brunna push is for sandboxed projects, "
                        "whose repo the owner's git must not run in)", project=project))
    paths = doc.get("paths") or {}
    origin = check_origin(paths.get("origin"))
    ws = hubconf.path(paths.get("workspace"))
    if not ws or not ws.is_dir():
        raise Refused(_("{project}: its workspace {path} is not there", project=project, path=ws))
    if branch is None:
        try:
            head = project_git.data(project, ws, "head", ws)
        except project_git.Unavailable as e:
            raise Failed(str(e)) from None
        branch = head.get("branch") if isinstance(head, dict) else None
        if not isinstance(branch, str):
            raise Refused(_("{project} has no branch checked out: name the branch", project=project))
    d = broker_dir(project)
    with locked(d):
        owned = Owned(d / "remote.git")
        owned.init()
        branch = valid_branch(owned, branch)
        default, has = remote_heads(owned, origin, branch)
        fetch_remote(owned, origin, default, branch, has)
        old = owned.sha(f"{REMOTE}/{branch}") if has else None
        have = sorted({t for t in (owned.sha(f"{REMOTE}/{default}"), old) if t})
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="hub-push-", dir=hubconf.private_tmp()))
        try:
            new = bundle_in(owned, project, ws, branch, have, tmp)
            if new is None or new == old:
                print(_("nothing to push: {branch} has no commits the remote lacks", branch=branch))
                return OK
            if old and owned.git("merge-base", "--is-ancestor", old, new).returncode != 0:
                raise Refused(_("{branch} is not a fast-forward of the remote's {old}: merge or rebase onto it in the "
                                "project first (brunna push never forces)", branch=branch, old=old[:12]))
            p = plan(owned, project, origin, branch, default, old, new)
            ci = (doc.get("enforce") or {}).get("git_ci") is True
            if p.github and not ci:
                raise Refused(_("{sha} changes .github/ against the remote's {default}: CI would run it with the repo's "
                                "secrets; the owner allows that with enforce.git_ci: true in the registry",
                                sha=new[:12], default=default))
            print("\n".join(p.lines()))
            confirm(yes)
            r = owned.git("-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false", "push", "--no-verify",
                          "--no-follow-tags", "--recurse-submodules=no", "--", origin, f"{new}:refs/heads/{branch}")
            said = visible((r.stderr + r.stdout).decode(errors="replace").strip()[-600:], lines=True)
            log(project, branch=branch, old=old, new=new, commits=p.n, github=p.github, ok=r.returncode == 0)
            if r.returncode != 0:
                raise Failed(_("git push failed: {why}", why=said))
            if said:
                print(said)
            print(_("pushed {branch}: {range}. The project's own repo is untouched: its origin/{branch} moves with its "
                    "next fetch", branch=branch, range=f"{(old or '')[:12]}..{new[:12]}" if old else new[:12]))
            return OK
        finally:
            owned.git("update-ref", "-d", INCOMING)
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "push" or argv[1].startswith("-"):
        print(_("usage: brunna push <project> [<branch>] [--yes]"), file=sys.stderr)
        return REFUSED
    try:
        return push(argv[1], argv[2:])
    except Refused as e:
        print(_("refused: {why}", why=e), file=sys.stderr)
        return REFUSED
    except Failed as e:
        print(_("not pushed: {why}", why=e), file=sys.stderr)
        return FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
