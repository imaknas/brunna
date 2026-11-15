#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""`brunna push`: a sandboxed project's branch to its registry remote, without running git in the project's repo. The
owner pushes with it (docs/design/git-broker.md, "Owner-run push, built first (11-15)"), and the platform pushes with it
on a session's request (docs/design/platform-push.md, r1 and r2).

A sandboxed project can't reach the owner's credentials. Run in the project's repo, the owner's git reads that repo's
`.git` (hooks, core.fsmonitor, core.sshCommand, credential.helper, filters…), which the project can write: project-planted
code run as the owner, outside every sandbox. Here instead:

  1. the project must be sandboxed (isolation.sandboxed), and its remote is the registry's paths.origin (owner-only):
     the only URL contacted. None, `(native)`, ext::/fd::, a local spelling that isn't absolute, and a local path a
     sandbox can write (a project's folders, the temp areas) are refused;
  2. the branch's commits are bundled AS THE PROJECT (project-git-job.py bundle-branch, through project_git.job, whose
     environment is built, never the owner's terminal's): the only git that runs in the project's repo, sandboxed, its
     stdout a bundle in hubconf.private_tmp(), capped;
  3. every other git runs in a bare repo the platform owns, named with --git-dir, from its folder, with an environment
     built from scratch: `<state>/git-broker/<project>/remote.git` (ls-remote --symref and a fetch from paths.origin,
     hooks off, no submodules, no tags, fsck on), and two staging repos borrowing its objects: the bundle goes into
     in.git, checked.git fetches it from there over file:// with git's fsck (a `.git` tree entry refused), the checks
     (bin/push_checks.py) run in checked.git, and only when they pass does the owned repo fetch from it (r1 S9): a
     refused push leaves nothing there;
  4. policy: fast-forward only (no force of any kind), no tags, no deletes, one branch per call; the default branch is the
     remote's HEAD by ls-remote --symref (refused when that can't be told, e.g. an empty remote);
  5. `push --no-verify --force-with-lease=refs/heads/<branch>:<old> <url> <sha>:refs/heads/<branch>` from the owned
     repo: the lease pins the push to the remote's ref as checked (it forces nothing: old..sha is a fast-forward). The
     project's repo is never written: its remote-tracking ref moves with its own next fetch;
  6. each push is a line in `<state>/git-broker/<project>/pushes.jsonl`, with each check's result.

The forms of `brunna push` (decided in this order):
  brunna push setup <project> [--branches none|others|all] [--email <address>…] [--public-email <address>…]
        the owner's setup of enforce.git_push (section 6): the remote's visibility, the emails proposed, the field
        written; `setup` is never a project
  brunna push --owner <project> [<branch>] [--yes]
        the owner's push, from anywhere: the commits and every check's flag shown; a failed check is overridden only by an
        interactive "y" to "push anyway?", never by --yes (--yes answers only the plain question of a push that passed
        every check); refused before contacting anything where it can't write the platform's folder (a sandbox)
  brunna push [<branch>] [--no-wait | --wait <seconds>]
        in a sandboxed project's folder (its workspace or runtime folder): a session's request. The branch (the one
        checked out unless named) and its commit, read with git's read-only commands, written as
        `<push_dir>/<project>/<id>.request.json`; it waits up to --wait (git_push.wait_s, default 90) for the answer:
        exit 0 pushed or nothing to push, 2 refused, 1 failed, 75 no answer yet (it comes as a letter). In an
        unsandboxed project's folder: push with git as usual (exit 2)
  brunna push <project> [<branch>] [--yes]
        outside every registered project's folder: the owner form, as before
  git_remote.py serve <project>
        the worker the mailbox dispatch starts (push_queue.tick): one queued request of the project, as the owner,
        outside every sandbox; the project's lock taken blocking, bounded by TIMEOUT_S
