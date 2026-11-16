#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""`brunna release <project> <tag>`: the owner publishes a GitHub release of a sandboxed project, with their own gh
(docs/design/platform-push.md, "11-16: releases and the plain owner form").

A sandboxed project's sessions aren't meant to reach gh, and `brunna push` never pushes tags. An archive such as Zenodo
takes its copy on GitHub's *release* event, not on a tag, so the owner makes the release; until now that was
`gh release create` by hand after the branch was pushed. This does it the platform's way:

  1. only the owner, at their own terminal: refused where it can tell it runs inside a sandbox or a coding agent's
     session, and in a linked worktree of the hub (owner_terminal.refusal, the check `brunna skills sync` uses), and
     unless both stdin and stdout are a terminal (the plan can't be hidden): a release can't be undone, so only a typed
     y publishes one; `--yes` is never taken for it (with a terminal it still asks). The session check is a guard
     against routine or accidental use, not a boundary: an unsandboxed agent holds the owner's gh login anyway. For a
     sandboxed project the boundary is meant to be its sandbox, denying gh's credentials to its sessions (pending a
     check that a gh token kept in the Keychain isn't reachable from there: direction.md);
  2. the project is registered and sandboxed, and its paths.origin is a GitHub repo (git_push_grant.parse_origin, the
     push grant's parsing); any other forge is refused with the reason;
  3. the remote is read from the bare repo the platform owns for the project (`<state>/git-broker/<project>/remote.git`,
     git_remote.Owned, --git-dir, the owner's git environment as the owner form of `brunna push` uses; never git in the
     project's repo): `ls-remote --symref` names the default branch and every tag, and the remote's branches are fetched
     into the platform's own namespace there (refs/<repo dir>/release/heads/*, pruned), never its tags;
  4. the target is a commit on the remote: the default branch's tip as fetched now, or `--target <branch|sha>`: 4 to 64
     hex digits are a commit when they name one uniquely (refused when a remote branch has that very name), reachable
     from one of the branches
     fetched now (an object the owned repo holds from an earlier, refused push doesn't count); anything else a branch
     the remote has. A tag the remote has already is refused, unless it names the target itself: the release is then
     made on it (a lightweight one is said to be one a run that stopped left; an annotated one is someone else's, said
     as a tag already at the target). A tag name git's check-ref-format refuses, one
     starting with "-", or one that can't be printed is refused;
  5. the plan is shown (the repo and its visibility, the tag, the target with its subject, author and date, the commits
     since the nearest earlier tag, the title, every line of the notes (at most 200 lines and 32 KB, or refused) or
     that there are none) and, prominently and always, that an archive connected to the repo (Zenodo archives every
     release once connected) keeps a permanent copy with a DOI, with the target's .zenodo.json or CITATION.cff named as
     evidence when it has one; then the
     consent in the push grant's style (which gh account, which repo, what it creates, that it can't be taken back),
     then [y/N]: only y or yes goes on;
  6. before the question, read-only: `gh api user` (the account named) and `gh release view <tag>` (a draft release
     can hold a tag name the remote doesn't have yet; only "release not found" lets it go on; the consent says which
     reads ran);
  7. after the yes, the tag is pinned before any release exists (`gh release create --target` applies only when gh
     makes the tag itself; a tag that appeared meanwhile would carry the release elsewhere): `gh release view` is asked
     again and the remote's tags read again; a tag now at another commit is refused; a missing one is made with
     `gh api --hostname github.com -X POST repos/<owner>/<repo>/git/refs -f ref=refs/tags/<tag> -f sha=<sha>` (a ref
     that exists can't be made again: GitHub's REST docs list 422 "Validation failed" and 409 without saying which an
     existing ref gives, so whatever the answer the tag is read back and only the shown sha goes on). Then
     `gh release create <tag> --repo github.com/<owner>/<repo> --verify-tag --title <title> --notes-file <file>`: the
     notes always through a private file (600, in the platform's private temp folder; empty for none), so no long text
     goes on argv and gh never prompts; gh runs from a folder of the platform's;
     Any failure after the tag is pinned says the tag was left at the sha and that a rerun continues from it.
     A window stays: between the pin's read-back and `gh release create`, and after the final read-back, anyone with
     write access to the repo can move the tag (GitHub's release create has no compare-and-swap); such an actor could
     publish a release directly anyway. A tag ruleset restricting updates and deletions closes it on the owner's side;
  8. the remote's tag is read back (ls-remote): only when it names the shown sha is the release a line in
     `<state>/git-broker/<project>/releases.jsonl` and the project told by an info letter (tag, sha, URL). Otherwise it
     says plainly that the release went out on another commit (exit 3), or that the tag couldn't be read (exit 1).

Exit: 0 published, 1 failed (git, gh, the network), 2 refused (nothing was created), 3 published on a commit other
than the one shown.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
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
import hubconf  # noqa: E402
import i18n  # noqa: E402
import names  # noqa: E402

_ = i18n.domain(__file__)
_c = i18n.domain(__file__, content=True)

OK, FAILED, REFUSED, ELSEWHERE = 0, 1, 2, 3
ARCHIVE_FILES = (".zenodo.json", "CITATION.cff")   # what Zenodo (and archives like it) read from a release
SHOWN_COMMITS = 15
MAX_NOTE_LINES = 200   # notes are shown whole before the yes: longer ones are refused
MAX_NOTE_BYTES = 32 << 10
WALK = 5000            # commits looked through for the nearest earlier tag
HEX = re.compile(r"[0-9a-fA-F]{4,64}")   # git takes a commit id from 4 hex digits on
MAX_TITLE = 200


class Refused(Exception):
    """Nothing was created: a policy or usage refusal, or the owner said no."""


class Failed(Exception):
    """git, gh or the network failed."""


def cli() -> str:
    return names.current("cli")


def heads_ns() -> str:
    """Where the remote's branches are fetched in the owned repo: the platform's own namespace, never refs/tags."""
    return names.repo_ref("release") + "/heads"


# ---------- who may run it ----------

def refuse_here() -> str | None:
    import owner_terminal
    why = owner_terminal.refusal()
    if why == "sandbox":
        return _("brunna release runs only in your own terminal, never inside a project's sandbox")
    if why == "session":
        return _("brunna release runs only in your own terminal, not in a coding agent's session: a release can't be "
                 "undone, so the yes has to be yours, typed")
    if why == "worktree":
        return _("{root} is a linked worktree of the hub; run brunna release from the main checkout ({main})",
                 root=hubconf.ROOT, main=hubconf.MAIN)
    return None


# ---------- the arguments ----------

def visible(text: str, lines: bool = False) -> str:
    import git_remote
    return git_remote.visible(text, lines)


def check_tag(tag: str) -> None:
    if not tag or tag.startswith("-") or not tag.isprintable() or any(c.isspace() for c in tag) or len(tag) > 100:
        raise Refused(_("{tag} is not a tag name brunna release makes (no leading -, nothing unprintable, no spaces, "
                        "at most 100 characters)", tag=visible(tag)))
    with tempfile.TemporaryDirectory(prefix="release-", dir=hubconf.private_tmp()) as d:
        r = subprocess.run(["git", "check-ref-format", f"refs/tags/{tag}"], cwd=d, capture_output=True,
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": d, "GIT_CONFIG_NOSYSTEM": "1",
                                "GIT_CONFIG_GLOBAL": "/dev/null"})
    if r.returncode != 0:
        raise Refused(_("{tag} is not a valid tag name (git check-ref-format)", tag=visible(tag)))


SHOWN_AS_IS = "\t\u3000"   # a tab and the ideographic space: blank, but nothing hidden or reordered


def shown(text: str) -> str:
    """A line of the owner's own text (title, notes) for the terminal: tabs and U+3000 as they are, every other
    character that can't be printed (controls, bidi and zero-width marks) escaped, as visible() does."""
    return "".join(ch if ch in SHOWN_AS_IS else visible(ch) for ch in text)


def check_title(title: str) -> None:
    if not title.strip() or not all(c.isprintable() or c == "\u3000" for c in title) or len(title) > MAX_TITLE:
        raise Refused(_("the title must be one printable line of at most {n} characters", n=MAX_TITLE))


def read_notes(notes_file: str | None, notes: str | None) -> str:
    """The notes as they will be sent: read once here, so what is shown is what goes (the owner's file may change)."""
    if notes_file is not None:
        try:
            raw = pathlib.Path(notes_file).expanduser().read_bytes()
        except OSError as e:
            raise Refused(_("the notes file {path} can't be read ({why})", path=notes_file, why=e.strerror or e)) from None
        try:
            notes = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused(_("the notes file {path} is not UTF-8 text", path=notes_file)) from None
    notes = (notes or "").replace("\r\n", "\n")
    if "\0" in notes:
        raise Refused(_("the notes hold a NUL character"))
    lines = notes.rstrip("\n").count("\n") + 1 if notes.strip() else 0
    if lines > MAX_NOTE_LINES or len(notes.encode()) > MAX_NOTE_BYTES:
        raise Refused(_("the notes are {n} lines and {kb} KB: every line is shown before you say yes, so at most {max} "
                        "lines and {max_kb} KB are taken; shorten them (they can link to a longer changelog)",
                        n=lines, kb=(len(notes.encode()) + 1023) >> 10, max=MAX_NOTE_LINES, max_kb=MAX_NOTE_BYTES >> 10))
    return notes


# ---------- the project ----------

def project_doc(project: str) -> tuple[dict, str, str]:
    """(the registry entry, paths.origin, owner/repo), or Refused."""
    import git_push_grant
    import isolation
    doc = (hubconf.projects() or {}).get(project)
    if doc is None:
        raise Refused(_("{project} is not in the registry", project=project))
    doc = {**doc, "name": project}
    if not isolation.sandboxed(doc):
        raise Refused(_("{project} is not sandboxed: its sessions can make a release with gh themselves (brunna "
                        "release is for sandboxed projects, which can't reach gh)", project=project))
    origin = git_push_grant.origin_of(doc)
    if not origin:
        raise Refused(_("{project} has no remote (paths.origin): there is nothing to release on", project=project))
    try:
        _host, repo = git_push_grant.parse_origin(origin)
    except git_push_grant.Refused:
        raise Refused(_("{project}'s paths.origin {url} is not a GitHub repo (github.com/<owner>/<repo>): brunna "
                        "release makes only GitHub releases, with your gh; on another forge, make the release there",
                        project=project, url=visible(origin))) from None
    return doc, origin, repo


# ---------- the remote, read from the owned repo ----------

class Remote:
    def __init__(self, default: str | None, heads: dict[str, str], tags: dict[str, str], annotated: set | None = None):
        self.default, self.heads, self.tags, self.annotated = default, heads, tags, annotated or set()


def network(op: str, r) -> Failed:
    raw = r.stderr.decode(errors="replace").strip()[-400:]
    return Failed(_("{op} failed: {why}", op=op, why=visible(raw, lines=True) or r.returncode))


def read_remote(owned, url: str) -> Remote:
    """ls-remote --symref of every ref: the default branch, the branches, the tags (peeled: the commit a tag names)."""
    import git_remote
    r = owned.git(*git_remote.SAFE, "ls-remote", "--symref", "--", url)
    if r.returncode != 0:
        raise network(f"git ls-remote {visible(url)}", r)
    default, heads, tags, annotated = None, {}, {}, set()
    for line in r.stdout.decode(errors="replace").splitlines():
        left, _tab, name = line.partition("\t")
        if name == "HEAD" and left.startswith("ref: refs/heads/"):
            default = left.removeprefix("ref: refs/heads/")
        elif name.startswith("refs/heads/") and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", left):
            heads[name.removeprefix("refs/heads/")] = left
        elif name.startswith("refs/tags/") and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", left):
            t = name.removeprefix("refs/tags/")
            if t.endswith("^{}"):
                tags[t[:-3]] = left            # the peeled commit wins over the tag object
                annotated.add(t[:-3])
            else:
                tags.setdefault(t, left)
    return Remote(default, heads, tags, annotated)


def fetch_heads(owned, url: str) -> dict[str, str]:
    """The remote's branches, fetched now into the platform's namespace (pruned): {branch: sha}."""
    import git_remote
    ns = heads_ns()
    r = owned.git(*git_remote.SAFE, "fetch", "-q", "--no-tags", "--prune", "--no-recurse-submodules",
                  "--no-write-fetch-head", "--", url, f"+refs/heads/*:{ns}/*")
    if r.returncode != 0:
        raise network(f"git fetch {visible(url)}", r)
    out = {}
    for line in owned.out("for-each-ref", "--format=%(objectname) %(refname)", ns).splitlines():
        sha, _sp, ref = line.partition(" ")
        out[ref.removeprefix(ns + "/")] = sha
    return out


def on_remote(owned, sha: str, heads: dict[str, str]) -> bool:
    """Whether sha is reachable from a branch the remote has now (as fetched now), not merely an object in the repo."""
    if not heads:
        return False
    r = owned.git("rev-list", "--max-count=1", sha, "--stdin",
                  input="".join(f"^{h}\n" for h in sorted(set(heads.values()))).encode())
    return r.returncode == 0 and not r.stdout.strip()


def resolve_target(owned, target: str | None, remote: Remote, heads: dict[str, str]) -> tuple[str, str]:
    """(the full sha, the branch it is on) of a commit already on the remote; Refused otherwise."""
    if target is None:
        if not remote.default or remote.default not in heads:
            raise Refused(_("the remote's default branch can't be told (ls-remote --symref shows no HEAD; an empty "
                            "remote?): push its first commit, then release"))
        return heads[remote.default], remote.default
    if HEX.fullmatch(target):
        # a commit id first: a branch named like one could stand for another commit
        if target in heads:
            raise Refused(_("--target {target} is a commit id and also a branch on the remote: rename the branch, or "
                            "name the commit by another length of its id", target=visible(target)))
        sha = owned.sha(target.lower())
        if sha and on_remote(owned, sha, heads):
            on = [b for b in heads if owned.git("merge-base", "--is-ancestor", sha, heads[b]).returncode == 0]
            return sha, (remote.default if remote.default in on else sorted(on)[0])
        raise Refused(_("{target} is not a commit on the remote (none of its branches has it): push it first (brunna "
                        "push), then release; a tag is never made on an unpushed commit", target=visible(target)))
    if target in heads:
        return heads[target], target
    raise Refused(_("--target {target} is neither a branch the remote has nor a commit id", target=visible(target)))


# ---------- what the owner is shown ----------

class Plan:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def plan(owned, project: str, repo: str, tag: str, sha: str, branch: str, remote: Remote, title: str, notes: str,
         vis: str, existing: str | None = None) -> Plan:
    fmt = owned.out("log", "-1", "--no-show-signature", "--format=%an%n%ae%n%aI%n%s", sha).split("\n")
    author, email, date, subject = (fmt + ["", "", "", ""])[:4]
    by_sha: dict[str, list[str]] = {}
    for t, s in remote.tags.items():
        if t == tag:
            continue   # a tag of this name left at the target is not an earlier release
        by_sha.setdefault(s, []).append(t)
    prev = None
    for c in owned.out("rev-list", f"--max-count={WALK}", sha).split():
        if c in by_sha:
            prev = (sorted(by_sha[c])[0], c)
            break
    span = [sha, f"^{prev[1]}"] if prev else [sha]
    n = int(owned.out("rev-list", "--count", *span).strip() or 0)
    log = owned.out("log", "--no-show-signature", f"--max-count={SHOWN_COMMITS}", "--format=%h %s", *span).splitlines()
    archive = [f for f in ARCHIVE_FILES if owned.git("cat-file", "-e", f"{sha}:{f}").returncode == 0]
    return Plan(project=project, repo=repo, tag=tag, sha=sha, branch=branch, default=remote.default, author=author,
                email=email, date=date, subject=subject, prev=prev, n=n, log=log, title=title, notes=notes, vis=vis,
                archive=archive, existing=existing)


def plan_lines(p: Plan) -> list[str]:
    vis = {"public": _("public"), "private": _("private")}.get(p.vis, _("of unknown visibility"))
    out = [_("Release of {project}: github.com/{repo} ({visibility})", project=p.project, repo=p.repo, visibility=vis),
           _("  tag      {tag} (on the remote already, at the target: left by a run that stopped; the release is made "
             "on it)", tag=visible(p.tag)) if p.existing == "lightweight" else
           _("  tag      {tag} (an annotated tag already at the target, made by someone else; the release is made on "
             "it)", tag=visible(p.tag)) if p.existing == "annotated" else
           _("  tag      {tag} (new: made at the target before the release)", tag=visible(p.tag))]
    where = _("the remote's default branch {branch}", branch=visible(p.branch)) if p.branch == p.default else \
        _("on the remote's branch {branch}", branch=visible(p.branch))
    out += [_("  target   {sha} ({where})", sha=p.sha, where=where),
            f"           {visible(p.subject)}",
            _("           {author} <{email}>, {date}", author=visible(p.author), email=visible(p.email), date=p.date)]
    if p.prev:
        out.append(_("  commits  {n} since {tag} ({sha})", n=p.n, tag=visible(p.prev[0]), sha=p.prev[1][:12]))
    else:
        out.append(_("  commits  {n} in all (no earlier tag on the remote)", n=p.n))
    out += [f"           {visible(line)}" for line in p.log]
    if p.n > len(p.log):
        out.append(_("           … and {n} more", n=p.n - len(p.log)))
    out.append(_("  title    {title}", title=shown(p.title)))
    if p.notes.strip():
        lines = p.notes.rstrip("\n").split("\n")
        out.append(_("  notes    {n} {n:line|lines}:", n=len(lines)))
        out += [f"           > {shown(line)}" for line in lines]
    else:
        out.append(_("  notes    none: the release has no notes, and GitHub generates none"))
    out += ["", _("!! If Zenodo (or another archive) is connected to this repo, publishing this release makes a "
                  "permanent archived copy with a DOI that can't be withdrawn (Zenodo's GitHub integration archives "
                  "every release once it is switched on).")]
    if p.archive:
        out.append(_("!! The target has {files}, which such archives read: one may well be connected.",
                     files=", ".join(p.archive)))
    return out


def consent_lines(p: Plan, account: str) -> list[str]:
    return [_("Read so far with your gh login ({account}), read-only: the account's name, and that github.com/{repo} "
              "has no release named {tag} yet.", account=account, repo=p.repo, tag=visible(p.tag)),
            _("To publish it, the platform will use that login on github.com/{repo} to publish a release of the tag {tag} "
              "that is already at {sha}. Nothing else of your account is used, and nothing has been changed yet.",
              repo=p.repo, tag=visible(p.tag), sha=p.sha[:12]) if p.existing else
            _("To publish it, the platform will use that login on github.com/{repo} to create the tag {tag} at {sha} and "
              "a published release of it. Nothing else of your account is used, and nothing has been changed yet.",
              repo=p.repo, tag=visible(p.tag), sha=p.sha[:12]),
            _("Everyone who can see the repo sees the release at once, its watchers are notified, and webhooks and "
              "connected services (archives among them) run; deleting it later takes none of that back. So only your "
              "typed y goes on, never --yes.")]


def ask() -> None:
    try:
        a = input(_("Publish the release with your gh login?") + " [y/N] ").strip().lower()
    except EOFError:
        a = ""
    if a not in ("y", "yes"):
        raise Refused(_("You said no: no release was created, and nothing was changed."))


# ---------- gh ----------

def gh(*args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    import git_push_grant
    try:
        return git_push_grant.gh(*args, cwd=cwd)
    except git_push_grant.Failed as e:
        raise Failed(str(e)) from None


def no_release_yet(repo: str, tag: str, cwd: pathlib.Path) -> None:
    import git_push_grant
    r = gh("release", "view", tag, "--repo", git_push_grant.gh_repo(repo), "--json", "url,isDraft", cwd=cwd)
    if r.returncode == 0:
        try:
            url = json.loads(r.stdout or "{}").get("url") or ""
        except ValueError:
            url = ""
        raise Refused(_("github.com/{repo} has a release named {tag} already ({url}; a draft, if the remote has no such "
                        "tag): nothing was created", repo=repo, tag=visible(tag), url=visible(url) or "?"))
    if "release not found" not in (r.stderr or "").lower():
        raise Failed(_("gh couldn't tell whether github.com/{repo} has a release named {tag}: {why}; nothing was "
                       "created", repo=repo, tag=visible(tag), why=visible((r.stderr or "").strip()[-300:])
                       or r.returncode))


def pin_tag(owned, url: str, repo: str, tag: str, sha: str, cwd: pathlib.Path) -> None:
    """The tag at the shown sha before any release exists (after the yes). The remote's tags are read again: one at
    another commit is refused, one at the sha (left by a run that stopped) is kept; a missing one is made with the refs
    API, which can't move a ref that exists. Whatever gh answers, the tag is read back and only the sha goes on."""
    at = read_remote(owned, url).tags.get(tag)
    if at is None:
        try:
            r = gh("api", "--hostname", "github.com", "-X", "POST", f"repos/{repo}/git/refs", "-f",
                   f"ref=refs/tags/{tag}", "-f", f"sha={sha}", cwd=cwd)
            why = visible((r.stderr or "").strip()[-300:])
        except Failed as e:   # a timeout: whether GitHub made it is read back below
            why = str(e)
        at = tag_at(owned, url, tag)
        if at is None:
            raise Failed(_("the tag {tag} couldn't be made at {sha}: {why}; no release was created", tag=visible(tag),
                           sha=sha[:12], why=why or _("GitHub answered, but the tag isn't there when read back")))
    if at != sha:
        raise Refused(_("the remote's tag {tag} appeared at {seen} after the plan was shown, not at {sha}: no release was "
                        "created; look at who made it", tag=visible(tag), seen=at[:12], sha=sha[:12]))


def create(repo: str, tag: str, title: str, notes: str, cwd: pathlib.Path) -> str:
    """gh release create on the tag that exists (--verify-tag: never one gh makes itself), the notes in a private file;
    → the release's URL."""
    import git_push_grant
    fd, path = tempfile.mkstemp(prefix="release-notes-", suffix=".md", dir=hubconf.private_tmp())
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(notes)
        os.chmod(path, 0o600)
        try:
            r = git_push_grant.gh("release", "create", tag, "--repo", git_push_grant.gh_repo(repo), "--verify-tag",
                                  "--title", title, "--notes-file", path, cwd=cwd)
        except git_push_grant.Failed as e:
            raise Failed(_("{why}: whether the release was made isn't known. Look with gh release view {tag} --repo "
                           "github.com/{repo}; running brunna release again refuses if it is there",
                           why=e, tag=visible(tag), repo=repo)) from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    if r.returncode != 0:
        raise Failed(_("gh couldn't create the release: {why}", why=visible((r.stderr or "").strip()[-400:], lines=True)
                       or r.returncode))
    urls = re.findall(r"https://\S+", r.stdout or "")
    return urls[-1] if urls else ""


def tag_at(owned, url: str, tag: str) -> str | None:
    """The commit the remote's tag names now (read back after the release)."""
    import git_remote
    try:
        r = owned.git(*git_remote.SAFE, "ls-remote", "--", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    found = {}
    for line in r.stdout.decode(errors="replace").splitlines():
        left, _tab, name = line.partition("\t")
        found[name] = left
    return found.get(f"refs/tags/{tag}^{{}}") or found.get(f"refs/tags/{tag}")


# ---------- after ----------

def log(project: str, **rec) -> None:
    import git_remote
    f = git_remote.broker_dir(project) / "releases.jsonl"
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps({"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                             "project": project, **rec}, ensure_ascii=False) + "\n")


def tell_project(project: str, repo: str, tag: str, sha: str, url: str) -> None:
    import push_queue
    key = re.sub(r"[^A-Za-z0-9._-]", "-", f"release-{project}-{tag}")[:128]
    text = _c("The release {tag} of github.com/{repo} is published, at {sha}: {url}. Tags and releases are made by the "
              "owner (brunna release); there is nothing for you to do.", tag=tag, repo=repo, sha=sha[:12],
              url=url or f"https://github.com/{repo}/releases/tag/{tag}")
    try:
        push_queue.project_letter(project, text, key)
    except OSError as e:
        print(_("⚠️ {project} could not be told by letter ({why}); the release is published", project=project, why=e),
              file=sys.stderr)


# ---------- the command ----------

def release(project: str, tag: str, *, target: str | None = None, title: str | None = None,
            notes_file: str | None = None, notes: str | None = None, yes: bool = False) -> int:
    import git_remote
    if (why := refuse_here()):
        raise Refused(why)
    check_tag(tag)
    title = tag if title is None else title
    check_title(title)
    text = read_notes(notes_file, notes)
    _doc, origin, repo = project_doc(project)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):   # the plan is shown on a terminal, never into a pipe
        if yes:
            raise Refused(_("--yes isn't taken for a release: it can't be undone, so only your y typed at a terminal "
                            "publishes one. Run {cli} release in your own terminal; nothing was contacted",
                            cli=cli()))
        raise Refused(_("nobody is at a terminal to say yes: a release can't be undone, so only your y typed at a "
                        "terminal publishes one. Run {cli} release in your own terminal; nothing was contacted",
                        cli=cli()))
    try:
        url = git_remote.check_origin(origin)
    except git_remote.Refused as e:
        raise Refused(str(e)) from None
    if not git_remote.can_write_platform(project):
        raise Refused(_("the platform's folder for {project} can't be written from here (a sandbox?): run it in your "
                        "own terminal", project=project))
    try:
        with git_remote.locked(git_remote.broker_dir(project)):
            return published(project, tag, origin, url, repo, target, title, text, yes)
    except git_remote.Failed as e:   # the owned repo's own git
        raise Failed(str(e)) from None
    except git_remote.Refused as e:  # another push or release of the project is running
        raise Refused(str(e)) from None


def published(project: str, tag: str, origin: str, url: str, repo: str, target: str | None, title: str, text: str,
              yes: bool) -> int:
    """Under the project's push lock: the remote read, the plan shown, the yes asked, the release made."""
    import git_push_grant
    import git_remote
    run = pathlib.Path(tempfile.mkdtemp(prefix="release-", dir=hubconf.private_tmp()))
    try:
        owned = git_remote.Owned(git_remote.broker_dir(project) / "remote.git", git_remote.owner_env())
        owned.init()
        remote = read_remote(owned, url)
        heads = fetch_heads(owned, url)
        sha, branch = resolve_target(owned, target, remote, heads)
        if tag in remote.tags and remote.tags[tag] != sha:
            raise Refused(_("the remote has the tag {tag} already (at {sha}): nothing was created; pick another "
                            "name", tag=visible(tag), sha=remote.tags[tag][:12]))
        existing = None if tag not in remote.tags else "annotated" if tag in remote.annotated else "lightweight"
        p = plan(owned, project, repo, tag, sha, branch, remote, title, text, git_remote.visibility(origin),
                 existing=existing)
        try:
            account = git_push_grant.gh_login(cwd=run)
        except git_push_grant.Failed as e:
            raise Failed(str(e)) from None
        # read-only, so before the question (the 11-16 rule is about changes made with the owner's login): a release
        # that exists already is refused without asking
        no_release_yet(repo, tag, run)
        if yes:
            print(_("(--yes isn't taken for a release: answer the question below)"))
        print("\n".join(plan_lines(p)))
        print()
        print("\n".join(consent_lines(p, account)))
        ask()
        no_release_yet(repo, tag, run)   # again: the plan was on screen for a while
        pin_tag(owned, url, repo, tag, sha, run)
        try:
            link = create(repo, tag, title, text, run)
        except Failed as e:
            raise Failed(_("{why}. The tag {tag} is left at {sha}: running brunna release again continues from it",
                           why=e, tag=visible(tag), sha=sha[:12])) from None
        now = tag_at(owned, url, tag)
        if now is None:
            print(_("⚠️ the release {tag} is published ({url}), but its tag couldn't be read back: nothing was recorded "
                    "and {project} wasn't told; look at it on GitHub", tag=visible(tag), url=link or "?",
                    project=project), file=sys.stderr)
            return FAILED
        if now != sha:
            print(_("⚠️ the release {tag} went out on a different commit: its tag names {seen}, not the {sha} you were "
                    "shown. Look at it on GitHub ({url}) and delete the release if it shouldn't stay; an archive "
                    "connected to the repo may have its copy already, and with immutable releases on, the name can't "
                    "be used again. Nothing was recorded and {project} wasn't told", tag=visible(tag), seen=now[:12],
                    sha=sha[:12],
                    url=link or "?", project=project), file=sys.stderr)
            return ELSEWHERE
        log(project, repo=repo, tag=tag, sha=sha, branch=branch, title=title, url=link, by="owner",
            notes_sha256=hashlib.sha256(text.encode()).hexdigest(), notes_chars=len(text), archive=p.archive,
            tag_seen=now, tag_existing=p.existing)
        tell_project(project, repo, tag, sha, link)
        print(_("✓ released {tag} of github.com/{repo} at {sha}: {url}", tag=visible(tag), repo=repo, sha=sha[:12],
                url=link or "?"))
        return OK
    finally:
        shutil.rmtree(run, ignore_errors=True)


USAGE = ("usage: brunna release <project> <tag> [--target <branch|sha>] [--title <title>] "
         "[--notes-file <file> | --notes <text>]")


def main(argv: list[str]) -> int:
    pos, opts, it = [], {}, iter(argv)
    yes = False
    try:
        for a in it:
            if a in ("--yes", "-y"):
                yes = True
            elif a in ("--target", "--title", "--notes-file", "--notes"):
                v = next(it, None)
                if v is None:
                    raise Refused(_(USAGE))
                opts[a] = v
            elif "=" in a and a.split("=", 1)[0] in ("--target", "--title", "--notes-file", "--notes"):
                k, v = a.split("=", 1)
                opts[k] = v
            elif a.startswith("-"):
                raise Refused(_(USAGE))
            else:
                pos.append(a)
        if len(pos) != 2 or ("--notes-file" in opts and "--notes" in opts):
            raise Refused(_(USAGE))
        return release(pos[0], pos[1], target=opts.get("--target"), title=opts.get("--title"),
                       notes_file=opts.get("--notes-file"), notes=opts.get("--notes"), yes=yes)
    except Refused as e:
        print(_("refused: {why}", why=e), file=sys.stderr)
        return REFUSED
    except Failed as e:
        print(_("failed: {why}", why=e), file=sys.stderr)
        return FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
