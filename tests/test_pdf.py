"""A project's page printed to PDF by the platform (docs/design/capabilities.md, "pdf" and its r1): the request's checks
in the runner, a capability that admits only its kind, the profile, and the renderer's install and age.

The runner's run is tested with a stand-in renderer under an allow-all profile (whether the profile blocks is what the
profile's own tests and the measurement in the design say); the real print runs only with BRUNNA_PDF_RENDERER set to a
chrome-headless-shell binary.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import hubconf  # noqa: E402
import pdf_renderer  # noqa: E402
import seatbelt  # noqa: E402


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "bin" / file))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


cr = load("cap_runner_pdf", "cap-runner.py")
MAC = sys.platform == "darwin" and bool(shutil.which("sandbox-exec"))
V = "158.0.8127.36"


def index(version=V, plat="mac-arm64", url=None):
    url = url or f"{pdf_renderer.URL_PREFIX}{version}/{plat}/chrome-headless-shell-{plat}.zip"
    return {"channels": {"Stable": {"version": version, "downloads": {"chrome-headless-shell": [
        {"platform": plat, "url": url}, {"platform": "linux64", "url": "https://example.invalid/x.zip"}]}}}}


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class Catalog(Temp):
    def test_a_kind_capability_needs_no_fragment_and_admits_only_its_name(self):
        cat = {"pdf": {"kind": "pdf"}, "gpu": {}, "other": {"kind": "other"}, "_comment": "x"}
        with mock.patch.object(hubconf, "get", lambda k, *a: cat if k == "capabilities" else None):
            self.assertEqual(set(hubconf.capabilities()), {"pdf", "gpu"},
                             "pdf has no .sb yet exists; a kind the platform doesn't know is no capability")
            self.assertTrue(hubconf.kind_only("pdf"))
            self.assertFalse(hubconf.kind_only("gpu"))
        with mock.patch.object(hubconf, "get", lambda k, *a: {"pdf": {}} if k == "capabilities" else None):
            self.assertEqual(hubconf.capabilities(), {}, "pdf without its kind would need a fragment, which there isn't")
            self.assertFalse(hubconf.kind_only("pdf"))


class Checks(Temp):
    def setUp(self):
        super().setUp()
        self.ws = self.tmp / "ws"
        (self.ws / "docs").mkdir(parents=True)
        (self.ws / "docs" / "page.html").write_text("<p>x</p>")
        for target, value in [("kind_only", lambda name, entry=None: name == "pdf")]:
            p = mock.patch.object(hubconf, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(pdf_renderer, "refusal", lambda now=None: "")
        p.start()
        self.addCleanup(p.stop)

    def check(self, caps=("pdf",), **req):
        return cr.check({"v": 1, "id": "j1", **req}, "j1", self.ws, list(caps))

    def test_a_good_request(self):
        why, clean = self.check(kind="pdf", html="docs/page.html", pdf="out/../docs/page.pdf")
        self.assertEqual(why, "")
        self.assertEqual(clean, {"kind": "pdf", "html": self.ws / "docs" / "page.html",
                                 "pdf": self.ws / "docs" / "page.pdf", "timeout_s": pdf_renderer.TIMEOUT_S})

    def test_refusals(self):
        outside = self.tmp / "secret.html"
        outside.write_text("s")
        (self.ws / "link.html").symlink_to(outside)
        (self.ws / "dir.html").mkdir()
        (self.ws / "notes.txt").write_text("n")
        (self.ws / "named.html").symlink_to("notes.txt")
        cases = {
            "not declared": dict(caps=("gpu",), kind="pdf", html="docs/page.html", pdf="a.pdf"),
            "absolute input": dict(kind="pdf", html=str(self.ws / "docs" / "page.html"), pdf="a.pdf"),
            "input outside": dict(kind="pdf", html="../secret.html", pdf="a.pdf"),
            "input through a link out": dict(kind="pdf", html="link.html", pdf="a.pdf"),
            "input not a page": dict(kind="pdf", html="docs/page.txt", pdf="a.pdf"),
            "input a folder": dict(kind="pdf", html="dir.html", pdf="a.pdf"),
            "input a page's name on a text file": dict(kind="pdf", html="named.html", pdf="a.pdf"),
            "input missing": dict(kind="pdf", html="docs/none.html", pdf="a.pdf"),
            "output outside": dict(kind="pdf", html="docs/page.html", pdf="../a.pdf"),
            "output not a pdf": dict(kind="pdf", html="docs/page.html", pdf="a.html"),
            "output absolute": dict(kind="pdf", html="docs/page.html", pdf="/tmp/a.pdf"),
            "a NUL": dict(kind="pdf", html="docs/page.html\0", pdf="a.pdf"),
            "no paths": dict(kind="pdf"),
        }
        for name, req in cases.items():
            with self.subTest(name):
                why, clean = self.check(**req)
                self.assertTrue(why, name)
                self.assertEqual(clean, {})

    def test_the_renderers_refusal_is_the_requests(self):
        with mock.patch.object(pdf_renderer, "refusal", lambda now=None: "too old"):
            self.assertEqual(self.check(kind="pdf", html="docs/page.html", pdf="a.pdf"), ("too old", {}))

    def test_pdf_never_runs_a_command(self):
        why, clean = self.check(cap="pdf", argv=["/bin/sh", "-c", "id"], cwd=".")
        self.assertIn("pdf", why)
        self.assertEqual(clean, {})

    def test_pdf_never_runs_a_command_whatever_the_catalog_says(self):
        with mock.patch.object(hubconf, "kind_only", lambda name, entry=None: False):
            why, clean = self.check(cap="pdf", argv=["/bin/sh", "-c", "id"], cwd=".")
        self.assertIn("pdf", why)
        self.assertEqual(clean, {})

    def test_an_unknown_kind_is_refused(self):
        why, clean = self.check(caps=("gpu",), kind="shell", cap="gpu", argv=["/bin/sh"], cwd=".")
        self.assertIn("shell", why)
        self.assertEqual(clean, {})


class Profile(Temp):
    def pol(self, **over):
        return {"workspace": str(self.tmp / "ws"), "other_projects": [], "credentials": [], "deny_read": [],
                "credential_files": [], "platform_state": [], "protect": [], "protect_roots": [], **over}

    def test_what_it_holds(self):
        (self.tmp / "ws").mkdir()
        r, s = self.tmp / "renderer", self.tmp / "scratch"
        text = seatbelt.pdf_profile(self.pol(), r, s)
        lines = text.splitlines()
        self.assertIn("(deny network*)", lines)
        self.assertIn("(deny user-preference-write)", lines)
        self.assertTrue(any("com.apple.pasteboard" in x and x.startswith("(deny mach-lookup") for x in lines))
        guard = lines.index("(deny process-exec)")
        self.assertEqual(lines[guard + 1], f'(allow process-exec (subpath "{r.resolve()}"))',
                         "no program runs but the renderer's own")
        self.assertNotIn("/private/tmp", next(x for x in lines if x.startswith("(allow file-write*")),
                         "the shared temp areas aren't writable")
        deny_home = next(i for i, x in enumerate(lines) if x.startswith("(deny file-read*") and "/Users" in x)
        allow_back = next(i for i, x in enumerate(lines) if x.startswith("(allow file-read*") and str(r.resolve()) in x)
        self.assertLess(deny_home, allow_back, "the allow comes after the home's deny (the last rule wins)")

    def test_the_git_fence_stays(self):
        (self.tmp / "ws").mkdir()
        fence = {"roots": [str(self.tmp / "ws")], "paths": [], "names": ["config"]}
        with mock.patch.object(seatbelt, "git_fence_rules", lambda pol: ["; FENCE"] if pol.get("git_fence") else []):
            text = seatbelt.pdf_profile(self.pol(git_fence=fence), self.tmp / "r", self.tmp / "s")
        self.assertIn("; FENCE", text)


class Index(unittest.TestCase):
    def test_pick(self):
        self.assertEqual(pdf_renderer.pick(index(), "mac-arm64"),
                         (V, f"{pdf_renderer.URL_PREFIX}{V}/mac-arm64/chrome-headless-shell-mac-arm64.zip"))
        for bad in (index(version="158.0.x"), index(url="https://example.com/x.zip"),
                    index(url=f"http://storage.googleapis.com/chrome-for-testing-public/{V}/mac-arm64/"
                              "chrome-headless-shell-mac-arm64.zip"),
                    index(plat="mac-x64"), {"channels": {}}, []):
            with self.subTest(bad=str(bad)[:80]), self.assertRaises(RuntimeError):
                pdf_renderer.pick(bad, "mac-arm64")


class Renderer(Temp):
    def setUp(self):
        super().setUp()
        for target, value in [("root", lambda: self.tmp / "tools"), ("platform_name", lambda: "mac-arm64")]:
            p = mock.patch.object(pdf_renderer, target, value)
            p.start()
            self.addCleanup(p.stop)

    def fake_copy(self, version=V):
        exe = self.tmp / "tools" / version / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        import datetime
        (self.tmp / "tools" / "current.json").write_text(json.dumps(
            {"version": version, "platform": "mac-arm64",
             "checked": datetime.datetime.now().astimezone().isoformat()}))
        return exe

    def test_refused_with_no_copy(self):
        self.assertIn("brunna/pdf-renderer", pdf_renderer.refusal())

    def test_refused_once_behind_too_long(self):
        self.fake_copy()
        self.assertEqual(pdf_renderer.refusal(), "")
        import datetime
        now = datetime.datetime.now().astimezone()
        rec = json.loads((self.tmp / "tools" / "current.json").read_text())
        for days, refused in ((3, False), (pdf_renderer.STALE_DAYS + 1, True)):
            rec["behind_since"] = (now - datetime.timedelta(days=days)).isoformat()
            (self.tmp / "tools" / "current.json").write_text(json.dumps(rec))
            self.assertEqual(bool(pdf_renderer.refusal(now)), refused, days)

    def test_refused_when_not_checked_for_too_long(self):
        self.fake_copy()
        import datetime
        now = datetime.datetime.now().astimezone()
        rec = json.loads((self.tmp / "tools" / "current.json").read_text())
        for days, refused in ((1, False), (pdf_renderer.STALE_DAYS + 1, True)):
            rec["checked"] = (now - datetime.timedelta(days=days)).isoformat()
            (self.tmp / "tools" / "current.json").write_text(json.dumps(rec))
            self.assertEqual(bool(pdf_renderer.refusal(now)), refused, days)

    def test_a_link_as_the_binary_is_no_copy(self):
        exe = self.fake_copy()
        exe.unlink()
        exe.symlink_to("/bin/sh")
        self.assertIsNone(pdf_renderer.current())

    def test_the_command_takes_only_the_paths(self):
        a = pdf_renderer.argv(pathlib.Path("/r/x"), pathlib.Path("/s"), pathlib.Path("/w/a #?%.html"),
                              pathlib.Path("/w/o.pdf"))
        self.assertEqual(a[-1], "file:///w/a%20%23%3F%25.html")
        self.assertEqual(a[-2], "--print-to-pdf=/w/o.pdf")
        self.assertIn("--user-data-dir=/s/chrome", a)


@unittest.skipUnless(sys.platform == "darwin", "ditto is macOS's")
class Install(Temp):
    def setUp(self):
        super().setUp()
        for target, value in [("root", lambda: self.tmp / "tools"), ("platform_name", lambda: "mac-arm64"),
                              ("declared", lambda: ["p"]), ("fixture_prints", lambda folder, exe: "")]:
            p = mock.patch.object(pdf_renderer, target, value)
            p.start()
            self.addCleanup(p.stop)

    def archive(self, link=None):
        src = self.tmp / "src"
        d = src / "chrome-headless-shell-mac-arm64"
        d.mkdir(parents=True)
        (d / "chrome-headless-shell").write_text("#!/bin/sh\n")
        (d / "chrome-headless-shell").chmod(0o755)
        if link:
            (d / "lib.dylib").symlink_to(link)
        z = self.tmp / f"a{time.time_ns()}.zip"
        subprocess.run(["/usr/bin/ditto", "-c", "-k", str(src), str(z)], check=True)
        shutil.rmtree(src)
        return z.read_bytes()

    def fetch(self, data, idx=None):
        def f(url, limit, dest=None):
            if url == pdf_renderer.INDEX:
                return json.dumps(idx or index()).encode()
            dest.write(data)
            return b""
        return f

    def test_installs_then_says_current(self):
        self.assertEqual(pdf_renderer.install(self.fetch(self.archive())), 0)
        rec = pdf_renderer.record()
        self.assertEqual(rec["version"], V)
        self.assertEqual(len(rec["sha256"]), 64)
        self.assertIsNotNone(pdf_renderer.current())
        self.assertEqual(pdf_renderer.install(self.fetch(b"")), 0, "the same version again downloads nothing")
        self.assertNotIn("behind_since", pdf_renderer.record())

    def test_a_link_leaving_the_archive_refuses_it(self):
        with self.assertRaises(RuntimeError):
            pdf_renderer.install(self.fetch(self.archive(link="/etc/hosts")))
        self.assertIsNone(pdf_renderer.current())

    def test_a_version_that_doesnt_print_is_kept_aside(self):
        pdf_renderer.install(self.fetch(self.archive()))
        new = "159.0.1.2"
        with mock.patch.object(pdf_renderer, "fixture_prints", lambda folder, exe: "no pdf"):
            self.assertEqual(pdf_renderer.install(self.fetch(self.archive(), index(version=new))), 1)
        rec = pdf_renderer.record()
        self.assertEqual(rec["version"], V, "the old copy stays current")
        self.assertIn("behind_since", rec)
        self.assertEqual(rec["refused"], {new: "no pdf"})

    def test_the_clock_starts_even_when_the_download_fails(self):
        pdf_renderer.install(self.fetch(self.archive()))

        def broken(url, limit, dest=None):
            if url == pdf_renderer.INDEX:
                return json.dumps(index(version="159.0.1.2")).encode()
            raise OSError("down")
        with self.assertRaises(OSError):
            pdf_renderer.install(broken)
        self.assertIn("behind_since", pdf_renderer.record())

    def test_an_entry_under_a_link_is_refused_before_anything_is_written(self):
        import zipfile
        z = self.tmp / "evil.zip"
        with zipfile.ZipFile(z, "w") as f:
            link = zipfile.ZipInfo("chrome-headless-shell-mac-arm64/out")
            link.external_attr = (0o120777 << 16)
            f.writestr(link, str(self.tmp / "escape"))
            f.writestr("chrome-headless-shell-mac-arm64/out/x", "x")
        dest = self.tmp / "dest"
        dest.mkdir()
        with self.assertRaises(RuntimeError):
            pdf_renderer.unpack(z, dest)
        self.assertEqual(list(dest.iterdir()), [])
        self.assertFalse((self.tmp / "escape").exists())

    def test_nothing_without_a_declaring_project(self):
        with mock.patch.object(pdf_renderer, "declared", lambda: []):
            self.assertEqual(pdf_renderer.install(lambda *a: self.fail("fetched")), 0)


@unittest.skipUnless(MAC, "needs a mac (sandbox-exec)")
class RunnerRuns(Temp):
    """The runner's plumbing for a print: a stand-in renderer under an allow-all profile."""

    def setUp(self):
        super().setUp()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        (self.ws / "page.html").write_text("<p>x</p>")
        folder = self.tmp / "renderer"
        folder.mkdir()
        exe = folder / "fake"
        # writes "%PDF-" to the --print-to-pdf= path, and says where its HOME is
        exe.write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in --print-to-pdf=*) printf %%PDF- > "${a#--print-to-pdf=}";; '
                       'esac; done\necho "home=$HOME"\n')
        exe.chmod(0o755)
        allow = "(version 1)(allow default)"
        self.projects = {"p": {"dirs": [self.ws], "caps": ["pdf"], "models": {}}}
        patches = [(cr, "eligible", lambda: self.projects), (cr, "DIR", self.tmp / "state"),
                   (cr, "LOG", self.tmp / "state" / "log.jsonl"), (cr, "exclude_hub_run", lambda project, ws: None),
                   (hubconf, "kind_only", lambda name, entry=None: name == "pdf"),
                   (pdf_renderer, "refusal", lambda now=None: ""), (pdf_renderer, "current", lambda: (folder, exe)),
                   (pdf_renderer, "sweep", lambda keep=(): 0), (seatbelt, "pdf_profile", lambda *a: allow)]
        import policy
        patches.append((policy, "for_project", lambda name: {"workspace": str(self.ws)}))
        for obj, target, value in patches:
            p = mock.patch.object(obj, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.runner = cr.Runner()

    def run_request(self, ws, job_id="j1"):
        d = ws / ".hub-run" / "requests"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{job_id}.json").write_text(json.dumps({"v": 1, "id": job_id, "kind": "pdf", "html": "page.html",
                                                      "pdf": "o.pdf"}))
        t0 = time.time()
        st = None
        while time.time() - t0 < 15:
            self.runner.tick()
            try:
                st = json.loads((ws / ".hub-run" / "jobs" / job_id / "state.json").read_text())
            except OSError:
                st = None
            if st and st["state"] in cr.TERMINAL:
                return st
            time.sleep(0.1)
        return st

    def test_a_failed_start_closes_its_files_and_removes_nothing_it_didnt_make(self):
        fds = len(os.listdir("/dev/fd"))
        theirs = self.tmp / "state" / "p" / "j1.d"
        theirs.mkdir(parents=True)
        (theirs / "keep").write_text("k")
        st = self.run_request(self.ws)
        self.assertEqual(st.get("state"), "rejected", st)
        self.assertEqual(len(os.listdir("/dev/fd")), fds)
        self.assertTrue((theirs / "keep").exists(), "a scratch folder this job didn't make is left alone")

    def test_a_request_from_another_folder_doesnt_print(self):
        other = self.tmp / "runtime"
        other.mkdir()
        (other / "page.html").write_text("<p>x</p>")
        self.projects["p"]["dirs"].append(other)
        st = self.run_request(other)
        self.assertEqual(st.get("state"), "rejected", st)
        self.assertFalse((other / "o.pdf").exists())

    def test_prints_and_cleans_its_scratch(self):
        st = self.run_request(self.ws)
        self.assertEqual((st or {}).get("state"), "done", st)
        self.assertEqual(st.get("exit"), 0)
        self.assertEqual((self.ws / "o.pdf").read_bytes(), b"%PDF-")
        out = (self.ws / ".hub-run" / "jobs" / "j1" / "stdout").read_text()
        scratch = self.tmp / "state" / "p" / "j1.d"
        self.assertIn(f"home={scratch}", out, "HOME is the platform's scratch, not the project's folder")
        self.assertFalse(scratch.exists(), "the scratch goes when the job ends")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("cc"), "proc_pidpath is macOS's; needs a C compiler")
