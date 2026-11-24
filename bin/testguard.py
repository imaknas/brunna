"""The platform a test run sees: what bin/run-tests sets up for its tests, and the same for a test run started by hand.

A test run must never reach the machine's live platform. Run in a linked worktree, hubconf finds the main checkout and takes
its .state and .hub-home (the live state and instance), and many modules fix a path under them at import (alert.SENT,
cap-runner's DIR…), so a test that patches only hubconf.STATE still writes live (11-14: a fake conformance result that
doctor then quoted). And a test that leaves a temp folder behind leaves it in the user's own temp folder.

So a run gets, as bin/run-tests gives every module:
  - the tree under test as the platform (an explicit root: no lookup of the main checkout, so not its .state or
    instance). Only the root: HOME and STATE would leak into the fake hubs the tests build
    (docs/design/test-waiting.md A1)
  - a fresh HOME (no nvm there, so hubconf.tool and nvm_bin find none) and a recording `claude` first on PATH: no test
    reaches the real coding agent; a call that lands there fails the run
  - a secrets directory of its own, and a temp folder of its own (in the instance's private temp area, never the
    system's on an install: every sandboxed project can write that one), both removed afterwards
  - and at the end, what a test left in HOME, the secrets' temp area or the temp folder fails the run, and so does a disk
    image still attached from under the run's folders (macOS): it is detached, with any hdiutil or newfs_hfs still working
    on it, as the run's folders are removed (on an interrupt too: SIGINT, and the SIGTERM a tier's time limit sends). The
    next run's start sweeps what a run that couldn't (killed) left: images under a dead run's folder in the private temp
    area, never anything else (11-21: two images of test_no_mounts attached for hours, a newfs_hfs hung for 12)

`bin/py -m unittest …` (any form: tests.<module>, discover, or a test file run as a script) is such a run too: hubconf's
import calls reexec() when the entry point is unittest or a file in this tree's tests/ and no runner set things up; it
runs the same command again as a child under this environment, cleans up and exits with the child's code. A child, not
this process patched: the temp folder and HOME may already have been read, and the private temp area is found through
the live instance before the root is pointed elsewhere.
"""
from __future__ import annotations

import fcntl
import os
import pathlib
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import names  # noqa: E402

TREE = pathlib.Path(__file__).resolve().parents[1]
MARK = "TEST_GUARD"   # set for a guarded run's child (bin/run-tests sets TEST_RUNNER): its own imports start nothing again


class Guard:
    """A run's throwaway HOME, PATH stand-ins and secrets (in the tree's .state, not the temp area: the sandbox tests need
    a HOME the sandbox can't write; test_cap_profile skips otherwise) and its temp folder."""

    def __init__(self, tree: pathlib.Path = TREE):
        state = tree / ".state"
        state.mkdir(parents=True, exist_ok=True)
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="run-tests-guard-", dir=state))
        self.home = self.dir / "home"
        (self.home / "workspace").mkdir(parents=True)
        (self.dir / "bin").mkdir()
        self.calls = self.dir / "calls.log"
        claude = self.dir / "bin" / "claude"
        claude.write_text(f'#!/bin/sh\necho "$PWD :: $*" >> "{self.calls}"\n'
                          'echo "run-tests: no test may start a real claude" >&2\nexit 97\n')
        claude.chmod(0o755)
        self.secrets = self.dir / "secrets"
        try:
            self.tmp = private_temp()
            self._lock = hold(self.tmp)   # this run is alive while it holds this: another run's sweep leaves its folders alone
        except OSError:
            shutil.rmtree(self.dir, ignore_errors=True)
            raise
        self.roots = [self.tmp, self.dir]   # where this run's tests make what they make: its disk images are those under them
        self._removed = False
        if DARWIN and (area := sweep_area(self.tmp)):
            try:
                if gone := sweep(area):
                    print("tests: what runs and tests that are gone left, disk images detached and disk tools stopped:\n  "
                          + "\n  ".join(gone), file=sys.stderr, flush=True)
            except Exception as e:   # a sweep that can't run never stops the run
                print(f"tests: the sweep of disk images dead runs left could not run: {e}", file=sys.stderr, flush=True)

    def env(self, base: dict[str, str] | None = None, tree: pathlib.Path = TREE) -> dict[str, str]:
        env = {**(os.environ if base is None else base), "HOME": str(self.home),
               "PATH": f"{self.dir / 'bin'}{os.pathsep}{(os.environ if base is None else base).get('PATH', '')}",
               "TMPDIR": str(self.tmp)}
        names.env_set(env, "SECRETS", str(self.secrets))   # this run's own: what is left there is this run's tests'
        if not names.env("PYTHON", environ=env):
            names.env_set(env, "PYTHON", sys.executable)   # bin/py looks for it under HOME
        # no bytecode written, as the test runner's tiers write none: an interpreter's own cache can live under HOME
        # (macOS's python3 keeps its .pyc in ~/Library/Caches/com.apple.python), and what is left in HOME is a leak
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        names.env_set(env, "ROOT", names.env("ROOT", environ=env) or str(tree))
        return env

    def leaks(self) -> list[str]:
        """What the run's tests left where a real run would write real files."""
        try:
            out = [f"HOME: {p.relative_to(self.home)}" for p in self.home.rglob("*") if not p.is_dir()]
            out += [f"private temp: {p.name}" for p in (self.secrets / "tmp").glob("*")]
            out += [f"temp folder: {p.name}" for p in sorted(self.tmp.iterdir())]
        except OSError as e:
            out = [f"the leak check could not run: {e}"]
        if DARWIN:
            try:
                out += [f"disk image: {im['dev'] or '(no device yet)'} {im['path']}" for im in attached()
                        if under(im["path"], self.roots)]
                procs = processes()
                busy = set(stuck(self.roots, procs=procs))
                out += [f"disk tool still running: {' '.join(argv)[:200]}" for pid, argv in procs if pid in busy]
            except OSError as e:
                out.append(f"the disk image check could not run: {e}")
        return out

    def agent_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def remove(self):
        """Detach this run's disk images, remove its folders, let its lock go. Once: on an interrupt the runner's finally
        calls it again."""
        if self._removed:
            return
        self._removed = True
        if DARWIN:
            try:
                release(self.roots)
            except OSError as e:
                print(f"tests: this run's disk images could not be detached: {e}", file=sys.stderr, flush=True)
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        lock_path(self.tmp).unlink(missing_ok=True)
        os.close(self._lock)