The worker's credential is push_credential_env: never the owner's own (the owner's decision 1, 11-15): the platform's
per-repo push key (bin/git_push_grant.py, ssh_env), or a refusal for the owner to set it up; a local origin needs none.

Exit: 0 pushed or nothing to push, 1 failed (git, network, the project's job), 2 refused (policy or usage), 75 the
request form's answer isn't in yet.
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
import time

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import gitdirs  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import isolation  # noqa: E402
import names  # noqa: E402
import policy  # noqa: E402
import project_git  # noqa: E402
import seatbelt  # noqa: E402

_ = i18n.domain(__file__)

OK, FAILED, REFUSED, PENDING = 0, 1, 2, 75
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
CHECK_NAMES = ("origin", "ref", "fast-forward", "objects", "size", "ci", "identity", "secrets", "names")
# the remote's or the transport's refusal → (check, the classified reason, who fixes it, the owner to-do's cause). The
# project never sees git's, ssh's or the credential helper's own words (r1 S8)
CLASSES = [
    (r"GH013|push protection|secret scanning", "remote", "secret-scanning", "project", ""),
    (r"GH007|private email", "remote", "email-privacy", "owner", "remote-rules"),
    (r"GH006|protected branch|hook declined|pre-receive", "remote", "protected", "owner", "remote-rules"),
    (r"stale info|non-fast-forward|fetch first|\[rejected\]", "remote", "moved", "wait", ""),
    (r"Permission denied|Authentication failed|could not read Username|Host key verification failed|denied to|"
     r"publickey|terminal prompts disabled|\b40[13]\b|Invalid username or password", "credential", "credential", "owner",
     "credential"),
    (r"Could not resolve host|Connection refused|timed out|Network is unreachable|Connection reset|Couldn't connect",
     "remote", "unreachable", "wait", ""),
    (r"not found|does not appear to be a git repository|No such file or directory", "remote", "origin", "owner",
     "origin"),
]


def class_reason(kind: str) -> str:
    return {
        "secret-scanning": _("the remote's secret push protection refused it: rewrite the unpushed commits so the secret "
                             "never appears"),
        "email-privacy": _("the remote refused an email address it keeps private (GH007)"),
        "protected": _("the remote refused the push: a protected branch or a rule of its own"),
        "moved": _("the remote's branch moved while the platform pushed: ask again"),
        "credential": _("the platform's credential can't reach the remote"),
        "unreachable": _("the remote can't be reached right now: ask again later"),
        "origin": _("the remote named by the registry's paths.origin isn't there"),
    }.get(kind, _("the platform failed for a reason it can't classify; the owner has the details"))


class Refused(Exception):
    """A policy or usage refusal: nothing was pushed. check: which check; fix: project | owner | wait; cause: the owner
    to-do's when fix is owner; public: what the project is told when the message carries git's own words."""

    def __init__(self, msg, check=None, fix="project", cause="", public=None, raw=""):
        super().__init__(msg)
        self.check, self.fix, self.cause, self.public, self.raw = check, fix, cause, public, raw


class Failed(Refused):
    """git, the network or the project's job failed: nothing was pushed."""

    def __init__(self, msg, check=None, fix="owner", cause="", public=None, raw=""):
        super().__init__(msg, check, fix, cause, public, raw)


def visible(text: str, lines: bool = False) -> str:
    """What the project (or the remote) wrote, safe to print on the owner's terminal: control characters are shown as
    escapes, so none can move the cursor or repaint what is above it. lines: git's own output keeps its line breaks."""
    return "".join(ch if ch.isprintable() or (lines and ch == "\n") else ch.encode("unicode_escape").decode("ascii")
                   for ch in text)


def owner_env() -> dict[str, str]:
    return {k: os.environ[k] for k in KEEP_ENV if k in os.environ}


def scratch_env(project: str) -> dict[str, str]:
    """An environment from scratch for the platform's own git (the worker's, and its staging repos'): none of the owner's
    configuration, helpers, agent or askpass."""
    d = broker_dir(project)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    empty = d / "empty-gitconfig"
    if not empty.is_file():
        empty.write_text("")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(d), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": str(empty), "GIT_TERMINAL_PROMPT": "0"}
    for k in ("LANG", "LC_ALL", "TZ"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def run_git(argv: list[str], cwd: pathlib.Path, env: dict[str, str], input: bytes | None = None,
            capture: bool = True) -> subprocess.CompletedProcess:
    """The one place the platform's git starts (tests record it). argv starts with git --git-dir <owned repo>."""
    return subprocess.run(argv, cwd=cwd, env=env, input=input, capture_output=capture, timeout=TIMEOUT_S)


class Owned:
    """A bare repo the platform owns; every git the platform's side runs is in it, named with --git-dir, from its folder.
    env: the environment its git runs with (the owner's for the owner form)."""

    def __init__(self, repo: pathlib.Path, env: dict[str, str] | None = None):
        self.repo, self.env = repo, env

    def git(self, *args: str, check: bool = False, input: bytes | None = None) -> subprocess.CompletedProcess:
        r = run_git(["git", "--git-dir", str(self.repo), *args], cwd=self.repo.parent,
                    env=self.env if self.env is not None else owner_env(), input=input)
        if check and r.returncode != 0:
            raw = r.stderr.decode(errors="replace").strip()[-400:]
            raise Failed(_("git {op} failed: {why}", op=next((a for a in args if not a.startswith("-") and "=" not in a), "?"),
                           why=visible(raw, lines=True)), check="objects", raw=raw,
                         public=_("the platform's own git failed; the owner has the details"))
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

    def borrow(self, other: "Owned") -> None:
        """A staging repo: init, its objects borrowed from the owned repo (the bundle's prerequisites, the remote's tips)."""
        self.git("init", "-q", "--bare", "--template=", check=True)
        (self.repo / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (self.repo / "objects" / "info" / "alternates").write_text(str(other.repo.resolve() / "objects") + "\n")

    def bytes_git(self, *args: str, input: bytes | None = None) -> bytes:
        """For push_checks: stdout, or push_checks.Failure."""
        import push_checks
        r = self.git(*args, input=input)
        if r.returncode != 0:
            raise push_checks.Failure("objects", _("git {op} failed in the platform's staging repo", op=args[0]))
        return r.stdout


def broker_dir(project: str) -> pathlib.Path:
    """<state>/git-broker/<project>: the platform's, which no sandbox writes (taken at call time, so a test's state)."""
    return policy.git_broker_dir(project)


def temp_roots() -> list[pathlib.Path]:
    """Folders every sandboxed project's processes can write, whatever this platform's own sandbox says: the system's
    temp areas (the same uid)."""
    roots = [*seatbelt.TMP, tempfile.gettempdir(), "/tmp", "/var/tmp", "/private/tmp", "/private/var/folders"]
    if sys.platform.startswith("linux"):
        roots.append("/dev/shm")
    return [pathlib.Path(r) for r in roots if r]


def check_origin(origin: str | None) -> str:
    """The URL git is given. A local origin must be an absolute path (file:///… or /…; ~ expanded) outside every folder
    a sandboxed project can write: the owner's git would read that repo's config, as it would the project's own."""
    if not origin or origin == "(native)":
        raise Refused(_("the registry has no paths.origin for it: brunna push sends only to the remote the registry "
                        "names"), check="origin", fix="owner", cause="origin")
    if origin.startswith("-") or re.match(r"^(ext|fd)::", origin):
        raise Refused(_("paths.origin {url} is not a remote brunna push contacts", url=origin), check="origin",
                      fix="owner", cause="origin")
    if "://" in origin:
        if not origin.lower().startswith("file://"):
            return origin   # https://, ssh://, git://
        local = origin[len("file://"):]
    elif re.match(r"^[^/]+:", origin):
        return origin       # host:path, ssh's short form
    else:
        local = os.path.expanduser(origin)
    if not local.startswith("/"):
        raise Refused(_("paths.origin {url}: a local remote has to be an absolute path", url=origin), check="origin",
                      fix="owner", cause="origin")
    named = pathlib.Path(local)
    if gitdirs.holder(named, project_git.sandbox_writes()):
        raise Refused(_("paths.origin {url} is in a folder a sandboxed project writes", url=origin), check="origin",
                      fix="owner", cause="origin")
    roots = {f"{i}": r for i, r in enumerate(temp_roots())}
    if gitdirs.holder(named, roots) or any(named.resolve().is_relative_to(r.resolve()) for r in roots.values()):
        raise Refused(_("paths.origin {url} is in a temp folder, which every sandboxed project can write", url=origin),
                      check="origin", fix="owner", cause="origin")
    return origin if "://" in origin else str(named)


def local_origin(origin: str) -> bool:
    return origin.lower().startswith("file://") or origin.startswith("/")


def branch_arg(args: list[str]) -> tuple[str | None, bool]:
    """(the branch, or None for the checked-out one; --yes). Everything git push could do besides one fast-forward
    of one branch is refused here, with its reason."""
    yes, pos = False, []
    for a in args:
        if a in ("--yes", "-y"):
            yes = True
        elif a.split("=", 1)[0] in REFUSED_FLAGS or a.startswith("--force"):
            raise Refused(_("{flag}: brunna push makes only a fast-forward of one branch (no force, tags, deletes or "
                            "mirrors)", flag=a), check="ref")
        elif a.startswith("-"):
            raise Refused(_("unknown option {flag}", flag=a), check="ref")
        else:
            pos.append(a)
    if len(pos) > 1:
        raise Refused(_("one branch per call (got {n})", n=len(pos)), check="ref")
    b = pos[0] if pos else None
    if b is not None:
        ref_shape(b)
    return b, yes


def ref_shape(b: str) -> None:
    if b.startswith("+"):
        raise Refused(_("{arg}: a + refspec forces; brunna push makes fast-forwards only", arg=b), check="ref")
    if ":" in b:
        raise Refused(_("{arg}: name one branch, not a refspec (no deletes, no other destination)", arg=b), check="ref")
    if b.startswith("refs/tags/"):
        raise Refused(_("{arg}: brunna push pushes no tags", arg=b), check="ref")
    if b.startswith("refs/") or b == "HEAD" or "@{" in b:
        raise Refused(_("{arg}: name a branch (as in refs/heads/<branch>), not a ref", arg=b), check="ref")


def valid_branch(owned: Owned, b: str) -> str:
    if b.startswith("-") or owned.git("check-ref-format", f"refs/heads/{b}").returncode != 0:
        raise Refused(_("{arg} is not a branch name", arg=visible(b)), check="ref")
    return b


def classify(raw: str) -> tuple[str, str, str, str]:
    """git's or ssh's own words → (check, kind, fix, cause)."""
    for rx, check, kind, fix, cause in CLASSES:
        if re.search(rx, raw, re.I):
            return check, kind, fix, cause
    return "remote", "unknown", "owner", "unknown"


def network_failure(msg: str, raw: str) -> Failed:
    check, kind, fix, cause = classify(raw)
    return Failed(msg, check=check, fix=fix, cause=cause, public=class_reason(kind), raw=raw)


def remote_heads(owned: Owned, origin: str, branch: str) -> tuple[str, bool]:
    """(the remote's default branch, whether <branch> is on the remote), from ls-remote --symref; refused when the
    default branch can't be told (the remote is empty, or its HEAD names no branch)."""
    r = owned.git(*SAFE, "ls-remote", "--symref", "--", origin, "HEAD", f"refs/heads/{branch}")
    if r.returncode != 0:
        raw = r.stderr.decode(errors="replace").strip()[-400:]
        raise network_failure(_("ls-remote {url} failed: {why}", url=origin, why=visible(raw, lines=True)), raw)
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
                        "its first push is not brunna push's"), check="remote", fix="owner", cause="empty-remote")
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
        raw = r.stderr.decode(errors="replace").strip()[-400:]
        raise network_failure(_("fetching {url} failed: {why}", url=origin, why=visible(raw, lines=True)), raw)


def bundle_in(owned: Owned, project: str, ws: pathlib.Path, branch: str, have: list[str], tmp: pathlib.Path,
              cap: int | None = None, env: dict | None = None) -> tuple[str, Owned | None]:
    """The branch as the project's job bundles it, through in.git into checked.git with git's fsck (r1 S9): (the
    branch's commit, checked.git) — checked.git None when the remote's tips reach all of it (the job says only the
    branch's commit id then: one the owned repo already has, or it is refused). Nothing reaches the owned repo here."""
    bundle = tmp / "push.bundle"
    with bundle.open("wb") as fh:
        r = project_git.job(project, ws, "bundle-branch", ws, branch, *have, out=fh,
                            timeout=project_git.BUNDLE_TIMEOUT_S, cap=cap or project_git.MAX_BUNDLE)
    why = visible(r.stderr.strip()[-300:])
    public = _("the project's bundle job failed; the owner has the details")
    if r.returncode == NOTHING:
        tip = bundle.read_bytes()[:100].decode(errors="replace").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tip) or owned.sha(tip) != tip:
            raise Refused(_("the project's job said the remote has every commit of {branch}, then named one the "
                            "remote's tips don't reach: {sha}", branch=visible(branch), sha=visible(tip[:64])),
                          check="objects", public=_("the project's bundle job named a commit the remote doesn't have"))
        return tip, None
    if r.returncode == NO_BRANCH:
        raise Refused(_("{project} has no branch {branch}", project=project, branch=visible(branch)), check="ref")
    if r.returncode == project_git.TOO_BIG:
        raise Refused(_("the branch's new commits are larger than a push may be ({mb} MB): push in parts",
                        mb=(cap or project_git.MAX_BUNDLE) >> 20), check="size")
    if r.returncode == project_git.EX_SANDBOX:
        raise Failed(_("the sandbox refused the project's bundle job ({why})", why=why), check="objects", public=public,
                     raw=why)
    if r.returncode != 0:
        raise Failed(_("the project's bundle job failed ({why})", why=why), check="objects", public=public, raw=why)
    # in.git takes the bundle unchecked; checked.git fetches it over a transport, which checks objects on every git
    # version (a fetch of a bundle checks them only on newer git)
    staged = Owned(tmp / "in.git", env if env is not None else owned.env)
    staged.borrow(owned)
    r = staged.git(*SAFE, "-c", "transfer.fsckObjects=false", "-c", "fetch.fsckObjects=false", "fetch", "-q",
                   "--no-tags", "--no-write-fetch-head", "--", str(bundle), f"+refs/heads/{branch}:refs/heads/incoming")
    if r.returncode != 0:
        raw = r.stderr.decode(errors="replace").strip()[-400:]
        raise Refused(_("the project's bundle was refused: {why}", why=visible(raw, lines=True)), check="objects",
                      public=_("the project's bundle was not a valid bundle of the branch"), raw=raw)
    checked = Owned(tmp / "checked.git", staged.env)
    checked.borrow(owned)
    for k, v in OWNED_CONFIG:
        checked.git("config", k, v, check=True)
    r = checked.git(*SAFE, "-c", "protocol.file.allow=always", "fetch", "-q", "--no-tags", "--no-write-fetch-head",
                    "--", f"file://{staged.repo.resolve()}", "+refs/heads/incoming:refs/heads/incoming")
    if r.returncode != 0:
        raw = r.stderr.decode(errors="replace").strip()[-400:]
        raise Refused(_("the project's commits were refused by git's object checks: {why}", why=visible(raw, lines=True)),
                      check="objects", public=_("the project's commits were refused by git's object checks (e.g. a "
                                                ".git folder in a tree)"), raw=raw)
    return checked.sha("refs/heads/incoming"), checked


def take_in(owned: Owned, checked: Owned | None, sha: str) -> None:
    """The checked objects into the owned repo, only once every check passed."""
    if checked is None:
        return
    r = owned.git(*SAFE, "-c", "protocol.file.allow=always", "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--",
                  f"file://{checked.repo.resolve()}", f"+refs/heads/incoming:{INCOMING}")
    if r.returncode != 0 or owned.git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raw = r.stderr.decode(errors="replace").strip()[-400:]
        raise Failed(_("the checked commits could not be taken into the platform's repo: {why}",
                       why=visible(raw, lines=True)), check="objects", raw=raw,
                     public=_("the platform's own git failed; the owner has the details"))


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
                         "(allowed by enforce.git_ci)", default=visible(self.default)))
        rng = f"{self.old[:12]}..{self.new[:12]}" if self.old else _("{sha} (a new branch on the remote)", sha=self.new[:12])
        out.append(_("push {project} {branch} → {url}", project=self.project, branch=visible(self.branch),
                     url=visible(self.origin)))
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


