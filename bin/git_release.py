#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""`brunna release <project> <tag>`: the owner publishes a GitHub release of a sandboxed project, with their own gh
(docs/design/platform-push.md, "11-16: releases and the plain owner form").

A sandboxed project's sessions can't reach gh, and `brunna push` never pushes tags. An archive such as Zenodo takes its
copy on GitHub's *release* event, not on a tag, so the owner makes the release; until now that was `gh release create`
by hand after the branch was pushed. This does it the platform's way:

  1. only the owner, at their own terminal: refused inside a sandbox, in a coding agent's session and in a linked
     worktree of the hub (owner_terminal.refusal, the check `brunna skills sync` uses), and with no terminal: a release
     can't be undone, so only a typed y publishes one; `--yes` is never taken for it (with a terminal it still asks);
  2. the project is registered and sandboxed, and its paths.origin is a GitHub repo (git_push_grant.parse_origin, the
     push grant's parsing); any other forge is refused with the reason;
  3. the remote is read from the bare repo the platform owns for the project (`<state>/git-broker/<project>/remote.git`,
     git_remote.Owned, --git-dir, the owner's git environment as the owner form of `brunna push` uses; never git in the
     project's repo): `ls-remote --symref` names the default branch and every tag, and the remote's branches are fetched
     into the platform's own namespace there (refs/<repo dir>/release/heads/*, pruned), never its tags;
  4. the target is a commit on the remote: the default branch's tip as fetched now, or `--target <branch|sha>`, a branch
     the remote has or a commit reachable from one of the branches fetched now (an object the owned repo holds from an
     earlier, refused push doesn't count). The full sha is what is shown and what gh is given, so GitHub makes the tag
     there and nowhere else. A tag the remote has already is refused; a tag name git's check-ref-format refuses, one
     starting with "-", or one that can't be printed is refused;
  5. the plan is shown (the repo and its visibility, the tag, the target with its subject, author and date, the commits
     since the nearest earlier tag, the title, the notes or that there are none) and, prominently, when the target has
     a .zenodo.json or CITATION.cff, that an archive connected to the repo keeps a permanent copy with a DOI; then the
     consent in the push grant's style (which gh account, which repo, what it creates, that it can't be taken back),
     then [y/N]: only y or yes goes on;
  6. after the yes: `gh release view <tag>` (a draft release can hold a tag name the remote doesn't have yet; only
     "release not found" lets it go on), then `gh release create <tag> --repo github.com/<owner>/<repo> --target <sha>
     --title <title> --notes-file <file>`: the notes always through a private file (600, in the platform's private temp
     folder; empty for none), so no long text goes on argv and gh never prompts; gh runs from a folder of the platform's;
  7. the remote's tag is read back (ls-remote) and must be at the sha; each release is a line in
     `<state>/git-broker/<project>/releases.jsonl`, and the project gets an info letter (tag, sha, URL).

Exit: 0 published, 1 failed (git, gh, the network), 2 refused (nothing was created).
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

OK, FAILED, REFUSED = 0, 1, 2
ARCHIVE_FILES = (".zenodo.json", "CITATION.cff")   # what Zenodo (and archives like it) read from a release
SHOWN_COMMITS = 15
SHOWN_NOTE_LINES = 20
WALK = 5000            # commits looked through for the nearest earlier tag
MAX_NOTES = 125_000    # GitHub's limit on a release's body (characters)
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


def check_title(title: str) -> None:
    if not title.strip() or not title.isprintable() or len(title) > MAX_TITLE:
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
    if len(notes) > MAX_NOTES:
        raise Refused(_("the notes are {n} characters; GitHub takes at most {max}", n=len(notes), max=MAX_NOTES))
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
    def __init__(self, default: str | None, heads: dict[str, str], tags: dict[str, str]):
        self.default, self.heads, self.tags = default, heads, tags


def network(op: str, r) -> Failed:
    raw = r.stderr.decode(errors="replace").strip()[-400:]
    return Failed(_("{op} failed: {why}", op=op, why=visible(raw, lines=True) or r.returncode))


def read_remote(owned, url: str) -> Remote:
    """ls-remote --symref of every ref: the default branch, the branches, the tags (peeled: the commit a tag names)."""
    import git_remote
    r = owned.git(*git_remote.SAFE, "ls-remote", "--symref", "--", url)
    if r.returncode != 0:
        raise network(f"git ls-remote {visible(url)}", r)
    default, heads, tags = None, {}, {}
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
            else:
                tags.setdefault(t, left)
    return Remote(default, heads, tags)


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
    if target in heads:
        return heads[target], target
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", target):
        sha = owned.sha(target.lower())
        if sha and on_remote(owned, sha, heads):
            on = [b for b in heads if owned.git("merge-base", "--is-ancestor", sha, heads[b]).returncode == 0]
            return sha, (remote.default if remote.default in on else sorted(on)[0])
        raise Refused(_("{target} is not a commit on the remote (none of its branches has it): push it first (brunna "
                        "push), then release; a tag is never made on an unpushed commit", target=visible(target)))
    raise Refused(_("--target {target} is neither a branch the remote has nor a commit id", target=visible(target)))


# ---------- what the owner is shown ----------

class Plan:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def plan(owned, project: str, repo: str, tag: str, sha: str, branch: str, remote: Remote, title: str, notes: str,
         vis: str) -> Plan:
    fmt = owned.out("log", "-1", "--no-show-signature", "--format=%an%n%ae%n%aI%n%s", sha).split("\n")
    author, email, date, subject = (fmt + ["", "", "", ""])[:4]
    by_sha: dict[str, list[str]] = {}
    for t, s in remote.tags.items():
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
                archive=archive)


def plan_lines(p: Plan) -> list[str]:
    vis = {"public": _("public"), "private": _("private")}.get(p.vis, _("of unknown visibility"))
    out = [_("Release of {project}: github.com/{repo} ({visibility})", project=p.project, repo=p.repo, visibility=vis),
           _("  tag      {tag} (new: GitHub makes it at the target)", tag=visible(p.tag))]
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
    out.append(_("  title    {title}", title=visible(p.title)))
    if p.notes.strip():
        lines = p.notes.rstrip("\n").split("\n")
        out.append(_("  notes    {n} {n:line|lines}:", n=len(lines)))
        out += [f"           > {visible(line)}" for line in lines[:SHOWN_NOTE_LINES]]
        if len(lines) > SHOWN_NOTE_LINES:
            out.append(_("           … and {n} more {n:line|lines}", n=len(lines) - SHOWN_NOTE_LINES))
    else:
        out.append(_("  notes    none: the release has no notes, and GitHub generates none"))
    if p.archive:
        out += ["",
                _("!! The target has {files}, which archives such as Zenodo read.", files=", ".join(p.archive)),
                _("!! If Zenodo (or another archive) is connected to this repo, publishing this release makes a "
                  "permanent archived copy with a DOI that can't be withdrawn.")]
    return out


def consent_lines(p: Plan, account: str) -> list[str]:
    return [_("To publish it, the platform will use your gh login ({account}) on github.com/{repo}: it checks that no "
              "release named {tag} exists yet, then creates the tag {tag} at {sha} and a published release of it. "
              "Nothing else of your account is used.", account=account, repo=p.repo, tag=visible(p.tag),
              sha=p.sha[:12]),
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


def create(repo: str, tag: str, sha: str, title: str, notes: str, cwd: pathlib.Path) -> str:
    """gh release create with the notes in a private file; → the release's URL."""
    import git_push_grant
    fd, path = tempfile.mkstemp(prefix="release-notes-", suffix=".md", dir=hubconf.private_tmp())
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(notes)
        os.chmod(path, 0o600)
        try:
            r = git_push_grant.gh("release", "create", tag, "--repo", git_push_grant.gh_repo(repo), "--target", sha,
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
    try:   # the release exists by now: whatever this read does, the record and the letter still follow
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
    if not sys.stdin.isatty():
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
        if tag in remote.tags:
            raise Refused(_("the remote has the tag {tag} already (at {sha}): nothing was created; pick another "
                            "name", tag=visible(tag), sha=remote.tags[tag][:12]))
        heads = fetch_heads(owned, url)
        sha, branch = resolve_target(owned, target, remote, heads)
        p = plan(owned, project, repo, tag, sha, branch, remote, title, text, git_remote.visibility(origin))
        try:
            account = git_push_grant.gh_login(cwd=run)
        except git_push_grant.Failed as e:
            raise Failed(str(e)) from None
        if yes:
            print(_("(--yes isn't taken for a release: answer the question below)"))
        print("\n".join(plan_lines(p)))
        print()
        print("\n".join(consent_lines(p, account)))
        ask()
        no_release_yet(repo, tag, run)
        link = create(repo, tag, sha, title, text, run)
        now = tag_at(owned, url, tag)
        log(project, repo=repo, tag=tag, sha=sha, branch=branch, title=title, url=link, by="owner",
            notes_sha256=hashlib.sha256(text.encode()).hexdigest(), notes_chars=len(text), archive=p.archive,
            tag_seen=now)
        tell_project(project, repo, tag, sha, link)
        print(_("✓ released {tag} of github.com/{repo} at {sha}: {url}", tag=visible(tag), repo=repo, sha=sha[:12],
                url=link or "?"))
        if now != sha:
            print(_("⚠️ the remote's tag {tag} reads {seen} right after, not {sha}: look at the release on GitHub",
                    tag=visible(tag), seen=(now or _("missing"))[:12], sha=sha[:12]), file=sys.stderr)
            return FAILED
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
