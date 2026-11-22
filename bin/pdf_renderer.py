#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The platform's PDF renderer: Chrome for Testing's chrome-headless-shell, which the runner (bin/cap-runner.py, kind
pdf) runs under seatbelt.pdf_profile to print a project's page (docs/design/capabilities.md, "pdf" and its r1).

  pdf_renderer.py install   the platform job brunna/pdf-renderer (daily): only when some sandboxed project declares
                            pdf. Reads Chrome for Testing's index (verified TLS); when its Stable is newer than the
                            current copy: downloads it, unpacks it with ditto into a new version folder, refuses a link
                            that leaves the folder, records the binary's sha256 (trust on first use: Google publishes no
                            digests and signs the build ad hoc), prints a fixture page under pdf_profile with a
                            synthetic project's policy, and only then makes it current. A version that doesn't print is
                            kept aside and said (exit 1).
  pdf_renderer.py status    what is current, and whether a print would be refused now.

Kept in <tools>/chrome-headless-shell/ (hubconf.tools_dir(): in no sandbox's writes): <version>/ folders and
current.json {version, platform, sha256, installed, checked, latest, behind_since}. A print is refused, with the reason,
when there is no current copy, on another OS than macOS, or when the index has had a newer Stable for more than
STALE_DAYS that isn't current (behind_since). Never another browser instead.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402

_ = i18n.domain(__file__)

INDEX = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
URL_PREFIX = "https://storage.googleapis.com/chrome-for-testing-public/"
VERSION = re.compile(r"^\d+(\.\d+){3}$")
STALE_DAYS = 14
TIMEOUT_S = 120            # one print, the runner's deadline (the request sets none)
MAX_INDEX = 4 * 1024 * 1024
MAX_ZIP = 600 * 1024 * 1024
FIXTURE = ("<!doctype html><html><head><style>@page{size:Letter;margin:.5in}</style></head>"
           "<body style='display:flex'><div>brunna</div><div>pdf</div></body></html>")


def root() -> pathlib.Path:
    return hubconf.tools_dir() / "chrome-headless-shell"


def platform_name() -> str | None:
    """Chrome for Testing's name for this machine, or None where the runner can't confine a print (not macOS)."""
    if sys.platform != "darwin":
        return None
    return {"arm64": "mac-arm64", "x86_64": "mac-x64"}.get(platform.machine())


def record() -> dict:
    try:
        d = json.loads((root() / "current.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _binary_in(folder: pathlib.Path, plat: str) -> pathlib.Path:
    return folder / f"chrome-headless-shell-{plat}" / "chrome-headless-shell"


def current() -> tuple[pathlib.Path, pathlib.Path] | None:
    """(the version folder, the binary) of the current copy, when it is there as a plain file; else None."""
    rec, plat = record(), platform_name()
    v = rec.get("version")
    if not plat or not isinstance(v, str) or not VERSION.match(v) or rec.get("platform") != plat:
        return None
    folder = root() / v
    exe = _binary_in(folder, plat)
    try:
        st = os.lstat(exe)
    except OSError:
        return None
    import stat
    if not stat.S_ISREG(st.st_mode) or not os.access(exe, os.X_OK):
        return None
    return folder, exe


def refusal(now: datetime.datetime | None = None) -> str:
    """Why a print would be refused now ('' when it would run)."""
    if not platform_name():
        return _("printing to PDF needs macOS: the platform's runner confines it with Seatbelt")
    if current() is None:
        return _("the platform has no PDF renderer yet (the platform job brunna/pdf-renderer installs it)")
    behind = record().get("behind_since")
    if isinstance(behind, str):
        try:
            since = datetime.datetime.fromisoformat(behind)
        except ValueError:
            return _("the PDF renderer's record is unreadable; the platform job brunna/pdf-renderer rewrites it")
        now = now or datetime.datetime.now().astimezone()
        if (now - since).days > STALE_DAYS:
            return _("the PDF renderer is more than {days} days behind Chrome's stable release; it is refused until the "
                     "platform job brunna/pdf-renderer installs the new one", days=STALE_DAYS)
    return ""


def argv(exe: pathlib.Path, scratch: pathlib.Path, html: pathlib.Path, pdf: pathlib.Path) -> list[str]:
    """The whole command of one print: nothing from the request but the two paths, as a file URI and an output path.
    Chrome's own sandbox can't start inside the platform's (measured), which is the boundary."""
    return [str(exe), "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
            f"--user-data-dir={scratch / 'chrome'}", f"--print-to-pdf={pdf}", pathlib.Path(html).as_uri()]


def env(scratch: pathlib.Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": str(scratch), "TMPDIR": str(scratch) + "/",
            "LANG": os.environ.get("LANG", "en_US.UTF-8")}


# ---------- processes left behind ----------

def sweep(keep_groups=()) -> int:
    """Kill every process whose program is in the renderer's folder and whose group isn't a running job's (a renderer
    that left its group); the number killed."""
    base = str(root().resolve()) + "/"
    try:
        out = subprocess.run(["ps", "-axo", "pid=,pgid=,comm="], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    n = 0
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, pgid, comm = int(parts[0]), int(parts[1]), parts[2]
        if comm.startswith(base) and pgid not in keep_groups and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGKILL)
                n += 1
            except (ProcessLookupError, PermissionError):
                pass
    return n


# ---------- install ----------

def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):
        raise RuntimeError(_("no certificate authorities could be loaded: the download can't be verified"))
    return ctx


def _get(url: str, limit: int, dest=None) -> bytes:
    with urllib.request.urlopen(url, timeout=60, context=_context()) as r:
        if dest is None:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise RuntimeError(_("{url} is larger than {n} bytes", url=url, n=limit))
            return data
        got = 0
        while chunk := r.read(1 << 20):
            got += len(chunk)
            if got > limit:
                raise RuntimeError(_("{url} is larger than {n} bytes", url=url, n=limit))
            dest.write(chunk)
        return b""


def pick(index: dict, plat: str) -> tuple[str, str]:
    """(version, url) of the Stable headless shell for plat, checked: a dotted version and Google's own download host for
    exactly that version and platform."""
    try:
        stable = index["channels"]["Stable"]
        version = stable["version"]
        rows = stable["downloads"]["chrome-headless-shell"]
    except (KeyError, TypeError):
        raise RuntimeError(_("Chrome for Testing's index has no Stable headless shell")) from None
    if not isinstance(version, str) or not VERSION.match(version):
        raise RuntimeError(_("Chrome for Testing's index names an odd version: {v!r}", v=version))
    want = f"{URL_PREFIX}{version}/{plat}/chrome-headless-shell-{plat}.zip"
    urls = [r.get("url") for r in rows if isinstance(r, dict) and r.get("platform") == plat]
    if urls != [want]:
        raise RuntimeError(_("Chrome for Testing's index gives no download at {want}", want=want))
    return version, want


def unpack(archive: pathlib.Path, dest: pathlib.Path) -> None:
    """ditto into dest (new, empty); a link in it that is absolute or leaves dest refuses the version."""
    subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(dest)], check=True, capture_output=True, timeout=600)
    top = dest.resolve()
    for d, dirs, files in os.walk(dest):
        for name in [*dirs, *files]:
            p = pathlib.Path(d) / name
            if p.is_symlink():
                target = os.readlink(p)
                if os.path.isabs(target) or top not in (p.parent / target).resolve().parents:
                    raise RuntimeError(_("the archive holds a link that leaves it: {p}", p=p.relative_to(dest)))


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def fixture_policy(ws: pathlib.Path) -> dict:
    """A synthetic project's policy: a scratch folder as its workspace, every registered project another."""
    import policy
    import registry
    index = registry.refresh()
    return policy.policy("pdf-fixture", {"name": "pdf-fixture", "paths": {"workspace": str(ws)},
                                         "enforce": {"sandbox": True}}, index)