def plan(owned: Owned, project: str, origin: str, branch: str, default: str, old: str | None, new: str,
         repo: Owned | None = None) -> Plan:
    """What the owner sees, read in repo (the staging repo holding the new commits; the owned repo by default), the
    remote's tips from the owned repo."""
    tips = [t for t in (owned.sha(f"{REMOTE}/{default}"), old) if t]
    usual = set(owned.out("log", "--no-show-signature", f"--max-count={USUAL_DEPTH}", "--format=%ae",
                          f"{REMOTE}/{default}").split())
    owned = repo or owned
    # one field per line: a name, an email or a subject never holds a newline (git ends each header line there), so
    # none can shift the fields after it
    log = owned.out("log", "--no-show-signature", "-z", f"--max-count={MAX_COMMITS}", "--format=%h%n%an%n%ae%n%s",
                    new, "--not", *tips)
    commits = [tuple(rec.split("\n")) for rec in log.split("\0") if rec.strip()]
    if any(len(c) != 4 for c in commits):
        raise Refused(_("a commit's author or subject can't be shown as one line each; not pushed"), check="identity")
    n = int(owned.out("rev-list", "--count", new, "--not", *tips).strip() or 0)
    base = old or owned.out("merge-base", new, tips[0]).strip() or owned.out("hash-object", "-t", "tree", "/dev/null").strip()
    raw = owned.out("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", base, new).split("\0")
    paths = [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2) if raw[i]]
    emails = {c[2] for c in commits}
    return Plan(project, origin, branch, default, old, new, commits, n, paths,
                github_entry(owned, new) != github_entry(owned, tips[0]), emails - usual if usual else set())


