"""A test run leaves no disk image attached (bin/testguard.py; 11-21: two images of test_no_mounts stayed attached for hours
and a newfs_hfs hung for 12). Against a stand-in hdiutil first on PATH (its `info -plist` scripted, every call recorded),
never the machine's: only images under the run's own folders are detached, and an image anywhere else is never touched
(controls beside each case); one a test left attached fails the run and is detached with its folders, on an interrupt
too; the next run's sweep detaches only what dead runs left; an hdiutil or newfs_hfs still working on the run's images is
stopped, a lookalike is not. macOS only, as the guard's disk check is.

    bin/py -m unittest tests.test_disk_leftovers
"""
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import names  # noqa: E402
import testguard  # noqa: E402

STUB = '''#!{python}
import json, os, plistlib, sys
STATE = {state!r}
with open(STATE) as f:
    s = json.load(f)
s["calls"].append(sys.argv[1:])
if sys.argv[1:] == ["info", "-plist"]:
    sys.stdout.buffer.write(plistlib.dumps({{"images": [
        {{"image-path": im["path"], "system-entities": [{{"dev-entry": d}} for d in im["devs"]]}} for im in s["images"]]}}))
elif sys.argv[1] == "detach":
    s["images"] = [im for im in s["images"] if sys.argv[-1] not in im["devs"]]
else:
    sys.exit(1)
with open(STATE + ".new", "w") as f:   # whole, for a reader at the same time
    json.dump(s, f)
os.replace(STATE + ".new", STATE)
'''

# a test of the suite a nested run runs: it "attaches" an image under its own temp folder, beside one elsewhere
LEAKY = '''import json, os, tempfile, time, unittest


class Leaky(unittest.TestCase):
    def test_attaches(self):
        state = os.environ["STUB_DISKS"]
        with open(state) as f:
            s = json.load(f)
        s["images"].append({"path": os.path.join(tempfile.gettempdir(), "tmpq", "ws", "x.dmg"),
                            "devs": ["/dev/disk9911", "/dev/disk9911s1"]})
        with open(state + ".new", "w") as f:
            json.dump(s, f)
        os.replace(state + ".new", state)
        if os.environ.get("STUB_HANG"):
            time.sleep(120)
'''


def image(path, n: int) -> dict:
    return {"path": str(path), "devs": [f"/dev/disk{n}", f"/dev/disk{n}s1"]}


@unittest.skipUnless(sys.platform == "darwin", "the guard checks disk images on macOS only")
class Stub(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "disks.json"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "hdiutil").write_text(STUB.format(python=sys.executable, state=str(self.state)))
        (self.bin / "hdiutil").chmod(0o755)
        self.mine: set[int] = set()
        self.path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        p = mock.patch.dict(os.environ, {"PATH": self.path})
        p.start()
        self.addCleanup(p.stop)

    def script(self, *images):
        self.state.write_text(json.dumps({"images": list(images), "calls": []}))

    def detached(self) -> list[str]:
        return [c[-1] for c in json.loads(self.state.read_text())["calls"] if c[0] == "detach"]

    def left(self) -> list[str]:
        return [im["path"] for im in json.loads(self.state.read_text())["images"]]

    def fake(self, tool: str, *args) -> subprocess.Popen:
        """A process that ps shows as `tool args…` (what the guard matches by), sleeping."""
        p = subprocess.Popen([tool, "-c", "import time; time.sleep(120)", *args], executable=sys.executable)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.mine.add(p.pid)
        return p

    def ps(self) -> list:
        """The process listing the guard is given here: only this test's own processes, never the machine's."""
        return [x for x in testguard.processes() if x[0] in self.mine]


class Release(Stub):
    def test_only_images_under_the_roots_are_detached(self):
        root = self.tmp / "brunna-test.a" / "run-tests-a"
        ours = image(root / "tmp1" / "ws" / "x.dmg", 9901)
        sibling = image(self.tmp / "brunna-test.a" / "run-tests-ab" / "x.dmg", 9902)   # a prefix of ours is not ours
        owners = image("/Users/someone/Disk.dmg", 9903)
        ram = {"path": "ram://2048", "devs": ["/dev/disk9904"]}
        self.script(ours, sibling, owners, ram)
        self.assertEqual(testguard.release([root], ps=self.ps), [f"/dev/disk9901 {ours['path']}"])
        self.assertEqual(self.detached(), ["/dev/disk9901"])
        self.assertEqual(self.left(), [sibling["path"], owners["path"], "ram://2048"])

    def test_a_disk_tool_on_the_runs_images_is_stopped_a_lookalike_is_not(self):
        root = self.tmp / "run-tests-k"
        self.script(image(root / "ws" / "x.dmg", 9931), image(self.tmp / "elsewhere.dmg", 9932))
        vol = f"{testguard.NOMOUNT_VOLUME}{os.getpid()}"   # a live test's: the sweep of another run leaves it alone
        create = self.fake("hdiutil", "create", "-volname", "v", str(root / "ws" / "y.dmg"))   # by its path
        newfs = self.fake("newfs_hfs", "-v", vol, "/dev/rdisk9931s1")   # on our image's device
        others = [self.fake("hdiutil", "attach", str(self.tmp / "run-tests-kk" / "x.dmg")),   # a sibling's path
                  self.fake("newfs_hfs", "-v", vol, "/dev/rdisk99310s1"),   # 99310 is not 9931
                  self.fake("newfs_hfs", "-v", "x", "/dev/rdisk9932s1"),   # an image not under the root
                  self.fake("sleeper", str(root / "ws" / "x.dmg"))]   # not a disk tool
        time.sleep(0.5)
        testguard.release([root], ps=self.ps)
        for p in (create, newfs):
            self.assertIsNotNone(p.wait(timeout=10))
        for p in others:
            self.assertIsNone(p.poll(), p.args)
        self.assertEqual(self.detached(), ["/dev/disk9931"])

    def test_a_volume_name_matches_exactly(self):
        procs = [(1, ["newfs_hfs", "-v", "brunna-nomount-123", "/dev/rdisk1s1"]),
                 (2, ["/sbin/newfs_hfs", "-v", "brunna-nomount-12", "/dev/rdisk2s1"])]
        self.assertEqual(testguard.stuck([], names=["brunna-nomount-12"], procs=procs), [2])


