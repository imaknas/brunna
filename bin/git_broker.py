"""The git broker's library (docs/design/git-broker.md; "Step 2 build list", slice a): for a project whose git goes through
the platform (the registry's enforce.git: platform), the one way a sandboxed process of it writes its git dirs. The
service is bin/git-broker.py (started only by tests in step 2: no service is declared, open point 12); the caller is
the platform's `git` (bin/git_wrapper.py) or `brunna git`.

The channel (slice a): two Unix sockets per project in <state>/git-broker/<project>/, `session` and `service` (r3
S-d); a socket path over SOCKET_MAX bytes (macOS's limit) is bound and reached by its name from its folder
(unix_bind, unix_connect), and the sandbox's rule still matches its full path (measured). Per connection, frames of
<kind: 1 byte><length: 4 bytes, big endian><payload>:
  N  broker → caller   {"nonce", "dir"}: the caller proves the project by creating <dir>/<nonce> (dir is the project's
                       <workspace>/.hub-run/git/), then sends
  P  caller → broker   (empty) the file is there; the broker opens the workspace, .hub-run and git through no-follow
                       directory descriptors (never blocking: a FIFO or a link there is refused), checks for a regular
                       file, removes it, and only then reads
  R  caller → broker   {"argv", "cwd", "env"}: the env is what git_allow.relevant_env keeps
  I  caller → broker   stdin, in chunks; an empty one is the end
  O / E                stdout / stderr, as git writes them
  X  broker → caller   {"code"}: the last frame
An answer where nothing runs is E then X with code 2.

Whom it serves (each refusal runs nothing; the reasons are the caller's message):
  - a project switched on (policy.git_platform), on its own sockets; "git through the platform isn't on for this
    project" otherwise;
  - not a project whose nonce folder another sandboxed project's write roots hold, nor one sharing a root with another
    (r2 S6, r3 S-h, r1 S6: that project could prove it);
  - a request whose working folder and every -C resolve inside the project's own folders (the deepest root claims a
    folder, as test-runner.claimant decides); a cwd outside them (a Codex app worktree) is told to use a worktree inside
    the project (r3 question 4, decided);
  - in a repo whose common dir the project owns: recorded in <state>/git-broker/<project>/repos.json (real path, device
    and inode of its .git), as make_repo records it; the workspace's own repo without a record (or with another device
    or inode) is "switched on, not rebuilt" (open point 7); a linked worktree only when its pointers agree
    (gitdirs.confirmed).
What it runs in slice a (op from git_allow): whole under P1, the git-dir writer (seatbelt.git_writer_profile: the
request's git dir writable minus the names git runs or reads elsewhere, the project's own read rules, no network, the
exec guard), with the worktree read-only to it: add, rm --cached, commit, hash-object -w, apply --cached, notes
add|append -m, gc; the config op (config set/unset, remote add/set-url/remove: the value rules of
git_fence.check_key narrowed by r2 S3, only the registry's own URL, config.lock taken first, the edit made on a copy
in the private temp folder, installed by a rename). Every other forwarded subcommand answers "not built yet".
B2's check here (r1 S8): no tracked path whose last component folds to HEAD, in the index a commit is written from
(read through the git dir's descriptor before git commits) and in the index apply --cached would leave (P1 applies to
a copy in the git dir under the broker's index.lock; the copy is read and checked, and only then renamed into place).
Every git it runs: built from scratch (no variable of the broker's or the request's but the allowlisted ones), with
GIT_CONFIG_GLOBAL its own file (in the shared platform state, which P1 reads as the project does: policy read),
GIT_CONFIG_NOSYSTEM, no replace objects, no prompt, `:` as the editors, abbreviations off, and -c core.hooksPath=<an
empty platform folder> -c core.fsmonitor=false -c transfer.fsckObjects=true on its command line (N1, N3).
Limits (each from brunna.json git_broker.*):
  - a timeout per call (timeout_s; long_timeout_s for gc) stops the process group: SIGTERM, so git removes its lock
    files, then SIGKILL after STOP_GRACE_S; a message on stdin (-F -) is read whole, capped, before git starts (git
    commit would hold index.lock while it waits for it);
  - before its proof, a connection has handshake_s for the whole handshake (not per read); at most
    unproved_per_socket such connections per socket and unproved_total in all, the rest turned away with the reason;
  - at most concurrency calls per project at once, queue more waiting, the next refused with the reason;
  - out of descriptors, accept backs off; the service raises its open-file limit (raise_nofile);
  - <state>/git-broker/log.jsonl (project, socket, subcommand, the path taken, exit code, duration, never an argument's
    value), moved to log.jsonl.1 past LOG_MAX.
Until every sandboxed project's profiles deny the broker's folder (step 6, before the service is declared), any
sandboxed project's process can connect to these sockets; the bounds above keep that from exhausting the service.
Every index the broker checks also refuses a gitlink (no submodules, the owner's decision); overlap() also refuses a
project whose recorded repo another sandboxed project's folders hold, or whose workspace is in the shared temp area.
macOS only: on an adapter without git-dirs-fenced (Linux's Landlock) it refuses to start, and answers nothing.

  Broker(state, shared, projects, policy_for, tmp)   .start() binds; .serve(stop) accepts until stop is set
  make_repo(project, path) → Path                     (Broker method) a repo made for the project, recorded
  rebuilt(project, state) → str | None                when the project's workspace repo was recorded (doctor)
"""
from __future__ import annotations

import contextlib
import datetime
import errno
import json
import os
import pathlib
import re
import secrets
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gitdirs  # noqa: E402
import git_allow  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import names  # noqa: E402

_ = i18n.domain(__file__)

SOCKET_MAX = 103            # macOS: sun_path is 104 bytes, the last one its NUL
FRAME_MAX = 8 << 20
NOT_RUN = 2
TIMED_OUT = 124
CONFIG_CAP = 1 << 20
INDEX_CAP = 256 << 20
MESSAGE_CAP = 1 << 20      # a message given on stdin (-F -), read whole before git starts
LOG_MAX = 4 << 20          # log.jsonl beyond this moves to log.jsonl.1 (one kept)
STOP_GRACE_S = 2           # at a timeout: SIGTERM to the group, then this long before SIGKILL
HANDSHAKE_FRAME_MAX = 1 << 20
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
DIRF = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
PRESETS = (("branch.autoSetupMerge", "false"),)   # what git would otherwise write itself on a checkout (the design)
IGNORABLE = {0x200C, 0x200D, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF}
BUILT = {"add", "rm-cached", "commit", "hash-object", "apply-cached", "notes-add", "gc", "config-set", "config-unset",
         "remote-add", "remote-set-url", "remote-remove"}