def confirm(yes: bool, refused: list[str] | None = None) -> None:
    """The owner's look: --yes answers only the plain question of a push that passed every check (r1 S1)."""
    if yes and not refused:
        return
    if not sys.stdin.isatty():
        raise Refused(_("a check refused it ({why}): only your interactive y pushes past a check, never --yes",
                        why="; ".join(refused)) if refused else
                      _("no terminal to confirm on: run it in a terminal, or add --yes"))
    try:
        answer = input(_("A check refused it. Push anyway? [y/N] ") if refused else _("Push these? [y/N] "))
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise Refused(_("not pushed"))


def log(project: str, **rec) -> None:
    f = broker_dir(project) / "pushes.jsonl"
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps({"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "project": project,
                             **rec}, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def locked(d: pathlib.Path, wait_s: float = 0):
    """The project's push lock: refused at once (the owner form), or waited for up to wait_s (the worker, r1 S10)."""
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(d / "push.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refused(_("another brunna push of this project is running"), fix="wait") from None
                time.sleep(min(2.0, max(0.05, deadline - time.monotonic())))
        yield
    finally:
        os.close(fd)


# ---------- the remote's visibility (check 8; r1 nit, r2 S-l) ----------

def visibility(origin: str) -> str:
    """public | private | unknown: the push grant's anonymous probe (git_push_grant.visibility, the one probe), a local
    path private. Unknown is treated as public by every caller; without the grant module, every remote is unknown."""
    if local_origin(origin):
        return "private"
    try:
        import git_push_grant
    except ImportError:
        return "unknown"
    return git_push_grant.visibility(origin)


# ---------- the credential (one function; the owner's decision 1, 11-15) ----------

def push_credential_env(project: str, origin: str, owner_form: bool) -> dict[str, str]:
    """The environment every network step of this push runs with. The owner form: the owner's git environment, as
    before. The worker: a local origin needs no credential (an environment from scratch); any other gets the platform's
    per-repo push key (git_push_grant.ssh_env), never the owner's own credential; no key → refused for the owner."""
    if owner_form:
        return owner_env()
    env = scratch_env(project)
    if local_origin(origin):
        return env
    setup = _("the platform's push key isn't set up: {command}", command=f"{names.current('cli')} push setup {project}")
    try:
        import git_push_grant
    except ImportError:
        raise Refused(setup, check="credential", fix="owner", cause="credential") from None
    try:
        return {**env, **git_push_grant.ssh_env(project)}
    except getattr(git_push_grant, "NotSetUp", ()) as e:
        raise Refused(setup, check="credential", fix="owner", cause="credential", raw=str(e)[:300]) from None


def push_url(project: str, origin: str, owner_form: bool) -> str:
    """Where the push goes: paths.origin, or the grant's push URL when it names one (an https origin's ssh form)."""
    if owner_form or local_origin(origin):
        return origin
    try:
        import git_push_grant
    except ImportError:
        return origin
    fn = getattr(git_push_grant, "push_url", None)
    return fn(project) if callable(fn) else origin


# ---------- the pipeline ----------

class Prepared:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def registered(project: str) -> dict:
    doc = hubconf.projects().get(project)
    if doc is None:
        raise Refused(_("{project} is not in the registry", project=project))
    doc = {**doc, "name": project}
    if not isolation.sandboxed(doc):
        raise Refused(_("{project} is not sandboxed: push it with git as usual (brunna push is for sandboxed projects, "
                        "whose repo the owner's git must not run in)", project=project))
    return doc


def field(doc: dict) -> dict:
    return ((doc.get("enforce") or {}).get("git_push")) or {}


def prepare(project: str, doc: dict, branch: str | None, tmp: pathlib.Path, *, owner_form: bool, sha: str | None = None,
            supersedes: dict | None = None) -> Prepared:
    """Registry, remote, bundle, staging: everything up to the checks (section 3, "The pipeline", steps 1–4)."""
    paths = doc.get("paths") or {}
    origin = check_origin(paths.get("origin"))
    branches = field(doc).get("branches", "none")
    if not owner_form and branches == "none":
        raise Refused(_("the owner hasn't let the platform push {project}'s branches (enforce.git_push)", project=project),
                      check="field", fix="owner", cause="branch")
    ws = hubconf.path(paths.get("workspace"))
    if not ws or not ws.is_dir():
        raise Refused(_("{project}: its workspace {path} is not there", project=project, path=ws), fix="owner",
                      cause="workspace", public=_("the project's workspace is not where the registry says"))
    env = push_credential_env(project, origin, owner_form)   # before the first network step
    if branch is None:
        try:
            head = project_git.data(project, ws, "head", ws)
        except project_git.Unavailable as e:
            raise Failed(str(e), check="ref", public=_("the project's checked-out branch could not be read")) from None
        branch = head.get("branch") if isinstance(head, dict) else None
        if not isinstance(branch, str):
            raise Refused(_("{project} has no branch checked out: name the branch", project=project), check="ref")
    owned = Owned(broker_dir(project) / "remote.git", env)
    owned.init()
    branch = valid_branch(owned, branch)
    url = push_url(project, origin, owner_form)
    default, has = remote_heads(owned, url, branch)
    if not owner_form and branches == "others" and branch == default:
        raise Refused(_("the owner lets the platform push {project}'s branches but not its default branch {branch}",
                        project=project, branch=visible(branch)), check="field", fix="owner", cause="branch")
    fetch_remote(owned, url, default, branch, has)
    old = owned.sha(f"{REMOTE}/{branch}") if has else None
    have = sorted({t for t in (owned.sha(f"{REMOTE}/{default}"), old) if t})
    cap = None if owner_form else ((limits()["max_total_mb"] * 5) >> 2) << 20
    tip, checked = bundle_in(owned, project, ws, branch, have, tmp, cap=cap,
                             env=None if owner_form else scratch_env(project))
    look = checked or owned
    new = tip
    if sha is not None:   # pinned to what the session asked for (r2 S-g): reachable from the branch, pushed as is
        if look.git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0 \
                or look.git("merge-base", "--is-ancestor", sha, tip).returncode != 0:
            raise Refused(_("{sha} is not on {branch} any more (the branch was rewritten since the request): ask again",
                            sha=sha[:12], branch=visible(branch)), check="fast-forward")
        new = sha
    if supersedes and supersedes.get("sha") and \
            look.git("merge-base", "--is-ancestor", supersedes["sha"], new).returncode != 0:
        raise Refused(_("{sha} doesn't contain the commits of the request it replaced ({id}): the branch was rewritten",
                        sha=new[:12], id=supersedes.get("id", "?")), check="fast-forward")
    if old and new != old and look.git("merge-base", "--is-ancestor", old, new).returncode != 0:
        raise Refused(_("{branch} is not a fast-forward of the remote's {old}: merge or rebase onto it in the project "
                        "first (brunna push never forces)", branch=visible(branch), old=old[:12]), check="fast-forward")
    return Prepared(project=project, doc=doc, origin=origin, url=url, branch=branch, default=default, old=old, new=new,
                    owned=owned, checked=checked, look=look, env=env)


def limits() -> dict:
    import push_queue
    return push_queue.limits()


def run_checks(p: Prepared, owner_form: bool, deadline: float, logf=None) -> tuple[list, str]:
    """Checks 4–9 (push_checks) in look, after the remote's visibility: → (findings, visibility)."""
    import push_checks
    vis = visibility(p.origin)
    public = vis != "private"
    tips = [t for t in (p.owned.sha(f"{REMOTE}/{p.default}"), p.old) if t]
    try:
        found = push_checks.run(p.look.bytes_git, project=p.project, doc=p.doc, new=p.new, tips=tips,
                                default_tip=p.owned.sha(f"{REMOTE}/{p.default}"), public=public,
                                limits=limits(), deadline=deadline, log=logf, owner_form=owner_form)
    except push_checks.Failure as e:
        found = [push_checks.Finding(e.check, e.reason, e.fix, e.cause)]
    recorded = field(p.doc).get("visibility")
    if recorded and recorded != ("public" if public else "private") and not owner_form:
        found.append(push_checks.Finding("names", _("the remote's visibility is {now}, but setup recorded {then}: the "
                                                    "owner runs brunna push setup again", now=vis, then=recorded),
                                         "owner", "visibility"))
    return found, vis


def send(p: Prepared) -> str:
    """The push, pinned by the lease to the remote's ref as checked. → git's own words (for the owner only)."""
    r = p.owned.git("-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false", "push", "--no-verify",
                    "--no-follow-tags", "--recurse-submodules=no", f"--force-with-lease=refs/heads/{p.branch}:{p.old or ''}",
                    "--", p.url, f"{p.new}:refs/heads/{p.branch}")
    said = visible((r.stderr + r.stdout).decode(errors="replace").strip()[-600:], lines=True)
    if r.returncode != 0:
        raise network_failure(_("git push failed: {why}", why=said), said)
    return said


def landed_check(project: str):
    """landed(sha): what the remote's branch now holds contains sha (closing a "branch not allowed" to-do)."""
    owned = Owned(broker_dir(project) / "remote.git")

    def landed(sha: str) -> bool:
        heads = owned.out("for-each-ref", "--format=%(objectname)", REMOTE).split()
        return any(owned.git("merge-base", "--is-ancestor", sha, h).returncode == 0 for h in heads)
    return landed


def update_tracking(p: Prepared) -> None:
    p.owned.git("update-ref", f"{REMOTE}/{p.branch}", p.new)


# ---------- the owner form ----------

def can_write_platform(project: str) -> bool:
    d = broker_dir(project)
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".probe-", dir=d)
        os.close(fd)
        os.unlink(name)
        return True
    except OSError:
        return False


def flag_lines(found, vis: str) -> list[str]:
    bad = {f.check for f in found}
    marks = []
    for c in CHECK_NAMES:
        if c == "names" and vis == "private":
            marks.append(_("{check} – (a private remote)", check=c))
        else:
            marks.append(f"{c} {'!!' if c in bad else '✓'}")
    out = [_("checks: {marks}", marks=", ".join(marks))]
    out += [f"!! [{f.check}] {visible(f.reason)}" for f in found]
    return out


def owner_push(project: str, args: list[str]) -> int:
    branch, yes = branch_arg(args)
    doc = registered(project)
    if not can_write_platform(project):
        raise Refused(_("the platform's folder for {project} can't be written from here (a sandbox?): run it in your own "
                        "terminal, outside the project's sandbox", project=project))
    d = broker_dir(project)
    with locked(d):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="hub-push-", dir=hubconf.private_tmp()))
        owned = None
        try:
            p = prepare(project, doc, branch, tmp, owner_form=True)
            owned = p.owned
            if p.new == p.old:
                print(_("nothing to push: the remote's {branch} is already at {sha}", branch=visible(p.branch),
                        sha=p.new[:12]))
                return OK
            found, vis = run_checks(p, True, time.monotonic() + limits()["scan_timeout_s"])
            pl = plan(owned, project, p.origin, p.branch, p.default, p.old, p.new, repo=p.look)
            print("\n".join(pl.lines()))
            print("\n".join(flag_lines(found, vis)))
            confirm(yes, refused=[visible(f.reason) for f in found])
            take_in(owned, p.checked, p.new)   # only once the owner said yes
            said = send(p)
            update_tracking(p)
            log(project, branch=p.branch, old=p.old, new=p.new, commits=pl.n, github=pl.github, ok=True, by="owner",
                checks={f.check: f.reason for f in found}, overridden=bool(found))
            import push_queue
            push_queue.close_todos(project, landed_check(project), log=lambda m: print(m, file=sys.stderr))
            if said:
                print(said)
            print(_("pushed {branch}: {range}. The project's own repo is untouched: its origin/{branch} moves with its "
                    "next fetch", branch=visible(p.branch),
                    range=f"{p.old[:12]}..{p.new[:12]}" if p.old else p.new[:12]))
            return OK
        except Failed as e:
            if "git push failed" in str(e):
                log(project, branch=branch, ok=False, by="owner", error=e.raw[-300:])
            raise
        finally:
            if owned is not None:
                owned.git("update-ref", "-d", INCOMING)
            shutil.rmtree(tmp, ignore_errors=True)


