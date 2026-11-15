"""No mounts from inside a sandbox (guarantee no-mounts; docs/design/capabilities.md, "11-15: no mounts"): a process
under the platform's Seatbelt profiles (services and command jobs, cap-run) can't mount a disk image it wrote over a
folder, at /Volumes or anywhere, can't make an image device, can't mount any file system itself, and can't unmount
the owner's volumes. Every route is observed from outside (the mount table, the attached images, the disk list),
never from the route's exit code: hdiutil returns 0 for an attach that made nothing. An unsandboxed positive control
shows the observation sees a mount, and ordinary work still runs under the same profile. Landlock's side is its own
(no mount, no topology change for a landlocked process): the Linux conformance probe.

    bin/py -m unittest tests.test_no_mounts
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import isolation  # noqa: E402
import landlock  # noqa: E402
import seatbelt  # noqa: E402

MAC = sys.platform == "darwin" and seatbelt.can_sandbox() is None


def pol_for(ws: pathlib.Path) -> dict:
    return {"workspace": ws, "write": [], "other_projects": [], "deny_read": [], "credentials": [],
            "credential_files": []}


class Profile(unittest.TestCase):
    def test_both_kinds_carry_it_and_the_adapters_declare_it(self):
        ws = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        for text in (seatbelt.service_profile(pol_for(ws), [ws]), seatbelt.profile(pol_for(ws), "gpu")):
            for line in seatbelt.mount_denies():
                self.assertIn(line, text)
            # nothing after it allows a mount back (later rules win)
            tail = text[text.index("(deny file-mount)"):]
            self.assertNotRegex(tail, r"\(allow (default|file-mount|mach-lookup|iokit-open)")
        self.assertIn("no-mounts", seatbelt.guarantees())
        self.assertIn("no-mounts", landlock.guarantees())
        self.assertIn("no-mounts", isolation.REQUIRED)


@unittest.skipUnless(MAC, "needs macOS Seatbelt and hdiutil")
class Routes(unittest.TestCase):
    """Each route under the service profile and the cap profile (gpu): no mount, no device, nothing unmounted."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        cls.ws = cls.tmp / "ws"
        (cls.ws / "mnt").mkdir(parents=True)
        cls.vol = f"brunnanomount{os.getpid()}"
        src = cls.tmp / "src"
        (src / ".git").mkdir(parents=True)   # what the hole planted: a .git with a config of its choosing
        (src / ".git" / "config").write_text("[core]\n\tfsmonitor = false\n")
        cls.img = cls.ws / "x.dmg"   # bytes the sandboxed process could have written itself
        subprocess.run(["hdiutil", "create", "-quiet", "-srcfolder", str(src), "-fs", "HFS+", "-volname", cls.vol,
                        str(cls.img)], check=True, capture_output=True)
        cls.ram0 = None   # ram disks counted as this test's: only those new across one of its own ram routes
        cls.profiles = {"service": seatbelt.service_profile(pol_for(cls.ws), [cls.ws]),
                        "gpu": seatbelt.profile(pol_for(cls.ws), "gpu")}

    @classmethod
    def tearDownClass(cls):
        cls.clean()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def attached(match) -> set[str]:
        """The devices of the attached images whose image-path line matches."""
        info = subprocess.run(["hdiutil", "info"], capture_output=True, text=True).stdout
        devs, ours = set(), False
        for line in info.splitlines():
            if "image-path" in line:
                ours = match(line)
            elif ours and re.match(r"^/dev/disk\d+\s", line):
                devs.add(line.split()[0])
                ours = False
        return devs

    @classmethod
    def seen(cls) -> tuple[set[str], list[str]]:
        """From outside, only what is this test's (other tests and the owner may attach at the same time, and nothing
        of theirs is counted or detached): the devices of this image, ram disks new across this test's own ram
        route, and the mounts of this volume or under this test's folder."""
        devs = cls.attached(lambda line: str(cls.img) in line)
        if cls.ram0 is not None:
            devs |= cls.attached(lambda line: "ram://" in line) - cls.ram0
        table = subprocess.run(["mount"], capture_output=True, text=True).stdout
        return devs, [x for x in table.splitlines() if str(cls.tmp) in x or cls.vol in x]

    @classmethod
    def clean(cls):
        for _ in range(3):
            devs, mounts = cls.seen()
            if not devs and not mounts:
                return
            for x in mounts:
                subprocess.run(["umount", "-f", x.split(" on ")[1].split(" (")[0]], capture_output=True)
            for d in devs:
                subprocess.run(["hdiutil", "detach", "-quiet", "-force", d], capture_output=True)
            time.sleep(0.5)

    def sandboxed(self, kind: str, cmd: str):
        return subprocess.run(["/usr/bin/sandbox-exec", "-p", self.profiles[kind], "/bin/sh", "-c", cmd], cwd=self.ws,
                              capture_output=True, text=True, timeout=120)

    def assertNothing(self, kind, name, r):
        try:
            for _ in range(4):   # an automount after the command returned counts too
                devs, mounts = self.seen()
                self.assertEqual((devs, mounts), (set(), []), f"{kind} {name}: {r.stdout} {r.stderr}")
                time.sleep(0.25)
        finally:
            self.clean()

    def test_the_observation_sees_a_mount_unsandboxed(self):
        try:
            subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-noautoopen", "-mountpoint",
                            str(self.ws / "mnt"), str(self.img)], check=True, capture_output=True, timeout=120)
            devs, mounts = self.seen()
            self.assertTrue(devs and mounts, (devs, mounts))
            self.assertTrue((self.ws / "mnt" / ".git" / "config").exists())
        finally:
            self.clean()
        self.assertEqual(self.seen(), (set(), []))

    def test_no_route_mounts_or_attaches(self):
        mnt, img = self.ws / "mnt", self.img
        routes = {
            "hdiutil -mountpoint": f'hdiutil attach -nobrowse -noautoopen -mountpoint "{mnt}" "{img}"',
            "hdiutil at /Volumes": f'hdiutil attach -nobrowse -noautoopen "{img}"',
            "hdiutil -nomount": f'hdiutil attach -nomount "{img}"',
            "hdiutil ram disk": "hdiutil attach -nomount ram://2048",
            "diskutil --mountPoint": f'diskutil image attach --nobrowse --mountPoint "{mnt}" "{img}"',
            "diskutil at /Volumes": f'diskutil image attach --nobrowse "{img}"',
            "diskutil --noMount": f'diskutil image attach --noMount "{img}"',
            "diskutil ram disk": "diskutil image attach --noMount ram://2048",
            "mount_devfs": f'/sbin/mount_devfs devfs "{mnt}"',
            "mount -t devfs": f'/sbin/mount -t devfs devfs "{mnt}"',
            "open (LaunchServices)": f'/usr/bin/open "{img}"',
        }
        for kind in self.profiles:
            for name, cmd in routes.items():
                with self.subTest(kind=kind, route=name):
                    # a ram disk has no image of ours to name it: those present before this route are not ours
                    type(self).ram0 = self.attached(lambda line: "ram://" in line) if "ram://" in cmd else None
                    try:
                        self.assertNothing(kind, name, self.sandboxed(kind, cmd))
                    finally:
                        type(self).ram0 = None

    def test_an_existing_device_is_not_mounted(self):
        """A device attached from outside (the owner's, or a stick): not mounted at a folder, nor at /Volumes."""
        for kind in self.profiles:
            for name, cmd in {"diskutil mount -mountPoint": 'diskutil mount -mountPoint "{mnt}" {dev}',
                              "diskutil mount": "diskutil mount {dev}",
                              "mount -t hfs": '/sbin/mount -t hfs {dev} "{mnt}"'}.items():
                with self.subTest(kind=kind, route=name):
                    try:
                        out = subprocess.run(["hdiutil", "attach", "-nomount", str(self.img)], capture_output=True,
                                             text=True, timeout=120).stdout
                        dev = re.findall(r"^(/dev/disk\d+s\d+)\s+Apple_HFS", out, re.M)[0]
                        r = self.sandboxed(kind, cmd.format(mnt=self.ws / "mnt", dev=dev))
                        time.sleep(0.5)
                        self.assertEqual(self.seen()[1], [], f"{kind} {name}: {r.stderr}")
                    finally:
                        self.clean()

    def test_the_owners_volume_is_not_unmounted(self):
        owner = self.tmp / "owner"
        owner.mkdir(exist_ok=True)
        for kind in self.profiles:
            for name, cmd in {"umount": f'umount -f "{owner}"', "diskutil unmount": f'diskutil unmount force "{owner}"',
                              "hdiutil detach": f'hdiutil detach -force "{owner}"'}.items():
                with self.subTest(kind=kind, route=name):
                    try:
                        subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-noautoopen", "-mountpoint",
                                        str(owner), str(self.img)], check=True, capture_output=True, timeout=120)
                        r = self.sandboxed(kind, cmd)
                        time.sleep(0.5)
                        self.assertTrue(any(str(owner) in x for x in self.seen()[1]), f"{kind} {name}: {r.stderr}")
                    finally:
                        self.clean()

    def test_ordinary_work_still_runs(self):
        cmd = (f'"{sys.executable}" -c "import json, sqlite3, ssl; sqlite3.connect(\':memory:\')" && '
               "git init -q g && git -C g -c user.name=t -c user.email=t@t commit -q --allow-empty -m m && "
               "df -h . >/dev/null")
        for kind in self.profiles:
            with self.subTest(kind=kind):
                r = self.sandboxed(kind, cmd)
                self.assertEqual(r.returncode, 0, r.stderr)
                shutil.rmtree(self.ws / "g", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