def on_term():
    """SIGTERM (a tier's time limit: TERM to the group, KILL after a grace) as an interrupt, so the run's finally still
    detaches and removes."""
    def interrupt(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)


# Disk images (macOS). A test that attaches one (tests/test_no_mounts.py) can leave it attached when it is killed or an
# hdiutil hangs; what is detached is matched by the image's path, under this run's folders or a dead run's, never by
# anything else.
DARWIN = sys.platform == "darwin"
DISK_TOOLS = {"hdiutil", "newfs_hfs"}   # what works on an image while it is made or attached, and can hang there
INFO_S, DETACH_S, PS_S = 15, 20, 10   # each bounded: a tier's TERM leaves 10 s before KILL, and the next run sweeps the rest
DEV = re.compile(r"/dev/r?disk(\d+)(?:s\d+)*")
KILL_S = 2   # how long a killed call's output is waited for
RUN_PREFIX = "run-tests-"   # private_temp's folder for a run
NOMOUNT_VOLUME = "brunna-nomount-"   # + the test process's pid: the volume tests/test_no_mounts.py makes
NOMOUNT = re.compile(rf"{re.escape(NOMOUNT_VOLUME)}(\d{{1,9}})")   # a pid, bounded: os.kill takes no larger


def hdiutil() -> str:
    return shutil.which("hdiutil") or "/usr/bin/hdiutil"