# ---------- the worker (serve) ----------

def serve(project: str, letter=None) -> int:
    """One queued request of the project, as the owner (push_queue.tick starts it detached)."""
    import push_queue
    d = broker_dir(project)
    try:
        with locked(d, wait_s=TIMEOUT_S):
            with push_queue.queue_locked(project):
                reqs = push_queue.queued(project)
                if not reqs:
                    return OK
                req = reqs[0]
                w = push_queue.worker(project)
                import runtime_ports
                push_queue._write_json(d / "worker.json", {**w, "pid": os.getpid(), "start": runtime_ports.process_start(
                    os.getpid()), "id": req["id"], "started": req["id"], "started_at": push_queue.now_iso()})
            res = serve_request(project, req)
            push_queue.finish(project, req, res, letter=letter, log=_log_line(project))
            return {"pushed": OK, "nothing": OK, "refused": REFUSED}.get(res["state"], FAILED)
    except Refused:   # the lock wasn't free in time: the request stays queued for the next tick's worker
        (d / "worker.json").unlink(missing_ok=True)
        return PENDING


def _log_line(project: str):
    def say(msg: str) -> None:
        print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {project}: {msg}", file=sys.stderr, flush=True)
    return say


def serve_request(project: str, req: dict) -> dict:
    """The pipeline for one request → its result (push_queue.result). Every exception becomes a classified reason; git's
    and the helpers' own words, and the platform's paths, go only to pushes.jsonl and the owner's to-do (r1 S8)."""
    import push_checks
    import push_queue
    rid, branch, sha = req["id"], req["branch"], req["sha"]
    say = _log_line(project)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hub-push-", dir=hubconf.private_tmp()))
    p, rec, res = None, {"id": rid, "branch": branch, "sha": sha, "by": "session",
                         "requested_by": req.get("session")}, None
    try:
        doc = registered(project)
        ref_shape(branch)
        p = prepare(project, doc, branch, tmp, owner_form=False, sha=sha, supersedes=req.get("supersedes"))
        rec.update(old=p.old, new=p.new)
        if p.new == p.old:
            res = push_queue.result(project, rid, "nothing", _("the remote's {branch} is already at {sha}",
                                                               branch=branch, sha=p.new[:12]), None, None)
        else:
            found, vis = run_checks(p, False, time.monotonic() + limits()["scan_timeout_s"], logf=say)
            rec["checks"] = [{"check": f.check, "reason": f.reason} for f in found] or "passed"
            rec["visibility"] = vis
            if found:
                f = found[0]
                raise Refused(f.reason, check=f.check, fix=f.fix, cause=f.cause)
            take_in(p.owned, p.checked, p.new)
            rec["commits"] = int(p.owned.out("rev-list", "--count", p.new, "--not",
                                             *[t for t in (p.owned.sha(f"{REMOTE}/{p.default}"), p.old) if t]).strip()
                                 or 0)
            said = send(p)
            update_tracking(p)
            rec.update(ok=True, said=said[-300:])
            res = push_queue.result(project, rid, "pushed", _("{n} {n:commit|commits} pushed to {branch}",
                                                              n=rec["commits"], branch=branch), None, None,
                                    old=p.old, new=p.new, commits=rec["commits"],
                                    range=f"{p.old[:12]}..{p.new[:12]}" if p.old else p.new[:12])
            push_queue.close_todos(project, landed_check(project), log=say)
            _unclassified(project, reset=True)
    except Refused as e:   # Failed too
        state = "failed" if isinstance(e, Failed) else "refused"
        reason = e.public or str(e)
        rec.update(ok=False, check=e.check, error=(e.raw or str(e))[-600:])
        todo = None
        if e.fix == "owner":
            cause = e.cause or e.check or "unknown"
            if cause == "unknown" and _unclassified(project) < 3:
                todo = None
            else:
                todo = push_queue.raise_todo(project, cause, owner_text(project, branch, sha, cause, e), sha=sha,
                                             log=say)
        res = push_queue.result(project, rid, state, reason, e.fix, e.check, **({"todo": todo} if todo else {}))
    except push_checks.Failure as e:
        rec.update(ok=False, check=e.check, error=e.reason)
        res = push_queue.result(project, rid, "refused", e.reason, e.fix, e.check)
    except Exception as e:   # noqa: BLE001 (every exception is a classified answer, never a traceback to the project)
        say(f"serve {rid}: {e!r}")
        rec.update(ok=False, check="platform", error=repr(e)[-600:])
        n = _unclassified(project)
        todo = push_queue.raise_todo(project, "unknown", owner_text(project, branch, sha, "unknown", e), log=say) \
            if n >= 3 else None
        res = push_queue.result(project, rid, "failed", _("the platform failed while pushing; the owner has the "
                                                          "details"), "owner" if todo else "wait", "platform",
                                **({"todo": todo} if todo else {}))
    finally:
        if p is not None:
            p.owned.git("update-ref", "-d", INCOMING)
        shutil.rmtree(tmp, ignore_errors=True)
    log(project, **rec, state=res["state"])
    return {**res, "branch": branch}


