"""The key in the status page's address (bin/status-server.py): who made it, who reads it.

The page is served on this machine's 127.0.0.1, which every account on it and every program that can be made to ask it
reaches, so the address holds a key: http://127.0.0.1:18790/<key>/. It is in the platform's secrets folder
(status-page-key, owner only; no sandboxed project reads that folder). Only the server makes it, when it starts and there
is none; `brunna glance` reads it to print the address, and nothing else does (not doctor, whose output goes to agents and
which must never hand it to whatever holds the port).

  read()          the key, or None (no file, unreadable, or not a key)
  ensure()        the same, made first when there is no file; OSError when it can't be
  address(url)    the page's address under url (the local one, or the settings' status_page.url), or None without a key
  answers()       what holds the port, asked without the key: "ours", "page" (it gives this instance's page to anyone),
                  "other" or None (nothing answers)

Standard library + hubconf.
"""
from __future__ import annotations

import errno
import os
import pathlib
import re
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402

PORT = 18790
LOCAL = f"http://127.0.0.1:{PORT}/"   # never "localhost": that is ::1 first, where another account can listen
KEY = re.compile(r"[A-Za-z0-9_-]{43}")
MARK = "X-Brunna-Status-Page"         # on every answer of the server's, the refusals too


def path() -> pathlib.Path:
    return hubconf.secrets_dir() / "status-page-key"


def read() -> str | None:
    try:
        fd = os.open(path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = os.read(fd, 64)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = raw.decode("ascii", "replace").strip()
    return text if KEY.fullmatch(text) else None


def ensure() -> str | None:
    """Whole from the first moment it is there: written beside it, then linked into place, so of two at once the first
    wins and both read that. A file there that holds no key is left as it is (None): whoever put it there sees the 404."""
    if not os.path.lexists(path()):
        path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path().with_name(f".{path().name}.{secrets.token_hex(8)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path())
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
        finally:
            tmp.unlink(missing_ok=True)
    return read()


def address(url: str = LOCAL) -> str | None:
    key = read()
    if not key:
        return None
    base = url.strip().removesuffix("index.html").rstrip("/")
    return base + "/" if base.endswith("/" + key) else f"{base}/{key}/"


def answers(page: pathlib.Path | None = None, timeout: float = 2.0) -> str | None:
    """page: the status page's file. At most timeout in all, whatever answers."""
    import threading
    got: list[str | None] = ["other"]   # it took the connection and never answered whole
    t = threading.Thread(target=lambda: got.__setitem__(0, _ask(page, timeout)), daemon=True)
    t.start()
    t.join(2 * timeout)
    return got[0]


def _ask(page: pathlib.Path | None, timeout: float) -> str | None:
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        c.connect()
    except OSError:
        return None
    try:
        c.request("GET", "/index.html")
        r = c.getresponse()
        if r.status == 404 and r.getheader(MARK):
            return "ours"
        mine = page.read_bytes() if page and r.status == 200 else b""
        return "page" if mine and r.read(len(mine) + 1) == mine else "other"
    except (OSError, http.client.HTTPException):   # it took the connection and said nothing a web server says
        return "other"
    finally:
        c.close()