class Sweep(Temp):
    """Strays are found by the program the kernel runs, not by what they call themselves; the installer's fixture and
    running jobs are left alone."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(pdf_renderer, "root", lambda: self.tmp / "tools")
        p.start()
        self.addCleanup(p.stop)
        (self.tmp / "tools").mkdir()
        # a program of its own in the folder (a copy of a system binary is killed at start on macOS)
        src = self.tmp / "sleeper.c"
        src.write_text("#include <unistd.h>\nint main(void){for(;;)pause();}\n")
        self.exe = self.tmp / "tools" / "sleeper"
        subprocess.run([shutil.which("cc"), "-o", str(self.exe), str(src)], check=True, capture_output=True)

    def start(self, argv0=None):
        p = subprocess.Popen([str(argv0 or self.exe)], executable=str(self.exe), start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.3)
        return p

    def test_a_stray_is_killed_even_under_another_name(self):
        p = self.start(argv0="/usr/bin/innocent")
        self.assertEqual(pdf_renderer.exe_path(p.pid), str(self.exe))
        self.assertEqual(pdf_renderer.sweep(), 1)
        self.assertEqual(p.wait(timeout=5), -9)

    def test_a_running_jobs_group_and_the_fixture_are_kept(self):
        job, fixture = self.start(), self.start()
        (self.tmp / "tools" / "fixture.json").write_text(json.dumps({"pgid": fixture.pid}))
        self.assertEqual(pdf_renderer.sweep({job.pid}), 0)
        self.assertIsNone(job.poll())
        self.assertIsNone(fixture.poll())


@unittest.skipUnless(MAC and os.environ.get("BRUNNA_PDF_RENDERER"),
                     "set BRUNNA_PDF_RENDERER to a chrome-headless-shell binary to print for real")
class RealPrint(unittest.TestCase):
    def test_the_fixture_prints_under_the_profile(self):
        exe = pathlib.Path(os.environ["BRUNNA_PDF_RENDERER"]).resolve()
        self.assertEqual(pdf_renderer.fixture_prints(exe.parent, exe), "")


if __name__ == "__main__":
    unittest.main()