def fixture_prints(folder: pathlib.Path, exe: pathlib.Path) -> str:
    """'' when a fixture page prints under pdf_profile with this copy; else what went wrong."""
    import seatbelt
    top = pathlib.Path(tempfile.mkdtemp(prefix="pdf-fixture-", dir=hubconf.private_tmp()))
    try:
        ws, scratch = top / "ws", top / "scratch"
        ws.mkdir()
        scratch.mkdir()
        (ws / "page.html").write_text(FIXTURE)
        prof = top / "profile.sb"
        prof.write_text(seatbelt.pdf_profile(fixture_policy(ws), folder, scratch))
        out = ws / "page.pdf"
        p = subprocess.Popen([seatbelt.SANDBOX_EXEC, "-f", str(prof), *argv(exe, scratch, ws / "page.html", out)],
                             cwd=ws, env=env(scratch), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            p.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _("the fixture page didn't print in {s} s", s=TIMEOUT_S)
        finally:
            try:   # its own group only: a job's print may be running beside it
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait()
        try:
            ok = out.read_bytes()[:5] == b"%PDF-"
        except OSError:
            ok = False
        return "" if ok else _("the fixture page didn't print (exit {code})", code=p.returncode)
    finally:
        shutil.rmtree(top, ignore_errors=True)


def declared() -> list[str]:
    """The sandboxed projects that declare pdf."""
    return sorted(n for n, d in hubconf.projects().items()
                  if (d.get("enforce") or {}).get("sandbox") is True
                  and "pdf" in ((d.get("enforce") or {}).get("capabilities") or []))


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _save(rec: dict) -> None:
    root().mkdir(parents=True, exist_ok=True)
    hubconf.write_atomic(root() / "current.json", json.dumps(rec, ensure_ascii=False, indent=1))


def install(fetch=_get) -> int:
    plat = platform_name()
    if not declared():
        print(_("no sandboxed project declares pdf: nothing to install"))
        return 0
    if not plat:
        print(_("printing to PDF needs macOS: the platform's runner confines it with Seatbelt"))
        return 0
    index = json.loads(fetch(INDEX, MAX_INDEX))
    version, url = pick(index, plat)
    rec = record()
    rec["checked"], rec["latest"] = _now(), version
    if rec.get("version") == version and current() is not None:
        rec.pop("behind_since", None)
        _save(rec)
        print(_("the PDF renderer is current: {v}", v=version))
        return 0
    if rec.get("version") and not rec.get("behind_since"):
        rec["behind_since"] = _now()
    root().mkdir(parents=True, exist_ok=True)
    folder = root() / version
    staging = pathlib.Path(tempfile.mkdtemp(prefix=f"pdf-renderer-{version}-", dir=hubconf.private_tmp()))
    try:
        archive = staging / "download.zip"
        with open(archive, "wb") as f:
            fetch(url, MAX_ZIP, f)
        unpacked = staging / "unpacked"
        unpacked.mkdir()
        unpack(archive, unpacked)
        exe = _binary_in(unpacked, plat)
        if not exe.is_file() or exe.is_symlink():
            raise RuntimeError(_("the archive has no chrome-headless-shell for {plat}", plat=plat))
        if folder.exists():
            shutil.rmtree(folder)
        os.rename(unpacked, folder)
    except Exception:
        _save(rec)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    exe = _binary_in(folder, plat)
    why = fixture_prints(folder, exe)
    if why:
        rec.setdefault("refused", {})[version] = why
        _save(rec)
        print(_("✗ the PDF renderer {v} was kept aside: {why}", v=version, why=why))
        return 1
    old = rec.get("version")
    rec.update({"version": version, "platform": plat, "sha256": sha256(exe), "installed": _now()})
    rec.pop("behind_since", None)
    rec.pop("refused", None)
    _save(rec)
    for d in root().iterdir():   # the copy before stays (a print may still be running from it); older ones go
        if d.is_dir() and not d.is_symlink() and VERSION.match(d.name) and d.name not in (version, old):
            shutil.rmtree(d, ignore_errors=True)
    print(_("✓ the PDF renderer is now {v}", v=version))
    return 0


def status() -> int:
    rec = record()
    print(_("PDF renderer: {v} (checked {at})", v=rec.get("version") or "-", at=rec.get("checked") or "-"))
    why = refusal()
    print(_("  a print would be refused: {why}", why=why) if why else _("  prints are served"))
    return 0


def main(argv_: list[str]) -> int:
    if argv_ == ["install"]:
        try:
            return install()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
            print(_("✗ the PDF renderer could not be installed: {err}", err=e), file=sys.stderr)
            return 1
    if argv_ == ["status"]:
        return status()
    print("usage: pdf_renderer.py install|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
