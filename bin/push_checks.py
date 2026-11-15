#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The checks that replace the owner's look before the platform pushes a sandboxed project's branch
(docs/design/platform-push.md, section 4, r1 and r2). bin/git_remote.py runs them in its staging repo (checked.git), on the
objects only the push brings: `rev-list --objects <new> --not <remote tips>`, each read raw by type with `cat-file`. Each
check fails closed: when it can't run or can't tell, it refuses and says which check.

  objects  (4)  no new gitlink; no Git LFS pointer or `filter=lfs` attribute; every new commit's headers are tree, parent,
                author, committer and (UTF-8 only) encoding: a signature, mergetag or anything else is refused (r2 S-b)
  size     (9)  new commits, each new blob, a commit object, and every new object together (r2 S-m); run before the scans
  ci       (5)  the pushed commit's CI entries (every forge's, CI_ENTRIES) equal the remote default branch's, unless
                enforce.git_ci
  identity (6)  every new commit's author and committer email in enforce.git_push.emails; on a public remote only noreply
                ones and those in public_emails (r2 S-l)
  secrets  (7)  every new blob as text, also as UTF-16 and with NULs stripped (r2 S-d), every new commit object whole,
                every new tree's entry names: the built-in rules always (RULES, FILENAMES over every (path, blob) pair of
                the new trees, r2 S-c), gitleaks when installed (hardened: `gitleaks:allow` neutralised, a fresh folder, an
                empty ignore file, an environment from scratch; on stdin it sees no path, so its path rules don't apply),
                and every secret value the platform has seen the project declare, matched as a substring (Declared;
                kept owner-only in the secrets folder, never dropped: r2 S-e, review B1)
  names    (8)  on a public remote: no private project name (private_names) but its own, no owner's home path, no owner's
                user name as a path segment, in blobs, commits or any new tree's paths; the project is told that a
                private name is there, never which (review S3)

What a check reports names the commit, the path or "message", and the rule; never the value. The new trees are walked
once each, with caps on entries, depth and time (review B2), after the sizes are checked.
"""
from __future__ import annotations

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
import unicodedata
from typing import NamedTuple

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import nofollow  # noqa: E402

_ = i18n.domain(__file__)

COMMIT_CAP = 1 << 20          # one commit object (r2 S-m)
SECRET_CAP = 64 << 10          # one declared secret file
MIN_SECRET = 12                # a declared value counts from this length
GITLEAKS_MIN = (8, 19)         # `gitleaks stdin`
CHUNK = 4 << 20                # blobs joined for one gitleaks run
BATCH = 16 << 20               # blobs read at once
MAX_ENTRIES = 200_000          # tree entries walked in one push (review B2)
MAX_DEPTH = 256                # folders deep
MAX_PATH = 4096                # one path's bytes
MAX_PATH_BYTES = 32 << 20      # every new tree's paths together
MAX_TREE = 16 << 20            # one tree object (about 400 000 entries)
MAX_TREES = 64 << 20           # every new tree object together
NEUTRAL = b"gitleaks-allow-neutralised"
CI_ENTRIES = (".github", ".gitlab-ci.yml", ".circleci", ".buildkite", ".travis.yml", "Jenkinsfile", "azure-pipelines.yml",
              "bitbucket-pipelines.yml", ".gitea", ".forgejo", ".woodpecker", ".woodpecker.yml")
HEADERS = ("tree", "parent", "author", "committer")
LFS = b"version https://git-lfs.github.com/spec/v1"
# high-confidence only: what the platform refuses with or without gitleaks
RULES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")),
    ("aws-access-key-id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("stripe-live-key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}")),
    ("openai-project-key", re.compile(r"\bsk-proj-[0-9A-Za-z_-]{20,}")),
]
FILENAME_RULES = [
    ("env-file", re.compile(r"^\.env(\..+)?$"), re.compile(r"\.(example|sample|template)$")),
    ("key-file", re.compile(r"\.(pem|p12|pfx|key)$", re.I), None),
    ("ssh-key-file", re.compile(r"^id_(rsa|ed25519)"), None),
]
NOREPLY = re.compile(r"@users\.noreply\.github\.com$|noreply", re.I)
TOKEN = re.compile(r"[^\s\"'`,;<>(){}\[\]|]+")
LONG_TOKEN = re.compile(r"[^\s\"'`,;<>(){}\[\]|]{513,}")   # a word longer than MAX_VALUE


class Finding(NamedTuple):
    check: str            # objects | size | ci | identity | secrets | names
    reason: str           # for the project and the owner: the commit, the path or "message", the rule; never a value
    fix: str = "project"  # project | owner | wait
    cause: str = ""       # the owner to-do's cause when fix is owner
    detail: str = ""      # for the owner's record only (pushes.jsonl), never told to the project


class Failure(Exception):
    """A check couldn't run: the push is refused, the reason names the check."""

    def __init__(self, check: str, reason: str, fix: str = "owner", cause: str = ""):
        super().__init__(reason)
        self.check, self.reason, self.fix, self.cause = check, reason, fix, cause or f"{check}-failed"


# ---------- reading the new objects ----------

class Objects:
    """The objects only the push brings, read from a bare repo the platform owns (git(*args, input=) → bytes, raising
    Failure on an error)."""

    def __init__(self, git, new: str, tips: list[str]):
        self.git, self.new, self.tips = git, new, tips
        # ids only: the names rev-list would print are paths, as long as the project makes them (the re-review's B2)
        out = git("rev-list", "--objects", "--no-object-names", new, "--not", *tips).decode(errors="replace")
        oids = [line.strip() for line in out.splitlines() if line.strip()]
        self.types, self.sizes = {}, {}
        if oids:
            got = git("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                      input=("\n".join(oids) + "\n").encode()).decode(errors="replace")
            for line in got.splitlines():
                parts = line.split()
                if len(parts) != 3:
                    raise Failure("objects", _("an object of the push can't be read ({line})", line=line[:80]))
                self.types[parts[0]], self.sizes[parts[0]] = parts[1], int(parts[2])
        self.commits = [o for o, t in self.types.items() if t == "commit"]
        self.trees = [o for o, t in self.types.items() if t == "tree"]
        self.blobs = [o for o, t in self.types.items() if t == "blob"]
        self.raw: dict[str, bytes] = {}

    def read(self, oids) -> dict[str, bytes]:
        """oid → content, by cat-file --batch (one process)."""
        oids = [o for o in oids if o not in self.raw]
        if oids:
            data = self.git("cat-file", "--batch", input=("\n".join(oids) + "\n").encode())
            pos = 0
            for _o in oids:
                nl = data.index(b"\n", pos)
                head = data[pos:nl].decode(errors="replace").split()
                if len(head) != 3:
                    raise Failure("objects", _("an object of the push can't be read ({line})", line=" ".join(head)[:80]))
                n = int(head[2])
                self.raw[head[0]] = data[nl + 1:nl + 1 + n]
                pos = nl + 1 + n + 1
        return self.raw

    def blobs_read(self):
        """(oid, content) of each new blob, in batches of about BATCH bytes (one cat-file --batch each), not kept."""
        batch, size = [], 0
        for oid in self.blobs:
            batch.append(oid)
            size += self.sizes[oid]
            if size >= BATCH:
                yield from self._batch(batch)
                batch, size = [], 0
        if batch:
            yield from self._batch(batch)

    def _batch(self, oids):
        data, pos = self.git("cat-file", "--batch", input=("\n".join(oids) + "\n").encode()), 0
        for _o in oids:
            nl = data.index(b"\n", pos)
            head = data[pos:nl].decode(errors="replace").split()
            if len(head) != 3:
                raise Failure("objects", _("an object of the push can't be read ({line})", line=" ".join(head)[:80]))
            n = int(head[2])
            yield head[0], data[nl + 1:nl + 1 + n]
            pos = nl + 1 + n + 1

    def tree_entries(self, oid: str) -> list[tuple[str, str, str]]:
        """(mode, name, oid) of a tree object, parsed from its raw form (names as bytes decoded with replacement)."""
        raw, out, pos = self.read([oid])[oid], [], 0
        width = 20 if len(oid) == 40 else 32
        while pos < len(raw):
            sp = raw.index(b" ", pos)
            nul = raw.index(b"\0", sp)
            mode = raw[pos:sp].decode()
            name = raw[sp + 1:nul].decode(errors="replace")
            out.append((mode, name, raw[nul + 1:nul + 1 + width].hex()))
            pos = nul + 1 + width
        return out

    def walk(self, deadline: float | None = None) -> tuple[list[tuple[str, str, str]], set[str], dict[str, str]]:
        """Every entry of every new tree, each new tree walked once (by its id, under the first path it is reached at),
        from each new commit's root: ([(mode, full path, oid)], the gitlinks' paths, {oid: the first new commit that brings
        it}). A tree the remote already has is not walked (its entries' names are there already). Iterative, with caps
        on the entries, the depth and the time: a tree that names one subtree twice, k levels deep, would otherwise list
        2^k paths (review B2); past a cap the push is refused as a size finding, never walked on."""
        import time
        # a tree's size says how many entries it has, before it is read: one too wide, or all of them too large, is
        # refused unread
        if (wide := max((self.sizes[t] for t in self.trees), default=0)) > MAX_TREE:
            raise Failure("size", _("a new tree is {mb} MB as an object: push in parts", mb=wide >> 20), fix="project")
        if sum(self.sizes[t] for t in self.trees) > MAX_TREES:
            raise Failure("size", _("the new trees come to more than {mb} MB: push in parts", mb=MAX_TREES >> 20),
                          fix="project")
        self.read([*self.trees, *self.commits])   # every new tree and commit in one batch, bounded just above
        new_trees, entries, seen, first, path_bytes = set(self.trees), [], set(), {}, 0
        stack = [(self.commit_header(c, "tree")[0], "", c, 0) for c in reversed(self.commits)]
        while stack:
            tree, prefix, commit, depth = stack.pop()
            if tree not in new_trees or tree in seen:
                continue
            seen.add(tree)
            if depth > MAX_DEPTH:
                raise Failure("size", _("the new trees nest deeper than {n} folders", n=MAX_DEPTH), fix="project")
            if deadline is not None and time.monotonic() > deadline:
                raise Failure("size", _("the new trees took too long to read: push in parts"), fix="project")
            for mode, name, oid in self.tree_entries(tree):
                path = f"{prefix}{name}"
                # the paths are as long as the project makes them, nested: counted in bytes as they are built (a
                # character can be four; the third re-review's S-4)
                n = len(path.encode("utf-8", errors="surrogatepass"))
                if n > MAX_PATH:
                    raise Failure("size", _("a new path is longer than {n} bytes", n=MAX_PATH), fix="project")
                path_bytes += n
                if path_bytes > MAX_PATH_BYTES:
                    raise Failure("size", _("the new trees' paths come to more than {mb} MB: push in parts",
                                            mb=MAX_PATH_BYTES >> 20), fix="project")
                entries.append((mode, path, oid))
                if len(entries) > MAX_ENTRIES:
                    raise Failure("size", _("the new trees hold more than {n} entries: push in parts", n=MAX_ENTRIES),
                                  fix="project")
                first.setdefault(oid, commit)
                if mode == "40000":
                    stack.append((oid, path + "/", commit, depth + 1))
        return entries, {p for m, p, _o in entries if m == "160000"}, first

    def commit_header(self, oid: str, key: str) -> list[str]:
        return [v for k, v in commit_headers(self.read([oid])[oid]) if k == key]


def commit_headers(raw: bytes) -> list[tuple[str, str]]:
    """A commit object's headers (continuation lines joined to theirs), up to the blank line before the message."""
    head = raw.split(b"\n\n", 1)[0].decode(errors="replace")
    out: list[tuple[str, str]] = []
    for line in head.split("\n"):
        if line.startswith(" ") and out:
            out[-1] = (out[-1][0], out[-1][1] + "\n" + line[1:])
        else:
            k, _sp, v = line.partition(" ")
            out.append((k, v))
    return out


def ident_email(value: str) -> str:
    m = re.search(r"<([^<>]*)>", value)
    return m.group(1) if m else ""


def short(oid: str) -> str:
    return oid[:12]


# ---------- the checks ----------

def objects_check(objs: Objects, entries, gitlinks, deadline: float | None = None) -> list[Finding]:
    """Commit headers, gitlinks and Git LFS. Every object it reads is read in one batch (one git process, not one per
    object: the third re-review's S-3), and it stops at the deadline."""
    import time
    out = []
    paths = blob_paths(entries)
    lfs = [o for o in objs.blobs if objs.sizes.get(o, 0) < 1024
           or any(p.rsplit("/", 1)[-1] == ".gitattributes" for p in paths.get(o, ()))]
    objs.read([*objs.commits, *lfs])
    if deadline is not None and time.monotonic() > deadline:
        raise Failure("secrets", _("the scanner failed: it ran past its time limit"))
    for c in objs.commits:
        raw = objs.raw[c]
        for k, v in commit_headers(raw):
            if k in HEADERS:
                continue
            if k == "encoding" and v.strip().lower() in ("utf-8", "utf8"):
                continue
            out.append(Finding("objects", _("commit {sha} carries a {header} header (a signature, mergetag or another "
                                            "encoding): the platform pushes plain commits only; recreate it without one",
                                            sha=short(c), header=k[:40])))
            break
    for p in sorted(gitlinks):
        out.append(Finding("objects", _("{path} is a gitlink (a submodule): the platform pushes no submodules",
                                        path=p[:200])))
    for oid in lfs:
        data = objs.raw[oid]
        for p in sorted(paths.get(oid, ())):
            if objs.sizes.get(oid, 0) < 1024 and data.startswith(LFS):
                out.append(Finding("objects", _("{path} is a Git LFS pointer: LFS objects don't travel with the platform's "
                                                "push", path=p[:200])))
            elif p.rsplit("/", 1)[-1] == ".gitattributes" and b"filter=lfs" in data:
                out.append(Finding("objects", _("{path} turns on Git LFS (filter=lfs): LFS objects don't travel with the "
                                                "platform's push", path=p[:200])))
    return out


def blob_paths(entries) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for mode, path, oid in entries:
        if mode not in ("40000", "160000"):
            out.setdefault(oid, set()).add(path)
    return out


def size_check(objs: Objects, limits: dict) -> list[Finding]:
    out = []
    if len(objs.commits) > limits["max_commits"]:
        out.append(Finding("size", _("{n} new commits, over the limit of {max}: push in parts", n=len(objs.commits),
                                     max=limits["max_commits"])))
    for c in objs.commits:
        if objs.sizes[c] > COMMIT_CAP:
            out.append(Finding("size", _("commit {sha} is over {mb} MB as an object", sha=short(c), mb=COMMIT_CAP >> 20)))
    file_cap = limits["max_file_mb"] << 20
    for b in objs.blobs:
        if objs.sizes[b] > file_cap:
            out.append(Finding("size", _("a new file of {mb} MB (object {sha}) is over the limit of {max} MB",
                                         mb=objs.sizes[b] >> 20, sha=short(b), max=limits["max_file_mb"])))
    total = sum(objs.sizes.values())
    if total > limits["max_total_mb"] << 20:
        out.append(Finding("size", _("the new objects come to {mb} MB, over the limit of {max} MB",
                                     mb=total >> 20, max=limits["max_total_mb"])))
    return out


def ci_check(git, new: str, default_tip: str | None, allowed: bool) -> list[Finding]:
    if allowed:
        return []
    out = []
    for name in CI_ENTRIES:
        mine = git("ls-tree", new, "--", name).decode(errors="replace").split("\t")[0].strip()
        theirs = git("ls-tree", default_tip, "--", name).decode(errors="replace").split("\t")[0].strip() \
            if default_tip else ""
        if mine != theirs:
            folder = " tree " in f" {mine} {theirs} "
            out.append(Finding("ci", _("{sha} changes {entry} against the remote's default branch: CI would run it with "
                                       "the repo's secrets; only the owner allows that (enforce.git_ci)",
                                       sha=short(new), entry=name + ("/" if folder else ""))))
    return out


def identity_check(objs: Objects, gp: dict, public: bool) -> list[Finding]:
    emails = set(gp.get("emails") or [])
    if not emails:
        return [Finding("identity", _("the platform has no list of the emails this project commits with"), fix="owner",
                        cause="emails")]
    shown = set(gp.get("public_emails") or [])
    out = []
    for c in objs.commits:
        for role in ("author", "committer"):
            for v in objs.commit_header(c, role):
                e = ident_email(v)
                if e not in emails:
                    out.append(Finding("identity", _("commit {sha}'s {role} email is not one this project pushes with: "
                                                     "re-author the unpushed commits", sha=short(c), role=role)))
                elif public and not (NOREPLY.search(e) or e in shown):
                    out.append(Finding("identity", _("commit {sha}'s {role} email would be published on a public remote: "
                                                     "re-author the unpushed commits with a noreply address",
                                                     sha=short(c), role=role)))
    return dedupe(out)


def dedupe(found: list[Finding]) -> list[Finding]:
    return list(dict.fromkeys(found))


def variants(data: bytes) -> list[str]:
    """The texts a blob is scanned as (r2 S-d): UTF-8 with errors replaced, whatever its bytes; UTF-16 when it has a byte
    order mark or alternating NULs; with its NULs stripped. gitleaks:allow neutralised in each (B2)."""
    data = data.replace(b"gitleaks:allow", NEUTRAL)
    out = [data.decode("utf-8", errors="replace")]
    head = data[:4096]
    enc = None
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
    elif len(head) >= 8 and head[1::2].count(0) > len(head[1::2]) * 0.4:
        enc = "utf-16-le"
    elif len(head) >= 8 and head[0::2].count(0) > len(head[0::2]) * 0.4:
        enc = "utf-16-be"
    if enc:
        out.append(data.decode(enc, errors="replace").replace("gitleaks:allow", NEUTRAL.decode()))
    if b"\0" in data:
        out.append(data.replace(b"\0", b"").decode("utf-8", errors="replace"))
    return out


def rule_hits(texts: list[str]) -> list[str]:
    return sorted({name for t in texts for name, rx in RULES if rx.search(t)})


def filename_hits(path: str) -> list[str]:
    base = path.rsplit("/", 1)[-1]
    return [name for name, rx, unless in FILENAME_RULES if rx.search(base) and not (unless and unless.search(base))]


# ---------- the project's declared secret values (r2 S-e) ----------

def declared_file(project: str) -> pathlib.Path:
    """<secrets>/push-declared/<project>.jsonl: what the platform has seen the project declare, kept where every sandbox
    is denied (policy.secret_dirs), owner-only (0600 in a 0700 folder). Append-only: one line per value
    ({"v": value, "name"}) or, for a value longer than MAX_VALUE, its hash ({"h": sha256, "name"}), matched against a
    whole new file. Never rewritten, never shrunk (review B1 and its re-review)."""
    return hubconf.secrets_dir() / "push-declared" / f"{project}.jsonl"


MAX_VALUE = 512          # bytes: a declared value is a short secret; a longer one is matched as a whole file
MAX_VALUES = 4096        # values and hashes in one project's store: past it the push is refused for the owner
STORE_CAP = 8 << 20      # the store read whole, or the push refused


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _load(project: str) -> list[dict]:
    """The whole store, or Failure (fail closed: a store that can't be read whole never lets a push through, and is
    never rewritten from a partial read). Split on b"\n" only: the records are written ASCII-only, so no character a
    name holds (U+2028, U+0085…) can split one (the third re-review's B-2)."""
    f = declared_file(project)
    try:
        raw = nofollow.read(f.parent, f.name, STORE_CAP) if f.parent.is_dir() else None
    except OSError as e:
        raise Failure("secrets", _("the platform's record of this project's declared secrets can't be read whole "
                                   "({why})", why=e.strerror or type(e).__name__), cause="declared-store") from None
    out = []
    for line in (raw or b"").split(b"\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue   # a line cut by a crash mid-append: the ones before and after it stand
        if isinstance(rec, dict) and (isinstance(rec.get("v"), str) or isinstance(rec.get("h"), str)):
            out.append(rec)
    return out


def dotenv_values(text: str) -> dict[str, str]:
    """KEY=value pairs as dotenv reads them: `export` and whitespace before a key, a single-quoted value literal, a
    double-quoted one with its escapes (\\" \\\\ \\n) and across lines, an unquoted one up to whitespace before #."""
    out, i, n = {}, 0, len(text)
    line_rx = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*=[ \t]*")
    while i < n:
        m = line_rx.match(text, i)
        end = text.find("\n", i)
        end = n if end < 0 else end
        if not m or m.end() > end:
            i = end + 1
            continue
        key, j = m.group(1), m.end()
        q = text[j:j + 1]
        if q == "'":
            close = text.find("'", j + 1)
            close = n if close < 0 else close
            out[key], i = text[j + 1:close], text.find("\n", close)
        elif q == '"':
            k, val = j + 1, []
            while k < n and text[k] != '"':
                if text[k] == "\\" and k + 1 < n:
                    val.append({"n": "\n", "t": "\t", "r": "\r"}.get(text[k + 1], text[k + 1]))
                    k += 2
                else:
                    val.append(text[k])
                    k += 1
            out[key], i = "".join(val), text.find("\n", k)
        else:
            out[key], i = re.split(r"[ \t]+#", text[j:end], maxsplit=1)[0].strip(), end
        i = n if i < 0 else i + 1
    return out


def _values(raw: bytes, env_key: str | None) -> list[str]:
    text = _nfc(raw.decode("utf-8", errors="replace"))
    if env_key:
        v = dotenv_values(text).get(env_key)
        return [v] if v and len(v) >= MIN_SECRET else []
    whole = text.strip()
    if whole and "\n" not in whole:
        return [whole] if len(whole) >= MIN_SECRET else []
    # a file of many lines: its long mixed tokens (a key in a JSON file), never its ordinary words
    return [t for t in TOKEN.findall(text) if len(t) >= 20 and re.search(r"[A-Za-z]", t) and re.search(r"\d", t)]


def _head(v: str, n: int) -> str:
    return v.encode("utf-8")[:n].decode("utf-8", errors="ignore")


def _tail(v: str, n: int) -> str:
    return v.encode("utf-8")[-n:].decode("utf-8", errors="ignore")


def _records(v: str, name: str) -> list[dict]:
    """What the store keeps of one value: itself up to MAX_VALUE bytes; a longer one as its hash (a whole file or a long
    word) and its first and last MAX_VALUE bytes as plain values, so it is found inside a longer word too (KEY=…, ?k=…;
    the third re-review's S-2)."""
    if len(v.encode("utf-8")) <= MAX_VALUE:
        return [{"v": v, "name": name}]
    return [{"h": _sha(v), "name": name}, {"v": _head(v, MAX_VALUE), "name": name},
            {"v": _tail(v, MAX_VALUE), "name": name}]


def remember_declared(project: str, doc: dict, log=None) -> None:
    """Add the project's declared secret values as they are now to what the platform has seen (never dropped when a
    declaration goes: the project can change secrets:, so it can't turn the check off; r2 S-e). Only entries without kind
    whose path is relative, read with nofollow from its own workspace or runtime folder: no link followed, regular
    single-link files only, at most SECRET_CAP. Anything else is skipped, said only to the owner's log (S3: a refusal
    must not tell the project anything about files it can't read). At most MAX_VALUES records in all, past which the push
    is refused for the owner (never one dropped). Each record one ASCII-only line (ensure_ascii), after a newline when the
    file doesn't end with one (a crash mid-append costs only its own line)."""
    import registry
    d = registry.dirs({**doc, "name": project})
    bases = [b for b in (d.get("workspace"), d.get("runtime")) if b]
    have = _load(project)
    seen = {r.get("v") for r in have} | {r.get("h") for r in have}
    new = []
    for sec in doc.get("secrets") or []:
        if not isinstance(sec, dict) or sec.get("kind"):
            continue
        rel, name = str(sec.get("path") or ""), str(sec.get("name") or sec.get("path") or "?")[:80]
        if not rel or rel.startswith(("/", "~")) or ".." in pathlib.PurePosixPath(rel).parts:
            if log:
                log(f"{project}: declared secret {name}: path {rel!r} is not relative to the project's folder; skipped")
            continue
        for base in bases:
            try:
                raw = nofollow.read(base, rel, SECRET_CAP, single=True)
            except OSError as e:
                if log:
                    log(f"{project}: declared secret {name} in {base}: {e}; skipped")
                continue
            if raw is None:
                continue
            for v in _values(raw, sec.get("env_key")):
                for rec in _records(v, name):
                    key = rec.get("v") or rec.get("h")
                    if key not in seen and (rec.get("h") or len(key) >= MIN_SECRET):
                        seen.add(key)
                        new.append(rec)
    if not new:
        return
    if len(have) + len(new) > MAX_VALUES:
        raise Failure("secrets", _("this project has declared more than {n} secret values; the owner looks at its "
                                   "secrets: entries", n=MAX_VALUES), cause="declared-store")
    f = declared_file(project)
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(f.parent, 0o700)
    fd = os.open(f, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as fh:
        size = os.fstat(fd).st_size
        lead = b""
        if size:
            fh.seek(size - 1)
            lead = b"" if fh.read(1) == b"\n" else b"\n"
        fh.write(lead + b"".join(json.dumps({**rec, "at": datetime.date.today().isoformat()}).encode() + b"\n"
                                 for rec in new))


def _trie_pattern(keys: list[str]) -> str:
    """One regex for many literals of one length, factored as a trie (as deep as the keys are long): a position that
    starts no key is left after a character or two."""
    trie: dict = {}
    for k in keys:
        node = trie
        for ch in k:
            node = node.setdefault(ch, {})

    def emit(node: dict) -> str:
        alts = [re.escape(ch) + emit(sub) for ch, sub in sorted(node.items())]
        return "" if not alts else alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
    return emit(trie)


class Declared:
    """Every value the platform has seen the project declare: a short one matched as a substring of a text, a long one
    by the hash of a whole new file or a long word in one (and its first and last bytes as short ones). → the declared
    names found, never the values.
    The match can't fail on what the values are (the third re-review's S-1: a regex of them nests as deep as they share
    prefixes): each value is keyed by its first KEY characters, one shallow regex finds where any key starts
    (overlapping, by a lookahead), and each place is checked against the values with that key. Chunks check the
    deadline."""
    CHUNK = 1 << 20
    KEY = MIN_SECRET

    def __init__(self, project: str):
        recs = _load(project)
        self.names = {_nfc(r["v"]): r.get("name", "?") for r in recs if r.get("v") and len(r["v"]) >= self.KEY}
        self.hashes = {r["h"]: r.get("name", "?") for r in recs if r.get("h")}
        self.by_key: dict[str, list[str]] = {}
        for v in self.names:
            self.by_key.setdefault(v[:self.KEY], []).append(v)
        self.longest = max((len(v) for v in self.names), default=0)
        # the keys as a trie (as deep as KEY, never as deep as a value): shared prefixes leave a position fast
        self.rx = re.compile("(?=(" + _trie_pattern(sorted(self.by_key)) + "))") \
            if self.by_key else None

    def hits(self, texts: list[str], deadline: float | None = None) -> list[str]:
        import time
        out = set()
        for t in texts:
            t = _nfc(t)
            if self.hashes:   # a long value: the whole text, or a long word in it
                cands = {t, t.strip(), *LONG_TOKEN.findall(t)}
                for h in {_sha(c) for c in cands} & set(self.hashes):
                    out.add(self.hashes[h])
            if not self.rx:
                continue
            step = self.CHUNK
            for i in range(0, max(len(t), 1), step):
                if deadline is not None and time.monotonic() > deadline:
                    raise Failure("secrets", _("the scanner failed: it ran past its time limit"))
                for m in self.rx.finditer(t, i, min(len(t), i + step + self.KEY - 1)):
                    pos = m.start()
                    for v in self.by_key[m.group(1)]:
                        if t.startswith(v, pos):
                            out.add(self.names[v])
        return sorted(out)


# ---------- gitleaks ----------

def gitleaks() -> str | None:
    return shutil.which("gitleaks")


def gitleaks_version(exe: str) -> tuple[int, ...] | None:
    try:
        r = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=30, env=scan_env(None),
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", r.stdout + r.stderr)
    return tuple(int(x) for x in m.groups() if x is not None) if m and r.returncode == 0 else None


def scan_env(home) -> dict[str, str]:
    """An environment from scratch for the scanner: no GITLEAKS_CONFIG, nothing of the owner's but PATH."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home or "/nonexistent"), "LC_ALL": "C"}


def gitleaks_scan(exe: str, items: list[tuple[str, str]], deadline: float) -> list[tuple[str, str]]:
    """(label, rule) for each finding over items [(label, text)], joined in chunks with a header line per item that maps
    a finding's line back. Raises Failure for anything but a clean run or findings."""
    import time
    found: list[tuple[str, str]] = []
    work = pathlib.Path(tempfile.mkdtemp(prefix="hub-scan-", dir=hubconf.private_tmp()))
    try:
        ignore = work / "ignore"
        ignore.write_text("")
        cwd = work / "cwd"
        cwd.mkdir()
        chunks, cur, size = [], [], 0
        for label, text in items:
            if size + len(text) > CHUNK and cur:
                chunks.append(cur)
                cur, size = [], 0
            cur.append((label, text))
            size += len(text)
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            lines, starts = [], []
            for label, text in chunk:
                starts.append((len(lines) + 1, label))
                lines.append(f"==== {label} ====")
                lines.extend(text.split("\n"))
            report = work / "report.json"
            report.unlink(missing_ok=True)
            left = deadline - time.monotonic()
            if left <= 0:
                raise Failure("secrets", _("the scanner failed: it ran past its time limit"))
            try:
                r = subprocess.run([exe, "stdin", "--redact", "--no-banner", "--exit-code", "1", "--report-format",
                                    "json", "--report-path", str(report), "--gitleaks-ignore-path", str(ignore)],
                                   input="\n".join(lines).encode(), cwd=cwd, env=scan_env(work), capture_output=True,
                                   timeout=left)
            except subprocess.TimeoutExpired:
                raise Failure("secrets", _("the scanner failed: it ran past its time limit")) from None
            except OSError as e:
                raise Failure("secrets", _("the scanner failed: {why}", why=type(e).__name__)) from None
            if r.returncode not in (0, 1):
                raise Failure("secrets", _("the scanner failed (gitleaks exit {code})", code=r.returncode))
            if r.returncode == 1:
                try:
                    rep = json.loads(report.read_text() or "[]")
                except (OSError, ValueError):
                    raise Failure("secrets", _("the scanner failed: its report can't be read")) from None
                if not rep:
                    raise Failure("secrets", _("the scanner failed: it reported findings it didn't list"))
                for f in rep:
                    line = int(f.get("StartLine") or 0)
                    label = next((lab for start, lab in reversed(starts) if start <= line), "?")
                    found.append((label, str(f.get("RuleID") or "gitleaks")[:60]))
        return found
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------- names on a public remote (check 8) ----------

def owner_paths() -> tuple[list[str], str]:
    home = str(pathlib.Path.home())
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or pathlib.Path(home).name
    out = [home] + [f"/Users/{user}", f"/home/{user}"]
    return [p for p in dict.fromkeys(out) if len(p) > 2], user


def name_patterns(project: str) -> list:
    """[(name, pattern)] of the instance's private names list but the project's own (empty when the deployment has
    none); a list that is named but can't be read refuses."""
    import private_names
    try:
        f = private_names.names_file()
        text = f.read_text() if f and f.exists() else ""
    except (private_names.ListError, OSError):
        raise Failure("names", _("the private names list is named but can't be read")) from None
    return [(n, pat) for n, pat in private_names.patterns(text) if n.lower() != project.lower()]


# ---------- all of them ----------

def run(git, *, project: str, doc: dict, new: str, tips: list[str], default_tip: str | None, public: bool,
        limits: dict, deadline: float, log=None, owner_form: bool = False) -> list[Finding]:
    """Checks 4, 9, 5, 6, 7, 8 in that order (1–3 are git_remote's) → every finding, in order. A size finding stops
    before the scans (r1 S9). owner_form: no emails list is a note for the owner's look, not a refusal (the owner form
    worked before the field existed)."""
    enf = doc.get("enforce") or {}
    gp = enf.get("git_push") or {}
    objs = Objects(git, new, tips)
    # sizes first (they are known from batch-check alone): what the walk reads is bounded by them
    size = size_check(objs, {**limits, "max_file_mb": int(gp.get("max_file_mb") or 10)})
    if size:
        return size
    entries, gitlinks, first = objs.walk(deadline)
    found = objects_check(objs, entries, gitlinks, deadline)
    found += ci_check(git, new, default_tip, enf.get("git_ci") is True)
    if not (owner_form and not gp.get("emails")):
        found += identity_check(objs, gp, public)
    found += scan(objs, entries, first, project=project, doc=doc, public=public, deadline=deadline, log=log)
    return dedupe(found)


def scan(objs: Objects, entries, first, *, project, doc, public, deadline, log=None) -> list[Finding]:
    """Checks 7 and 8 over the new commits whole, the new trees' paths and the new blobs."""
    import time
    remember_declared(project, doc, log)
    declared = Declared(project)
    out: list[Finding] = []
    paths = blob_paths(entries)
    homes, user = owner_paths() if public else ([], "")
    private = name_patterns(project) if public else []
    items: list[tuple[str, str]] = []

    def where_blob(oid):
        ps = sorted(paths.get(oid, ()))
        return _("{path} in commit {sha}", path=(ps[0] if ps else short(oid))[:200], sha=short(first.get(oid, "?")))

    def look(label: str, texts: list[str]):
        for rule in rule_hits(texts):
            out.append(Finding("secrets", _("{where}: a secret ({rule}); rewrite the unpushed commits so it never "
                                            "appears", where=label, rule=rule)))
        for name in declared.hits(texts, deadline):
            out.append(Finding("secrets", _("{where}: the value of this project's secret {name}; rewrite the unpushed "
                                            "commits so it never appears", where=label, name=name)))
        if public:
            for t in texts:
                # which name only in the owner's record (review S3): the project is told that one is there
                for n in sorted({n for n, pat in private if pat.search(t)}):
                    # neither the name nor where it is (the re-review's 6): the owner's to-do has both
                    out.append(Finding("names", _("a new commit's message, file or path names a private project and the "
                                                  "remote is public; the owner has the details"),
                                       fix="owner", cause="private-name", detail=f"{n} ({label})"))
                if any(h in t for h in homes):
                    out.append(Finding("names", _("{where} holds the owner's home path and the remote is public",
                                                  where=label)))
    for c in objs.commits:
        text = objs.read([c])[c].replace(b"gitleaks:allow", NEUTRAL).decode("utf-8", errors="replace")
        label = _("commit {sha} (message)", sha=short(c))
        look(label, [text])
        items.append((label, text))
    for mode, path, oid in entries:
        for rule in filename_hits(path) if mode not in ("40000", "160000") else []:
            out.append(Finding("secrets", _("{path} in commit {sha}: a file of a kind that holds secrets ({rule}); "
                                            "rewrite the unpushed commits so it never appears", path=path[:200],
                                            sha=short(first.get(oid, "?")), rule=rule)))
    # the paths in chunks of about a megabyte, never one string of them all (the re-review's B2)
    chunk, size = [], 0
    for _m, path, _o in entries:
        chunk.append(path)
        size += len(path) + 1
        if size >= 1 << 20:
            look(_("a path in the new trees"), ["\n".join(chunk).replace("gitleaks:allow", NEUTRAL.decode())])
            chunk, size = [], 0
    if chunk:
        look(_("a path in the new trees"), ["\n".join(chunk).replace("gitleaks:allow", NEUTRAL.decode())])
    if entries:
        if public and any(user and seg == user for _m, p, _o in entries for seg in p.split("/")):
            out.append(Finding("names", _("a path in the new trees has the owner's user name as a folder and the remote "
                                          "is public")))
    for oid, data in objs.blobs_read():
        if time.monotonic() > deadline:
            raise Failure("secrets", _("the scanner failed: it ran past its time limit"))
        texts = variants(data)
        label = where_blob(oid)
        look(label, texts)
        items += [(label, t) for t in texts]
    exe = gitleaks()
    if exe:
        v = gitleaks_version(exe)
        if v is None or v < GITLEAKS_MIN:
            raise Failure("secrets", _("gitleaks is installed but older than {min} (it has no stdin scan): the owner "
                                       "updates or removes it", min=".".join(map(str, GITLEAKS_MIN))), cause="gitleaks")
        for label, rule in gitleaks_scan(exe, items, deadline):
            out.append(Finding("secrets", _("{where}: a secret ({rule}); rewrite the unpushed commits so it never "
                                            "appears", where=label, rule=rule)))
    return out
