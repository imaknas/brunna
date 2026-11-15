#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""What a git call inside a sandbox of a project whose git goes through the platform may be, and how it runs (the git
broker's allowlists; docs/design/git-broker.md, "The allowlists", r2 S1/S3, r3 S-f, and "Step 2 build list", slice a).
Pure: no git runs, no file is read; any OS. Both the wrapper (bin/git_wrapper.py, inside the sandbox) and the broker
(bin/git_broker.py, outside it) classify every call with it; the broker never trusts the wrapper's answer.

  classify(argv, env) → Call
    kind   in_place   real git, inside the caller's own sandbox, no broker: the read forms, and every call whose
                      environment or global options select a repo, index or object folder (it runs unchanged; a write
                      to a git dir then fails loudly with the fence's refusal; never forwarded with them dropped)
           p1         forwarded, run whole by the git-dir writer (slice a builds these: op says which)
           composed   forwarded, written from plumbing by the git-dir writer and the materialiser (slice b)
           network    forwarded, the owner's network side (slice d)
           config     forwarded, the config op (slice a: op says which)
           refused    nothing runs: reason says why
    op     for p1 and config: add, rm-cached, commit, hash-object, apply-cached, notes-add, gc, config-set,
           config-unset, remote-add, remote-set-url, remote-remove
    sub    the subcommand; args the arguments after it; cdirs each global -C in order; gopts the global options
           passed on (-c pairs included); env what is passed on to the broker's git; editor: the caller has an
           editor of its own (refused where git would open one: the editor callback is step 8)

Every option is matched exactly against a table per subcommand (short clusters and sticky values parsed against it;
`--` and `--end-of-options` end the options), so an abbreviation, an alias (an unknown subcommand) or an option not in
the table is refused. GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS=true is set for every broker git as well (ENV_FORCED).
The environment: ENV_PASS is passed on; ENV_SELECT makes a call run in place; the editors are noted; ENV_IGNORED
changes nothing a forwarded call does (the pager is off, prompts are off) and is left out; GIT_TRACE* to stderr is
passed on; every other GIT_* variable refuses a forwarded call (one naming a program or a file git would write:
GIT_EXTERNAL_DIFF, GIT_SSH*, GIT_ASKPASS, GIT_EXEC_PATH, GIT_TEMPLATE_DIR, GIT_CONFIG*, GIT_PROXY_COMMAND, a trace file,
and any not known here). Nothing is dropped silently.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import i18n  # noqa: E402

_ = i18n.domain(__file__)

IN_PLACE, P1, COMPOSED, NETWORK, CONFIG, REFUSED = "in_place", "p1", "composed", "network", "config", "refused"

# ---------- the environment ----------
ENV_PASS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
            "GIT_COMMITTER_DATE", "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS",
            "GIT_ICASE_PATHSPECS", "GIT_MERGE_AUTOEDIT", "LANG", "LANGUAGE", "TZ", "NO_COLOR", "TERM")
ENV_SELECT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
              "GIT_COMMON_DIR")
