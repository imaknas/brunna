#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The git work the platform needs done in a project's repo, run **as that project**
(docs/design/projects-and-repos.md section 4, "Platform git in project repos").

A repo's `.git` (config, hooks, attributes, `objects/info/alternates`) is writable by its projects, and git runs or
reads what it names: `git log` runs the repo's `gpg.program`, `status`/`add` its fsmonitor and clean filters, `diff` its
`diff.external`, `worktree add`/`remove` its hooks and filters, and a fetch or push from the repo serves whatever its
`alternates` point at with the reader's authority. So the platform never runs git in a project's repo itself:
project_git.job() starts this script (isolation.as_project), through bin/sandboxed-run for a project with enforce.sandbox (a no-op for the
others: they have the user's authority already), and what the platform needs back comes as data on stdout (JSON facts,
or a bundle) that it parses or fetches into a repo it owns (project_git.py).

Standard library only, and every path is an argument: inside the sandbox the platform's state and config are not
readable. Nothing here decides anything for the platform; the caller treats the output as untrusted data.

  worktrees <repo> <days>                 JSON {base, worktrees: [{path, branch, last, merged, clean, exists, files,
                                          hidden}], main: [shas on the base branch in the last <days> days]}: the linked
                                          worktrees of another branch than the base (never the main one, the repo's own
                                          folder or the memory branch's). last: the newest of the branch's tip, what
                                          HEAD there last moved at and its index, null when one can't be read; hidden: files
                                          there that git is told not to look at (skip-worktree, assume-unchanged), at most
                                          5; clean: git says so, untracked files included whatever the config shows, and
                                          none hidden
  ignored <worktree>                      JSON [ignored entries]: a folder with nothing else in it as one entry
  worktree-add <repo> <path>              a detached worktree of HEAD at <path>; prints its HEAD
  worktree-remove <repo> <path> [--force]
                                          without --force, refused when the worktree isn't clean (as `worktrees` says)
  worktree-plan <repo> <branch>           JSON {exists: whether refs/heads/<branch> exists}: asked before
                                          session-worktree, so an undo knows whether the branch is the make's to delete
  session-worktree <repo> <path> <branch> <rules>
                                          a coding agent's worktree, made by the platform as the project (its tool's
                                          own worktree mode would make it as the owner: docs/design/git-broker.md r3
                                          B-1): <branch> (new from HEAD, or the existing one) checked out at <path>.
                                          <rules>: JSON {names: [control names, at any depth], roots: [{name,
                                          writable}]}: the tracked paths the sandbox may not write are left out of the
                                          checkout (skip-worktree, so git doesn't see them as deleted). JSON {path,
                                          branch, skipped (at most 50), n_skipped}
  session-worktree-undo <repo> <path> <branch> <new>
                                          what a session-worktree that didn't finish left: the worktree at <path>
                                          (session-worktree-remove's way), and <branch> when <new> is 1 (it made it)
  session-worktree-remove <repo> <path>   a coding agent's worktree removed: everything in it but its .git first,
                                          bottom-up, never through a link; only when all of that went is it
                                          unregistered (`worktree remove --force`), so a folder that can't be deleted
                                          stays a registered worktree (exit 1, what is left on stderr). Then its branch
                                          with `branch -d` (merged ones only)
  branch-delete <repo> <branch>           git branch -d (merged branches only)
  remove-dep <worktree> <dep> <marker> <line>
                                          delete a dependency folder (never through a symlink) and append <line> to
                                          <marker> in the worktree's git dir
  checkout-sigs <repo> <base>             JSON {path: signature}: the files of <base> the checkout did not write as the
                                          blob byte for byte (taken before the agent starts; usually empty)
  changed <repo> <base>                   stdin: checkout-sigs' JSON; JSON [[path, exists]]: changed since <base> by
                                          their bytes, committed or not, untracked too
  copy-back <src> <dst> <rel>…            copy each rel from src to dst (deleted in dst when gone from src); a symlink
                                          in src is refused, not followed; JSON {copied, refused}
  hooks <repo> <hook>…                    JSON {hooks_path, hooks: {name: text, "-> target" for a symlink, or null}}
                                          for <repo>/.git/hooks
  hook-write <repo> <hook>                stdin → <repo>/.git/hooks/<hook>, replacing a symlink instead of writing
                                          through it
  hook-remove <repo> <hook>
  bundle-unpushed <ws>                    stdout: a bundle of the branches not on any remote (exit 3: nothing)
  diff-head <ws>                          stdout: git diff HEAD --binary
  exclude <repo> <pattern>                the pattern into the repo's .git/info/exclude
  save <ws> <folder> <name>               stdin → a new file <ws>/<folder>/<name> (never overwriting, never through a
                                          link); prints its path relative to ws
  head <ws>                               JSON {branch, dirty}
  bundle-head <ws>                        stdout: a bundle of the checked-out branch
  bundle-branch <ws> <branch> [<sha>…]    stdout: a bundle of refs/heads/<branch> without what the given commits reach
                                          (the remote's tips, for brunna push: bin/git_remote.py); a sha this repo
                                          lacks is left out of that. Exit 3: nothing the given commits don't reach
                                          (stdout: the branch's commit id instead), 4: no such branch
  set-origin <ws> <url> <branch>          origin = url, the branch tracking it
  set-identity <ws> <name> <email>        user.name and user.email in the repo's own config: the address the
                                          platform's push allows (brunna push setup, bin/git_push_grant.py)
  tracked <repo> <name>…                  JSON [the tracked paths at or under one of the names, at any depth] (at
                                          most 200): what a sandboxed project tracks among the control names
  test-runs <repo> <since>                JSON [run log lines at or after <since> (ISO time)]: the test runner's
                                          measurements in the repo's git common dir (tiers.RunLog), newest MAX_RUNS
  memory-remotes <ws> <branch>            JSON {remote: [urls]}: the remotes known here to have <branch>
  memory-sync <memdir> <bare> <branch> [--lock] [--rules=<name>,…]
                                          commit memory/ and STATE.md, merge what <bare>'s <branch> has, then stdout: a
                                          bundle of HEAD that <bare> lacks (empty: nothing to push); --lock takes
                                          with-lock.sh's lock; what a memory branch may not hold (memory_paths, with the
                                          rules files' names --rules gives) stays in the folder, uncommitted, said on stderr
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import names  # noqa: E402  (standard library only, like this file)

# the real git, by the absolute path project_git resolved: for a project whose git goes through the platform,
# sandboxed-run puts the platform's wrapper first on PATH, which refuses bundle, worktree and the like. Its folder goes
# first on PATH too, for what this job's helpers run (memory_paths)
GIT = names.env("GIT_BIN") or ""
if os.path.isabs(GIT) and os.path.basename(GIT) == "git" and os.access(GIT, os.X_OK):
    os.environ["PATH"] = os.pathsep.join([os.path.dirname(GIT), *filter(None, [os.environ.get("PATH")])])
else:
    GIT = "git"

MEMORY_PATHS = ("memory", "STATE.md")
NOTHING = 3


def git(cwd, *args, check=False, env=None, text=True, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run([GIT, "-C", str(cwd), *args], capture_output="stdout" not in kw, text=text,
                       env={**os.environ, **(env or {})}, **kw)
    if check and r.returncode != 0:
        sys.stderr.write(f"git {' '.join(args)}: {(r.stderr or '').strip()[-300:]}\n")
        sys.exit(1)
    return r


def out(cwd, *args) -> str:
    r = git(cwd, *args)
    return r.stdout if r.returncode == 0 else ""


def emit(obj) -> int:
    json.dump(obj, sys.stdout)
    return 0


def base_branch(repo) -> str | None:
    return next((b for b in ("main", "master") if out(repo, "rev-parse", "--verify", "-q", b)), None)


def op_worktrees(repo: str, days: str) -> int:
    base = base_branch(repo)
    if not base:
        return emit({"base": None, "worktrees": [], "main": []})
    rp = pathlib.Path(repo).resolve()
    wts, cur, n = [], {}, 0
    for line in out(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
            n += 1   # git lists the main worktree first, whichever one this is asked in
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "" and cur:
            p = pathlib.Path(cur["path"])
            if n > 1 and p.resolve() != rp and p.name != ".agent-memory" and cur.get("branch") not in (None, "agent-memory", base):
                b = cur["branch"]
                here = p.is_dir()
                hidden = hidden_files(p) if here else []
                cur.update(merged=git(repo, "merge-base", "--is-ancestor", f"refs/heads/{b}", base).returncode == 0,
                           last=last_work(repo, p, b) if here else None,
                           clean=here and hidden == [] and is_clean(p), hidden=(hidden or [])[:5],
                           exists=here,
                           files=out(repo, "diff", "--name-only", f"{base}...refs/heads/{b}").split())
                wts.append(cur)
            cur = {}
    main = out(repo, "log", f"--since={int(days)} days ago", "--format=%H", base).split()
    return emit({"base": base, "worktrees": wts, "main": main})


# Looking at a worktree: without git's optional locks (a status that refreshes the index would make the worktree look used),
# and with what the repo's config could turn off in a status turned on
LOOK = {"GIT_OPTIONAL_LOCKS": "0"}
STATUS = ("-c", "status.showUntrackedFiles=all", "-c", "core.fsmonitor=false")


def is_clean(wt) -> bool:
    """git says nothing changed here; one that can't say is not clean."""
    r = git(wt, *STATUS, "status", "--porcelain", "--ignore-submodules=none", env=LOOK)
    return r.returncode == 0 and not r.stdout.strip()


def hidden_files(wt) -> list[str] | None:
    """The files here that git is told not to look at (`ls-files -v`: S skip-worktree, lower case assume-unchanged), so a status
    shows no edit in them. One that isn't there (a path left out of the checkout: a sparse one, the control files of a sandboxed
    project's worktree) holds nothing. None when git can't say."""
    r = git(wt, "ls-files", "-v", "-z", env=LOOK, text=False)
    if r.returncode != 0:
        return None
    return [e[2:] for e in _z(r) if (e[0] == "S" or e[0].islower()) and os.path.lexists(pathlib.Path(wt) / e[2:])]


REFLOG_AT = re.compile(rb"> (\d+) [+-]\d{4}(\t|$)")


def head_moved(gd: pathlib.Path) -> int:
    """When HEAD last moved there: what the last line of logs/HEAD says, not the file's time, which `gc` (reflog expire)
    renews in every worktree. 0 when there is none (expired, or logging is off)."""
    try:
        with open(gd / "logs" / "HEAD", "rb") as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - 65536))
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return 0
    m = REFLOG_AT.search(lines[-1]) if lines else None
    return int(m[1]) if m else 0


def last_work(repo, wt, branch: str) -> int | None:
    """The newest of the branch's tip, what HEAD there last moved at (a checkout, a reset, its making) and the index: a
    session that committed nothing still staged or checked out there. Not the admin folder's own time: gc, maintenance and
    any status renew it. The index's is another matter: git writes it again only when what's on disk is not what it noted
    (measured with git 2.30 and 2.50, untracked cache included: a status in a worktree left alone leaves it), so a tool
    that polls status makes a worktree younger once after a change, not every time, and where it does, the worktree is
    only kept longer. None when one of them can't be read."""
    tip = out(repo, "log", "-1", "--format=%ct", f"refs/heads/{branch}", "--").strip()
    gd = out(wt, "rev-parse", "--absolute-git-dir").strip()
    try:
        return max(int(tip), head_moved(pathlib.Path(gd)), int(os.stat(pathlib.Path(gd) / "index").st_mtime)) if gd else None
    except (OSError, ValueError):
        return None


def op_ignored(wt: str) -> int:
    # both said here, whatever status.showUntrackedFiles is: `no` would list no ignored file, `all` each one in a folder
    r = git(wt, "status", "--porcelain", "-z", "--untracked-files=normal", "--ignored=traditional", env=LOOK, text=False)
    if r.returncode != 0:
        sys.stderr.write(f"git status: {os.fsdecode(r.stderr).strip()[-300:]}\n")
        return 1
    return emit([e[3:].rstrip("/") for e in _z(r) if e.startswith("!! ")])


def op_worktree_add(repo: str, path: str) -> int:
    git(repo, "worktree", "add", "--detach", path, "HEAD", check=True)
    print(out(path, "rev-parse", "HEAD").strip())
    return 0


def op_worktree_remove(repo: str, path: str, *flags: str) -> int:
    force = "--force" in flags
    if not force and os.path.isdir(path) and (hidden_files(path) != [] or not is_clean(path)):
        sys.stderr.write(f"{path} holds changes that are not committed\n")
        return 1
    r = git(repo, *STATUS, "worktree", "remove", *(["--force"] if force else []), path)
    sys.stderr.write(r.stderr)
    return r.returncode


def op_worktree_plan(repo: str, branch: str) -> int:
    if git(repo, "check-ref-format", "--branch", branch).returncode != 0:
        sys.stderr.write(f"not a branch name: {branch}\n")
        return 2
    return emit({"exists": git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0})


def _remove_worktree(repo: str, path: str) -> list[str]:
    """Empty the worktree but its .git (bottom-up, links removed as links), then unregister it only if nothing was left:
    git's own `worktree remove` unregisters even when it can't delete the folder (measured 11-15: a control file the
    project's sandbox may not remove). Only this one: `worktree prune` would drop the entry of every worktree whose folder
    isn't there right now (an unmounted disk), with what was staged in it. What was left, if anything."""
    top = pathlib.Path(path)
    if not top.is_dir() or top.is_symlink():
        if not os.path.lexists(top):   # gone already: only its admin entry, if any (a link's target is another's)
            git(repo, "worktree", "remove", "--force", path)
        return []
    left = []
    for root, dirs, files in os.walk(top, topdown=False, followlinks=False):
        r = pathlib.Path(root)
        for n in files + [d for d in dirs if (r / d).is_symlink()]:
            if r == top and n == ".git":
                continue
            try:
                os.unlink(r / n)
            except FileNotFoundError:
                pass
            except OSError:
                left.append(str((r / n).relative_to(top)))
        for d in dirs:
            if (r / d).is_symlink() or (r == top and d == ".git"):
                continue
            try:
                os.rmdir(r / d)
            except FileNotFoundError:
                pass
            except OSError:
                left.append(str((r / d).relative_to(top)) + "/")
    if left:
        return left
    r = git(repo, "worktree", "remove", "--force", path)
    if r.returncode != 0:
        return [f"git worktree remove: {r.stderr.strip()[-200:]}"]
    return []


def op_session_worktree_remove(repo: str, path: str) -> int:
    branch = out(path, "symbolic-ref", "-q", "--short", "HEAD").strip()
    if left := _remove_worktree(repo, path):
        sys.stderr.write(f"left in the worktree, which stays registered: {', '.join(left[:10])}\n")
        return 1
    if branch:
        git(repo, "branch", "-d", branch)   # merged only: a branch with work of its own stays
    return 0


def op_session_worktree_undo(repo: str, path: str, branch: str, new: str) -> int:
    if left := _remove_worktree(repo, path):
        sys.stderr.write(f"left in the worktree, which stays registered: {', '.join(left[:10])}\n")
        return 1
    if new == "1" and git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
        git(repo, "branch", "-D", branch)
    return 0


def _not_writable(path: str, rules: dict) -> bool:
    """What the project's sandbox may not write in a checkout (policy.protect and protect_roots, passed in): at or under a
    control name at any depth, or in a runtime's own folder outside the entries it names writable."""
    q, parts = "/" + path, pathlib.PurePosixPath(path).parts
    if any(q.endswith("/" + n) or f"/{n}/" in q + "/" for n in rules.get("names") or []):
        return True
    return any(parts[:1] == (r.get("name"),) and (len(parts) < 2 or parts[1] not in (r.get("writable") or []))
               for r in rules.get("roots") or [])


def op_session_worktree(repo: str, path: str, branch: str, rules: str) -> int:
    rules = json.loads(rules)
    if git(repo, "check-ref-format", "--branch", branch).returncode != 0:
        sys.stderr.write(f"not a branch name: {branch}\n")
        return 2
    if os.path.lexists(path):
        sys.stderr.write(f"{path} is already there\n")
        return 2
    exists = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0
    git(repo, "worktree", "add", "--no-checkout", *([path, branch] if exists else ["-b", branch, path, "HEAD"]),
        check=True)

    def step(*args, **kw):
        r = git(path, *args, **kw)
        if r.returncode != 0:   # undone, so the same name can be made again: the worktree, and the branch if new
            sys.stderr.write(f"git {args[0]}: {(r.stderr if isinstance(r.stderr, str) else os.fsdecode(r.stderr or b'')).strip()[-300:]}\n")
            git(repo, "worktree", "remove", "--force", path)
            if not exists:
                git(repo, "branch", "-D", branch)
            sys.exit(1)
        return r
    step("reset", "-q")   # the index from the branch; nothing written in the folder yet
    skipped = [p for p in _z(step("ls-files", "-z", text=False)) if _not_writable(p, rules)]
    if skipped:
        step("update-index", "--skip-worktree", "-z", "--stdin", input=b"".join(os.fsencode(p) + b"\0" for p in skipped),
             text=False)
    step("checkout-index", "-a")
    return emit({"path": path, "branch": branch, "skipped": skipped[:50], "n_skipped": len(skipped)})


def op_branch_delete(repo: str, branch: str) -> int:
    return git(repo, "branch", "-d", branch).returncode


def op_remove_dep(wt: str, dep: str, marker: str, line: str) -> int:
    d = pathlib.Path(wt) / dep
    if "/" in dep or d.is_symlink() or not d.is_dir():
        return 1
    shutil.rmtree(d)
    gd = out(wt, "rev-parse", "--absolute-git-dir").strip()
    if gd and "/" not in marker:
        with open(pathlib.Path(gd) / marker, "a") as fh:
            fh.write(line + "\n")
    return 0


# What the agent changed in a throwaway worktree is decided by the files' bytes, never by git's view of them: git
# status and diff run the clean filters and line-ending conversions that attributes name, and a `.gitattributes` the
# agent writes (`*.md filter=lfs` on a machine whose git config defines filter.lfs, as git-lfs installs do) turns
# files it never touched into changes, which copy-back would then write over the real checkout's (an uncommitted edit
# lost). So each file is hashed as it is (a git blob id of its raw bytes, a link's target for a symlink, plus the
# executable bit) and compared with the base tree's entry (`ls-tree`, which reads objects only); where the checkout
# itself wrote other bytes than the blob (a project's own filter or line endings), checkout-sigs records what it
# wrote before the agent starts, and that is the reference instead. Plain git plumbing every supported git has.

def _z(r) -> list[str]:
    return [os.fsdecode(x) for x in r.stdout.split(b"\0") if x]


def _tree(repo: str, base: str) -> tuple[dict[str, str], set[str]]:
    """(path → the signature the checkout of <base> should have written, the submodules' paths, left out)"""
    sigs, gitlinks = {}, set()
    for entry in _z(git(repo, "ls-tree", "-r", "-z", "--full-tree", base, check=True, text=False)):
        meta, _, path = entry.partition("\t")
        mode, kind, oid = meta.split(" ")
        if kind == "blob":
            sigs[path] = f"l:{oid}" if mode == "120000" else f"f:{oid}:{'x' if mode == '100755' else '-'}"
        else:
            gitlinks.add(path)
    return sigs, gitlinks


def _sig(root: str, rel: str, algo: str) -> str | None:
    """the file as it is: f:<blob id>:<x|->, l:<blob id of the link's target>, other (a folder, a FIFO…), or None when
    missing or under a folder that is a symlink (then it is not the file git checked out)."""
    p = pathlib.Path(root)
    for part in pathlib.PurePath(rel).parts[:-1]:
        p = p / part
        if p.is_symlink() or not p.is_dir():
            return None
    p = p / pathlib.PurePath(rel).name
    try:
        st = os.lstat(p)
    except OSError:
        return None
    h = hashlib.new(algo)
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(os.fsencode(p))
        h.update(b"blob %d\0" % len(target) + target)
        return f"l:{h.hexdigest()}"
    if not stat.S_ISREG(st.st_mode):
        return "other"
    with open(p, "rb") as fh:
        h.update(b"blob %d\0" % os.fstat(fh.fileno()).st_size)
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return f"f:{h.hexdigest()}:{'x' if st.st_mode & 0o100 else '-'}"


def _algo(tree: dict[str, str]) -> str:
    oid = next(iter(tree.values()), "f:" + "0" * 40).split(":")[1]
    return "sha256" if len(oid) == 64 else "sha1"


def op_checkout_sigs(repo: str, base: str) -> int:
    """JSON {path: signature} for the files of <base> whose checkout in <repo> is not the blob byte for byte (null when
    missing); run right after the worktree is made, before anyone works in it. Usually empty."""
    tree = _tree(repo, base)[0]
    algo = _algo(tree)
    return emit({p: s for p, want in tree.items() if (s := _sig(repo, p, algo)) != want})


def op_changed(repo: str, base: str) -> int:
    """stdin: checkout-sigs' JSON for this worktree. The paths of <base> whose bytes (or link, or executable bit) are no
    longer what the checkout wrote, and the files that are not in <base> (tracked since, or untracked and not ignored).
    A git that fails here fails the op: an empty list would read as "the agent changed nothing"."""
    try:
        wrote = json.loads(sys.stdin.read())
    except ValueError:
        wrote = None
    if not isinstance(wrote, dict):
        sys.stderr.write("changed: stdin is not checkout-sigs' JSON\n")
        return 1
    tree, gitlinks = _tree(repo, base)
    algo = _algo(tree)
    paths = {p for p, want in tree.items() if _sig(repo, p, algo) != wrote.get(p, want)}
    for listing in (("ls-files", "-z"), ("ls-files", "-z", "--others", "--exclude-standard")):
        paths |= {p for p in _z(git(repo, *listing, check=True, text=False))
                  if p not in tree and p not in gitlinks}
    return emit([[p, os.path.lexists(pathlib.Path(repo) / p)] for p in sorted(paths) if p])


def op_tracked(repo: str, *names: str) -> int:
    """JSON [the tracked paths at or under one of these names, at any depth] (policy.is_control's test), at most 200."""
    out = []
    for p in _z(git(repo, "ls-files", "-z", check=True, text=False)):
        q = "/" + p
        if p and any(q.endswith("/" + n) or f"/{n}/" in q + "/" for n in names):
            out.append(p)
            if len(out) >= 200:
                break
    return emit(out)


def op_copy_back(src: str, dst: str, *rels: str) -> int:
    copied, refused = [], []
    for rel in rels:
        s, d = pathlib.Path(src) / rel, pathlib.Path(dst) / rel
        if ".." in pathlib.PurePath(rel).parts or rel.startswith("/") or s.is_symlink():
            refused.append(rel)
            continue
        if s.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            if d.is_symlink():
                d.unlink()
            shutil.copy2(s, d)
        elif d.exists() or d.is_symlink():
            d.unlink()
        copied.append(rel)
    return emit({"copied": copied, "refused": refused})


def hook_file(repo: str, hook: str) -> pathlib.Path:
    if "/" in hook:
        sys.exit(2)
    return pathlib.Path(repo) / ".git" / "hooks" / hook


def op_hooks(repo: str, *hooks: str) -> int:
    texts = {}
    for h in hooks:
        f = hook_file(repo, h)
        texts[h] = f"-> {os.readlink(f)}" if f.is_symlink() else f.read_text(errors="replace") if f.is_file() else None
    return emit({"hooks_path": out(repo, "config", "core.hooksPath").strip(), "hooks": texts})


def op_hook_write(repo: str, hook: str) -> int:
    f = hook_file(repo, hook)
    tmp = f.with_name(f".{hook}.agent-hub-new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.write_text(sys.stdin.read())
    tmp.chmod(0o755)
    os.replace(tmp, f)   # replaces a symlink at f instead of writing to where it points
    return 0


def op_hook_remove(repo: str, hook: str) -> int:
    f = hook_file(repo, hook)
    if f.is_symlink() or f.exists():
        f.unlink()
    return 0


def op_bundle_unpushed(ws: str) -> int:
    r = git(ws, "bundle", "create", "-", "--branches", "--not", "--remotes", text=False, stdout=sys.stdout.buffer,
            stderr=subprocess.PIPE)
    if r.returncode and b"empty bundle" in (r.stderr or b""):
        return NOTHING
    sys.stderr.write((r.stderr or b"").decode(errors="replace"))
    return r.returncode


def op_diff_head(ws: str) -> int:
    return git(ws, "diff", "HEAD", "--binary", text=False, stdout=sys.stdout.buffer).returncode


def op_memory_sync(memdir: str, bare: str, branch: str, *flags: str) -> int:
    """git-sync-memory.sh's commit, then what git-safe-push.sh's pull did (merge the bare repo's branch), then a bundle
    of what the bare repo lacks. --lock: take with-lock.sh's lock (the same directory in the git dir); without it the
    caller holds it."""
    import memory_paths
    # its own toplevel, the memory branch checked out (memory_paths.is_checkout): anywhere else these commits and merges
    # would be another branch's
    top = out(memdir, "rev-parse", "--is-inside-work-tree", "--show-prefix").strip() == "true"
    if top and memory_paths.begun(memdir):   # first: a rebase has no branch checked out, and checking it out throws it away
        return say_under_way(memdir)
    if not top or out(memdir, "symbolic-ref", "-q", "--short", "HEAD").strip() != branch:
        sys.stderr.write(f"{memdir} is not a checkout of {branch}\n")
        return memory_paths.BRANCH_BAD
    for f in flags:
        if f.startswith("--rules="):
            memory_paths.RULES_NAMES = tuple(n for n in f[len("--rules="):].split(",") if n)
    if "--lock" not in flags:
        return memory_sync(memdir, bare, branch)
    try:
        with memory_paths.locked(memdir):
            return memory_sync(memdir, bare, branch)
    except memory_paths.Blocked as e:
        sys.stderr.write(f"not synced this time: {e}\n")
        return 1


def say_under_way(memdir: str) -> int:
    """A merge (or the like) is someone's to finish, whoever began it: staging its files would mark the conflicts in them
    resolved."""
    import memory_paths
    sys.stderr.write(f"a merge or the like is under way in {memdir}; nothing is committed until it is finished. Take the conflict marks "
                     f"out of {', '.join(memory_paths.unfinished(memdir)[:5])}, then run: {memory_paths.finish_with(memdir)}\n")
    return memory_paths.UNDER_WAY


def memory_sync(memdir: str, bare: str, branch: str) -> int:
    import memory_paths
    MG = memory_paths.GIT
    if memory_paths.under_way(memdir):
        return say_under_way(memdir)
    for p in MEMORY_PATHS:
        if os.path.lexists(pathlib.Path(memdir) / p) or git(memdir, "ls-files", "--error-unmatch", "--", p).returncode == 0:
            git(memdir, *MG, "add", "-A", "--", p, check=True)   # a failed one (the index locked) is a failed sync
    try:
        left = memory_paths.unstage_refused(memdir)
    except memory_paths.Unreadable as e:
        sys.stderr.write(f"what was staged can't be checked, nothing committed ({e})\n")
        return 1
    if left:
        sys.stderr.write("left in the folder, not committed (a memory branch holds only plain notes): " + "; ".join(left) + "\n")
    files = _z(git(memdir, "diff", "--cached", "--name-only", "--no-renames", "-z", "--", "memory/", "STATE.md", check=True, text=False))
    if files:
        git(memdir, *MG, "--literal-pathspecs", "commit", "-q", "-m", "brunna: sync memory/STATE.md", "--", *files, check=True)
    up = out(memdir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
    track = f"refs/remotes/{up}" if up and "/" in up else names.repo_ref("memory-remote")
    git(memdir, *MG, "fetch", "-q", bare, f"+refs/heads/{branch}:{track}", check=True)
    head, remote = out(memdir, "rev-parse", "HEAD").strip(), out(memdir, "rev-parse", track).strip()
    if git(memdir, "merge-base", "--is-ancestor", remote, head).returncode != 0:
        if memory_paths.under_way(memdir):
            return say_under_way(memdir)
        r = git(memdir, *MG, "merge", "--no-edit", "-q", remote)
        if r.returncode != 0:
            if (files := memory_paths.undo_conflict(memdir, remote, r.returncode)) is not None:
                sys.stderr.write(f"the notes here and the platform's both changed {', '.join(files[:5])}; the merge was undone. Someone "
                                 f"has to merge them: git -C '{memdir}' fetch -q '{bare}' {branch} && git -C '{memdir}' merge FETCH_HEAD, "
                                 f"take the conflict marks out, then git -C '{memdir}' commit -a -m merge\n")
                return memory_paths.CONFLICT
            if memory_paths.under_way(memdir):
                return say_under_way(memdir)
            sys.stderr.write(f"the notes could not be merged with the platform's: {memory_paths.scrub(r.stderr or r.stdout).strip()[-300:]}\n")
            return 1
        head = out(memdir, "rev-parse", "HEAD").strip()
    if head == remote:
        return 0
    sys.stdout.flush()
    return git(memdir, "bundle", "create", "-q", "-", "HEAD", f"^{remote}", text=False,
               stdout=sys.stdout.buffer).returncode


def op_memory_remotes(ws: str, branch: str) -> int:
    """JSON {remote: [urls]}: the remotes this repo knows to have <branch> (its remote-tracking refs; nothing is asked over
    the network)."""
    have = {}
    for r in out(ws, "remote").split():
        if git(ws, "rev-parse", "-q", "--verify", f"refs/remotes/{r}/{branch}").returncode == 0:
            have[r] = out(ws, "remote", "get-url", "--all", r).splitlines()
    return emit(have)


def op_exclude(repo: str, pattern: str) -> int:
    """pattern into the repo's .git/info/exclude (shared by its worktrees) when it isn't there yet."""
    common = out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    if not common:
        return 1
    ex = pathlib.Path(common) / "info" / "exclude"
    lines = ex.read_text().splitlines() if ex.exists() else []
    if pattern not in lines:
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text("\n".join(lines + [pattern]) + "\n")
    return 0


def op_save(ws: str, folder: str, name: str) -> int:
    """stdin's bytes into <ws>/<folder>/<name>, a new file (a number added when the name is taken); prints its path
    relative to ws."""
    if "/" in folder or "/" in name or folder.startswith(".") or not name:
        return 2
    d = pathlib.Path(ws) / folder
    d.mkdir(exist_ok=True)
    data, stem = sys.stdin.buffer.read(), name
    for i in range(1, 1000):
        try:
            fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        except FileExistsError:
            name = f"{i}-{stem}"
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        print(f"{folder}/{name}")
        return 0
    return 1


def op_head(ws: str) -> int:
    """JSON {branch, dirty}: the checked-out branch (null when detached) and whether tracked files have changes. A git
    that fails (exit 128: it can't read the repo, or the sandbox denied it something) is an error, not a detached HEAD
    or a clean tree: symbolic-ref -q exits 1 only for a detached HEAD."""
    b = git(ws, "symbolic-ref", "--short", "-q", "HEAD")
    if b.returncode not in (0, 1):
        sys.stderr.write(f"git symbolic-ref: {b.stderr.strip()[-300:]}\n")
        return 1
    s = git(ws, "status", "--porcelain", "--untracked-files=no", check=True)
    return emit({"branch": b.stdout.strip() if b.returncode == 0 else None, "dirty": bool(s.stdout.strip())})


README_NAMES = ("README.md", "README", "readme.md", "README.rst", "README.txt", "Readme.md")


def op_first_look(ws: str) -> int:
    """JSON {readme, commits, tree} for a first summary of a newly connected project (bin/first-look.py): read from the
    committed tree (a README that is a link is a link blob, never what it points to), each capped."""
    tree = [x for x in out(ws, "ls-tree", "--name-only", "HEAD").splitlines() if x][:120]
    readme = ""
    for name in README_NAMES:
        if name in tree:
            kind = out(ws, "ls-tree", "HEAD", "--", name).split()
            if kind and kind[0] == "100644":   # a regular file, not a link (120000) or a submodule
                readme = out(ws, "show", f"HEAD:{name}")[:12000]
            break
    commits = [x for x in out(ws, "log", "-30", "--format=%as %s").splitlines() if x]
    count = out(ws, "rev-list", "--count", "HEAD").strip()
    return emit({"readme": readme, "commits": commits, "tree": tree, "commit_count": int(count) if count.isdigit() else 0})


def op_bundle_head(ws: str) -> int:
    """stdout: a bundle of the checked-out branch (its whole history)."""
    b = out(ws, "symbolic-ref", "--short", "-q", "HEAD").strip()
    if not b:
        return 1
    return git(ws, "bundle", "create", "-q", "-", f"refs/heads/{b}", text=False, stdout=sys.stdout.buffer).returncode


NO_BRANCH = 4


def op_bundle_branch(ws: str, branch: str, *have: str) -> int:
    """stdout: a bundle of refs/heads/<branch>, less what the commits in <have> reach (only the ones this repo has: a
    bundle's prerequisites must exist here). The caller fetches it into a bare repo that holds those commits."""
    if git(ws, "check-ref-format", "--branch", branch).returncode != 0 or branch.startswith("-"):
        sys.stderr.write(f"not a branch name: {branch}\n")
        return 2
    ref = f"refs/heads/{branch}"
    if git(ws, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode != 0:
        sys.stderr.write(f"no branch {branch} in this repo\n")
        return NO_BRANCH
    known = [h for h in have if len(h) in (40, 64) and all(c in "0123456789abcdef" for c in h)
             and git(ws, "cat-file", "-e", f"{h}^{{commit}}").returncode == 0]
    if out(ws, "rev-list", "--count", ref, "--not", *known, "--").strip() == "0":
        # the remote has every commit (git's bundle would print its header before refusing an empty one): the ref
        # may still have to move there, a new branch at a known commit, say
        sys.stdout.write(out(ws, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").strip())
        return NOTHING
    r = git(ws, "bundle", "create", "-q", "-", ref, *(f"^{h}" for h in known), text=False, stdout=sys.stdout.buffer,
            stderr=subprocess.PIPE)
    sys.stderr.write((r.stderr or b"").decode(errors="replace"))
    return r.returncode


def op_set_origin(ws: str, url: str, branch: str) -> int:
    """origin = url, and the branch tracks origin's branch of the same name (the platform pushed it)."""
    has = git(ws, "remote", "get-url", "origin").returncode == 0
    for args in (["remote", "set-url" if has else "add", "origin", url],
                 ["config", f"branch.{branch}.remote", "origin"],
                 ["config", f"branch.{branch}.merge", f"refs/heads/{branch}"]):
        if git(ws, *args).returncode != 0:
            return 1
    return 0


def op_set_identity(ws: str, name: str, email: str) -> int:
    """The repo's commits carry this name and email (its own config; worktrees share it)."""
    if any(c in name + email for c in "\n\r\0") or "@" not in email:
        sys.stderr.write("set-identity: a name and an email address, one line each\n")
        return 2
    for key, value in (("user.name", name), ("user.email", email)):
        if git(ws, "config", "--local", key, value).returncode != 0:
            return 1
    return 0


MAX_RUNS = 5000


def op_test_runs(repo: str, since: str) -> int:
    """Read here, as the project: the git dir is the project's to write (a link or a FIFO in the file's place is
    refused by the reader, never followed)."""
    common = out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    if not common:
        sys.stderr.write("not a git repo\n")
        return 1
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import tiers
    try:
        cut = datetime.datetime.fromisoformat(since)
    except ValueError:
        sys.stderr.write(f"since: not an ISO time: {since}\n")
        return 2
    keep = []
    for r in tiers.RunLog(pathlib.Path(common)).records():
        try:
            if datetime.datetime.fromisoformat(str(r.get("at"))) >= cut:
                keep.append(r)
        except (ValueError, TypeError):
            continue
    return emit(keep[-MAX_RUNS:])


OPS = {"test-runs": op_test_runs, "worktrees": op_worktrees, "ignored": op_ignored, "worktree-add": op_worktree_add,
       "worktree-remove": op_worktree_remove, "worktree-plan": op_worktree_plan, "session-worktree": op_session_worktree,
       "session-worktree-undo": op_session_worktree_undo, "session-worktree-remove": op_session_worktree_remove, "branch-delete": op_branch_delete, "remove-dep": op_remove_dep,
       "checkout-sigs": op_checkout_sigs, "changed": op_changed, "copy-back": op_copy_back, "hooks": op_hooks,
       "hook-write": op_hook_write, "hook-remove": op_hook_remove, "bundle-unpushed": op_bundle_unpushed, "diff-head": op_diff_head,
       "memory-sync": op_memory_sync, "memory-remotes": op_memory_remotes, "exclude": op_exclude, "save": op_save, "head": op_head, "first-look": op_first_look,
       "bundle-head": op_bundle_head, "bundle-branch": op_bundle_branch, "set-origin": op_set_origin, "tracked": op_tracked,
       "set-identity": op_set_identity}


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in OPS:
        sys.stderr.write(f"usage: project-git-job.py {{{'|'.join(OPS)}}} …\n")
        sys.exit(2)
    try:
        sys.exit(OPS[sys.argv[1]](*sys.argv[2:]))
    except PermissionError as e:
        # a write this job's own code makes (copy-back, a removal), run as the project under its sandbox: refused there
        # (git's own refusals come back as its exit code and stderr, not here)
        sys.stderr.write(f"{sys.argv[1]}: writing {e.filename} was refused ({e.strerror}); this job runs as the project, "
                         "under its sandbox\n")
        sys.exit(1)