def _unclassified(project: str, reset: bool = False) -> int:
    """Failures in a row the platform couldn't classify (a to-do from the third)."""
    f = broker_dir(project) / "unclassified"
    if reset:
        f.unlink(missing_ok=True)
        return 0
    try:
        n = int(f.read_text() or 0) + 1
    except (OSError, ValueError):
        n = 1
    f.write_text(str(n))
    return n


def owner_text(project: str, branch: str, sha: str, cause: str, e) -> str:
    cli = names.current("cli")
    owner_cmd = f"{cli} push --owner {project} {branch}"
    head = {
        "branch": _("{project} asked the platform to push {branch} ({sha}), which enforce.git_push doesn't allow: look "
                    "at it and push it yourself with {command}", project=project, branch=branch, sha=sha[:12],
                    command=owner_cmd),
        "emails": _("list the emails {project} commits with: {command}", project=project,
                    command=f"{cli} push setup {project}"),
        "visibility": _("{project}'s remote changed visibility since its push setup: {command}", project=project,
                        command=f"{cli} push setup {project}"),
        "credential": _("the platform can't reach {project}'s remote to push: {command}", project=project,
                        command=f"{cli} push setup {project}"),
    }.get(cause, _("the platform couldn't push {project}'s {branch} ({sha}): {why}. You can push it yourself with "
                   "{command}", project=project, branch=branch, sha=sha[:12], why=cause, command=owner_cmd))
    raw = getattr(e, "raw", "") or str(e)
    return head + (f"\n\n{visible(raw[-600:], lines=True)}" if raw else "")