ENV_EDITOR = ("GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "VISUAL", "EDITOR")
NO_EDITOR = ("", ":", "true", "/usr/bin/true", "/bin/true")
ENV_IGNORED = ("GIT_PAGER", "PAGER", "GIT_TERMINAL_PROMPT", "GIT_PROGRESS_DELAY", "GIT_FLUSH", "GIT_OPTIONAL_LOCKS",
               "GIT_NO_REPLACE_OBJECTS", "GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS", "GIT_ADVICE")
TRACE_TO_STDERR = ("", "0", "1", "2", "true", "false")
ENV_FORCED = {"GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS": "true"}


def relevant_env(environ) -> dict[str, str]:
    """What of a caller's environment the classification reads (the wrapper sends this much and nothing else)."""
    return {k: v for k, v in environ.items()
            if k.startswith(("GIT_", "LC_")) or k in ENV_PASS or k in ENV_EDITOR or k == "PAGER"}


# ---------- the global options ----------
GLOBAL_FLAGS = ("--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs")
GLOBAL_SELECT = ("--git-dir", "--work-tree", "--bare", "--namespace", "--exec-path", "--super-prefix", "--config-env",
                 "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--paginate", "-p",
                 "--no-replace-objects", "--list-cmds", "--attr-source")
# -c: only keys that change no file git writes and run nothing (r3 S-f's list); with a fixed value where one is given
INERT_KEYS = {"user.name": None, "user.email": None, "core.quotepath": None, "init.defaultbranch": None,
              "rebase.updaterefs": None, "merge.conflictstyle": None, "diff.renames": None,
              "commit.gpgsign": "false", "log.showsignature": "false"}
INERT_PREFIXES = ("color.", "advice.")

# ---------- subcommands by class ----------
READS = {"status", "diff", "log", "show", "blame", "annotate", "grep", "rev-parse", "ls-files", "ls-tree", "cat-file",
         "for-each-ref", "show-ref", "describe", "merge-base", "rev-list", "name-rev", "shortlog", "diff-tree",
         "diff-index", "diff-files", "check-ignore", "check-attr", "check-ref-format", "clean", "count-objects",
         "range-diff", "version", "help", "whatchanged", "cherry", "show-branch", "var", "verify-commit",
         "verify-tag"}
COMPOSED_SUBS = {"mv", "restore", "reset", "switch", "checkout", "merge", "rebase", "cherry-pick", "revert", "am",
                 "init", "clone", "update-index", "read-tree", "write-tree", "symbolic-ref", "update-ref"}
NETWORK_SUBS = {"fetch", "pull", "push", "ls-remote"}
REFUSED_SUBS = ("submodule", "filter-branch", "difftool", "mergetool", "hook", "bisect", "daemon", "instaweb",
                "http-backend", "upload-pack", "receive-pack", "shell", "send-email", "credential", "fast-import",
                "bundle", "replace", "maintenance")


def refused_sub(sub: str) -> str:
    """Why a subcommand is refused by name (the design's list, with the owner's decisions)."""
    serves = _("it serves the repo")
    return {
        "submodule": _("submodules aren't served (the owner's decision)"),
        "filter-branch": _("it runs programs over the history"),
        "difftool": _("it runs a program of the caller's"), "mergetool": _("it runs a program of the caller's"),
        "hook": _("hooks run through the platform's forwarder only"),
        "bisect": _("bisect isn't in the allowlist"),
        "daemon": serves, "instaweb": serves, "http-backend": serves, "upload-pack": serves, "receive-pack": serves,
        "shell": serves,
        "send-email": _("it sends mail with the owner's settings"),
        "credential": _("credentials are never reached from a sandbox"),
        "fast-import": _("it writes objects and refs unchecked"), "bundle": _("bundles are the platform's own"),
        "replace": _("replace refs change what the owner's git shows"),
        "maintenance": _("it writes the owner's global config and schedules jobs"),
    }[sub]
STRATEGIES = ("ort", "recursive", "resolve", "octopus", "ours", "subtree")
REF_NAME = re.compile(r"[A-Za-z0-9._/-]{1,200}")

# per subcommand: each option's exact spelling and its arity (0: none; 1: a value, as the next argument, after = or
# sticky; "=": an optional value, only after = or sticky)
OPTS = {
    "add": {"-n": 0, "--dry-run": 0, "-v": 0, "--verbose": 0, "-f": 0, "--force": 0, "-u": 0, "--update": 0, "-A": 0,
            "--all": 0, "--no-all": 0, "--ignore-removal": 0, "--no-ignore-removal": 0, "-N": 0, "--intent-to-add": 0,
            "--refresh": 0, "--ignore-errors": 0, "--ignore-missing": 0, "--renormalize": 0, "--chmod": 1,
            "--sparse": 0, "-q": 0, "--quiet": 0},
    "rm": {"--cached": 0, "-r": 0, "-f": 0, "--force": 0, "-n": 0, "--dry-run": 0, "-q": 0, "--quiet": 0,
           "--ignore-unmatch": 0, "--sparse": 0},
    "commit": {"-m": 1, "--message": 1, "-F": 1, "--file": 1, "-a": 0, "--all": 0, "--amend": 0, "--no-edit": 0,
               "--allow-empty": 0, "--allow-empty-message": 0, "-q": 0, "--quiet": 0, "--author": 1, "--date": 1,
               "-n": 0, "--no-verify": 0, "--verify": 0, "-s": 0, "--signoff": 0, "--no-signoff": 0, "--cleanup": 1,
               "--trailer": 1, "-o": 0, "--only": 0, "-i": 0, "--include": 0, "--reset-author": 0, "-C": 1,
               "--reuse-message": 1, "--fixup": 1, "--dry-run": 0, "--short": 0, "--porcelain": 0, "--long": 0,
               "-z": 0, "--null": 0, "--status": 0, "--no-status": 0, "-u": "=", "--untracked-files": "=", "-v": 0,
               "--verbose": 0, "--no-post-rewrite": 0},
    "hash-object": {"-w": 0, "-t": 1, "--stdin": 0, "--stdin-paths": 0, "--path": 1, "--no-filters": 0},
    "apply": {"--cached": 0, "--index": 0, "--check": 0, "--stat": 0, "--numstat": 0, "--summary": 0, "--apply": 0,
              "-p": 1, "-C": 1, "--recount": 0, "--unidiff-zero": 0, "-R": 0, "--reverse": 0, "--allow-empty": 0,
              "--inaccurate-eof": 0, "-v": 0, "--verbose": 0, "-q": 0, "--quiet": 0, "--whitespace": 1,
              "--ignore-space-change": 0, "--ignore-whitespace": 0, "--exclude": 1, "--include": 1, "--no-add": 0,
              "--directory": 1, "--binary": 0, "--allow-binary-replacement": 0, "-z": 0},
    "notes": {"--ref": 1},
    "notes add": {"-m": 1, "--message": 1, "-F": 1, "--file": 1, "-f": 0, "--force": 0, "--allow-empty": 0},
    "gc": {"--aggressive": 0, "--auto": 0, "--quiet": 0, "-q": 0, "--prune": "=", "--no-prune": 0, "--force": 0,
           "--keep-largest-pack": 0},
    "config": {"--local": 0, "--unset": 0, "--type": 1, "--bool": 0, "--int": 0, "--path": 0, "--no-type": 0},
    "remote": {"-v": 0, "--verbose": 0},
}
OPTS["notes append"] = OPTS["notes add"]
SIGNING = {"-S", "--gpg-sign", "--signed"}
INTERACTIVE = {"add": {"-p", "--patch", "-i", "--interactive", "-e", "--edit"},
               "commit": {"-p", "--patch", "--interactive"}}
NEEDS_EDITOR = {"commit": {"-e": 0, "--edit": 0, "-c": 1, "--reedit-message": 1, "-t": 1, "--template": 1,
                           "--squash": 1}}


@dataclasses.dataclass
class Call:
    kind: str
    sub: str | None = None
    args: list = dataclasses.field(default_factory=list)
    cdirs: list = dataclasses.field(default_factory=list)
    gopts: list = dataclasses.field(default_factory=list)
    env: dict = dataclasses.field(default_factory=dict)
    editor: bool = False
    op: str | None = None
    reason: str | None = None
    opts: list = dataclasses.field(default_factory=list)     # (spelling, value) parsed against the table
    pos: list = dataclasses.field(default_factory=list)      # positionals, in order


class Refused(Exception):
    pass


def refused(reason: str, **kw) -> Call:
    return Call(REFUSED, reason=reason, **kw)


def parse(args: list[str], table: dict) -> tuple[list[tuple[str, str | None]], list[str]]:
    """(options, positionals) of args against an exact table; raises Refused for anything not in it."""
    opts, pos, i = [], [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a in ("--", "--end-of-options"):
            pos += args[i:]
            break
        if a == "-" or not a.startswith("-"):
            pos.append(a)
            continue
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            ar = table.get(name)
            if ar is None:
                raise Refused(_("unknown option {option}", option=name))
            if eq:
                if ar == 0:
                    raise Refused(_("{option} takes no value", option=name))
                opts.append((name, val))
            elif ar == 1:
                if i >= len(args):
                    raise Refused(_("{option} needs a value", option=name))
                opts.append((name, args[i]))
                i += 1
            else:
                opts.append((name, None))
            continue
        j = 1
        while j < len(a):
            name = "-" + a[j]
            ar = table.get(name)
            if ar is None:
                raise Refused(_("unknown option {option}", option=name))
            rest = a[j + 1:]
            if ar == 0:
                opts.append((name, None))
                j += 1
                continue
            if rest or ar == "=":
                opts.append((name, rest or None))
            else:
                if i >= len(args):
                    raise Refused(_("{option} needs a value", option=name))
                opts.append((name, args[i]))
                i += 1
            break
    return opts, pos


def _before_dashdash(args):
    out = []
    for a in args:
        if a in ("--", "--end-of-options"):
            break
        out.append(a)
    return out


def _short_has(args, chars) -> bool:
    return any(a.startswith("-") and not a.startswith("--") and len(a) > 1 and any(c in a[1:] for c in chars)
               for a in _before_dashdash(args))


def owner_decisions(sub: str, args: list[str]) -> str | None:
    """The refusals by name that hold for every forwarded subcommand (the owner's decisions, 11-15)."""
    head = _before_dashdash(args)
    for a in head:
        name = a.split("=", 1)[0]
        if name.startswith("--recurse-submodules"):
            return _("submodules aren't served (the owner's decision)")
        if name in SIGNING or (a.startswith("-S") and not a.startswith("--")):
            return _("signing isn't done through the platform: your key is never used on a project's request")
        if name == "--separate-git-dir" or (sub in ("init", "clone") and name in ("--bare", "--mirror")):
            return _("a bare or separate git dir is a shape your own git would enter")
    if sub == "tag" and any(a.split("=", 1)[0] in ("--sign", "--local-user") or a.startswith("-u")
                            or re.fullmatch(r"-[afns]*s[afns]*", a) for a in head):
        return _("signing isn't done through the platform: your key is never used on a project's request")
    if sub in ("merge", "rebase", "pull", "cherry-pick", "revert"):
        for k, a in enumerate(head):
            val = None
            if a.startswith("--strategy="):
                val = a.split("=", 1)[1]
            elif a == "--strategy" and k + 1 < len(head):
                val = head[k + 1]
            elif sub in ("merge", "rebase", "pull") and a == "-s" and k + 1 < len(head):
                val = head[k + 1]
            elif sub in ("merge", "rebase", "pull") and a.startswith("-s") and not a.startswith("--") and len(a) > 2:
                val = a[2:]
            if val is not None and val not in STRATEGIES:
                return _("only git's own merge strategies ({names}): another name makes git look for a program",
                         names=", ".join(STRATEGIES))
    return None


def _inert(pair: str) -> str | None:
    key, eq, val = pair.partition("=")
    if not eq:
        return _("-c {key} without a value", key=key)
    low = key.lower()
    if "\n" in val or len(val) > 200:
        return _("-c {key}: the value isn't plain", key=key)
    if low in INERT_KEYS:
        want = INERT_KEYS[low]
        return None if want is None or val.lower() == want else _("-c {key} only as {key}={value}", key=key, value=want)
    if low.startswith(INERT_PREFIXES):
        return None
    return _("-c {key} isn't one of the settings passed on", key=key)


def split_globals(argv: list[str]) -> tuple[list[str], list[str], list[str], bool, str | None, list[str]]:
    """(cdirs, gopts, -c pairs, selects, sub, args): the global options before the subcommand."""
    cdirs, gopts, pairs, selects, i = [], [], [], False, 0
    while i < len(argv):
        a = argv[i]
        if a == "-C" and i + 1 < len(argv):
            cdirs.append(argv[i + 1])
            i += 2
        elif a == "-c" and i + 1 < len(argv):
            pairs.append(argv[i + 1])
            i += 2
        elif a in GLOBAL_FLAGS:
            gopts.append(a)
            i += 1
        elif a.split("=", 1)[0] in GLOBAL_SELECT or a in ("--version", "-v", "--help", "-h", "--html-path",
                                                           "--man-path", "--info-path"):
            selects = True
            if a in ("--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--attr-source") \
                    and i + 1 < len(argv):
                i += 1
            i += 1
        elif a.startswith("-"):
            selects = True   # an option git may know and this doesn't: never forwarded, run as the caller
            i += 1
        else:
            return cdirs, gopts, pairs, selects, a, argv[i + 1:]
    return cdirs, gopts, pairs, selects, None, []


def env_check(env: dict) -> tuple[dict, bool, str | None, bool]:
    """(passed on, the caller has an editor, why refused, selects a repo)."""
    passed, editor, why, selects = {}, False, None, False
    for k, v in sorted(env.items()):
        if k in ENV_SELECT:
            selects = selects or bool(v)
        elif k in ENV_PASS or k.startswith("LC_"):
            passed[k] = v
        elif k in ENV_EDITOR:
            editor = editor or v.strip() not in NO_EDITOR
        elif k in ENV_IGNORED or not k.startswith("GIT_"):
            continue
        elif k.startswith("GIT_TRACE"):
            if v.strip().lower() in TRACE_TO_STDERR:
                passed[k] = v
            elif why is None:
                why = _("{var} names a file git would write", var=k)
        elif why is None and v != "":
            why = _("{var} isn't passed through the platform's git", var=k)
    return passed, editor, why, selects


def classify(argv: list[str], env: dict) -> Call:
    cdirs, gopts, pairs, gselect, sub, args = split_globals(list(argv))
    passed, editor, env_why, eselect = env_check(env)
    base = {"sub": sub, "args": args, "cdirs": cdirs, "env": passed, "editor": editor}
    if gselect or eselect or sub is None:
        return Call(IN_PLACE, **base)
    head = _before_dashdash(args)
    if "--help" in head or "-h" in head:
        return Call(IN_PLACE, **base)
    kind, op = kind_of(sub, args)
    if kind == IN_PLACE:
        return Call(IN_PLACE, **base)
    if kind == REFUSED:
        return refused(op, **base)
    if env_why:
        return refused(env_why, **base)
    for pair in pairs:
        if (why := _inert(pair)):
            return refused(why, **base)
    base["gopts"] = [*gopts, *(x for p in pairs for x in ("-c", p))]
    if (why := owner_decisions(sub, args)):
        return refused(why, **base)
    if kind in (COMPOSED, NETWORK):
        return Call(kind, **base)
    try:
        return checked(kind, op, sub, args, editor, base)
    except Refused as e:
        return refused(str(e), **base)


def kind_of(sub: str, args: list[str]) -> tuple[str, str | None]:
    """(class, op or the reason): by subcommand, then by the form it takes."""
    head = _before_dashdash(args)
    if sub in REFUSED_SUBS:
        return REFUSED, refused_sub(sub)
    if sub.startswith(("credential-", "remote-")):
        return REFUSED, _("it isn't run for a project")
    if sub in READS:
        return IN_PLACE, None
    if sub == "archive":
        return (REFUSED, _("archive --remote reaches another repo")) if any(
            a.split("=", 1)[0] == "--remote" for a in head) else (IN_PLACE, None)
    if sub == "fsck":
        return (REFUSED, _("fsck --lost-found writes into the git dir")) if "--lost-found" in head else (IN_PLACE, None)
    if sub == "format-patch":
        return (IN_PLACE, None) if "--stdout" in head else \
            (REFUSED, _("format-patch writes files: use --stdout"))
    if sub == "hash-object":
        if "--literally" in head:
            return REFUSED, _("hash-object --literally writes objects git wouldn't check")
        return (P1, "hash-object") if "-w" in head or _short_has(args, "w") else (IN_PLACE, None)
    if sub in NETWORK_SUBS:
        return NETWORK, None
    if sub in COMPOSED_SUBS:
        return COMPOSED, None
    if sub in ("add", "commit", "gc"):
        return P1, sub
    if sub == "rm":
        return (P1, "rm-cached") if "--cached" in head else (COMPOSED, None)
    if sub == "apply":
        writes = {"--cached", "--index", "--apply"} & set(head)
        if not writes and {"--check", "--stat", "--numstat", "--summary"} & set(head):
            return IN_PLACE, None
        if "--cached" in head and "--index" not in head:
            return P1, "apply-cached"
        return COMPOSED, None
    if sub == "notes":
        return _notes_kind(args)
    if sub == "branch":
        return _branch_kind(args)
    if sub == "tag":
        return _tag_kind(args)
    if sub == "stash":
        first = next((a for a in head if not a.startswith("-")), None)
        return (IN_PLACE, None) if first in ("list", "show") else (COMPOSED, None)
    if sub == "worktree":
        first = next((a for a in head if not a.startswith("-")), None)
        return (IN_PLACE, None) if first == "list" else (COMPOSED, None)
    if sub == "reflog":
        first = next((a for a in head if not a.startswith("-")), None)
        if first in (None, "show", "exists") or first not in ("expire", "delete", "drop"):
            return IN_PLACE, None
        return REFUSED, _("reflog {action} isn't in the allowlist", action=first)
    if sub == "remote":
        return _remote_kind(args)
    if sub == "config":
        return _config_kind(args)
    return REFUSED, _("git {sub} isn't one the platform's git runs (an alias is refused the same way)", sub=sub)


def _notes_kind(args):
    try:
        opts, pos = parse_until_sub(args, OPTS["notes"])
    except Refused as e:
        return REFUSED, str(e)
    action = pos[0] if pos else "list"
    if action in ("list", "show", "get-ref"):
        return IN_PLACE, None
    if action in ("add", "append"):
        return P1, "notes-add"
    return REFUSED, _("notes {action} isn't in the allowlist", action=action)


def parse_until_sub(args, table):
    """notes' own options, before its action: (options, [action, rest…])."""
    opts, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        name, eq, val = a.partition("=")
        if table.get(name) is None:
            raise Refused(_("unknown option {option}", option=name))
        if eq:
            opts.append((name, val))
            i += 1
        else:
            if i + 1 >= len(args):
                raise Refused(_("{option} needs a value", option=name))
            opts.append((name, args[i + 1]))
            i += 2
    return opts, args[i:]


BRANCH_WRITES = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-u", "--set-upstream-to",
                 "--unset-upstream", "--edit-description", "-f", "--force", "-t", "--track", "--no-track",
                 "--create-reflog"}
BRANCH_LISTS = {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
                "--show-current", "-a", "--all", "-r", "--remotes"}


def _branch_kind(args):
    head = _before_dashdash(args)
    names = {a.split("=", 1)[0] for a in head}
    if names & BRANCH_WRITES or _short_has(args, "dDmMcCuft"):
        return COMPOSED, None
    pos = [a for a in head if not a.startswith("-")]
    if pos and not names & {"-l", "--list"}:
        if names & (BRANCH_LISTS - {"-a", "--all", "-r", "--remotes"}):
            return IN_PLACE, None   # their value is the positional
        return COMPOSED, None
    return IN_PLACE, None


def _tag_kind(args):
    head = _before_dashdash(args)
    names = {a.split("=", 1)[0] for a in head}
    if names & {"-l", "--list", "-v", "--verify"} or any(a.startswith("-n") and not a.startswith("--") for a in head):
        return IN_PLACE, None
    pos = [a for a in head if not a.startswith("-")]
    listing = {"--contains", "--no-contains", "--points-at", "--merged", "--no-merged"}
    if not pos or names & listing and len(pos) <= 1:
        return IN_PLACE, None
    return COMPOSED, None


def _remote_kind(args):
    head = _before_dashdash(args)
    pos = [a for a in head if not a.startswith("-")]
    action = pos[0] if pos else None
    if action is None or action == "get-url":
        return IN_PLACE, None
    if action == "show":
        return (IN_PLACE, None) if "-n" in head else (NETWORK, None)
    if action in ("add", "set-url", "remove", "rm"):
        return CONFIG, {"add": "remote-add", "set-url": "remote-set-url"}.get(action, "remote-remove")
    if action in ("prune", "update"):
        return NETWORK, None
    if action in ("rename", "set-head"):
        return COMPOSED, None
    return REFUSED, _("remote {action} isn't in the allowlist", action=action)


CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list", "--get-color",
                "--get-colorbool", "get", "list"}
