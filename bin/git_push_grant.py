#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The platform's push grant for a sandboxed project: one write deploy key per repo, held by the platform
(docs/design/platform-push.md, open question 1 "If the grant", and "The owner's answers (11-15)").

The platform pushes a sandboxed project's branches with this key only, never with the owner's own gh, ssh agent or
Keychain: its reach is one repo, it works with the screen locked and on Linux, and when it is missing or broken the push
is refused with an owner to-do. There is no fallback to the owner's credential.

Where it lives: `<secrets>/git-push/<project>/` (folder 700): `key` and `key.pub` (600), and `grant.json` (the repo, the
push URL, the key's fingerprint and public half, the deploy key's id, when it was made). Outside `<secrets>/credentials/`,
whose `*/*.json` files credentials.py walks as its own grants; every sandbox denies the secrets directory
(policy.secret_dirs). Never in the cloud backup (backup.covers, the restic excludes): re-granting is cheap, so after a
restore check-secrets says it is missing and the owner runs setup again.

The forge: GitHub only (`github.com`, its host keys pinned in bin/known_hosts/github.com, checked in). Any other remote,
an ssh alias included (`-F /dev/null` reads no ssh config), is refused with that reason: a forge without deploy keys
gets no platform push, never a fallback.

For the push core (bin/git_remote.py):
  ssh_env(project) -> dict   what git's environment is overlaid with for a push through the grant (it must win over
                             the owner's environment: GIT_SSH_COMMAND, an empty global config, no system config, no
                             prompt); raises NotSetUp(reason) when the key or its grant is missing, unreadable, or no
                             longer matches the registry's paths.origin
  push_url(project) -> str   the URL to push to: git@github.com:<owner>/<repo>.git (paths.origin may be https://, and
                             an https URL never uses the key); raises NotSetUp the same way
  status(project) -> dict    {state: ok | not_set_up | not_needed | unsupported, detail, repo, fingerprint, created},
                             no network (doctor and the status page)
  probe(project)             (ok, reason): git ls-remote through the grant (check-secrets' daily probe)

Owner-run (`brunna push setup <project>`):
  git_push_grant.py setup <project> [--branches none|others|all] [--email <address>]… [--yes] [--grant-only]
      makes the key, adds it to the repo as a write deploy key with the owner's gh (`gh repo deploy-key add
      --allow-write`), checks it with `git ls-remote` through ssh_env, records the grant; then the policy (branches,
      emails: asked, or the flags) goes into the registry's enforce.git_push (only once the grant works) and the project
      repo's user.name/user.email are set to the allowed address (as the project, through project-git-job.py), so its
      sessions' commits pass the push's identity check. A rerun with a working grant keeps it.
      The owner's gh is never used without saying so and a yes (11-16, platform-push.md "the owner is asked before
      their GitHub login is used"): before a deploy key is added or replaced, consent() prints what it will do (the gh
      account, the repo, write access to that repo only, where the key stays, how to undo it), then asks [y/N].
      Passing --branches others|all (onboarding: --push) is the yes, and the text is still printed. --yes alone, or a
      run nobody answers, never makes a key: none (the registry as it is) plus the one-time setup to-do.
  git_push_grant.py setup <project> --revoke     deletes the deploy key at GitHub and the key here (the flag is the
                                                 yes; the gh account and the repos are printed first)
  git_push_grant.py status [<project>…]
  git_push_grant.py onboard <project> [--push none|others|all] [--push-email <address>]… [--agent] [--confirm]
      onboarding's step after registration (onboard-project.sh, upgrade-idea.py): with no --push, none plus the
      one-time setup to-do, whatever the remote's visibility; --push others|all is the yes for the key (the disclosure
      is printed); --confirm: that --push was a person's typed answer (connect's question), so the disclosure is
      followed by the [y/N] question; --agent: the choice was not a person's, so none, and the to-do too
  git_push_grant.py visibility <url>             public | private | unknown (an anonymous ls-remote)
  git_push_grant.py extra <json> [--sandbox]     onboard-project.sh's ONBOARD_REGISTRY_EXTRA with enforce.sandbox merged in

Exit: 0 done, 1 failed (gh, ssh, the network), 2 refused.
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import names  # noqa: E402

_ = i18n.domain(__file__)
_c = i18n.domain(__file__, content=True)

OK, FAILED, REFUSED = 0, 1, 2
BRANCHES = ("none", "others", "all")
KNOWN_HOSTS = {"github.com": BIN / "known_hosts" / "github.com"}
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIMEOUT_S = 60
PRIVATE_SIGNALS = ("could not read username", "terminal prompts disabled", "repository not found",
                   "authentication failed")


class NotSetUp(Exception):
    """No working grant for this project: the push is refused, and the owner runs `brunna push setup <project>`."""


class Refused(Exception):
    """Setup can't make a grant here (the forge, the project): nothing was changed."""


class Failed(Exception):
    """gh, ssh-keygen, ssh or the network failed."""


class Declined(Refused):
    """Nobody said yes to using the owner's gh login: no deploy key is made and nothing is written."""


# ---------- where things are ----------

def grants_dir() -> pathlib.Path:
    return hubconf.secrets_dir() / "git-push"


def grant_dir(project: str) -> pathlib.Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project or ""):
        raise Refused(_("{name} is not a project name", name=project))
    return grants_dir() / project


def state_file(project: str) -> pathlib.Path:
    return hubconf.STATE / "git-push-grant" / f"{project}.json"


def setup_command(project: str) -> str:
    return f"{names.current('cli')} push setup {project}"


def doc_of(project: str) -> dict | None:
    doc = (hubconf.projects() or {}).get(project)
    if doc is None:
        import registry
        registry.refresh()   # registered a moment ago (onboarding): the index may not have it yet
        doc = (hubconf.projects() or {}).get(project)
    return doc


def origin_of(doc: dict | None) -> str | None:
    o = ((doc or {}).get("paths") or {}).get("origin")
    return None if not o or o == "(native)" else str(o)


def git_push_of(doc: dict | None) -> dict:
    gp = ((doc or {}).get("enforce") or {}).get("git_push")
    return gp if isinstance(gp, dict) else {}


# ---------- the remote ----------

def parse_origin(origin: str | None) -> tuple[str, str]:
    """(host, owner/repo) of a remote the grant can serve, or Refused with why. git@host:o/r(.git), ssh://[git@]host[:22]/o/r,
    https://host/o/r(.git)."""
    if not origin:
        raise Refused(_("it has no remote (paths.origin): there is nothing to push to"))
    m = None if origin.startswith(("/", "~", "-", "file:")) else (
        re.fullmatch(r"(?:([A-Za-z0-9._-]+)@)?([A-Za-z0-9.-]+):(?!//)/?(.+)", origin)
        or re.fullmatch(r"ssh://(?:([A-Za-z0-9._-]+)@)?([A-Za-z0-9.-]+)(?::22)?/(.+)", origin)
        or re.fullmatch(r"https://()([A-Za-z0-9.-]+)/(.+)", origin))
    if not m or m.group(1) not in (None, "", "git"):
        raise Refused(_("paths.origin {url} is not a GitHub remote: the platform pushes only to GitHub, through a deploy "
                        "key (no other forge, no local path)", url=origin))
    host, path = m.group(2).lower(), m.group(3).rstrip("/")
    path = path[:-4] if path.endswith(".git") else path
    if host not in KNOWN_HOSTS:
        raise Refused(_("paths.origin {url}: {host} is not GitHub. A forge without GitHub's deploy keys gets no platform "
                        "push (and an ssh alias isn't read: the push reads no ssh config); push it yourself with {cli} "
                        "push --owner", url=origin, host=host, cli=names.current("cli")))
    if not REPO.fullmatch(path) or ".." in path:
        raise Refused(_("paths.origin {url} doesn't name one repo (<owner>/<repo>)", url=origin))
    return host, path


def ssh_url(host: str, repo: str) -> str:
    return f"git@{host}:{repo}.git"


def https_url(origin: str) -> str | None:
    """The https form of a remote, for the anonymous probe: the URL itself (an http:// one too), or the mechanical one for
    host:path and ssh://. None for a local path, and for a scheme with no such form (git://)."""
    if origin.startswith(("https://", "http://")):
        return origin
    m = (re.fullmatch(r"(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+):(?!//)/?(.+)", origin)
         or re.fullmatch(r"ssh://(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+)(?::\d+)?/(.+)", origin))
    return f"https://{m.group(1)}/{m.group(2)}" if m and not origin.startswith(("/", "~", "file:")) else None


def anonymous_ls_remote(url: str) -> subprocess.CompletedProcess:
    """git ls-remote with no credential at all: a throwaway HOME, no global or system config, no helper, no prompt. The one
    place the probe reaches the network (tests replace it)."""
    with tempfile.TemporaryDirectory(prefix="push-probe-", dir=hubconf.private_tmp()) as home:
        env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        try:
            return subprocess.run(["git", "-c", "credential.helper=", "ls-remote", "--heads", "--", url], cwd=home,
                                  env=env, capture_output=True, text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([], 124, "", "timed out")


def visibility(origin: str | None) -> str:
    """public | private | unknown, as an anonymous ls-remote of the https form sees it (check 8's probe). Unknown (no https
    form, the network) is treated as public by every caller: a false refusal, never a leak."""
    url = https_url(origin or "")
    if not url:
        # a local path is private; anything else with no https form (git://, an unknown scheme) can't be told, which
        # counts as public
        return "private" if (origin or "").startswith(("/", "~", "file:")) else "unknown"
    r = anonymous_ls_remote(url)
    if r.returncode == 0:
        return "public"
    # git's own words for "it wants a credential", read with the quoted URLs taken out (a repo named "x-403" says
    # nothing); an HTTP status alone (a rate limit's 403) can't tell, so it is unknown, which counts as public
    err = re.sub(r"'[^'\n]*'", "''", r.stderr or "").lower()
    if any(s in err for s in PRIVATE_SIGNALS):
        return "private"
    return "unknown"


# ---------- the key and the grant ----------

def pub_blob(pub: str) -> bytes:
    parts = pub.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("not an ed25519 public key")
    return base64.b64decode(parts[1], validate=True)


def fingerprint(pub: str) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(pub_blob(pub)).digest()).decode().rstrip("=")


def key_public_half(key: pathlib.Path) -> str:
    """What ssh-keygen derives from the private key: the public half that must be the one granted."""
    try:
        r = subprocess.run(["ssh-keygen", "-y", "-f", str(key)], capture_output=True, text=True, timeout=TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise NotSetUp(_("ssh-keygen is not installed (OpenSSH's client)")) from None
    if r.returncode != 0:
        raise NotSetUp(_("its key can't be read ({why})", why=(r.stderr or "").strip()[-200:]))
    return " ".join(r.stdout.split()[:2])


def private_file(p: pathlib.Path, mode: int) -> str | None:
    """Why p isn't a private regular file of this user with exactly this mode, or None."""
    try:
        st = p.lstat()
    except OSError:
        return _("{path} is missing", path=p)
    if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        return _("{path} is not a plain file or folder", path=p)
    if st.st_uid != os.getuid():
        return _("{path} is not yours", path=p)
    if stat.S_IMODE(st.st_mode) != mode:
        return _("{path} has mode {mode}, not {want}", path=p, mode=oct(stat.S_IMODE(st.st_mode)), want=oct(mode))
    return None


def read_grant(project: str) -> dict:
    """The grant, checked against the key on disk and the registry's origin: NotSetUp with the reason otherwise."""
    d = grant_dir(project)
    for p, mode in ((grants_dir(), 0o700), (d, 0o700), (d / "key", 0o600), (d / "grant.json", 0o600)):
        if (why := private_file(p, mode)):
            raise NotSetUp(why)
    try:
        g = json.loads((d / "grant.json").read_text())
    except (OSError, ValueError) as e:
        raise NotSetUp(_("its grant record can't be read ({why})", why=e)) from None
    if not isinstance(g, dict) or not all(isinstance(g.get(k), str) for k in ("repo", "host", "public_key", "push_url")):
        raise NotSetUp(_("its grant record is incomplete"))
    try:
        host, repo = parse_origin(origin_of(doc_of(project)))
    except Refused as e:
        raise NotSetUp(str(e)) from None
    if (host, repo.lower()) != (g["host"], g["repo"].lower()) or g["push_url"] != ssh_url(g["host"], g["repo"]):
        raise NotSetUp(_("the grant is for {granted}, but paths.origin is now {repo}: the origin changed, so the grant "
                         "no longer applies", granted=g["repo"], repo=repo))
    if key_public_half(d / "key") != " ".join(g["public_key"].split()[:2]):
        raise NotSetUp(_("its key is not the one granted"))
    return g


def empty_config() -> pathlib.Path:
    """An empty global git config of the platform's own (no owner credential helper, insteadOf or sshCommand is read)."""
    f = hubconf.STATE / "git-push-grant" / "empty.gitconfig"
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.is_file() or f.is_symlink() or f.stat().st_size:
        f.unlink(missing_ok=True)
        f.write_text("")
    return f


def ssh_command(key: pathlib.Path, known_hosts: pathlib.Path) -> str:
    """IdentityFile=none first: ssh then adds none of the owner's default keys (~/.ssh/id_*), even if the -i file were
    gone; IdentitiesOnly alone doesn't stop those."""
    return shlex.join(["ssh", "-F", "/dev/null", "-o", "IdentityFile=none", "-i", str(key), "-o", "IdentitiesOnly=yes",
                       "-o", "IdentityAgent=none",
                       "-o", "PasswordAuthentication=no", "-o", "BatchMode=yes",
                       "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                       "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no", "-o", "ControlMaster=no",
                       "-o", "ControlPath=none", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=20"])


def run_copy(project: str, g: dict, run_dir: pathlib.Path) -> pathlib.Path:
    """The grant's key copied into the run's own private folder (read without following a link, written 600 and new),
    and checked to be the granted one: what ssh is pointed at can't vanish or change under the run."""
    if (why := private_file(run_dir, 0o700)):
        raise NotSetUp(why)
    fd = os.open(grant_dir(project) / "key", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(64 << 10)
    copy = run_dir / "push-key"
    out = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(out, "wb") as fh:
        fh.write(data)
    if key_public_half(copy) != " ".join(g["public_key"].split()[:2]):
        copy.unlink(missing_ok=True)
        raise NotSetUp(_("its key is not the one granted"))
    return copy


def new_run_dir() -> pathlib.Path:
    """A private folder for one run's copy of the key, removed when this process ends."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="push-run-", dir=hubconf.private_tmp()))
    atexit.register(shutil.rmtree, d, True)
    return d


def ssh_env(project: str, run_dir: pathlib.Path | None = None) -> dict[str, str]:
    """The environment overlay for a push through the grant. It must win over the owner's environment (git_remote's
    owner_env keeps GIT_SSH* and SSH_AUTH_SOCK): the ssh command names a copy of the key in run_dir (a private folder
    the caller owns and removes; without one, a new one removed at this process's exit), no default keys, no agent and
    the pinned host keys; the global config is the platform's empty one and the system's is off, so no credential
    helper or insteadOf of the owner's applies."""
    g = read_grant(project)
    key = run_copy(project, g, run_dir or new_run_dir())
    return {"GIT_SSH_COMMAND": ssh_command(key, KNOWN_HOSTS[g["host"]]),
            "GIT_SSH_VARIANT": "ssh", "GIT_CONFIG_GLOBAL": str(empty_config()), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}   # empty: no helper asks (git reads empty as unset)


@contextlib.contextmanager
def push_env(project: str):
    """ssh_env with its run folder removed on leaving: `with push_env(p) as env: …`."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="push-run-", dir=hubconf.private_tmp()))
    try:
        yield ssh_env(project, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def push_url(project: str) -> str:
    return read_grant(project)["push_url"]


def needed(doc: dict | None) -> bool:
    """Whether a project has the derived grant `git-push`: its enforce.git_push lets the platform push something, to a
    remote that isn't a local path."""
    o = origin_of(doc)
    return git_push_of(doc).get("branches", "none") != "none" and bool(o) and https_url(o) is not None


def status(project: str) -> dict:
    """For doctor and the status page; no network."""
    doc = doc_of(project)
    row = {"project": project, "state": "ok", "detail": "", "repo": None, "fingerprint": None, "created": None}
    if not doc:
        return {**row, "state": "not_needed", "detail": _("not registered")}
    try:
        parse_origin(origin_of(doc))
    except Refused as e:
        return {**row, "state": "not_needed" if not origin_of(doc) else "unsupported", "detail": str(e)}
    try:
        g = read_grant(project)
    except NotSetUp as e:
        if not needed(doc) and not grant_dir(project).exists():
            return {**row, "state": "not_needed", "detail": _("no grant, and none needed while the platform pushes "
                                                              "nothing for it")}
        return {**row, "state": "not_set_up", "detail": _("{why}; the owner runs {command}", why=e,
                                                          command=setup_command(project))}
    return {**row, "repo": g["repo"], "fingerprint": g.get("fingerprint"), "created": g.get("created"),
            "detail": _("deploy key {id} on {repo}", id=g.get("deploy_key_id"), repo=g["repo"])}


def ls_remote(url: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """git ls-remote through the grant (tests replace it, or put a stand-in ssh on PATH)."""
    base = {k: os.environ[k] for k in ("HOME", "PATH", "USER", "LOGNAME", "LANG") if k in os.environ}
    with tempfile.TemporaryDirectory(prefix="push-grant-", dir=hubconf.private_tmp()) as cwd:
        try:
            return subprocess.run(["git", "ls-remote", "--heads", "--", url], cwd=cwd, env={**base, **env, "LC_ALL": "C"},
                                  capture_output=True, text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([], 124, "", "timed out")


def check(project: str) -> tuple[str, str]:
    """(ok | refused | not_set_up | failed, why): ls-remote through the grant. refused: GitHub said no to the key (gone
    there, or gh's sign-in that added it revoked): only that is a reason to make a new one. failed: the network, a
    changed host key, anything else: the grant is kept."""
    try:
        with push_env(project) as env:
            r = ls_remote(push_url(project), env)
    except NotSetUp as e:
        return "not_set_up", str(e)
    if r.returncode == 0:
        return "ok", ""
    err = (r.stderr or "").strip()
    if "permission denied (publickey)" in err.lower():
        return "refused", _("GitHub refused the deploy key (it was removed, or gh's sign-in that added it was revoked)")
    if "host key" in err.lower():
        return "failed", _("GitHub's host key didn't match the pinned one")
    return "failed", _("ls-remote through the grant failed: {why}", why=err[-200:] or r.returncode)


def probe(project: str) -> tuple[bool, str]:
    state, why = check(project)
    return state == "ok", why


# ---------- gh (the owner's, only here) ----------

def gh(*args: str) -> subprocess.CompletedProcess:
    """The owner's gh (tests put a recording stand-in first on PATH)."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Failed(_("gh is not installed: the deploy key is added with gh")) from None
    except subprocess.TimeoutExpired:
        raise Failed(_("gh {args} timed out", args=" ".join(args[:3]))) from None


def deploy_keys(repo: str) -> list[dict]:
    r = gh("repo", "deploy-key", "list", "--repo", gh_repo(repo), "--json", "id,key,title,readOnly")
    if r.returncode != 0:
        raise Failed(_("gh couldn't list {repo}'s deploy keys: {why}", repo=repo, why=r.stderr.strip()[-300:]))
    try:
        keys = json.loads(r.stdout or "[]")
    except ValueError:
        raise Failed(_("gh's list of deploy keys wasn't JSON")) from None
    out = []
    for k in keys:
        if isinstance(k, dict):
            # gh 2.103 answers `--json readOnly` with the API's own field, `read_only` (measured 11-16); either is read,
            # and a key with neither stays unknown, which counts as read-only (fail closed)
            k["readOnly"] = k.get("readOnly", k.get("read_only"))
            out.append(k)
    return out


def gh_repo(repo: str) -> str:
    """gh's --repo with the host named, so the owner's GH_HOST or default host can't point it elsewhere."""
    return f"github.com/{repo}"


def delete_key(repo: str, key_id) -> bool:
    """→ True when gh deleted it, False when GitHub had no such key; Failed otherwise."""
    r = gh("repo", "deploy-key", "delete", str(key_id), "--repo", gh_repo(repo))
    if r.returncode == 0:
        return True
    if "not found" in (r.stderr or "").lower():
        return False
    raise Failed(_("gh couldn't delete deploy key {id} from {repo}: {why}; the key here is kept, so run this again",
                   id=key_id, repo=repo, why=(r.stderr or "").strip()[-300:]))


def title(project: str) -> str:
    return f"{names.current('cli')} push: {project}"


def gh_login() -> str:
    """The account the owner's gh is signed in as on github.com, named before that login is used."""
    r = gh("api", "user", "--hostname", "github.com", "--jq", ".login")
    login = (r.stdout or "").strip().strip('"')   # an enterprise-managed account's login has a _
    if r.returncode != 0 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", login):
        raise Failed(_("gh isn't signed in to github.com ({why}): sign in with gh auth login and run this again; "
                       "nothing was changed", why=(r.stderr or "").strip()[-200:] or r.returncode))
    return login


def disclosure(project: str, repo: str, account: str, old: dict | None = None) -> str:
    """What adding the deploy key does, said before the owner's gh is used for it."""
    lines = [_("To push {name}'s branches, the platform will use your gh login ({account}) on github.com/{repo} to add a "
               "deploy key with WRITE access to that repo. The key can write only that repo, nothing else of your "
               "account. A deploy key titled \"{title}\" that the platform added there before is replaced.",
               name=project, account=account, repo=repo, title=title(project))]
    if old:
        lines.append(_("It first deletes the platform's earlier deploy key {id} from github.com/{old}.",
                       id=old.get("deploy_key_id"), old=old.get("repo")))
    lines.append(_("The platform keeps the private key in its secrets folder (not in the cloud backup) and uses it only "
                   "to push the branches of {name} that its sessions ask for, after the automatic checks.", name=project))
    lines.append(_("To undo it: {command} --revoke (deletes the key at GitHub and here).", command=setup_command(project)))
    return "\n".join(lines)


def consent(project: str, repo: str, mode: str, old: dict | None = None) -> None:
    """The owner's yes before their gh adds or replaces a deploy key. mode: explicit (the branches were given on the
    command line: the disclosure is printed, and that is the yes), ask (the disclosure, then [y/N]: only y or yes goes
    on; an empty answer or no input is a no), anything else (nobody to ask: no key, and no gh call at all). Declined
    otherwise, before anything has been changed."""
    if mode not in ("explicit", "ask"):
        raise Declined(_("Nobody said yes to using your gh login, so no deploy key was made for {name} and nothing was "
                         "changed: run {command} yourself (it says what it will do and asks first), or give --branches "
                         "others|all", name=project, command=setup_command(project)))
    print(disclosure(project, repo, gh_login(), old))
    if mode == "explicit":
        print(_("Going ahead: the branches were given on the command line, which counts as your yes."))
        return
    try:
        a = input(_("Add the deploy key with your gh login?") + " [y/N] ").strip().lower()
    except EOFError:
        a = ""
    if a not in ("y", "yes"):
        raise Declined(_("You said no: no deploy key was added for {name}, and nothing was changed. {command} asks "
                         "again whenever you want it.", name=project, command=setup_command(project)))


def same_key(a: str, b: str) -> bool:
    return a.split()[:2] == b.split()[:2]


# ---------- setup ----------

def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_private(path: pathlib.Path, text: str) -> None:
    """Atomically, 600, never through a link."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def private_dirs(project: str) -> pathlib.Path:
    top = grants_dir()
    hubconf.secrets_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    for d in (top, top / project):
        d.mkdir(mode=0o700, exist_ok=True)
        if d.is_symlink():
            raise Refused(_("{path} is a link", path=d))
        os.chmod(d, 0o700)
    return top / project


def make_key(project: str) -> pathlib.Path:
    """A new ed25519 key in a fresh folder of its own (key, key.pub at 600); the grant folder isn't touched yet."""
    private_dirs(project)
    new = pathlib.Path(tempfile.mkdtemp(prefix="push-key-", dir=hubconf.private_tmp()))   # beside it, on one disk
    try:
        r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", title(project), "-f", str(new / "key")],
                           capture_output=True, text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        shutil.rmtree(new, ignore_errors=True)
        raise Failed(_("ssh-keygen is not installed (OpenSSH's client)")) from None
    if r.returncode != 0:
        shutil.rmtree(new, ignore_errors=True)
        raise Failed(_("ssh-keygen failed: {why}", why=(r.stderr or "").strip()[-200:]))
    for f in ("key", "key.pub"):
        os.chmod(new / f, 0o600)
    return new


def grant(project: str, doc: dict, mode: str = "no") -> dict:
    """A working grant for the project: the one there is when it still works at GitHub, or a new one made, added there
    and checked. Each step that fails undoes the ones before it (a deploy key added and not working is deleted again).
    One whose files are fine but that GitHub refuses (deleted there, gh's sign-in revoked) is made again: that is what
    the probe's to-do asks the owner to run this for. Keeping a working grant uses no gh; anything else needs the owner's
    yes first (consent, by mode), before a file is written or gh is called."""
    host, repo = parse_origin(origin_of(doc))
    with contextlib.suppress(NotSetUp):
        g = read_grant(project)
        state, why = check(project)
        if state == "ok":
            print(_("✓ the grant exists: deploy key {id} on {repo} ({fp})", id=g.get("deploy_key_id"), repo=g["repo"],
                    fp=g.get("fingerprint")))
            return g
        if state != "refused":   # the network, the host key: no reason to drop a grant that may well work
            raise Failed(_("the grant couldn't be checked ({why}); it is kept: run this again once GitHub answers",
                           why=why))
        print(_("The grant here no longer works ({why}): making a new one", why=why))
    d = grant_dir(project)
    old = record(project)
    moved = bool(old and old.get("repo") and old.get("deploy_key_id") is not None
                 and old["repo"].lower() != repo.lower())
    consent(project, repo, mode, old if moved else None)
    if moved:
        # the origin changed: the old repo's write key goes before a new one is made
        if delete_key(old["repo"], old["deploy_key_id"]):
            print(_("✓ deploy key {id} deleted from {repo}", id=old["deploy_key_id"], repo=old["repo"]))
    new = make_key(project)
    try:
        pub = (new / "key.pub").read_text().strip()
        # a run that stopped after adding its key at GitHub left one with this title: replaced, not piled up
        for k in deploy_keys(repo):
            if k.get("title") == title(project) and not same_key(str(k.get("key", "")), pub):
                delete_key(repo, k.get("id"))
        r = gh("repo", "deploy-key", "add", str(new / "key.pub"), "--repo", gh_repo(repo), "--allow-write", "--title",
               title(project))
        if r.returncode != 0:
            raise Failed(_("gh couldn't add the deploy key to {repo}: {why}. An organisation or forge that turns deploy "
                           "keys off gets no platform push", repo=repo, why=r.stderr.strip()[-300:]))
        added = next((k for k in deploy_keys(repo) if same_key(str(k.get("key", "")), pub)), None)
        if not added:
            raise Failed(_("the deploy key added to {repo} isn't in its list", repo=repo))
        if added.get("readOnly") is not False:
            delete_key(repo, added.get("id"))
            raise Failed(_("GitHub added the key to {repo} read-only: the platform needs a write deploy key", repo=repo))
        rec = {"v": 1, "project": project, "host": host, "repo": repo, "push_url": ssh_url(host, repo),
               "origin": origin_of(doc), "public_key": pub, "fingerprint": fingerprint(pub),
               "deploy_key_id": added.get("id"), "title": title(project), "created": now()}
        for f in ("key", "key.pub"):
            os.replace(new / f, d / f)
        write_private(d / "grant.json", json.dumps(rec, indent=1) + "\n")
    finally:
        shutil.rmtree(new, ignore_errors=True)
    ok, why = probe(project)
    if not ok:
        delete_key(repo, rec["deploy_key_id"])   # a new key that doesn't work, whatever the reason
        for f in ("grant.json", "key", "key.pub"):
            (d / f).unlink(missing_ok=True)
        raise Failed(_("the new deploy key didn't work ({why}); it was removed again", why=why))
    print(_("✓ deploy key {id} added to {repo} with write access, and checked ({fp})", id=rec["deploy_key_id"], repo=repo,
            fp=rec["fingerprint"]))
    return rec


def owner_git(*args: str) -> str:
    """The owner's global git setting (never a project's repo: run from a folder of the platform's)."""
    r = subprocess.run(["git", "config", "--global", *args], capture_output=True, text=True, cwd=hubconf.private_tmp())
    return r.stdout.strip() if r.returncode == 0 else ""


def noreply() -> str | None:
    r = gh("api", "user", "--jq", '"\\(.id)+\\(.login)@users.noreply.github.com"')
    out = r.stdout.strip().strip('"')
    return out if r.returncode == 0 and EMAIL.fullmatch(out) else None


def default_emails(vis: str) -> list[str]:
    """Check 6's proposal: on a public (or unknown) remote GitHub's noreply address only (a deploy key has no user, so
    GH007 email privacy can't stop a real address); on a private one also the global email."""
    out = []
    if vis == "private" and EMAIL.fullmatch(e := owner_git("--get", "user.email")):
        out.append(e)
    with contextlib.suppress(Failed):
        if (n := noreply()):
            out.append(n)
    return out


def field_supported() -> bool:
    """Whether this platform's registry knows enforce.git_push (the push core adds it)."""
    import registry
    return not registry.validate_enforce({"name": "probe", "enforce": {"sandbox": True, "git_push": {"branches": "none"}}})


def commit_registry(message: str, *files: str) -> None:
    subprocess.run([str(BIN / "commit-hub-files.sh"), message, *files], check=False)


def with_git_push(text: str, git_push: dict) -> str:
    """The registry entry's text with only its enforce.git_push block written (replaced, or added at the end of
    enforce:), every other byte as it was: the entry's comments record the owner's decisions. Refused for a layout this
    doesn't edit (no block-style enforce:, a flow-style git_push)."""
    import yaml
    lines = text.splitlines(keepends=True)
    top = next((i for i, ln in enumerate(lines) if re.fullmatch(r"enforce:[ \t]*(#.*)?\n?", ln)), None)
    if top is None:
        raise Refused(_("its registry entry has no block-style enforce: to add git_push to; set it by hand"))
    end = top + 1   # the enforce block: everything up to the next line that starts a top-level key
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
        end += 1
    body = [i for i in range(top + 1, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    if not body:
        raise Refused(_("its registry entry has no block-style enforce: to add git_push to; set it by hand"))
    indent = re.match(r"[ \t]*", lines[body[0]]).group(0)
    rendered = yaml.safe_dump({"git_push": git_push}, allow_unicode=True, sort_keys=False, default_flow_style=False,
                              width=200)
    block = ["".join((indent, ln, "\n")) for ln in rendered.splitlines()]
    at = next((i for i in body if re.match(re.escape(indent) + r"git_push:", lines[i])), None)
    if at is None:   # added after enforce's last line of content
        last = body[-1]
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        return "".join(lines[:last + 1] + block + lines[last + 1:])
    if not re.fullmatch(re.escape(indent) + r"git_push:[ \t]*(#.*)?\n?", lines[at]):
        raise Refused(_("its registry entry's enforce.git_push isn't in block style; set it by hand"))
    comment = re.search(r"[ \t]+#.*", lines[at])
    if comment:   # the comment on the git_push: line stays
        block[0] = block[0].rstrip("\n") + comment.group(0).rstrip("\n") + "\n"
    stop = at + 1   # its own lines: everything indented deeper (blank lines between them included)
    while stop < end and (not lines[stop].strip() or re.match(re.escape(indent) + r"[ \t]", lines[stop])):
        stop += 1
    # blank and comment lines at its end belong to what follows (a note above the next key)
    while stop > at + 1 and (not lines[stop - 1].strip() or lines[stop - 1].lstrip().startswith("#")):
        stop -= 1
    return "".join(lines[:at] + block + lines[stop:])


def write_policy(project: str, policy: dict) -> bool:
    """enforce.git_push into the registry entry (validated first), committed like every script-written registry change.
    Only that block of the file changes (with_git_push), and the result must read back as exactly the entry intended.
    → whether it was written; when the registry doesn't know the field yet, says what to set instead."""
    shown = json.dumps(policy, ensure_ascii=False)
    if not field_supported():
        print(_("This platform's registry has no enforce.git_push yet: set it in projects/{name}.yaml once it does: "
                "enforce.git_push: {policy}", name=project, policy=shown))
        return False
    import registry
    import yaml
    reg = hubconf.registry_dir() / f"{project}.yaml"
    text = reg.read_text()
    doc = yaml.safe_load(text) or {}
    enf = doc.setdefault("enforce", {})
    enf["git_push"] = {**(enf.get("git_push") if isinstance(enf.get("git_push"), dict) else {}), **policy}
    if (errs := registry.validate(doc, project)):
        raise Refused(_("enforce.git_push {policy} isn't valid for {name}: {errors}", policy=shown, name=project,
                        errors="; ".join(errs)))
    new = with_git_push(text, enf["git_push"])
    try:
        back = yaml.safe_load(new)
    except yaml.YAMLError:
        back = None
    if back != doc or registry.validate(back, project):
        raise Refused(_("writing enforce.git_push into projects/{name}.yaml wouldn't read back as intended; set it by "
                        "hand: enforce.git_push: {policy}", name=project, policy=shown))
    reg.write_text(new)
    commit_registry(f"chore(registry): {project} git_push {policy.get('branches')} (brunna push setup)",
                    f"projects/{project}.yaml")
    registry.refresh()
    print(_("✓ projects/{name}.yaml: enforce.git_push: {policy}", name=project, policy=shown))
    return True


def set_identity(project: str, doc: dict, emails: list[str]) -> None:
    """The project repo's user.email (and user.name) set to the allowed address, as the project (never the owner's git
    in its repo): its sessions' commits then pass the push's identity check. The noreply address when it is allowed."""
    if not emails:
        return
    email = next((e for e in emails if e.endswith("@users.noreply.github.com")), emails[0])
    name = owner_git("--get", "user.name") or hubconf.owner_tag()
    import project_git
    import registry
    ws = registry.dirs(doc).get("workspace")
    if not ws:
        return
    r = project_git.job(project, ws, "set-identity", ws, name, email)
    if r.returncode != 0:
        print(_("⚠️ {name}'s repo identity was not set ({why}): its commits need the author email {email}",
                name=project, why=(r.stderr or "").strip()[-200:], email=email), file=sys.stderr)
    else:
        print(_("✓ {name}'s repo commits as {email}", name=project, email=email))


def ask(question: str, default: str) -> str:
    try:
        a = input(f"{question} [{default}] ").strip()
    except EOFError:
        return default
    return a or default


def warning(vis: str) -> str:
    return _("The remote is {visibility}. On a public remote every branch you allow is published as soon as it is "
             "pushed, and a site or deploy that builds from branches (gh-pages, a hosting 'production branch', preview "
             "deploys) runs it. 'others' keeps the default branch's CI and deploys yours, not the content private.",
             visibility={"public": _("public"), "private": _("private")}.get(vis, _("of unknown visibility (treated as "
                                                                                  "public)")))


def setup(project: str, *, yes: bool = False, branches: str | None = None, emails: list[str] | None = None,
          grant_only: bool = False, confirm: bool = False) -> int:
    """Owner-run: the grant, then the policy. Refused/Failed propagate to main(). The owner's gh is used only with a
    yes (consent): branches given here (--branches) are one; otherwise only a person answering [y/N]. confirm: the
    branches given were a person's typed answer elsewhere (connect's question), so the [y/N] still follows. With no
    yes: nothing written, the to-do raised, REFUSED."""
    doc = doc_of(project)
    if not doc:
        raise Refused(_("{name} is not in the registry", name=project))
    if not ((doc.get("enforce") or {}).get("sandbox")):
        raise Refused(_("{name} is not sandboxed: its agents push with your own git, and there is no platform push to "
                        "set up", name=project))
    if branches is not None and branches not in BRANCHES:
        raise Refused(_("--branches takes none, others or all"))
    bad = [e for e in emails or [] if not EMAIL.fullmatch(e)]
    if bad:
        raise Refused(_("not an email address: {emails}", emails=", ".join(bad)))
    if grant_only and branches is not None:
        raise Refused(_("--grant-only makes the grant and leaves the policy as it is: give it without --branches"))
    parse_origin(origin_of(doc))
    if not field_supported():
        # a grant whose policy can't be written would push nothing: none is made, and the to-do stays open
        raise Refused(_("this platform's registry doesn't know enforce.git_push yet, so no grant is made: update "
                        "brunna, then run {command}", command=setup_command(project)))
    interactive = not yes and sys.stdin.isatty()
    # the yes for using the owner's gh: the branches given here, or a person's [y/N]; nobody to ask is never one
    if confirm:
        mode = "ask" if sys.stdin.isatty() else "no"
    else:
        mode = "explicit" if branches is not None else "ask" if interactive else "no"
    # a rerun starts from what the registry has (the repair the probe's to-do asks for changes no policy)
    current = git_push_of(doc)
    have = current.get("branches") if current.get("branches") in BRANCHES else None
    try:
        if branches is None and not interactive and not grant_only:
            if have is None:   # nobody chose, now or before: none, and the to-do (11-16; no visibility default)
                consent(project, parse_origin(origin_of(doc))[1], "no")
            branches = have
        vis = visibility(origin_of(doc))
        print(warning(vis))
        default = have or ("all" if vis == "private" else "none")
        if interactive and branches is None and not grant_only:
            print(_("Which branches may the platform push for {name}? none: you push yourself; others: every branch but "
                    "the default; all: the default branch too. others and all add a deploy key with write access to "
                    "the repo with your gh login: you are told what that does and asked first.", name=project))
            while (branches := ask(_("none, others or all?"), default)) not in BRANCHES:
                pass
        g = grant(project, doc, mode) if branches != "none" or grant_only else None
    except Declined as e:
        print(e)
        raise_todo(project)
        return REFUSED
    if grant_only:
        close_todo(project)
        return OK
    if branches != "none" and emails is None:
        proposed = list(current.get("emails") or []) or default_emails(vis)
        if interactive:
            got = ask(_("Commit emails the platform's pushes may carry (comma-separated)"), ", ".join(proposed))
            proposed = [e.strip() for e in got.split(",") if e.strip()]
            if (bad := [e for e in proposed if not EMAIL.fullmatch(e)]):
                raise Refused(_("not an email address: {emails}", emails=", ".join(bad)))
        emails = proposed
    # none with no emails given leaves the emails the entry has (setting none again later loses nothing)
    policy = {"branches": branches, **({"emails": list(emails or [])} if branches != "none" or emails else {}),
              # what the probe saw now (unknown counts as public): a push that sees otherwise raises this setup's to-do
              # again (platform-push.md r2 S-l)
              "visibility": "private" if vis == "private" else "public"}
    if branches != "none" and not policy["emails"]:
        print(_("⚠️ no email is allowed yet, so every push will be refused until one is: run {command} --email "
                "<address>", command=setup_command(project)))
    if write_policy(project, policy) and g:   # the identity only matters once the platform pushes
        set_identity(project, doc, policy["emails"])
    if branches == "none" and grant_dir(project).exists():
        print(_("The platform pushes nothing for {name} now; its deploy key stays at GitHub until you run {command} "
                "--revoke", name=project, command=setup_command(project)))
    close_todo(project)
    return OK


def record(project: str) -> dict | None:
    """grant.json as it is, unchecked (None when missing or unreadable)."""
    with contextlib.suppress(OSError, ValueError):
        g = json.loads((grant_dir(project) / "grant.json").read_text())
        return g if isinstance(g, dict) else None
    return None


def revoke(project: str) -> int:
    """The project's write keys deleted at GitHub (the record's, and every key with its title on the record's repo and
    on the registry origin's repo, so a lost or broken record leaves none behind), then the key here. A GitHub that
    can't be reached keeps the key here, to run again. --revoke is the owner's yes; the gh account and the repos are
    said first."""
    d, g = grant_dir(project), record(project)
    repos = [g["repo"]] if g and isinstance(g.get("repo"), str) and REPO.fullmatch(g["repo"]) else []
    with contextlib.suppress(Refused):
        repos.append(parse_origin(origin_of(doc_of(project)))[1])
    if repos:
        account = None
        with contextlib.suppress(Failed):
            account = gh_login()
        print(_("Using your gh login ({account}) to delete the platform's deploy keys for {name} from {repos}, as you "
                "asked.", account=account or _("not signed in"), name=project,
                repos=", ".join(f"github.com/{r}" for r in dict.fromkeys(repos))))
    gone = set()
    if g and repos and g.get("repo") == repos[0] and g.get("deploy_key_id") is not None:
        if delete_key(g["repo"], g["deploy_key_id"]):
            print(_("✓ deploy key {id} deleted from {repo}", id=g["deploy_key_id"], repo=g["repo"]))
        else:
            print(_("deploy key {id} was already gone from {repo}", id=g["deploy_key_id"], repo=g["repo"]))
        gone.add((g["repo"].lower(), str(g["deploy_key_id"])))
    for repo in dict.fromkeys(repos):
        for k in deploy_keys(repo):
            if k.get("title") == title(project) and (repo.lower(), str(k.get("id"))) not in gone:
                if delete_key(repo, k.get("id")):
                    print(_("✓ deploy key {id} deleted from {repo}", id=k.get("id"), repo=repo))
    if d.is_dir() and not d.is_symlink():
        shutil.rmtree(d)
        print(_("✓ the key for {name} is deleted here", name=project))
    elif not g:
        print(_("{name} has no grant here", name=project))
    return OK


# ---------- the owner's to-do (one per episode) ----------

def read_state(project: str) -> dict:
    try:
        st = json.loads(state_file(project).read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(project: str, st: dict) -> None:
    f = state_file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=1))


def owner_notify(*args: str) -> None:
    """One hubbus send as the platform, to the owner (tests replace it)."""
    r = subprocess.run([*hubconf.launch(BIN / "hubbus"), "send", "--from", hubconf.hub_project(), "--to",
                        hubconf.owner_address(), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).strip()[-300:])


def raise_todo(project: str, why: str | None = None) -> bool:
    """The owner's to-do to run setup, sent once per episode. → whether one was sent now."""
    st = read_state(project)
    episode = int(st.get("episode") or 1)
    todo = st.get("todo") or {}
    if todo.get("episode") == episode and not todo.get("closed"):
        return False
    if todo.get("episode") == episode and todo.get("closed"):
        episode += 1
    key = re.sub(r"[^A-Za-z0-9._-]", "-", f"git-push-setup-{project}-{episode}")[:128]
    repo = None
    with contextlib.suppress(Refused):
        repo = parse_origin(origin_of(doc_of(project)))[1]
    # the disclosure in short and with no gh call (check-secrets raises this too); setup names the gh account
    text = _c("{project}: decide what the platform may push for it{why}: {command}. others or all adds a deploy key "
              "with write access to {repo} (that repo only) using your gh login, after saying so and asking; the "
              "platform keeps the private key in its secrets folder, not in the cloud backup, and uses it only to push "
              "this project's branches its sessions ask for, after the automatic checks. Undo: {command} --revoke",
              project=project, why=f" ({why[:200]})" if why else "", command=setup_command(project),
              repo=f"github.com/{repo}" if repo else _c("its GitHub repo"))
    try:
        owner_notify("--type", "task", "--id", key, "--text", text, "--context", "git-push")
    except OSError as e:
        print(_("⚠️ the owner to-do {key} could not be sent ({why})", key=key, why=e), file=sys.stderr)
        return False
    write_state(project, {**st, "episode": episode, "todo": {"episode": episode, "key": key, "sent": now(),
                                                             "closed": False}})
    print(_("An owner to-do asks for {command}", command=setup_command(project)))
    return True


def close_todo(project: str) -> None:
    st = read_state(project)
    todo = st.get("todo") or {}
    if not todo.get("sent") or todo.get("closed"):
        return
    try:
        owner_notify("--type", "done", "--task", todo["key"], "--text", _c("set up"), "--context", "git-push")
    except OSError as e:
        print(_("⚠️ the owner to-do {key} could not be closed ({why})", key=todo["key"], why=e), file=sys.stderr)
        return
    todo["closed"] = True
    write_state(project, {**st, "todo": todo})


# ---------- onboarding ----------

def onboard(project: str, push: str | None, emails: list[str] | None, agent: bool, confirm: bool = False) -> int:
    """After registration: a sandboxed project with a remote gets its push decided. An explicit --push is the choice,
    and others|all the yes for the deploy key (the disclosure is printed; with confirm, a person's typed answer, the
    [y/N] follows). Otherwise none, whatever the remote's visibility (11-16: the 11-15 "private: all" used the owner's
    gh unasked). none raises the one-time setup to-do unless a person chose it; a grant that can't be made, or wasn't
    agreed to, leaves none with the to-do."""
    doc = doc_of(project)
    if not doc or not (doc.get("enforce") or {}).get("sandbox") or not origin_of(doc):
        return OK
    try:
        parse_origin(origin_of(doc))
    except Refused as e:
        print(_("No platform push for {name}: {why}", name=project, why=e))
        return OK
    if agent:   # what an agent passes is never a choice: none, and the owner decides with setup
        push = "none"
    chosen = push is not None and not agent
    if push is None or push == "none":
        if not chosen:
            raise_todo(project)
        return OK
    try:
        setup(project, yes=True, branches=push, emails=emails, confirm=confirm)   # a no raises the to-do itself
        return OK
    except (Refused, Failed) as e:
        print(_("⚠️ the platform's push for {name} was not set up: {why}", name=project, why=e), file=sys.stderr)
        raise_todo(project, str(e))
        return OK


def extra(base: str, sandbox: bool) -> str:
    """ONBOARD_REGISTRY_EXTRA with enforce.sandbox merged in (what a caller already put there is kept)."""
    d = json.loads(base) if base.strip() else {}
    if sandbox:
        enf = d.setdefault("enforce", {})
        if not isinstance(enf, dict):
            raise ValueError("enforce is not a mapping")
        enf["sandbox"] = True
    return json.dumps(d, ensure_ascii=False) if d else ""


# ---------- CLI ----------

def usage() -> int:
    print(_("usage: brunna push setup <project> [--branches none|others|all] [--email <address>]… [--yes] "
            "[--grant-only] | brunna push setup <project> --revoke"), file=sys.stderr)
    return REFUSED


def main(argv: list[str]) -> int:
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if cmd == "setup":
            pos, opts, emails, it = [], set(), None, iter(rest)
            branches = None
            for a in it:
                if a in ("--yes", "-y", "--revoke", "--grant-only"):
                    opts.add(a)
                elif a == "--branches":
                    branches = next(it, None) or ""
                elif a.startswith("--branches="):
                    branches = a.split("=", 1)[1]
                elif a == "--email":
                    emails = [*(emails or []), next(it, "")]
                elif a.startswith("-"):
                    return usage()
                else:
                    pos.append(a)
            if len(pos) != 1:
                return usage()
            if "--revoke" in opts:
                return revoke(pos[0])
            return setup(pos[0], yes=bool(opts & {"--yes", "-y"}), branches=branches, emails=emails,
                         grant_only="--grant-only" in opts)
        if cmd == "status":
            rows = [status(p) for p in (rest or sorted(n for n, d in (hubconf.projects() or {}).items()
                                                       if (d.get("enforce") or {}).get("sandbox")))]
            for r in rows:
                print(f"{r['project']}: {r['state']}" + (f" — {r['detail']}" if r["detail"] else ""))
            return OK if all(r["state"] != "not_set_up" for r in rows) else FAILED
        if cmd == "onboard" and rest:
            push, emails, agent, confirm, it = None, None, False, False, iter(rest[1:])
            for a in it:
                if a == "--push":
                    push = next(it, "")
                elif a == "--push-email":
                    emails = [*(emails or []), next(it, "")]
                elif a == "--agent":
                    agent = True
                elif a == "--confirm":
                    confirm = True
                else:
                    return usage()
            if push is not None and push not in BRANCHES:
                return usage()
            return onboard(rest[0], push, emails, agent, confirm)
        if cmd == "visibility" and len(rest) == 1:
            print(visibility(rest[0]))
            return OK
        if cmd == "extra" and rest:
            print(extra(rest[0], "--sandbox" in rest[1:]))
            return OK
    except Refused as e:
        print(_("refused: {why}", why=e), file=sys.stderr)
        return REFUSED
    except Failed as e:
        print(_("failed: {why}", why=e), file=sys.stderr)
        return FAILED
    except NotSetUp as e:
        print(_("not set up: {why}", why=e), file=sys.stderr)
        return FAILED
    return usage()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
