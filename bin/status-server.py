#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The status page's server (service local.status-page): .state/status, on 127.0.0.1 only, under an address with a key in it.

It answers GET and HEAD under /<key>/ (status_key.py) and nothing else: any other path is the same 404, which says where the
address is found. No cookie and no header is asked about, so ssh -L and a proxy in front (tailscale serve) work as they
are. Served are the pages gen-status-page.py writes (PAGES) and the weekly review's folder, plain files only: no link, no
listing, no other file of that folder. The key is read for each request: a new one counts at once.

The key is in the path: no request is logged, and nothing is told where a link was followed from (Referrer-Policy; the
weekly review loads fonts from outside).

It exits when the checkout's code has moved on (code_watch), and the service manager starts it again.

  bin/py bin/status-server.py        (by hand, without the service: Ctrl-C ends it)
"""
from __future__ import annotations

import hmac
import http.server
import mimetypes
import pathlib
import socket
import socketserver
import sys
import threading
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import code_watch  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import nofollow  # noqa: E402
import status_key  # noqa: E402

_ = i18n.domain(__file__)
STATUS = hubconf.STATE / "status"
PAGES = ("index.html", "digest.html", "todo.html")
REVIEW = "reflect"       # gen-status-page.REFLECT_LINK: the weekly review, <data>/reflect
MAX_FILE = 32 << 20
MAX_OPEN = 32            # connections at once; more are closed
IDLE_S = 20              # a connection that says nothing
WHOLE_S = 60             # a connection in all, whatever it says
FOLLOW_S = 30            # how often it looks whether the code moved on
PRELOAD = ()             # platform modules imported inside functions (code_watch.preload): none


def target(rest: str) -> tuple[pathlib.Path, str] | str | None:
    """What a path below the key names: (folder, the file's path in it), the relative place to go to instead (a folder
    asked for without its slash), or None."""
    parts = rest.split("/")
    if rest == "":
        return STATUS, "index.html"
    if any(p in (".", "..") or p.startswith(".") or "\0" in p or "\\" in p for p in parts) or "" in parts[:-1]:
        return None
    if len(parts) == 1 and parts[0] in PAGES:
        return STATUS, parts[0]
    if parts[0] != REVIEW:
        return None
    if len(parts) == 1:
        return f"{REVIEW}/"
    base, rel = hubconf.data_file(REVIEW), "/".join(parts[1:])
    if rel == "" or rel.endswith("/"):
        return base, rel + "index.html"
    try:
        if nofollow.is_dir(base, rel):
            return f"{urllib.parse.quote(parts[-1])}/"
    except OSError:
        return None
    return base, rel


class Handler(http.server.BaseHTTPRequestHandler):
    server_version, sys_version = "brunna", ""
    timeout = IDLE_S

    def setup(self) -> None:
        super().setup()
        self.end = threading.Timer(WHOLE_S, self.cut)
        self.end.daemon = True
        self.end.start()

    def cut(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self) -> None:
        self.end.cancel()
        super().finish()

    def log_message(self, *args) -> None:
        pass   # the path holds the key

    def send_error(self, code, message=None, explain=None) -> None:
        self.answer(code, "text/plain; charset=utf-8", b"")

    def answer(self, code: int, kind: str, body: bytes, to: str | None = None) -> None:
        self.send_response(code)
        for k, v in ((status_key.MARK, "1"), ("Content-Type", kind), ("Content-Length", str(len(body))),
                     ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                     ("Content-Security-Policy", "frame-ancestors 'none'"), ("X-Frame-Options", "DENY"),
                     *([("Location", to)] if to else [])):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def not_here(self) -> None:
        self.answer(404, "text/plain; charset=utf-8", (_("There is no page at this address. The status page's own address has a key "
                                                         "in it: `brunna glance`, on the machine that runs brunna, prints it.") + "\n").encode())

    def do_GET(self) -> None:
        key = status_key.read()
        first, slash, rest = self.path.partition("?")[0].removeprefix("/").partition("/")
        if not key or not self.path.startswith("/") or not hmac.compare_digest(first.encode("utf-8", "surrogateescape"), key.encode()):
            return self.not_here()
        if not slash:
            return self.answer(302, "text/plain", b"", f"{key}/")
        try:
            got = target(urllib.parse.unquote(rest, errors="strict"))
        except UnicodeDecodeError:
            got = None
        if isinstance(got, str):
            return self.answer(302, "text/plain", b"", got)
        try:
            body = nofollow.read(*got, cap=MAX_FILE) if got else None
        except (OSError, ValueError):
            body = None
        if body is None:
            return self.not_here()
        kind = mimetypes.guess_type(got[1])[0] or "application/octet-stream"
        self.answer(200, f"{kind}; charset=utf-8" if kind.startswith("text/") or kind == "application/json" else kind, body)

    do_HEAD = do_GET


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 2 * MAX_OPEN   # the default, 5, resets a browser's connections on a busy machine

    def __init__(self, address=("127.0.0.1", status_key.PORT)):
        self.room = threading.BoundedSemaphore(MAX_OPEN)
        super().__init__(address, Handler)

    def server_bind(self) -> None:   # http.server's own looks this machine's name up, for nothing this server uses
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address) -> None:
        if not self.room.acquire(blocking=False):
            return self.shutdown_request(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.room.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.room.release()

    def handle_error(self, request, client_address) -> None:
        pass   # a client that went away; a traceback could hold the path


def main() -> int:
    if len(sys.argv) > 1:
        print(_(__doc__).strip())
        return 0 if sys.argv[1] in ("-h", "--help") else 2
    try:
        if not status_key.ensure():
            print(_("the status page's key ({path}) is not a key: the page can't be opened until that file is deleted and this is started again",
                    path=status_key.path()), file=sys.stderr, flush=True)
    except OSError as e:
        print(_("the status page's key ({path}) could not be made: {why}", path=status_key.path(), why=e), file=sys.stderr, flush=True)
        return 1
    try:
        server = Server()
    except OSError as e:
        print(_("127.0.0.1:{port} could not be listened on: {why}", port=status_key.PORT, why=e), file=sys.stderr, flush=True)
        return 1
    watch = code_watch.Watch("status-page")
    code_watch.preload(PRELOAD)
    stop = threading.Event()

    def follow() -> None:
        while not stop.wait(FOLLOW_S):
            if watch.stale():
                server.shutdown()
                return
    threading.Thread(target=follow, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