class Answer(Exception):
    """Nothing runs: the caller is told why (exit code, text)."""

    def __init__(self, text: str, code: int = NOT_RUN):
        super().__init__(text)
        self.code = code


def setting(key: str, default: int) -> int:
    return int(hubconf.get(f"git_broker.{key}", default))


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------- frames ----------

def send(sock: socket.socket, kind: bytes, data: bytes = b"") -> None:
    sock.sendall(kind + struct.pack(">I", len(data)) + data)


def _exact(sock: socket.socket, n: int, deadline: float | None = None) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("the handshake's time is up")
            sock.settimeout(left)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


_CWD = threading.Lock()


def _at(folder: str, act) -> None:
    """act() with the working folder at folder, put back after (a Unix socket's path is limited to SOCKET_MAX bytes,
    so a longer one is reached by its name from its folder; the sandbox's rule still matches the full path, measured)."""
    with _CWD:
        try:
            back = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            back = None
        here = os.getcwd() if back is None else None
        os.chdir(folder)
        try:
            act()
        finally:
            if back is not None:
                os.fchdir(back)
                os.close(back)
            else:
                os.chdir(here)


def unix_bind(path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    p = str(path)
    if len(os.fsencode(p)) <= SOCKET_MAX:
        s.bind(p)
    else:
        _at(os.path.dirname(p), lambda: s.bind(os.path.basename(p)))
    return s


def unix_connect(s: socket.socket, path) -> None:
    p = str(path)
    if len(os.fsencode(p)) <= SOCKET_MAX:
        s.connect(p)
    else:
        _at(os.path.dirname(p), lambda: s.connect(os.path.basename(p)))


def recv(sock: socket.socket, deadline: float | None = None, cap: int = FRAME_MAX) -> tuple[bytes, bytes] | None:
    """One frame; with a deadline, the whole frame must arrive by it (a caller dripping bytes gains no time)."""
    head = _exact(sock, 5, deadline)
    if head is None:
        return None
    n = struct.unpack(">I", head[1:])[0]
    if n > cap:
        raise ValueError("frame too large")
    data = _exact(sock, n, deadline) if n else b""
    return (head[:1], data) if data is not None else None


# ---------- paths ----------

def folds_to_head(path: bytes) -> bool:
    """A tracked path whose last component the volume would read as HEAD (case, and the characters git's own path check
    ignores, r2 B2)."""
    last = path.rstrip(b"/").rsplit(b"/", 1)[-1].decode("utf-8", errors="replace")
    name = "".join(ch for ch in last if ord(ch) not in IGNORABLE).casefold().rstrip(". ")
    return name == "head"


def real_of(fd: int) -> pathlib.Path:
    """The real path of an open folder (macOS F_GETPATH)."""
    import fcntl
    got = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    return pathlib.Path(got.split(b"\0", 1)[0].decode())


def open_below(base, parts, create: bool = False) -> int:
    """A folder below base, each step opened without following a link and without blocking (a FIFO there is refused
    at once); base itself is opened normally (its parent isn't the project's to change). create: missing folders are
    made, one at a time."""
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        for p in parts:
            try:
                nfd = os.open(p, DIRF, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(p, 0o755, dir_fd=fd)
                nfd = os.open(p, DIRF, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_at(dfd: int, name: str, cap: int) -> bytes | None:
    """A regular file in an open folder, not following a link, not blocking; None when it is missing."""
    try:
        f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(f).st_mode):
            raise OSError(errno.EINVAL, f"{name} is not a regular file")
        chunks, n = [], 0
        while chunk := os.read(f, 1 << 16):
            n += len(chunk)
            if n > cap:
                raise OSError(errno.EFBIG, f"{name} is over {cap} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(f)


def write_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
    os.fsync(fd)


def unlink_at(dfd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        pass


def project_roots(doc: dict) -> list[pathlib.Path]:
    """A project's own folders, resolved: workspace, runtime folder, allow_write (test-runner.roots)."""
    import registry
    d = registry.dirs(doc)
    extra = [hubconf.path(w) for w in doc.get("allow_write") or []]
    return [pathlib.Path(os.path.realpath(p)) for p in (d.get("workspace"), d.get("runtime"), *extra) if p]


def within(path: pathlib.Path, root: pathlib.Path) -> bool:
    return path == root or root in path.parents


def git_programs() -> tuple[str, list[str]]:
    """(the git binary the broker runs, what the exec guard lets run: that binary, the git on PATH it was found through,
    and git's exec-path folder), resolved now (the broker resolves them when it starts)."""
    import shutil
    wrapper = os.path.realpath(hubconf.ROOT / "bin" / "git-wrapper")
    path = os.pathsep.join(p for p in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
                           if p and os.path.realpath(p) != wrapper)
    found = shutil.which("git", path=path or "/usr/bin:/bin") or "/usr/bin/git"
    r = subprocess.run([found, "--exec-path"], capture_output=True, text=True, timeout=20,
                       env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
                       stdin=subprocess.DEVNULL)
    exec_path = os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    real = os.path.realpath(found)
    beside = os.path.realpath(os.path.join(exec_path, "..", "..", "bin", "git")) if exec_path else None
    binary = beside if beside and os.access(beside, os.X_OK) else real
    progs = list(dict.fromkeys([binary, real, *([exec_path + "/"] if exec_path else [])]))
    return binary, progs


def unsupported() -> str | None:
    """Why the broker can't run on this machine (an adapter without git-dirs-fenced: Landlock), else None."""
    import isolation
    return isolation.can_sandbox() or isolation.unmet({"git_fence": {"names": [".git"]}})


def default_policy(name: str, doc: dict, projects: dict) -> dict:
    import policy
    return policy.policy(name, doc, {"projects": projects})


def repos_file(state: pathlib.Path, project: str) -> pathlib.Path:
    import policy
    return policy.git_broker_dir(project, pathlib.Path(state)) / "repos.json"


def rebuilt(project: str, state: pathlib.Path | None = None) -> str | None:
    """When the project's workspace repo was recorded (made or rebuilt by the platform); None: not rebuilt."""
    try:
        recs = json.loads(repos_file(state or hubconf.STATE, project).read_text()).get("repos") or []
    except (OSError, ValueError):
        return None
    return next((r.get("at") for r in recs if r.get("workspace")), None)


class Broker:
    def __init__(self, state=None, shared=None, projects=None, policy_for=None, tmp=None):
        self.state = pathlib.Path(state or hubconf.STATE)
        self.root = self.state / "git-broker"
        self.etc = pathlib.Path(shared or hubconf.shared()) / "git-broker"
        self.projects = projects or hubconf.projects
        self.policy_for = policy_for or default_policy
        self.tmp = tmp or hubconf.private_tmp
        self.lock = threading.Lock()
        self.slots: dict[str, threading.BoundedSemaphore] = {}
        self.listeners: dict[socket.socket, tuple[str, str]] = {}
        self.git = None
        self.git_paths: list[str] = []
        self.threads: list[threading.Thread] = []
        self.unproved: dict[tuple[str, str], int] = {}   # connections still proving, per socket
        self.waiting: dict[str, int] = {}                  # calls waiting for a project's slot
        self.backoffs = 0                                  # accepts that ran out of descriptors

    # ---------- start ----------

    def start(self) -> None:
        if (why := unsupported()):
            raise Answer(_("git through the platform isn't supported on this system ({why}); nothing was run", why=why))
        self.git, self.git_paths = git_programs()
        self.prepare()
        for name, doc in sorted(self.projects().items()):
            if self._switched_on(name, doc):
                self.bind(name)

    def _switched_on(self, name: str, doc: dict | None) -> bool:
        import policy
        return bool(doc) and policy.git_platform(doc)

    def prepare(self) -> None:
        """Its own global config (the owner's name and email read from the owner's git, never written there), an empty
        hooks folder and an empty HOME, in the shared platform state, which P1 reads as the project does."""
        for d in (self.etc, self.etc / "hooks", self.etc / "home"):
            d.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        owner = {}
        for key in ("user.name", "user.email", "init.defaultBranch"):
            r = subprocess.run([self.git, "config", "--global", "--get", key], capture_output=True, text=True,
                               timeout=20, stdin=subprocess.DEVNULL,
                               env={k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "XDG_CONFIG_HOME",
                                                                                "GIT_CONFIG_GLOBAL")})
            if r.returncode == 0 and r.stdout.strip() and "\n" not in r.stdout.strip():
                owner[key] = r.stdout.strip()
        pairs = [*((k, owner[k]) for k in ("user.name", "user.email") if k in owner),
                 ("init.defaultBranch", owner.get("init.defaultBranch") or "main"),
                 ("safe.bareRepository", "explicit"), ("protocol.ext.allow", "never"), ("protocol.file.allow", "user"),
                 ("core.pager", "cat"), ("submodule.recurse", "false"), ("commit.gpgSign", "false"),
                 ("tag.gpgSign", "false"), ("gc.auto", "0")]
        new = self.etc / ".gitconfig.new"
        new.unlink(missing_ok=True)
        new.touch(mode=0o644)
        for k, v in pairs:
            self._git_config_file(new, [k, v])
        os.replace(new, self.etc / "gitconfig")
        self.default_branch = dict(pairs)["init.defaultBranch"]

    def _git_config_file(self, f: pathlib.Path, args: list[str]) -> subprocess.CompletedProcess:
        work = f.parent
        return subprocess.run([self.git, "config", "--file", str(f), *args], capture_output=True, text=True, timeout=20,
                              cwd=work, stdin=subprocess.DEVNULL, start_new_session=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(work), "GIT_CONFIG_NOSYSTEM": "1",
                                   "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"})

    def bind(self, project: str) -> None:
        d = self.root / project
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        for kind in ("session", "service"):
            p = d / kind
            if len(os.fsencode(kind)) > SOCKET_MAX:
                raise OSError(errno.ENAMETOOLONG, f"the broker's socket name is over {SOCKET_MAX} bytes: {p}")
            try:
                if stat.S_ISSOCK(os.lstat(p).st_mode):
                    p.unlink()
            except FileNotFoundError:
                pass
            s = unix_bind(p)
            s.listen(16)
            self.listeners[s] = (project, kind)

    def serve(self, stop: threading.Event) -> None:
        sel = selectors.DefaultSelector()
        for s in self.listeners:
            sel.register(s, selectors.EVENT_READ)
        try:
            while not stop.is_set():
                for key, _ev in sel.select(timeout=0.2):
                    try:
                        conn = self._accept(key.fileobj)
                    except OSError as e:
                        if e.errno in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM):
                            self.backoffs += 1
                            stop.wait(0.1)   # out of descriptors: let the calls under way end first
                        continue
                    project, kind = self.listeners[key.fileobj]
                    if not self._admit((project, kind)):
                        self._turn_away(conn, project, kind)
                        continue
                    t = threading.Thread(target=self.handle, args=(conn, project, kind), daemon=True)
                    t.start()
                    self.threads = [x for x in self.threads if x.is_alive()] + [t]
        finally:
            sel.close()

    def _accept(self, listener: socket.socket) -> socket.socket:
        return listener.accept()[0]

    def _admit(self, key: tuple[str, str]) -> bool:
        """A connection that hasn't proved its project yet is held only within these bounds (the slice's review, S2):
        per socket, and in all; it proves itself or is dropped within handshake_s."""
        with self.lock:
            if self.unproved.get(key, 0) >= setting("unproved_per_socket", 16) \
                    or sum(self.unproved.values()) >= setting("unproved_total", 64):
                return False
            self.unproved[key] = self.unproved.get(key, 0) + 1
            return True

    def _release(self, key: tuple[str, str]) -> None:
        with self.lock:
            self.unproved[key] = max(0, self.unproved.get(key, 0) - 1)

    def _turn_away(self, conn: socket.socket, project: str, kind: str) -> None:
        try:
            conn.setblocking(False)
            text = _("the platform's git broker is busy (too many connections are still proving their project); "
                     "nothing was run") + "\n"
            send(conn, b"E", text.encode())
            send(conn, b"X", json.dumps({"code": NOT_RUN}).encode())
        except OSError:
            pass
        finally:
            conn.close()
        self.log({"project": project, "socket": kind, "sub": None, "path": "busy", "exit": NOT_RUN, "ms": 0})

    def close(self) -> None:
        for s in self.listeners:
            s.close()
        self.listeners.clear()

    # ---------- one connection ----------

    def handle(self, conn: socket.socket, project: str, kind: str) -> None:
        t0 = time.monotonic()
        rec = {"project": project, "socket": kind, "sub": None, "path": "answer", "exit": NOT_RUN}
        held = [True]

        def release():
            if held[0]:
                held[0] = False
                self._release((project, kind))
        try:
            deadline = t0 + setting("handshake_s", 10)
            try:
                code, path, sub = self._serve(conn, project, kind, deadline, release)
                rec.update(exit=code, path=path, sub=sub)
            except Answer as a:
                rec["exit"] = a.code
                rec["sub"] = getattr(a, "sub", None)
                try:
                    send(conn, b"E", (str(a) + "\n").encode())
                    send(conn, b"X", json.dumps({"code": a.code}).encode())
                except OSError:
                    pass
        except (OSError, ValueError) as e:
            rec.update(path="broken", error=type(e).__name__)
        finally:
            release()
            rec["ms"] = int((time.monotonic() - t0) * 1000)
            self.log(rec)
            try:
                conn.close()
            except OSError:
                pass

    def log(self, rec: dict) -> None:
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            log = self.root / "log.jsonl"
            try:
                if log.stat().st_size > LOG_MAX:   # bounded: the newest part kept, one older part beside it
                    os.replace(log, self.root / "log.jsonl.1")
            except FileNotFoundError:
                pass
            with log.open("a") as f:
                f.write(json.dumps({"at": now(), **rec}, ensure_ascii=False) + "\n")

    def _serve(self, conn, project: str, kind: str, deadline: float, release) -> tuple[int, str, str | None]:
        import policy
        projects = self.projects()
        doc = projects.get(project)
        if not self._switched_on(project, doc):
            raise Answer(_("git through the platform isn't on for {project}; nothing was run", project=project))
        doc = {**doc, "name": project}
        gb = policy.git_broker(doc, project, self.state)
        if not gb or not gb.get("nonce_dir"):
            raise Answer(_("{project} has no workspace; nothing was run", project=project))
        if (why := self.overlap(project, doc, projects)):
            raise Answer(why)
        nonce = secrets.token_hex(16)
        send(conn, b"N", json.dumps({"nonce": nonce, "dir": gb["nonce_dir"]}).encode())
        got = recv(conn, deadline, HANDSHAKE_FRAME_MAX)
        if not got or got[0] != b"P":
            raise Answer(_("no proof of the project came first; nothing was run"))
        ws = pathlib.Path(registry_ws(doc))
        if not self.proved(ws, nonce):
            raise Answer(_("the proof wasn't found in {project}'s folder; nothing was run", project=project))
        got = recv(conn, deadline, HANDSHAKE_FRAME_MAX)
        if not got or got[0] != b"R":
            raise Answer(_("no request came; nothing was run"))
        release()
        req = json.loads(got[1])
        argv, cwd, env = req.get("argv"), req.get("cwd"), req.get("env") or {}
        if not (isinstance(argv, list) and all(isinstance(a, str) for a in argv) and isinstance(cwd, str)
                and isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
            raise Answer(_("the request isn't well formed; nothing was run"))
        call = git_allow.classify(argv, env)
        if call.kind == git_allow.REFUSED:
            raise _sub(Answer(git_allow.message(call)), call.sub)
        if call.kind == git_allow.IN_PLACE:
            raise _sub(Answer(_("git {sub} runs in place, inside your own sandbox, not through the platform; nothing was "
                                "run", sub=call.sub or "")), call.sub)
        if call.kind in (git_allow.COMPOSED, git_allow.NETWORK) or call.op not in BUILT:
            raise _sub(Answer(_("git {sub} through the platform isn't built yet (a later step of the git broker); "
                                "nothing was run", sub=call.sub)), call.sub)
        ctx = self.context(project, doc, projects, cwd, call)
        conn.settimeout(None)
        stdin = self.message_stdin(conn, call)
        with self.queued(project):
            if call.kind == git_allow.CONFIG:
                return self.config_op(conn, ctx, call), "config", call.sub
            return self.p1_op(conn, ctx, call, stdin), "p1", call.sub

    def message_stdin(self, conn, call) -> bytes | None:
        """A message given on stdin (-F -) is read whole before git starts (the slice's review, S1: git commit takes
        index.lock and then waits on its stdin, so a caller holding stdin open held the lock to the timeout), capped;
        None when the call takes no message from stdin (its stdin is then streamed)."""
        if not any(o in ("-F", "--file") and v == "-" for o, v in call.opts):
            return None
        timeout = setting("timeout_s", 600)
        deadline = time.monotonic() + timeout
        data = bytearray()
        try:
            while (got := recv(conn, deadline)) and got[0] == b"I" and got[1]:
                data += got[1]
                if len(data) > MESSAGE_CAP:
                    raise Answer(_("the message on stdin is over {n} bytes; nothing was run", n=MESSAGE_CAP))
        except TimeoutError:
            raise Answer(_("git: stopped after {s} seconds (the platform's limit for one call)", s=int(timeout)),
                         TIMED_OUT) from None
        finally:
            conn.settimeout(None)
        return bytes(data)

    @contextlib.contextmanager
    def queued(self, project: str):
        """A slot of the project's (git_broker.concurrency), waiting for one behind at most git_broker.queue others."""
        sem = self.slot(project)
        if not sem.acquire(blocking=False):
            with self.lock:
                if self.waiting.get(project, 0) >= setting("queue", 16):
                    raise Answer(_("{n} calls of {project}'s already wait for their turn; nothing was run",
                                   n=self.waiting[project], project=project))
                self.waiting[project] = self.waiting.get(project, 0) + 1
            try:
                sem.acquire()
            finally:
                with self.lock:
                    self.waiting[project] -= 1
        try:
            yield
        finally:
            sem.release()

    def proved(self, ws: pathlib.Path, nonce: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{32}", nonce):
            return False
        try:
            fd = open_below(ws, [".hub-run", "git"])
        except OSError:
            return False
        try:
            st = os.stat(nonce, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                return False
            os.unlink(nonce, dir_fd=fd)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def overlap(self, project: str, doc: dict, projects: dict) -> str | None:
        """Why the project isn't served: another sandboxed project's write roots hold its nonce folder, or the two share
        a root (that project could prove it)."""
        import isolation
        mine = project_roots(doc)
        ws = registry_ws(doc)
        if isolation.in_system_tmp(ws):
            return _("{project} isn't served: its workspace is in the temp area every sandboxed project writes; "
                     "nothing was run", project=project)
        nonce = pathlib.Path(os.path.realpath(ws)) / ".hub-run" / "git"
        repos = [pathlib.Path(r["git"]) for r in self.records(project)]
        for other, odoc in sorted(projects.items()):
            if other == project or (odoc.get("enforce") or {}).get("sandbox") is not True:
                continue
            theirs = project_roots({**odoc, "name": other})
            if any(within(nonce, r) for r in theirs) or set(mine) & set(theirs):
                return _("{project} isn't served: {other}'s folders hold or share its own, so {other} could act as it; "
                         "nothing was run", project=project, other=other)
            if (held := next((g for g in repos if any(within(g, r) for r in theirs)), None)):
                return _("{project} isn't served: its repo {path} is inside {other}'s folders, which {other} writes; "
                         "nothing was run", project=project, path=held, other=other)
        return None

    def records(self, project: str) -> list[dict]:
        try:
            return json.loads(repos_file(self.state, project).read_text()).get("repos") or []
        except (OSError, ValueError):
            return []

    def claimant(self, path: pathlib.Path, projects: dict) -> str | None:
        best = None
        for name, d in projects.items():
            if (d.get("enforce") or {}).get("sandbox") is not True:
                continue
            for r in project_roots({**d, "name": name}):
                if within(path, r) and (best is None or len(r.parts) > len(best[1].parts)):
                    best = (name, r)
        return best[0] if best else None

    def context(self, project: str, doc: dict, projects: dict, cwd: str, call) -> dict:
        here = pathlib.Path(cwd)
        if not here.is_absolute():
            raise Answer(_("the working folder isn't an absolute path; nothing was run"))
        for c in call.cdirs:
            here = here / c
        try:
            real = pathlib.Path(os.path.realpath(here))
        except OSError:
            raise Answer(_("the working folder can't be resolved; nothing was run")) from None
        if self.claimant(real, projects) != project:
            raise Answer(_("{path} isn't inside {project}'s own folders, where its git is served: use a worktree inside "
                           "the project; nothing was run", path=real, project=project))
        found = gitdirs.dirs(real)
        if not found:
            raise Answer(_("fatal: not a git repository: {path}", path=real), 128)
        common = pathlib.Path(os.path.realpath(found.common))
        if found.git_dir != found.common and not gitdirs.confirmed(found):
            raise Answer(_("this worktree's pointers don't agree with its repo; nothing was run"))
        ws_git = pathlib.Path(os.path.realpath(registry_ws(doc))) / ".git"
        rec = self.recorded(project, common)
        if rec is None:
            if common == ws_git:
                raise Answer(_("{project}'s git goes through the platform (enforce.git), but its repo isn't rebuilt yet: "
                               "the owner runs the rebuild; git can't write here until then; nothing was run",
                               project=project))
            raise Answer(_("this repo ({path}) isn't one the platform made for {project}; nothing was run", path=common,
                           project=project))
        pol = self.policy_for(project, doc, projects)
        import registry
        d = registry.dirs(doc)
        readable = [p for p in (d.get("workspace"), d.get("runtime")) if p] + [pathlib.Path(r) for r in pol.get("read") or []]
        return {"project": project, "doc": doc, "pol": pol, "cwd": real, "top": found.top, "git_dir": found.git_dir,
                "common": common, "readable": readable, "record": rec}

    def recorded(self, project: str, common: pathlib.Path) -> dict | None:
        try:
            recs = json.loads(repos_file(self.state, project).read_text()).get("repos") or []
        except (OSError, ValueError):
            return None
        for r in recs:
            if r.get("git") == str(common):
                try:
                    st = os.lstat(common)
                except OSError:
                    return None
                ok = stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (r.get("dev"), r.get("ino"))
                return r if ok else None
        return None

    def slot(self, project: str) -> threading.BoundedSemaphore:
        with self.lock:
            if project not in self.slots:
                self.slots[project] = threading.BoundedSemaphore(max(1, setting("concurrency", 8)))
            return self.slots[project]

    # ---------- running git ----------

    def env(self, call_env: dict, extra: dict | None = None) -> dict[str, str]:
        """From scratch (r1 S4): a fixed PATH, HOME and LANG; the request's allowlisted variables; the broker's own."""
        return {"PATH": "/usr/bin:/bin", "LANG": "C", **call_env, "HOME": str(self.etc / "home"),
                "GIT_CONFIG_GLOBAL": str(self.etc / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": ":",
                "GIT_SEQUENCE_EDITOR": ":", **git_allow.ENV_FORCED, **(extra or {})}

    def hardened(self) -> list[str]:
        return ["-c", f"core.hooksPath={self.etc / 'hooks'}", "-c", "core.fsmonitor=false",
                "-c", "transfer.fsckObjects=true"]

    def p1_argv(self, ctx: dict, args: list[str], gopts=()) -> list[str]:
        import seatbelt
        prof = seatbelt.git_writer_profile(ctx["pol"], [ctx["common"]], ctx["readable"], self.git_paths)
        return [SANDBOX_EXEC, "-p", prof, self.git, *gopts, *self.hardened(), *args]

    def run(self, conn, argv: list[str], cwd, env: dict, timeout: float, stdin_from_conn: bool = True,
            capture: bool = False, stdin_data: bytes | None = None) -> tuple[int, bytes]:
        """Run argv, stdin from the caller's I frames, from stdin_data, or nothing; stdout and stderr streamed back (or
        stdout captured). At the timeout the process group is stopped (_stop: git removes its lock files) (exit 124)."""
        piped = stdin_from_conn or stdin_data is not None
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        if stdin_data is not None:
            threading.Thread(target=_feed, args=(proc, stdin_data), daemon=True).start()
        elif stdin_from_conn:
            threading.Thread(target=self._pump_stdin, args=(conn, proc), daemon=True).start()
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, b"O")
        sel.register(proc.stderr, selectors.EVENT_READ, b"E")
        out = bytearray()
        deadline = time.monotonic() + timeout
        killed = False
        try:
            while sel.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    killed = True
                    _stop(proc)
                    break
                for key, _ev in sel.select(timeout=min(left, 0.5)):
                    chunk = os.read(key.fileobj.fileno(), 1 << 16)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    if capture and key.data == b"O":
                        out += chunk
                    else:
                        send(conn, key.data, chunk)
        except OSError:
            _stop(proc)   # the caller went away
            proc.wait()
            raise
        finally:
            sel.close()
            for f in (proc.stdout, proc.stderr):
                f.close()
        code = proc.wait()
        if killed:
            send(conn, b"E", (_("git: stopped after {s} seconds (the platform's limit for one call)", s=int(timeout))
                              + "\n").encode())
            return TIMED_OUT, bytes(out)
        return (128 - code if code < 0 else code), bytes(out)

    def _pump_stdin(self, conn, proc) -> None:
        try:
            while True:
                got = recv(conn)
                if not got or got[0] != b"I" or not got[1]:
                    break
                proc.stdin.write(got[1])
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def finish(self, conn, code: int) -> int:
        send(conn, b"X", json.dumps({"code": code}).encode())
        return code

    # ---------- the operations ----------

    def p1_op(self, conn, ctx: dict, call, stdin: bytes | None = None) -> int:
        timeout = setting("long_timeout_s", 1800) if call.op == "gc" else setting("timeout_s", 600)
        env = self.env(call.env, {"GIT_DIR": str(ctx["git_dir"]), "GIT_WORK_TREE": str(ctx["top"])})
        if call.op == "commit":
            self.check_index(ctx)
        if call.op == "apply-cached":
            return self.finish(conn, self.apply_cached(conn, ctx, call, env, timeout))
        code, _o = self.run(conn, self.p1_argv(ctx, [call.sub, *call.args], call.gopts), ctx["cwd"], env, timeout,
                            stdin_data=stdin)
        return self.finish(conn, code)

    def git_dir_fd(self, ctx: dict) -> int:
        """The request's git dir, without following a link; the common dir's device and inode as recorded."""
        fd = os.open(ctx["git_dir"], DIRF)
        if ctx["git_dir"] == ctx["common"]:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != (ctx["record"].get("dev"), ctx["record"].get("ino")):
                os.close(fd)
                raise Answer(_("this repo's git dir was replaced since the platform recorded it; nothing was run"))
        return fd

    def hash_len(self, ctx: dict) -> int:
        fd = os.open(ctx["common"], DIRF)
        try:
            data = read_at(fd, "config", CONFIG_CAP) or b""
        finally:
            os.close(fd)
        return 32 if re.search(rb"(?im)^\s*objectformat\s*=\s*sha256\s*$", data) else 20

    def bad_entries(self, data: bytes | None, ctx: dict) -> tuple[list[str], list[str]]:
        """(paths that fold to HEAD, gitlinks) in an index: B2's check (r2 B2), and no submodule entry (the owner's
        decision: none is served; the slice's review, N1)."""
        import git_fence
        if not data:
            return [], []
        try:
            entries, _odd, link = git_fence.read_index(data, self.hash_len(ctx))
        except ValueError:
            raise Answer(_("the index can't be read to check it; nothing was run")) from None
        if link:
            raise Answer(_("a split index can't be checked; nothing was run"))
        heads = [p.decode("utf-8", errors="replace") for p, _m in entries if folds_to_head(p)]
        links = [p.decode("utf-8", errors="replace") for p, m in entries if m & 0o170000 == 0o160000]
        return heads, links

    def check_index(self, ctx: dict) -> None:
        """B2 for commit (r1 S8): the tree a commit is written from holds no path that folds to HEAD."""
        fd = self.git_dir_fd(ctx)
        try:
            data = read_at(fd, "index", INDEX_CAP)
        finally:
            os.close(fd)
        heads, links = self.bad_entries(data, ctx)
        if heads:
            raise Answer(_("a tracked path named HEAD can't be committed through the platform ({path}): a file of that "
                           "name is a git-dir shape; nothing was run", path=heads[0]))
        if links:
            raise Answer(_("a submodule entry can't be committed through the platform ({path}): submodules aren't "
                           "served (the owner's decision); nothing was run", path=links[0]))

    def apply_cached(self, conn, ctx: dict, call, env: dict, timeout: float) -> int:
        """apply --cached on a copy of the index in the git dir, under the broker's index.lock (r3 S-e); the copy is
        checked (B2) and only then becomes the index."""
        fd = self.git_dir_fd(ctx)
        lock = copy = None
        try:
            try:
                lock = os.open("index.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                               dir_fd=fd)
            except FileExistsError:
                raise Answer(_("fatal: Unable to create '{path}': File exists (another git process seems to be running "
                               "in this repository)", path=ctx["git_dir"] / "index.lock"), 128) from None
            copy = f"index.broker-{secrets.token_hex(6)}"
            current = read_at(fd, "index", INDEX_CAP)
            if current is not None:
                c = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd)
                try:
                    write_fd(c, current)
                finally:
                    os.close(c)
            env = {**env, "GIT_INDEX_FILE": str(ctx["git_dir"] / copy)}
            code, _o = self.run(conn, self.p1_argv(ctx, [call.sub, *call.args], call.gopts), ctx["cwd"], env, timeout)
            if code != 0:
                return code
            data = read_at(fd, copy, INDEX_CAP)
            if data is None:
                return code
            heads, links = self.bad_entries(data, ctx)
            if heads or links:
                text = _("a tracked path named HEAD can't enter the index through the platform ({path}): a file of that "
                         "name is a git-dir shape; the index is unchanged", path=heads[0]) if heads else \
                    _("a submodule entry can't enter the index through the platform ({path}): submodules aren't served "
                      "(the owner's decision); the index is unchanged", path=links[0])
                send(conn, b"E", (text + "\n").encode())
                return NOT_RUN
            write_fd(lock, data)
            os.close(lock)
            lock = None
            os.replace("index.lock", "index", src_dir_fd=fd, dst_dir_fd=fd)
            return 0
        finally:
            if lock is not None:
                os.close(lock)
                unlink_at(fd, "index.lock")
            if copy:
                unlink_at(fd, copy)
                unlink_at(fd, copy + ".lock")
            os.close(fd)

    # ---------- the config op ----------

    def config_op(self, conn, ctx: dict, call) -> int:
        """config set/unset and remote add/set-url/remove: the allowlist and value rules (git_fence.check_key, narrowed by
        r2 S3), only the registry's own URL for a remote (B4); config.lock first, the edit on a copy, a rename."""
        fd = os.open(ctx["common"], DIRF)
        lock = None
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != (ctx["record"].get("dev"), ctx["record"].get("ino")):
                raise Answer(_("this repo's git dir was replaced since the platform recorded it; nothing was run"))
            lock = self.config_lock(fd)   # before anything is read (r1 nit)
            remotes = {remote_of(k) for k, _v in self._pairs_of(fd)} - {None}
            edits, refs_gone = self.config_edits(ctx, call, remotes)
            for args in edits:
                if args[0].startswith("--"):   # --unset, --remove-section: they add nothing
                    continue
                key, value = args
                if (why := self.config_value_ok(ctx, key, value, remotes | ({remote_of(key)} - {None}))):
                    raise Answer(git_allow.message(git_allow.Call(git_allow.REFUSED, reason=why)))
            held, lock = lock, None   # edit_config closes it, and removes it unless it became the config
            code, err = self.edit_config(fd, edits, held)
        finally:
            if lock is not None:
                os.close(lock)
                unlink_at(fd, "config.lock")
            os.close(fd)
        if err:
            send(conn, b"E", err.encode())
        if code == 0 and refs_gone:
            code = self.drop_tracking_refs(conn, ctx, refs_gone)
        return self.finish(conn, code)

    def work(self) -> pathlib.Path:
        """A folder of the broker's own in the private temp folder (never the system's, which projects write)."""
        import tempfile
        private_tmp = self.tmp
        return pathlib.Path(tempfile.mkdtemp(prefix="git-broker-", dir=private_tmp()))

    def _pairs_of(self, fd: int) -> list[tuple[str, str | None]]:
        import git_fence
        import shutil
        data = read_at(fd, "config", CONFIG_CAP)
        if data is None:
            return []
        work = self.work()
        try:
            (work / "config").write_bytes(data)
            return git_fence.config_pairs(work / "config", work)
        except git_fence.Unreadable as e:
            raise Answer(_("the repo's config can't be read ({why}); nothing was run", why=e)) from None
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def config_edits(self, ctx: dict, call, remotes: set[str]) -> tuple[list[list[str]], str | None]:
        """The `git config --file` edits for this op, and the remote whose tracking refs go."""
        op, pos = call.op, call.pos
        if op == "config-set":
            return [[pos[0], pos[1]]], None
        if op == "config-unset":
            key = pos[0]
            low = key.lower()
            if low.startswith(("extensions.", "core.repositoryformatversion")):
                raise Answer(git_allow.message(git_allow.Call(git_allow.REFUSED, reason=_(
                    "{key} says how the repo is stored", key=key))))
            return [["--unset", key]], None
        name = pos[1]
        if op == "remote-add":
            if name in remotes:
                raise Answer(_("error: remote {name} already exists.", name=name), 3)
            return [[f"remote.{name}.url", pos[2]], [f"remote.{name}.fetch", f"+refs/heads/*:refs/remotes/{name}/*"]], None
        if name not in remotes:
            raise Answer(_("error: No such remote: '{name}'", name=name), 2)
        if op == "remote-set-url":
            return [[f"remote.{name}.url", pos[2]]], None
        return [["--remove-section", f"remote.{name}"]], name

    def config_value_ok(self, ctx: dict, key: str, value: str, remotes: set[str]) -> str | None:
        import git_fence
        low = key.lower()
        section, _s, rest = low.partition(".")
        sub, _d, name = rest.rpartition(".")
        if section == "submodule":
            return _("submodules aren't served (the owner's decision)")
        if section == "remote" and name in ("pushurl", "push"):
            return _("{key}: pushes go only where the platform computes it", key=key)
        if section == "remote" and name == "url":
            origin = ((ctx["doc"].get("paths") or {}).get("origin") or "").strip()
            if not origin or value.strip() != origin:
                return _("{key}: only the URL the registry declares for {project}", key=key, project=ctx["project"])
        if section == "branch" and name == "merge" and value in self.protected(ctx):
            return _("{key}: {ref} is the default branch's", key=key, ref=value)
        why = git_fence.check_key(key, value, remotes)
        if why == "key":
            return _("{key} isn't one of the settings a project changes", key=key)
        if why == "value":
            return _("{key}: that value isn't allowed", key=key)
        return None

    def protected(self, ctx: dict) -> set[str]:
        """The default branch's refs: an upstream naming one is refused (r2 S3)."""
        branches = {getattr(self, "default_branch", "main"), "main", "master"}
        if (b := (ctx["doc"].get("paths") or {}).get("branch")):
            branches.add(str(b))
        return {f"refs/heads/{b}" for b in branches}

    def config_lock(self, fd: int) -> int:
        """config.lock, created exclusively and not following a link: the owner's concurrent git waits or fails."""
        try:
            return os.open("config.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                           dir_fd=fd)
        except FileExistsError:
            raise Answer(_("error: could not lock config file: config.lock exists (another git is changing it); nothing "
                           "was run"), 255) from None

    def edit_config(self, fd: int, edits: list[list[str]], lock: int) -> tuple[int, str]:
        """With config.lock held: the config read without following a link, edited as a copy in the private temp folder
        by `git config --file`, written into the lock, renamed over config. Never in place. The lock is this call's
        to close and, unless it became the config, remove."""
        done = False
        import shutil
        work = None
        try:
            work = self.work()
            data = read_at(fd, "config", CONFIG_CAP) or b""
            (work / "config").write_bytes(data)
            err = ""
            for args in edits:
                r = self._git_config_file(work / "config", args)
                if r.returncode != 0:
                    return r.returncode, r.stderr.replace(str(work / "config"), "config")
                err += r.stderr
            write_fd(lock, (work / "config").read_bytes())
            os.close(lock)
            lock = None
            os.replace("config.lock", "config", src_dir_fd=fd, dst_dir_fd=fd)
            done = True
            return 0, err
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
            if lock is not None:
                os.close(lock)
            if not done:
                unlink_at(fd, "config.lock")

    def drop_tracking_refs(self, conn, ctx: dict, remote: str) -> int:
        env = self.env({}, {"GIT_DIR": str(ctx["git_dir"]), "GIT_WORK_TREE": str(ctx["top"])})
        code, out = self.run(conn, self.p1_argv(ctx, ["for-each-ref", "--format=%(refname)", f"refs/remotes/{remote}/"]),
                             ctx["cwd"], env, setting("timeout_s", 600), stdin_from_conn=False, capture=True)
        if code != 0:
            return code
        refs = [r for r in out.decode(errors="replace").splitlines() if r.startswith(f"refs/remotes/{remote}/")]
        if not refs:
            return 0
        lines = "".join(f"delete {r}\n" for r in refs).encode()
        proc = subprocess.run(self.p1_argv(ctx, ["update-ref", "--stdin"]), cwd=str(ctx["cwd"]), env=env, input=lines,
                              capture_output=True, timeout=setting("timeout_s", 600), start_new_session=True)
        if proc.stderr:
            send(conn, b"E", proc.stderr)
        return proc.returncode

    # ---------- making a repo ----------

    def make_repo(self, project: str, path) -> pathlib.Path:
        """A repo for the project at path (inside its own folders), made by the platform (r3 S-a): the folder made
        through no-follow descriptors and its real path taken from the descriptor; `git init --template=` under the
        init profile, whose only writable folder is <real path>/.git; then the config replaced whole (the keys init
        wrote that the allowlist holds, plus the presets), the platform folder made, and the repo recorded (real path,
        device and inode of its .git)."""
        import git_fence
        import seatbelt
        if (why := unsupported()):
            raise Answer(_("git through the platform isn't supported on this system ({why}); nothing was run", why=why))
        if self.git is None:
            self.git, self.git_paths = git_programs()
            self.prepare()
        projects = self.projects()
        doc = {**projects[project], "name": project}
        want = pathlib.Path(os.path.abspath(path))
        roots = project_roots(doc)
        base = next((r for r in sorted(roots, key=lambda r: -len(r.parts))
                     if within(pathlib.Path(os.path.realpath(want.parent)) / want.name, r)), None)
        if base is None:
            raise Answer(_("{path} isn't inside {project}'s own folders; nothing was run", path=want, project=project))
        rel = (pathlib.Path(os.path.realpath(want.parent)) / want.name).relative_to(base)
        fd = open_below(base, rel.parts, create=True)
        try:
            real = real_of(fd)
            try:
                os.stat(".git", dir_fd=fd, follow_symlinks=False)
                raise Answer(_("{path} already holds a .git; nothing was run", path=real))
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)
        pol = self.policy_for(project, doc, projects)
        import registry
        d = registry.dirs(doc)
        readable = [p for p in (d.get("workspace"), d.get("runtime")) if p] + [pathlib.Path(r) for r in pol.get("read") or []]
        prof = seatbelt.git_init_profile(pol, real / ".git", [*readable, real], self.git_paths)
        r = subprocess.run([SANDBOX_EXEC, "-p", prof, self.git, *self.hardened(), "init", "-q", "--template=",
                            "-b", self.default_branch, str(real)], cwd=str(real), env=self.env({}),
                           capture_output=True, text=True, timeout=setting("timeout_s", 600), stdin=subprocess.DEVNULL,
                           start_new_session=True)
        if r.returncode != 0:
            raise Answer(_("git init failed: {why}", why=r.stderr.strip()[-300:]), r.returncode)
        gfd = open_below(real, [".git"])
        try:
            pairs = self._pairs_of(gfd)
            keep = [(k, v) for k, v in pairs if v is not None and git_fence.check_key(k, v, set()) is None]
            have = {k.lower() for k, _v in keep}
            keep += [(k, v) for k, v in PRESETS if k.lower() not in have]
            self.replace_config(gfd, keep)
            try:
                os.mkdir(names.current("repo_dir"), 0o755, dir_fd=gfd)
            except FileExistsError:
                pass
            st = os.fstat(gfd)
        finally:
            os.close(gfd)
        ws = registry_ws(doc)
        self.record(project, real / ".git", st, workspace=bool(ws) and real == pathlib.Path(os.path.realpath(ws)))
        return real

    def replace_config(self, fd: int, pairs) -> None:
        import shutil
        lock = self.config_lock(fd)
        work = None
        try:
            work = self.work()
            (work / "config").write_bytes(b"")
            for k, v in pairs:
                r = self._git_config_file(work / "config", ["--add", k, v])
                if r.returncode != 0:
                    raise Answer(_("the config couldn't be written: {why}", why=r.stderr.strip()[-200:]))
            write_fd(lock, (work / "config").read_bytes())
            os.close(lock)
            lock = None
            os.replace("config.lock", "config", src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            if lock is not None:
                os.close(lock)
                unlink_at(fd, "config.lock")
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)

    def record(self, project: str, git: pathlib.Path, st: os.stat_result, workspace: bool) -> None:
        f = repos_file(self.state, project)
        with self.lock:
            f.parent.mkdir(parents=True, exist_ok=True)
            try:
                recs = json.loads(f.read_text()).get("repos") or []
            except (OSError, ValueError):
                recs = []
            recs = [r for r in recs if r.get("git") != str(git)]
            recs.append({"git": str(git), "dev": st.st_dev, "ino": st.st_ino, "workspace": workspace, "at": now()})
            hubconf.write_atomic(f, json.dumps({"repos": recs}, indent=1) + "\n")


def _sub(a: Answer, sub) -> Answer:
    a.sub = sub
    return a


def _stop(proc, grace: float | None = None) -> None:
    """The process group: SIGTERM first, so git removes its lock files (an index.lock left behind blocks the owner's git
    and the project's, which can't remove it under the fence); SIGKILL after the grace for whatever is left."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=STOP_GRACE_S if grace is None else grace)
    except subprocess.TimeoutExpired:
        pass
    try:   # whatever of the group is left
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _feed(proc, data: bytes) -> None:
    try:
        proc.stdin.write(data)
    except (OSError, ValueError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def raise_nofile(target: int = 8192) -> int:
    """The service's open-file limit raised toward target (launchd's default soft limit is 256): each call holds a
    connection, pipes and descriptors. Returns the soft limit now."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        except (ValueError, OSError):
            pass
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def remote_of(key: str) -> str | None:
    """The remote a config key names (remote.<name>.<key>, the name may hold dots), else None."""
    section, _d, rest = key.partition(".")
    name, _d, _k = rest.rpartition(".")
    return name if section.lower() == "remote" and name else None


def registry_ws(doc: dict):
    import registry
    return registry.dirs(doc)["workspace"]
