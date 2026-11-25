"""The status page's server (bin/status-server.py) and the key in its address (bin/status_key.py): what is answered without
the key, what is served with it, who makes the key and who is told it."""
import contextlib
import http.client
import http.server
import importlib.util
import io
import json
import pathlib
import shutil
import socket
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import glance  # noqa: E402
import hubconf  # noqa: E402
import status_key  # noqa: E402


def load():
    spec = importlib.util.spec_from_file_location("status_server", ROOT / "bin" / "status-server.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.patch(hubconf, "secrets_dir", lambda: self.tmp / "secrets")
        self.patch(hubconf, "data_dir", lambda: self.tmp / "data")

    def patch(self, *a, **kw):
        p = mock.patch.object(*a, **kw)
        self.addCleanup(p.stop)
        return p.start()


class Key(Base):
    def test_made_once_for_the_owner_only(self):
        self.assertIsNone(status_key.read(), "reading makes nothing")
        self.assertFalse((self.tmp / "secrets").exists())
        key = status_key.ensure()
        self.assertRegex(key, r"\A[A-Za-z0-9_-]{43}\Z")
        self.assertEqual(stat.S_IMODE(status_key.path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(status_key.path().parent.stat().st_mode), 0o700)
        self.assertEqual((status_key.ensure(), status_key.read()), (key, key))
        self.assertEqual([p.name for p in status_key.path().parent.iterdir()], ["status-page-key"])

    def test_two_at_once_have_the_same(self):
        got, gate = [], threading.Barrier(8)

        def one():
            gate.wait()
            got.append(status_key.ensure())
        ts = [threading.Thread(target=one) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(set(got)), 1)
        self.assertEqual(len(list(status_key.path().parent.iterdir())), 1)

    def test_what_is_no_key_is_none_and_stays(self):
        status_key.path().parent.mkdir()
        for text in ("", "\n", "short", "x" * 44, "x" * 42 + "/", "é" * 43):
            status_key.path().write_text(text)
            self.assertIsNone(status_key.ensure(), repr(text))
            self.assertEqual(status_key.path().read_text(), text)

    def test_a_link_is_not_followed(self):
        status_key.path().parent.mkdir()
        (self.tmp / "other").write_text("k" * 43)
        status_key.path().symlink_to(self.tmp / "other")
        self.assertIsNone(status_key.ensure())

    def test_address(self):
        self.assertIsNone(status_key.address())
        k = status_key.ensure()
        self.assertEqual(status_key.address(), f"http://127.0.0.1:18790/{k}/")
        for url in ("https://hub.example.ts.net", "https://hub.example.ts.net/", "https://hub.example.ts.net/index.html",
                    f"https://hub.example.ts.net/{k}", f"https://hub.example.ts.net/{k}/"):
            self.assertEqual(status_key.address(url), f"https://hub.example.ts.net/{k}/", url)
        self.assertEqual(status_key.address("https://h.example/brunna/"), f"https://h.example/brunna/{k}/")


class Glance(Base):
    def test_the_address_with_the_key_and_without_one(self):
        url = {"status_page.url": None}
        self.patch(hubconf, "get", lambda k, *a: url.get(k))
        self.assertEqual(glance.status_page(), {"url": "http://127.0.0.1:18790/", "local_to": "hub"})
        url["status_page.url"] = "https://hub.example.ts.net"
        self.assertEqual(glance.status_page(), {"url": "https://hub.example.ts.net", "local_to": None})
        self.assertFalse((self.tmp / "secrets").exists(), "it makes no key")
        k = status_key.ensure()
        self.assertEqual(glance.status_page(), {"url": f"https://hub.example.ts.net/{k}/", "local_to": None})
        url["status_page.url"] = None
        self.assertEqual(glance.status_page(), {"url": f"http://127.0.0.1:18790/{k}/", "local_to": "hub"})

    def test_the_text_says_it_only_with_a_key(self):
        g = {"set_up": True, "needs_you": [], "mail": {"total": 0, "projects": []}, "health": {"state": "ok", "alerts": []},
             "scheduler": {"alive": True}, "status_page": {"url": "http://127.0.0.1:18790/"}}
        self.assertNotIn("18790", glance.text(g))
        status_key.ensure()
        self.assertIn("18790", glance.text(g))


class Served(Base):
    def setUp(self):
        super().setUp()
        self.m = load()
        self.status = self.tmp / "status"
        self.status.mkdir()
        self.m.STATUS = self.status
        for n in ("index.html", "digest.html", "todo.html", "glance.json", "other.html"):
            (self.status / n).write_text(f"PAGE {n}")
        r = self.tmp / "data" / "reflect"
        (r / "trajectory").mkdir(parents=True)
        (r / "index.html").write_text("WEEK")
        (r / "2026-W47.json").write_text("{}")
        (r / "trajectory" / "index.html").write_text("TRAJECTORY")
        (r / "empty").mkdir()
        (self.status / "reflect").symlink_to(r)
        (self.tmp / "outside").mkdir()
        (self.tmp / "outside" / "x.html").write_text("OUTSIDE")
        (r / "link.html").symlink_to(self.tmp / "outside" / "x.html")
        (r / "linked").symlink_to(self.tmp / "outside")
        (r / ".hidden").write_text("HIDDEN")
        self.key = status_key.ensure()
        self.serve()

    def serve(self):
        self.server = self.m.Server(("127.0.0.1", 0))
        t = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def get(self, path, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(c.close)
        c.putrequest(method, path, skip_host="Host" in (headers or {}))
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        return r.status, r.read().decode("utf-8", "replace"), r

    def raw(self, data: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            out = b""
            try:
                s.sendall(data)
                s.shutdown(socket.SHUT_WR)
                while chunk := s.recv(65536):
                    out += chunk
            except ConnectionError:   # closed with some of what was sent unread
                pass
            return out

    def test_without_the_key_every_path_is_the_same_404(self):
        bodies = set()
        wrong = "x" * 43
        for p in ("/", "/index.html", "/glance.json", "/reflect/", f"/{wrong}/", f"/{wrong}", "//", f"/x/../{self.key}/",
                  f"/{self.key[:-1]}/", f"/{self.key.swapcase()}/", f"/%2e/{self.key}/", "/%C3%A9/", "/?" + self.key, f"/?/{self.key}/",
                  f"http://127.0.0.1/{self.key}/"):
            code, body, r = self.get(p)
            self.assertEqual((code, "PAGE" in body), (404, False), p)
            self.assertEqual(r.getheader(status_key.MARK), "1", p)
            self.assertNotIn(self.key, body + str(r.getheaders()), p)
            bodies.add(body)
        self.assertEqual(len(bodies), 1)
        self.assertIn("brunna glance", bodies.pop())

    def test_headers_change_nothing(self):
        for h in ({"Host": "other.example"}, {"Cookie": f"key={self.key}"}, {"Authorization": f"Bearer {self.key}"}, {"Referer": f"http://127.0.0.1/{self.key}/"},
                  {"X-Forwarded-For": "127.0.0.1"}, {"Tailscale-User-Login": "owner@example.com"}):
            self.assertEqual(self.get("/", headers=h)[0], 404, h)
        self.assertEqual(self.get(f"/{self.key}/", headers={"Host": "hub.example.ts.net"})[0], 200, "a proxy keeps the browser's name")

    def test_no_key_at_all_opens_nothing(self):
        for text in (None, "", "\n", "short"):
            status_key.path().unlink(missing_ok=True)
            if text is not None:
                status_key.path().write_text(text)
            for p in ("/", "//", "/index.html", f"/{self.key}/", "/short/"):
                self.assertEqual(self.get(p)[0], 404, (text, p))

    def test_a_new_key_counts_at_once(self):
        status_key.path().unlink()
        new = status_key.ensure()
        self.assertEqual((self.get(f"/{self.key}/")[0], self.get(f"/{new}/")[0]), (404, 200))

    def test_the_pages_and_the_weekly_review(self):
        k = f"/{self.key}"
        for p, want in (("/", "PAGE index.html"), ("/index.html", "PAGE index.html"), ("/digest.html", "PAGE digest.html"), ("/todo.html?x=1", "PAGE todo.html"),
                        ("/reflect/", "WEEK"), ("/reflect/index.html", "WEEK"), ("/reflect/2026-W47.json", "{}"),
                        ("/reflect/trajectory/", "TRAJECTORY")):
            code, body, r = self.get(k + p)
            self.assertEqual((code, body), (200, want), p)
            self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
            self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertTrue(self.get(k + "/")[2].getheader("Content-Type").startswith("text/html"))

    def test_a_folder_without_its_slash_is_a_relative_redirect(self):
        for p, to in ((f"/{self.key}", f"{self.key}/"), (f"/{self.key}/reflect", "reflect/"), (f"/{self.key}/reflect/trajectory", "trajectory/")):
            code, _body, r = self.get(p)
            self.assertEqual((code, r.getheader("Location")), (302, to))

    def test_nothing_else_is_served(self):
        k = f"/{self.key}/"
        for p in ("glance.json", "GLANCE.JSON", "Index.HTML", "other.html", "../secrets/status-page-key", "%2e%2e/secrets/status-page-key", "reflect/../glance.json",
                  "reflect/%2e%2e/%2e%2e/secrets/status-page-key", "reflect/..%2f..%2fsecrets%2fstatus-page-key", "reflect/link.html", "reflect/linked/x.html",
                  "reflect/linked/", "reflect/linked", "reflect/.hidden", "reflect/empty/", "reflect//index.html", "reflect/index.html/", "reflect/%00", "/etc/passwd", "%2fetc/passwd",
                  "reflect/%2fetc/passwd", "reflect/%ff", "reflect/..%5c..%5cx", "index.html/"):
            code, body, _r = self.get(k + p)
            self.assertIn(code, (404,), p)
            self.assertNotRegex(body, "PAGE|OUTSIDE|HIDDEN|WEEK", p)

    def test_head_and_the_other_methods(self):
        code, body, r = self.get(f"/{self.key}/", "HEAD")
        self.assertEqual((code, body, r.getheader("Content-Length")), (200, "", str(len("PAGE index.html"))))
        for m in ("POST", "PUT", "DELETE", "OPTIONS", "CONNECT"):
            code, body, _r = self.get(f"/{self.key}/", m)
            self.assertEqual((code, body), (501, ""), m)

    def test_nothing_of_a_request_is_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            self.get(f"/{self.key}/")
            self.get("/%ff%fe")
            for data in (f"GET /{self.key}/ HTTP/9\r\n\r\n", f"BAD /{self.key}/\r\n\r\n", f"{self.key}\r\n\r\n", "GET /\xe9 HTTP/1.0\r\n\r\n", "", "\r\n"):
                self.raw(data.encode("latin-1"))
            self.raw(b"GET /" + b"a" * 70000 + b" HTTP/1.0\r\n\r\n")
        self.assertEqual(err.getvalue(), "")

    def test_connections_held_open_dont_stop_the_page_for_good(self):
        self.held({"IDLE": 1})

    def test_connections_that_say_a_little_now_and_then_neither(self):
        self.held({"WHOLE": 2}, drip=True)

    def held(self, limits, drip=False):
        import time
        self.patch(self.m.Handler, "timeout", limits.get("IDLE", 30))
        self.patch(self.m, "WHOLE_S", limits.get("WHOLE", 60))
        held = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(self.m.MAX_OPEN + 5)]
        self.addCleanup(lambda: [s.close() for s in held])
        stop = threading.Event()
        self.addCleanup(stop.set)

        def feed():
            while not stop.wait(0.2):
                for s in held:
                    try:
                        s.send(b"x")
                    except OSError:
                        pass
        if drip:
            threading.Thread(target=feed, daemon=True).start()
        end = time.time() + 15
        while time.time() < end:
            try:
                if self.get(f"/{self.key}/")[0] == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.2)
        self.fail("the page never came back")


class Answers(Base):
    """doctor's question of the port, which is asked without the key."""

    def port(self, handler):
        s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(s.server_close)
        self.addCleanup(s.shutdown)
        self.patch(status_key, "PORT", s.server_address[1])

    def test_nothing_there(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.patch(status_key, "PORT", s.getsockname()[1])
            self.assertIsNone(status_key.answers())

    def test_the_server_and_an_older_one(self):
        key, seen = status_key.ensure(), []
        m = load()
        m.STATUS = self.tmp
        self.port(m.Handler)
        self.assertEqual(status_key.answers(), "ours")

        class Old(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path + str(self.headers))
                self.send_response(200)
                self.end_headers()
        self.port(Old)
        self.assertEqual(status_key.answers(), "other")
        page = self.tmp / "index.html"
        page.write_text("THE PAGE")
        self.assertEqual(status_key.answers(page), "other")

        class Gives(Old):
            def do_GET(self):
                super().do_GET()
                self.wfile.write(b"THE PAGE")
        self.port(Gives)
        self.assertEqual(status_key.answers(page), "page")
        page.write_text("THE PAGE, AND MORE")
        self.assertEqual(status_key.answers(page), "other")
        self.assertTrue(seen)
        self.assertNotIn(key, "".join(seen))

    def test_doctor_says_it_only_of_another_and_never_the_key(self):
        loader = importlib.machinery.SourceFileLoader("agenthub_status", str(ROOT / "bin" / "agenthub"))
        hub = importlib.util.module_from_spec(importlib.util.spec_from_loader("agenthub_status", loader))
        loader.exec_module(hub)
        self.patch(hubconf, "STATE", self.tmp / "state")
        rows = []

        def doctor(got):
            rows.clear()
            side = AssertionError("not asked") if got == "never" else None
            with mock.patch.object(status_key, "answers", return_value=got, side_effect=side):
                hub.doctor_status_page(lambda *a: rows.append(a))
            return [r[0] for r in rows]
        self.assertEqual(doctor("never"), [], "no key, no page: another instance's or nobody's")
        (self.tmp / "state" / "status").mkdir(parents=True)
        (self.tmp / "state" / "status" / "index.html").write_text("x")
        for got, want in (("ours", []), (None, []), ("other", []), ("page", ["warn"])):
            self.assertEqual(doctor(got), want, got)
        (self.tmp / "state" / "services").mkdir()
        (self.tmp / "state" / "services" / "status-page.json").write_text("{}")
        with mock.patch("code_watch.record", return_value={"pid": 1}):
            self.assertEqual(doctor("ours"), ["bad"], "its key was deleted")
        key = status_key.ensure()
        for got, want in (("ours", []), (None, []), ("other", ["warn"]), ("page", ["warn"])):
            self.assertEqual(doctor(got), want, got)
            self.assertNotIn(key, str(rows))
        status_key.path().write_text("not one")
        self.assertEqual(doctor("ours"), ["bad"])


class WhoHoldsIt(unittest.TestCase):
    def test_only_the_server_makes_it_and_only_glance_prints_it(self):
        """bin/ as text: what reads or makes the key is these, so a new user of it is seen here."""
        users = sorted(f.name for f in (ROOT / "bin").iterdir() if f.is_file() and f.name != "status_key.py"
                       and "status_key." in f.read_text(errors="replace") and f.name != "registry.py")   # its port only, in a comment
        self.assertEqual(users, ["agenthub", "glance.py", "status-server.py"])
        self.assertEqual([n for n in users if "status_key.ensure" in (ROOT / "bin" / n).read_text()], ["status-server.py"])
        self.assertNotIn("status_key.address", (ROOT / "bin" / "agenthub").read_text())

    def test_a_sandboxed_project_reads_no_secret_of_the_platform(self):
        import policy
        index = {"projects": {"p": {"name": "p", "paths": {"workspace": "/w/p"}, "enforce": {"sandbox": True}}}}
        pol = policy.policy("p", index["projects"]["p"], index)
        self.assertTrue(any(pathlib.Path(x) == hubconf.secrets_dir() or pathlib.Path(x) in status_key.path().parents
                            for x in pol["deny_read"]), json.dumps([str(x) for x in pol["deny_read"]])[:500])


if __name__ == "__main__":
    unittest.main()