def bounded(cmd: list[str], timeout: float, **kw) -> subprocess.CompletedProcess:
    """cmd in a process group of its own, with a time limit: over it the whole group is killed and TimeoutExpired raised.
    Back within the limit and KILL_S whatever it started: a child in a session of its own (hdiutil's diskimages-helper)
    outlives the group's kill, and waiting for the end of the output it holds open would have no limit at all."""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.communicate(timeout=KILL_S)
        except subprocess.TimeoutExpired:
            for f in (p.stdout, p.stderr):
                f.close()
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def call(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """bounded, over the limit an OSError."""
    try:
        return bounded(cmd, timeout)
    except subprocess.TimeoutExpired:
        raise OSError(0, f"{os.path.basename(cmd[0])} {cmd[1]} took over {timeout} s") from None


def real(p) -> pathlib.Path:
    return pathlib.Path(os.path.realpath(p))


def under(path: str, roots) -> bool:
    """An image's or an argument's path in one of these folders (by components: run-tests-ab is not in run-tests-a)."""
    if not path or not path.startswith("/"):
        return False   # ram://, a URL, a flag: no folder of anyone's
    rp = real(path)
    return any(rp.is_relative_to(real(r)) for r in roots)


def attached() -> list[dict]:
    """The attached disk images: {"path": its image-path, "dev": its whole disk (/dev/diskN, "" before it has one), "devs":
    all its devices}. OSError when hdiutil can't tell."""
    r = call([hdiutil(), "info", "-plist"], INFO_S)
    if r.returncode != 0:
        raise OSError(0, f"hdiutil info: {r.stderr.decode(errors='replace').strip()[-300:]}")
    try:
        doc = plistlib.loads(r.stdout)
    except Exception as e:   # plistlib raises several kinds on a malformed document
        raise OSError(0, f"hdiutil info -plist: {e}") from None
    out = []
    for im in doc.get("images") or []:
        devs = sorted({e["dev-entry"] for e in im.get("system-entities") or [] if e.get("dev-entry")})
        whole = [d for d in devs if re.fullmatch(r"/dev/disk\d+", d)]
        out.append({"path": im.get("image-path") or "", "dev": (whole or devs or [""])[0], "devs": devs})
    return out


def processes() -> list[tuple[int, list[str]]]:
    """(pid, argv) of every process this user can see (split on blanks: the paths matched here have none)."""
    r = call(["/bin/ps", "-axww", "-o", "pid=,command="], PS_S)
    out = []
    for line in r.stdout.decode(errors="replace").splitlines():
        pid, _sep, cmd = line.strip().partition(" ")
        if pid.isdigit() and cmd.split():
            out.append((int(pid), cmd.split()))
    return out


def stuck(roots, disks=(), names=(), procs=None) -> list[int]:
    """hdiutil and newfs_hfs processes working on what is these roots': an argument that is a path under them, a device of
    one of these disk numbers (their images'), or exactly one of these volume names."""
    out = []
    for pid, argv in (processes() if procs is None else procs):
        if pid == os.getpid() or os.path.basename(argv[0]) not in DISK_TOOLS:
            continue
        for a in argv[1:]:
            m = DEV.fullmatch(a)
            if (m and m.group(1) in disks) or a in names or under(a, roots):
                out.append(pid)
                break
    return out


def kill(pids: list[int]):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        if sig == signal.SIGTERM and pids:
            time.sleep(0.5)


def release(roots, names=(), rounds: int = 3, ps=processes) -> list[str]:
    """Detach every disk image whose path is under these folders, first stopping an hdiutil or newfs_hfs still working on
    one (or on one of these volume names); a few rounds, for an image still attaching. Returns what was attached: "dev
    path". OSError when what is attached can't be read."""
    found: dict[str, str] = {}
    for i in range(rounds):
        ims = [im for im in attached() if under(im["path"], roots)]
        pids = stuck(roots, {m.group(1) for im in ims for d in im["devs"] if (m := DEV.fullmatch(d))}, names, ps())
        if not ims and not pids:
            break
        for im in ims:
            found[im["path"]] = im["dev"] or found.get(im["path"], "")
        kill(pids)
        for im in ims:
            if im["dev"]:
                try:
                    call([hdiutil(), "detach", "-force", im["dev"]], DETACH_S)
                except OSError:
                    pass
        time.sleep(0.5 * (i + 1))
    return [f"{d or '(no device yet)'} {p}" for p, d in found.items()]


def lock_path(tmp: pathlib.Path) -> pathlib.Path:
    return tmp.parent / f"{tmp.name}.lock"   # beside the folder, not in it: what is in it is the tests' (a leak)


def hold(tmp: pathlib.Path) -> int:
    """The run's lock, held while it runs (not inherited: a child the run left behind doesn't keep it alive)."""
    fd = os.open(lock_path(tmp), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def held(tmp: pathlib.Path) -> bool | None:
    """Whether a live run holds this folder's lock; None when it has none (an older guard's run: can't tell)."""
    try:
        fd = os.open(lock_path(tmp), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def sweep_area(tmp: pathlib.Path) -> pathlib.Path | None:
    """Where dead runs' folders are, from where this run's own went: the private temp area, whether run by hand (its
    run-tests-* folder at the top) or in a tier (the tier's <cli>-test.* folder holds it). None in a system temp area (no
    install: nothing of the platform's to sweep, and every sandboxed project writes there)."""
    import isolation
    parent = tmp.parent
    if parent.name.startswith(f"{names.current('cli')}-test."):
        parent = parent.parent
    return None if isolation.in_system_tmp(parent) else parent


def dead_runs(area: pathlib.Path, paths) -> set[pathlib.Path]:
    """The run folders these paths (images', or a disk tool's arguments) are in whose run is gone: a run's folder in the area
    (a tier's <cli>-test.*, a run-tests-* one) that no longer exists, or whose lock nobody holds. A run-tests-* folder with
    no lock (an older guard's) can't be told and is left alone; so is anything not in a run's folder."""
    tier = f"{names.current('cli')}-test."
    base = real(area)
    out = set()
    for p in paths:
        if not under(p, [base]):
            continue
        parts = real(p).relative_to(base).parts[:-1]   # the folders it is in
        if not parts or not (parts[0].startswith(RUN_PREFIX) or parts[0].startswith(tier)):
            continue
        if not (base / parts[0]).exists():
            out.add(base / parts[0])
            continue
        run = next((i for i, x in enumerate(parts) if x.startswith(RUN_PREFIX)), None)
        if run is None:
            continue
        folder = base.joinpath(*parts[:run + 1])
        if not folder.exists() or held(folder) is False:
            out.add(folder)
    return out


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def orphans(procs) -> list[tuple[int, list[str]]]:
    """Disk tools making a volume of a test that is gone: named NOMOUNT_VOLUME + the test's pid, and no such process (a
    live test's own, or a reused pid, is left alone). Its image may be detached already and the tool still hanging (11-21:
    a newfs_hfs 2 days on, its device gone)."""
    out = []
    for pid, argv in procs:
        if pid != os.getpid() and os.path.basename(argv[0]) in DISK_TOOLS and any(
                (m := NOMOUNT.fullmatch(a)) and not alive(int(m.group(1))) for a in argv[1:]):
            out.append((pid, argv))
    return out


def sweep(area: pathlib.Path, ps=processes) -> list[str]:
    """Detach the images (and stop the disk tools) of the runs in this area that are gone, and stop the disk tools of
    tests that are gone. Returns what it detached and stopped. ps: the process listing (a test's: only its own)."""
    procs = ps()
    left = orphans(procs)
    kill([pid for pid, _argv in left])
    paths = [im["path"] for im in attached()]
    paths += [a for _pid, argv in procs if os.path.basename(argv[0]) in DISK_TOOLS for a in argv[1:]]
    dead = dead_runs(area, paths)
    return [f"stopped {' '.join(argv)[:200]}" for _pid, argv in left] + (release(sorted(dead), ps=ps) if dead else [])


def private_temp() -> pathlib.Path:
    """The run's own temp folder. The tests run as the user and execute what they write there, and the system's temp folder
    is every sandboxed project's to write: an install's run takes one in its private temp area (hubconf.test_tmp). A clone
    that isn't an install (no instance, no settings file: CI, a fresh checkout) has no projects to race it: a folder of its
    own in the system's. Raises OSError for an install with no private folder outside every repo (the old layout's, in the
    hub root), which the test runner's tiers refuse too."""
    import hubconf
    import isolation
    if isolation.in_system_tmp(tempfile.gettempdir()) and (hubconf.instance_home() is not None or hubconf.config_path().exists()):
        return hubconf.test_tmp("run-tests-")
    return pathlib.Path(tempfile.mkdtemp(prefix="run-tests-"))


def guarded() -> bool:
    """A runner already set this process up (bin/run-tests, or reexec's child)."""
    return bool(names.env("TEST_RUNNER") or names.env(MARK))


def bare_test_run(tree: pathlib.Path = TREE) -> bool:
    """This process is a test run nobody set up: started as `python -m unittest …` or as a file in the tree's tests/."""
    if guarded():
        return False
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    if spec is not None and spec.name in ("unittest", "unittest.__main__"):
        return True
    f = getattr(main, "__file__", None)
    return bool(f) and pathlib.Path(f).resolve().parent == tree / "tests"


def reexec():
    """Run this process's command again as a guarded child, then clean up and leave with its exit code (1 when a test
    left something behind or reached for the real agent). Leaves with os._exit: this runs inside an import, which
    unittest's discovery would otherwise turn into an import error per module."""
    try:
        g = Guard()
    except OSError as e:
        print(f"tests: no private temp folder for this run ({e.strerror or e}); tests don't run in the system's, which every "
              f"sandboxed project can write. A hub in the old layout moves with {names.current('cli')} move-instance",
              file=sys.stderr, flush=True)
        os._exit(2)
    env = g.env()
    names.env_set(env, MARK, "1")
    code = 1
    try:
        on_term()
        code = subprocess.run([sys.executable, *sys.orig_argv[1:]], env=env).returncode
    except KeyboardInterrupt:
        code = 130
    finally:
        leaks, calls = g.leaks(), g.agent_calls()
        g.remove()
    if leaks:
        print("tests left files behind (removed with this run's throwaway folders; clean them up in the test):\n  "
              + "\n  ".join(leaks[:20]), file=sys.stderr)
        code = code or 1
    if calls:
        print("a test reached for the real coding agent; give it a stand-in:\n  " + "\n  ".join(calls[:20]), file=sys.stderr)
        code = code or 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code if code >= 0 else 128 - code)