# ---------- the session's request (the client) ----------

def project_of_folder(cwd: pathlib.Path) -> tuple[str, dict] | None:
    """The registered project whose workspace or runtime folder holds cwd, the nearest (hub-run's find_workspace), or the
    hub's own checkout."""
    real = cwd.resolve()
    best = None
    services = hubconf.p("services_root")
    for name, doc in hubconf.projects().items():
        for f in (hubconf.workspace(name), services / name if services and doc.get("runtime") is not None else None):
            if f and (real == f.resolve() or f.resolve() in real.parents) and (best is None or len(str(f)) > best[0]):
                best = (len(str(f)), name, {**doc, "name": name})
    if best:
        return best[1], best[2]
    for hub in (hubconf.MAIN, hubconf.ROOT):
        if real == hub.resolve() or hub.resolve() in real.parents:
            return hubconf.hub_project(), {"name": hubconf.hub_project()}
    return None


def request_id() -> str:
    import secrets
    return f"m{time.strftime('%m%d%H%M')}-{secrets.token_hex(6)}"


def client(project: str, args: list[str], cwd: pathlib.Path) -> int:
    """A session's `brunna push [<branch>] [--no-wait | --wait <s>]`: the request written, the answer waited for."""
    wait, pos, i = None, [], 0
    while i < len(args):
        a = args[i]
        if a == "--no-wait":
            wait = 0
        elif a == "--wait" and i + 1 < len(args) and args[i + 1].isdigit():
            wait, i = int(args[i + 1]), i + 1
        elif a.startswith("--wait="):
            wait = int(a.split("=", 1)[1]) if a.split("=", 1)[1].isdigit() else None
        elif a in ("--yes", "-y"):
            raise Refused(_("--yes is the owner's (brunna push --owner); a session's request is checked by the platform"))
        else:
            pos.append(a)
        i += 1
    branch, _yes = branch_arg(pos)
    if names.env("CONFINED") or os.environ.get("HUB_LLM_POINT"):
        raise Refused(_("a run the platform started can't ask for a push; a session of the project can"))
    git = ["git", "-C", str(cwd), "-c", "core.fsmonitor=false"]
    if branch is None:
        r = subprocess.run([*git, "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True, text=True)
        branch = r.stdout.strip()
        if r.returncode != 0 or not branch:
            raise Refused(_("no branch is checked out here: name the branch"), check="ref")
    r = subprocess.run([*git, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}"], capture_output=True,
                       text=True)
    sha = r.stdout.strip()
    if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise Refused(_("{project} has no branch {branch}", project=project, branch=visible(branch)), check="ref")
    rid = submit(project, branch, sha)
    print(_("asked the platform to push {branch} at {sha} ({id})", branch=visible(branch), sha=sha[:12], id=rid),
          flush=True)
    return await_result(project, rid, limits()["wait_s"] if wait is None else wait)


def submit(project: str, branch: str, sha: str, session: str | None = None) -> str:
    """The request file in <push_dir>/<project>/ (a dot-name first, then renamed). The folder is the platform's: a
    session can't make it."""
    d = policy.push_request_dir(project)
    if not d.is_dir() or d.is_symlink():
        raise Failed(_("this project's push request folder isn't there yet ({path}): the platform makes it within a "
                       "minute; ask again then", path=d), fix="wait")
    rid = request_id()
    req = {"v": 1, "id": rid, "branch": branch, "sha": sha}
    sid = session or _session()
    if sid:
        req["session"] = sid
    tmp = d / f".{rid}.tmp"
    tmp.write_text(json.dumps(req))
    tmp.rename(d / f"{rid}.request.json")
    return rid


def _session() -> str | None:
    try:
        import runtime_ports
        return runtime_ports.current_session()
    except Exception:   # noqa: BLE001 (the session id is for the log only)
        return None


def await_result(project: str, rid: str, wait: float) -> int:
    f = policy.push_request_dir(project) / f"{rid}.result.json"
    deadline = time.monotonic() + wait
    while True:
        try:
            res = json.loads(f.read_text())
            break
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                print(_("no answer yet: the push goes on, and its outcome comes as a letter ({id})", id=rid))
                return PENDING
            time.sleep(1)
    import push_queue
    print(push_queue.letter_text(res))
    return {"pushed": OK, "nothing": OK, "refused": REFUSED, "failed": FAILED}.get(res.get("state"), PENDING)


# ---------- doctor ----------

def doctor_lines() -> list[tuple[str, str, str]]:
    """(level, what, how to fix): the scanner, the push key, and each sandboxed project's push field."""
    import push_checks
    out = []
    exe = push_checks.gitleaks()
    if exe:
        v = push_checks.gitleaks_version(exe)
        if v is None or v < push_checks.GITLEAKS_MIN:
            out.append(("bad", _("gitleaks {version} is installed but has no stdin scan (needs {min}): the platform "
                                 "refuses every push it scans", version=".".join(map(str, v or ())) or "?",
                                 min=".".join(map(str, push_checks.GITLEAKS_MIN))), _("update or remove gitleaks")))
        else:
            out.append(("ok", _("gitleaks {version}: platform pushes are scanned by it and by the built-in rules",
                                version=".".join(map(str, v))), ""))
    else:
        out.append(("info", _("gitleaks is not installed: platform pushes are scanned by the built-in rules only"),
                    _("install gitleaks (8.19 or newer) for a wider scan")))
    import runtime_ports
    for rt in dict.fromkeys([runtime_ports.default_name(), *runtime_ports.installed()]):
        try:
            if rt != "none" and "session_write" not in runtime_ports.capabilities(rt):
                out.append(("info", _("{runtime}'s sessions can't ask the platform to push (brunna push): it has no "
                                      "{why}", runtime=rt, why=runtime_ports.missing_label(rt, "session_write")), ""))
        except (KeyError, ImportError):
            continue
    try:
        import git_push_grant
    except ImportError:
        git_push_grant = None
    for name, doc in sorted(hubconf.projects().items()):
        gp = ((doc.get("enforce") or {}).get("git_push")) or {}
        if gp.get("branches", "none") == "none" or not isolation.sandboxed({**doc, "name": name}):
            continue
        origin = (doc.get("paths") or {}).get("origin") or ""
        if local_origin(origin):
            out.append(("ok", _("{project}: the platform pushes {branches} branches to a local remote", project=name,
                                branches=gp["branches"]), ""))
            continue
        status = getattr(git_push_grant, "status", None) if git_push_grant else None
        setup_cmd = f"{names.current('cli')} push setup {name}"
        if git_push_grant is None:
            out.append(("bad", _("{project}: enforce.git_push is {branches}, but the platform's push key isn't built on "
                                 "this install: its pushes are refused", project=name, branches=gp["branches"]), ""))
            continue
        if callable(status):
            st = status(name) or {}
            if st.get("state") in ("not_set_up", "unsupported"):
                out.append(("bad", _("{project}: the platform's push key: {why}", project=name,
                                     why=st.get("detail") or st.get("state")), setup_cmd))
                continue
        out.append(credential_probe(name, origin, setup_cmd))
    return out


def credential_probe(project: str, origin: str, setup_cmd: str) -> tuple[str, str, str]:
    """ls-remote exactly as the worker would reach the remote (its environment from push_credential_env, from this
    process's environment: doctor's, which the dispatch service's may not match). → a doctor row."""
    try:
        env = push_credential_env(project, origin, owner_form=False)
        url = push_url(project, origin, owner_form=False)
    except Refused as e:
        return "bad", f"{project}: {e}", setup_cmd
    try:
        r = subprocess.run(["git", *SAFE, "ls-remote", "--heads", "--", url], cwd=broker_dir(project), env=env,
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "bad", _("{project}: the platform's push credential: {why}", project=project,
                        why=class_reason("unreachable")), ""
    if r.returncode == 0:
        return "ok", _("{project}: the platform's push credential reaches its remote", project=project), ""
    _check, kind, fix, _cause = classify(r.stderr)
    return "bad", _("{project}: the platform's push credential: {why}", project=project, why=class_reason(kind)), \
        setup_cmd if fix == "owner" else ""


# ---------- the forms ----------

USAGE = ("usage: brunna push [<branch>] [--no-wait | --wait <seconds>]   (in a sandboxed project's folder)\n"
         "       brunna push --owner <project> [<branch>] [--yes]\n"
         "       brunna push setup <project> [--branches none|others|all] [--email <address>…]")


def main(argv: list[str], cwd: pathlib.Path | None = None) -> int:
    if argv[:1] == ["serve"] and len(argv) == 2:
        return serve(argv[1])
    if not argv or argv[0] != "push":
        print(_(USAGE), file=sys.stderr)
        return REFUSED
    args = argv[1:]
    try:
        if args[:1] == ["setup"]:   # the owner's setup is the push grant's (bin/git_push_grant.py): never a project
            return subprocess.run([*hubconf.launch(BIN / "git_push_grant.py"), "setup", *args[1:]]).returncode
        if "--owner" in args:
            rest = [a for a in args if a != "--owner"]
            pos = [a for a in rest if not a.startswith("-")]
            if not pos:
                raise Refused(_(USAGE))
            project = pos[0]
            rest.remove(project)
            return owner_push(project, rest)
        here = project_of_folder(cwd or pathlib.Path.cwd())
        if here:
            name, doc = here
            if name != hubconf.hub_project() and isolation.sandboxed(doc):
                return client(name, args, cwd or pathlib.Path.cwd())
            raise Refused(_("{project} is not sandboxed: push with git as usual; for another project use brunna push "
                            "--owner <project> <branch>", project=name))
        if not args or args[0].startswith("-"):
            raise Refused(_(USAGE))
        return owner_push(args[0], args[1:])
    except Refused as e:   # Failed too
        if isinstance(e, Failed):
            print(_("not pushed: {why}", why=e), file=sys.stderr)
            return FAILED
        print(_("refused: {why}", why=e), file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