CONFIG_SCOPES = {"--global", "--system", "--file", "-f", "--blob", "--worktree"}
CONFIG_WRITES = {"--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section", "-e", "--edit"}


def _config_kind(args):
    head = _before_dashdash(args)
    names = {a.split("=", 1)[0] for a in head}
    if names & CONFIG_READS or (head and head[0] in ("get", "list")):
        return IN_PLACE, None
    if (other := sorted(names & CONFIG_WRITES)):
        return REFUSED, _("config {option} isn't in the allowlist", option=other[0])
    pos = [a for a in head if not a.startswith("-")]
    if head and head[0] in ("set", "unset"):
        pos = pos[1:]
        verb = head[0]
    else:
        verb = "unset" if "--unset" in names else ("set" if len(pos) >= 2 else "get")
    if verb == "get":
        return IN_PLACE, None
    if names & CONFIG_SCOPES:
        return REFUSED, _("only the repo's own config is changed through the platform")
    return CONFIG, f"config-{verb}"


def checked(kind: str, op: str, sub: str, args: list[str], editor: bool, base: dict) -> Call:
    """The forms the broker runs in slice a, parsed against their tables (Refused for anything else)."""
    if sub == "config":
        return _config_checked(op, args, base)
    if sub == "remote":
        return _remote_checked(op, args, base)
    if sub == "notes":
        nopts, rest = parse_until_sub(args, OPTS["notes"])
        for _n, ref in nopts:
            if not REF_NAME.fullmatch(ref) or ".." in ref or (ref.startswith("refs/") and not ref.startswith("refs/notes/")):
                raise Refused(_("--ref names only notes refs"))
        opts, pos = parse(rest[1:], OPTS[f"notes {rest[0]}"])
        if not any(o in ("-m", "--message", "-F", "--file") for o, _v in opts):
            raise Refused(_("notes without -m opens an editor, which the platform's git can't run yet"))
        if len(pos) > 1:
            raise Refused(_("notes {action} takes one object", action=rest[0]))
        return Call(kind, op=op, opts=opts, pos=pos, **base)
    table = {**OPTS[sub], **{n: 0 for n in INTERACTIVE.get(sub, ())}, **NEEDS_EDITOR.get(sub, {})}
    opts, pos = parse(args, table)
    for o, _v in opts:
        if o in INTERACTIVE.get(sub, ()):
            raise Refused(_("git {sub} {option} is interactive: write the patch and use git apply --cached",
                            sub=sub, option=o))
        if o in NEEDS_EDITOR.get(sub, {}):
            raise Refused(_("{option} opens an editor, which the platform's git can't run yet", option=o))
    names = [o for o, _v in opts]
    if sub == "commit":
        sources = {"-m", "--message", "-F", "--file", "-C", "--reuse-message", "--fixup"}
        for o, v in opts:
            if o == "--fixup" and ":" in (v or ""):
                raise Refused(_("--fixup={value} opens an editor, which the platform's git can't run yet", value=v))
        dry = {"--dry-run", "--short", "--porcelain", "--long"} & set(names)
        if editor and not dry and not sources & set(names) and not ("--amend" in names and "--no-edit" in names):
            raise Refused(_("this commit has no message, and your editor can't be run through the platform's git yet: "
                            "give it with -m or -F"))
    if sub == "gc" and pos:
        raise Refused(_("gc takes no arguments"))
    if sub == "hash-object" and "-w" not in names:
        return Call(IN_PLACE, **base)
    return Call(kind, op=op, opts=opts, pos=pos, **base)


def _config_checked(op, args, base):
    head = args
    if head and head[0] in ("set", "unset"):
        head = head[1:]
    opts, pos = parse(head, OPTS["config"])
    names = [o for o, _v in opts]
    if "--type" in names or "--bool" in names or "--int" in names or "--path" in names:
        raise Refused(_("the config op takes plain values"))
    if op == "config-unset":
        if len(pos) != 1:
            raise Refused(_("config --unset takes one key"))
    elif len(pos) != 2:
        raise Refused(_("config takes one key and one value"))
    return Call(CONFIG, op=op, opts=opts, pos=pos, **base)


def _remote_checked(op, args, base):
    opts, pos = parse(args, OPTS["remote"])
    want = {"remote-add": 3, "remote-set-url": 3, "remote-remove": 2}[op]
    if len(pos) != want:
        raise Refused(_("remote {action}: {n} arguments expected", action=pos[0] if pos else "", n=want - 1))
    name = pos[1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise Refused(_("{name} isn't a plain remote name", name=name))
    return Call(CONFIG, op=op, opts=opts, pos=pos, **base)


def message(call: Call) -> str:
    """What the caller is told when nothing runs (exit 2)."""
    return _("git: can't be done here ({why}); nothing was run", why=call.reason)
