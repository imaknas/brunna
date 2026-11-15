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
                empty ignore file, an environment from scratch), and every secret value the platform has seen the project
                declare (declared(), salted hashes, never dropped: r2 S-e)
  names    (8)  on a public remote: no private project name (private_names) but its own, no owner's home path, no owner's
                user name as a path segment, in blobs, commits or any new tree's paths

What a check reports names the commit, the path or "message", and the rule; never the value.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import tempfile
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


class Finding(NamedTuple):
    check: str            # objects | size | ci | identity | secrets | names
    reason: str           # for the project and the owner: the commit, the path or "message", the rule; never a value
    fix: str = "project"  # project | owner | wait
    cause: str = ""       # the owner to-do's cause when fix is owner


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
        out = git("rev-list", "--objects", new, "--not", *tips).decode(errors="replace")
        oids = [line.split(" ", 1)[0] for line in out.splitlines() if line.strip()]
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

    def walk(self) -> tuple[list[tuple[str, str, str]], set[str], dict[str, str]]:
        """Every entry of every new tree, under each new commit's root: ([(mode, full path, oid)], the gitlinks' paths,
        {oid: the first new commit that brings it}). A tree the remote already has is not walked (its entries' names are
        there already)."""
        new_trees, entries, seen, first = set(self.trees), [], set(), {}

        def go(tree: str, prefix: str, commit: str):
            if tree not in new_trees or (tree, prefix) in seen:
                return
            seen.add((tree, prefix))
            for mode, name, oid in self.tree_entries(tree):
                path = f"{prefix}{name}"
                entries.append((mode, path, oid))
                first.setdefault(oid, commit)
                if mode == "40000":
                    go(oid, path + "/", commit)
        for c in self.commits:
            go(self.commit_header(c, "tree")[0], "", c)
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

def objects_check(objs: Objects, entries, gitlinks) -> list[Finding]:
    out = []
    for c in objs.commits:
        raw = objs.read([c])[c]
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
    paths = blob_paths(entries)
    for oid in objs.blobs:
        for p in sorted(paths.get(oid, ())):
            if objs.sizes.get(oid, 0) < 1024 and objs.read([oid])[oid].startswith(LFS):
                out.append(Finding("objects", _("{path} is a Git LFS pointer: LFS objects don't travel with the platform's "
                                                "push", path=p[:200])))
            elif p.rsplit("/", 1)[-1] == ".gitattributes" and b"filter=lfs" in objs.read([oid])[oid]:
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
    import policy
    return policy.git_broker_dir(project) / "declared.json"


def _salt() -> bytes:
    import policy
    f = policy.git_broker_dir(".declared-salt")
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return bytes.fromhex(f.read_text().strip())
    except (OSError, ValueError):
        salt = _secrets.token_bytes(16)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(salt.hex())
        return salt


def _h(salt: bytes, value: str) -> str:
    return hashlib.sha256(salt + value.encode()).hexdigest()


def _values(raw: bytes, env_key: str | None) -> list[str]:
    text = raw.decode("utf-8", errors="replace")
    if env_key:
        out = []
        for line in text.splitlines():
            k, sep, v = line.strip().removeprefix("export ").partition("=")
            if sep and k.strip() == env_key:
                out.append(v.strip().strip("'\""))
        return [v for v in out if len(v) >= MIN_SECRET]
    whole = text.strip()
    if whole and "\n" not in whole:
        return [whole] if len(whole) >= MIN_SECRET else []
    # a file of many lines: its long mixed tokens (a key in a JSON file), never its ordinary words
    return [t for t in TOKEN.findall(text) if len(t) >= 20 and re.search(r"[A-Za-z]", t) and re.search(r"\d", t)]


def remember_declared(project: str, doc: dict, log=None) -> None:
    """Read the project's declared secret values as they are now and add their salted hashes to what the platform has seen
    (never dropped when a declaration goes: the project can change secrets:, so it can't turn the check off). Only
    entries without kind whose path is relative, read with nofollow from its own workspace or runtime folder: no link
    followed, regular single-link files only, at most SECRET_CAP. Anything else is skipped, said only to the owner's log
    (S3: a refusal must not tell the project anything about files it can't read)."""
    import registry
    d = registry.dirs({**doc, "name": project})
    bases = [b for b in (d.get("workspace"), d.get("runtime")) if b]
    f = declared_file(project)
    try:
        seen = json.loads(f.read_text())
    except (OSError, ValueError):
        seen = {}
    salt, changed = _salt(), False
    for sec in doc.get("secrets") or []:
        if not isinstance(sec, dict) or sec.get("kind"):
            continue
        rel, name = str(sec.get("path") or ""), str(sec.get("name") or sec.get("path") or "?")
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
                h = _h(salt, v)
                if h not in seen:
                    seen[h] = {"name": name[:80], "len": len(v), "at": datetime.date.today().isoformat()}
                    changed = True
    if changed:
        f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(seen, fh)
        os.replace(tmp, f)


def declared_hits(project: str, texts: list[str]) -> list[str]:
    """The names of declared values that appear in these texts (as a token, or the part of one after = or :)."""
    try:
        seen = json.loads(declared_file(project).read_text())
    except (OSError, ValueError):
        return []
    if not seen:
        return []
    salt, lengths, out = _salt(), {v["len"] for v in seen.values()}, set()
    for t in texts:
        for tok in TOKEN.findall(t):
            cands = {tok, *re.split(r"[=:]", tok)[1:], tok.split("=", 1)[-1], tok.split(":", 1)[-1]}
            for c in cands:
                if len(c) in lengths and (h := _h(salt, c)) in seen:
                    out.add(seen[h]["name"])
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
    entries, gitlinks, first = objs.walk()
    found = objects_check(objs, entries, gitlinks)
    size = size_check(objs, {**limits, "max_file_mb": int(gp.get("max_file_mb") or 10)})
    if size:
        return found + size
    found += ci_check(git, new, default_tip, enf.get("git_ci") is True)
    if not (owner_form and not gp.get("emails")):
        found += identity_check(objs, gp, public)
    found += scan(objs, entries, first, project=project, doc=doc, public=public, deadline=deadline, log=log)
    return dedupe(found)


def scan(objs: Objects, entries, first, *, project, doc, public, deadline, log=None) -> list[Finding]:
    """Checks 7 and 8 over the new commits whole, the new trees' paths and the new blobs."""
    import time
    remember_declared(project, doc, log)
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
        for name in declared_hits(project, texts):
            out.append(Finding("secrets", _("{where}: the value of this project's secret {name}; rewrite the unpushed "
                                            "commits so it never appears", where=label, name=name)))
        if public:
            for t in texts:
                for n in sorted({n for n, pat in private if pat.search(t)}):
                    out.append(Finding("names", _("{where} names a private project ({name}) and the remote is public",
                                                  where=label, name=n)))
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
    allpaths = "\n".join(p for _m, p, _o in entries)
    if allpaths:
        look(_("a path in the new trees"), [allpaths.replace("gitleaks:allow", NEUTRAL.decode())])
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