class Sweep(Stub):
    def test_only_dead_runs_folders_are_swept(self):
        area = self.tmp / "area"
        tier = f"{names.current('cli')}-test."
        live = area / f"{tier}live" / "run-tests-L"
        dead = area / f"{tier}dead" / "run-tests-D"
        hand = area / "run-tests-hand"   # a run by hand: its folder at the top
        old = area / "run-tests-old"   # an older guard's run: no lock, can't tell
        other = area / "someone"
        for d in (live, dead, hand, old, other):
            d.mkdir(parents=True)
        fd = testguard.hold(live)
        self.addCleanup(os.close, fd)
        for d in (dead, hand):
            os.close(testguard.hold(d))   # a lock nobody holds any more: the run was killed
        gone = area / f"{tier}gone" / "run-tests-G" / "tmp1" / "x.dmg"   # its folder removed, the image still attached
        self.script(image(live / "tmp1" / "x.dmg", 9921), image(dead / "tmp1" / "x.dmg", 9922), image(gone, 9923),
                    image(old / "tmp1" / "x.dmg", 9924), image(hand / "tmp1" / "x.dmg", 9925),
                    image(other / "x.dmg", 9926), image(area / "someone-gone" / "x.dmg", 9927),
                    image(self.tmp / "elsewhere" / "run-tests-z" / "x.dmg", 9928))
        testguard.sweep(area, ps=self.ps)
        self.assertEqual(sorted(self.detached()), ["/dev/disk9922", "/dev/disk9923", "/dev/disk9925"])
        self.assertEqual(len(self.left()), 5)

    def test_a_tool_on_a_gone_tests_volume_is_stopped_a_live_ones_is_not(self):
        """Its image detached already, the tool still hanging: tied by the volume's pid, only when that test is gone."""
        self.script()
        done = subprocess.Popen([sys.executable, "-c", ""])
        done.wait()   # a test process that is gone
        gone = self.fake("newfs_hfs", "-v", f"{testguard.NOMOUNT_VOLUME}{done.pid}", "/dev/rdisk9941s1")
        live = self.fake("newfs_hfs", "-v", f"{testguard.NOMOUNT_VOLUME}{os.getpid()}", "/dev/rdisk9942s1")
        longer = self.fake("newfs_hfs", "-v", f"{testguard.NOMOUNT_VOLUME}{done.pid}x", "/dev/rdisk9943s1")
        time.sleep(0.5)
        testguard.sweep(self.tmp / "area", ps=self.ps)
        self.assertIsNotNone(gone.wait(timeout=10))
        self.assertIsNone(live.poll())
        self.assertIsNone(longer.poll())
        self.assertEqual(self.detached(), [])


class Run(Stub):
    """bin/run-tests over a one-test suite that leaves an image attached."""

    def suite(self) -> pathlib.Path:
        tests = self.tmp / "suite" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_leaky.py").write_text(LEAKY)
        self.script(image("/Users/someone/Disk.dmg", 9912))
        return tests

    def env(self, **extra) -> dict:
        run_tmp = pathlib.Path(tempfile.mkdtemp(dir=self.tmp))
        env = {**os.environ, "PATH": self.path, "TMPDIR": str(run_tmp), "STUB_DISKS": str(self.state), **extra}
        env.pop("TEST_DURATIONS", None)   # the tier's own timings: not the nested run's to write
        return env

    def runner(self) -> str:
        """A copy of bin/: the nested run's guard keeps its folder in that tree's .state, not this one's (which other
        tests watch)."""
        hub = self.tmp / "hub"
        shutil.copytree(ROOT / "bin", hub / "bin", ignore=shutil.ignore_patterns("__pycache__"), symlinks=True)
        return str(hub / "bin" / "run-tests")

    def test_a_leftover_fails_the_run_and_is_detached(self):
        tests = self.suite()
        r = subprocess.run([sys.executable, self.runner(), str(tests)], env=self.env(),
                           capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        self.assertIn("Ran 1 test", out)   # the test itself passed
        self.assertIn("tests left files behind", out)
        self.assertIn("disk image: /dev/disk9911 ", out)
        self.assertEqual(r.returncode, 1, out)
        self.assertEqual(self.detached(), ["/dev/disk9911"])
        self.assertEqual(self.left(), ["/Users/someone/Disk.dmg"])   # the owner's: never touched

    def test_an_interrupted_run_still_detaches(self):
        tests = self.suite()
        p = subprocess.Popen([sys.executable, self.runner(), str(tests)], env=self.env(STUB_HANG="1"),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        end = time.monotonic() + 60
        while len(self.left()) < 2:
            self.assertLess(time.monotonic(), end, "the suite's test never attached")
            time.sleep(0.2)
        p.send_signal(signal.SIGTERM)   # what a tier's time limit sends
        out, _err = p.communicate(timeout=60)
        self.assertNotEqual(p.returncode, 0, out)
        self.assertEqual(self.detached(), ["/dev/disk9911"], out)
        self.assertEqual(self.left(), ["/Users/someone/Disk.dmg"])


if __name__ == "__main__":
    unittest.main()
