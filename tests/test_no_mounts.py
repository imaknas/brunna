"""No mounts from inside a sandbox (guarantee no-mounts; docs/design/capabilities.md, "11-15: no mounts"): a process
under the platform's Seatbelt profiles (services and command jobs, cap-run) can't mount a disk image it wrote over a
folder, at /Volumes or anywhere, can't make an image device, can't mount any file system itself, can't have
LaunchServices open an image for it, and can't unmount the owner's volumes; nor can it read the raw device of an image
the owner attached (its blocks past every read deny). Every route is observed from outside (the mount table, the
attached images), never from the route's exit code: hdiutil returns 0 for an attach that made nothing. Each route is
first run under a profile that allows everything and must be seen there (a positive control per route: a flag gone or a
tool moved would otherwise pass as "refused"); the sandboxed run is then watched twice as long as the control took to
show. Ordinary work still runs under the same profiles. Landlock's side is its own (no mount, no topology change for a
landlocked process): the Linux conformance probe.

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
HDIUTIL, DISKUTIL, MOUNT, UMOUNT = "/usr/bin/hdiutil", "/usr/sbin/diskutil", "/sbin/mount", "/sbin/umount"
OPEN_ALL = "(version 1)\n(allow default)\n"   # the positive control: the same route with nothing denied
CONTROL_S = 60   # how long a control may take to show (DiskImageMounter starting, an attach verifying the image)
MARK = "fsmonitor = false"
# LaunchServices asked from code the sandboxed process runs itself (not /usr/bin/open, which an exec deny stops before
# LaunchServices is reached): opening a .dmg starts DiskImageMounter outside the sandbox
LSOPEN = """import ctypes, sys
cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
cs.LSOpenCFURLRef.restype = ctypes.c_int32
cs.LSOpenCFURLRef.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
p = sys.argv[1].encode()
print("LSOpenCFURLRef", cs.LSOpenCFURLRef(cf.CFURLCreateFromFileSystemRepresentation(None, p, len(p), False), None))
"""


def pol_for(ws: pathlib.Path, deny_read=()) -> dict:
    return {"workspace": ws, "write": [], "other_projects": [], "deny_read": list(deny_read), "credentials": [],
            "credential_files": []}


def profiles(ws: pathlib.Path, deny_read=()) -> dict:
    return {"service": seatbelt.service_profile(pol_for(ws, deny_read), [ws]),
            "gpu": seatbelt.profile(pol_for(ws, deny_read), "gpu")}


class Profile(unittest.TestCase):
    def test_both_kinds_carry_it_and_the_adapters_declare_it(self):
        ws = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        caps = sorted(p.stem for p in (BIN.parent / "capabilities").glob("*.sb"))
        self.assertIn("gpu", caps)
        for text in (seatbelt.service_profile(pol_for(ws), [ws]), *(seatbelt.profile(pol_for(ws), c) for c in caps)):
            for line in seatbelt.mount_denies():
                self.assertIn(line, text)
            self.assertIn("(global-name \"com.apple.diskimagescontroller\")", text)
            self.assertIn("(deny lsopen)", text)   # LaunchServices: the fourth layer
            # nothing after the block allows a mount, a service or a driver back (later rules win), in any spelling
            tail = text[text.index("(deny file-mount)"):]
            self.assertNotRegex(tail, r"\(allow (default|file-mount|file-unmount|file\*|mach|iokit|lsopen)")
            # nor a raw disk device
            dev = f'(deny file-read* file-write* (regex #"{seatbelt.DISK_DEVICES}"))'
            self.assertIn(dev, text)
            self.assertNotRegex(text[text.index(dev):], r"\(allow (default|file)")
        self.assertIn("no-mounts", seatbelt.guarantees())
        self.assertIn("no-mounts", landlock.guarantees())
        self.assertIn("no-mounts", isolation.REQUIRED)


@unittest.skipUnless(MAC, "needs macOS Seatbelt and hdiutil")
class Routes(unittest.TestCase):
    """Each route under the service profile and the cap profile (gpu): no mount, no device, nothing unmounted, no raw
    read; each first seen under OPEN_ALL."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        cls.ws = cls.tmp / "ws"
        (cls.ws / "mnt").mkdir(parents=True)
        cls.owner = cls.tmp / "owner"   # a folder of the owner's, outside the workspace
        cls.owner.mkdir()
        cls.vol = f"brunna-nomount-{os.getpid()}"
        # the mount point DiskArbitration gives the volume at /Volumes (with " 1" and so on on a clash), exactly
        cls.at_volumes = re.compile(rf" on /Volumes/{re.escape(cls.vol)}( \d+)? \(")
        src = cls.tmp / "src"
        (src / ".git").mkdir(parents=True)   # what the hole planted: a .git with a config of its choosing
        (src / ".git" / "config").write_text(f"[core]\n\t{MARK}\n")
        cls.img = cls.ws / "x.dmg"   # bytes the sandboxed process could have written itself
        subprocess.run([HDIUTIL, "create", "-quiet", "-srcfolder", str(src), "-fs", "HFS+", "-volname", cls.vol,
                        str(cls.img)], check=True, capture_output=True)
        (cls.ws / "lsopen.py").write_text(LSOPEN)
        cls.ram0 = None   # ram disks counted as this test's: only those new across one of its own ram routes
        cls.profiles = profiles(cls.ws)

    @classmethod
    def tearDownClass(cls):
        cls.clean()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def attached(match) -> set[str]:
        """The devices of the attached images whose image-path line matches."""
        info = subprocess.run([HDIUTIL, "info"], capture_output=True, text=True).stdout
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
        route, and the mounts under this test's folder or of this volume at /Volumes (matched exactly)."""
        devs = cls.attached(lambda line: str(cls.img) in line)
        if cls.ram0 is not None:
            devs |= cls.attached(lambda line: "ram://" in line) - cls.ram0
        table = subprocess.run([MOUNT], capture_output=True, text=True).stdout
        return devs, [x for x in table.splitlines() if str(cls.tmp) in x or cls.at_volumes.search(x)]

    @classmethod
    def clean(cls):
        for _ in range(3):
            devs, mounts = cls.seen()
            if not devs and not mounts:
                return
            for x in mounts:
                subprocess.run([UMOUNT, "-f", x.split(" on ")[1].rsplit(" (", 1)[0]], capture_output=True)
            for d in devs:
                subprocess.run([HDIUTIL, "detach", "-quiet", "-force", d], capture_output=True)
            time.sleep(0.5)

    def run_under(self, profile: str, cmd: str):
        return subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", cmd], cwd=self.ws,
                              capture_output=True, text=True, timeout=120)

    def sandboxed(self, kind: str, cmd: str):
        return self.run_under(self.profiles[kind], cmd)

    def control(self, name: str, cmd: str, shows) -> float:
        """Run the route with nothing denied and wait until `shows()`: without that the route tests nothing. Returns
        how long a sandboxed run of it is then watched (twice what the control took, at least a second)."""
        t0 = time.monotonic()
        r = self.run_under(OPEN_ALL, cmd)
        while not shows():
            if time.monotonic() - t0 > CONTROL_S:
                self.fail(f"control {name}: the route did nothing even unsandboxed: {r.stdout} {r.stderr}")
            time.sleep(0.25)
        return max(1.0, 2 * (time.monotonic() - t0))

    def watch(self, wait: float, check):
        """`check()` holds for the whole wait (an automount after the command returned counts too)."""
        end = time.monotonic() + wait
        while True:
            check()
            if time.monotonic() >= end:
                return
            time.sleep(0.25)

    def ram_window(self, cmd: str):
        # a ram disk has no image of ours to name it: those present before this route are not ours
        type(self).ram0 = self.attached(lambda line: "ram://" in line) if "ram://" in cmd else None

    def attach_device(self) -> str:
        """Attach this image from outside without mounting it (the owner's, or a stick): its HFS partition."""
        out = subprocess.run([HDIUTIL, "attach", "-nomount", str(self.img)], capture_output=True, text=True,
                             timeout=120).stdout
        return re.findall(r"^(/dev/disk\d+s\d+)\s+Apple_HFS", out, re.M)[0]

    def mount_owner(self) -> str:
        """Attach and mount this image over the owner's folder from outside: its HFS partition."""
        out = subprocess.run([HDIUTIL, "attach", "-nobrowse", "-noautoopen", "-mountpoint", str(self.owner),
                              str(self.img)], check=True, capture_output=True, text=True, timeout=120).stdout
        return re.findall(r"^(/dev/disk\d+s\d+)\s+Apple_HFS", out, re.M)[0]

    def owner_mounted(self) -> bool:
        return any(f" on {self.owner} (" in x for x in self.seen()[1])

    def test_the_observation_sees_a_mount_unsandboxed(self):
        try:
            subprocess.run([HDIUTIL, "attach", "-quiet", "-nobrowse", "-noautoopen", "-mountpoint",
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
            "hdiutil -mountpoint": f'{HDIUTIL} attach -nobrowse -noautoopen -mountpoint "{mnt}" "{img}"',
            "hdiutil at /Volumes": f'{HDIUTIL} attach -nobrowse -noautoopen "{img}"',
            "hdiutil -nomount": f'{HDIUTIL} attach -nomount "{img}"',
            "hdiutil ram disk": f"{HDIUTIL} attach -nomount ram://2048",
            "diskutil --mountPoint": f'{DISKUTIL} image attach --nobrowse --mountPoint "{mnt}" "{img}"',
            "diskutil at /Volumes": f'{DISKUTIL} image attach --nobrowse "{img}"',
            "diskutil --noMount": f'{DISKUTIL} image attach --noMount "{img}"',
            "diskutil ram disk": f"{DISKUTIL} image attach --noMount ram://2048",
            "mount_devfs": f'/sbin/mount_devfs devfs "{mnt}"',
            "mount -t devfs": f'{MOUNT} -t devfs devfs "{mnt}"',
            "open (LaunchServices, the binary)": f'/usr/bin/open "{img}"',
            "LSOpenCFURLRef (its own code)": f'"{sys.executable}" -I "{self.ws / "lsopen.py"}" "{img}"',
        }
        for name, cmd in routes.items():
            with self.subTest(route=name):
                try:
                    self.ram_window(cmd)
                    try:
                        wait = self.control(name, cmd, lambda: any(self.seen()))
                    finally:
                        self.clean()
                    for kind in self.profiles:
                        with self.subTest(kind=kind):
                            self.ram_window(cmd)
                            try:
                                r = self.sandboxed(kind, cmd)
                                self.watch(wait, lambda: self.assertEqual(
                                    self.seen(), (set(), []), f"{kind} {name}: {r.stdout} {r.stderr}"))
                            finally:
                                self.clean()
                finally:
                    type(self).ram0 = None

    def test_an_existing_device_is_not_mounted(self):
        """A device attached from outside (the owner's, or a stick): not mounted at a folder, nor at /Volumes."""
        for name, cmd in {"diskutil mount -mountPoint": DISKUTIL + ' mount -mountPoint "{mnt}" {dev}',
                          "diskutil mount": DISKUTIL + " mount {dev}",
                          "mount -t hfs": MOUNT + ' -t hfs -o rdonly {dev} "{mnt}"'}.items():
            with self.subTest(route=name):
                try:
                    dev = self.attach_device()
                    wait = self.control(name, cmd.format(mnt=self.ws / "mnt", dev=dev), lambda: self.seen()[1])
                finally:
                    self.clean()
                for kind in self.profiles:
                    with self.subTest(kind=kind):
                        try:
                            dev = self.attach_device()
                            r = self.sandboxed(kind, cmd.format(mnt=self.ws / "mnt", dev=dev))
                            self.watch(wait, lambda: self.assertEqual(self.seen()[1], [], f"{kind} {name}: {r.stderr}"))
                        finally:
                            self.clean()

    def test_the_owners_volume_is_not_unmounted(self):
        owner = self.owner
        for name, cmd in {"umount": f'{UMOUNT} -f "{owner}"', "diskutil unmount": f'{DISKUTIL} unmount force "{owner}"',
                          "hdiutil detach": f'{HDIUTIL} detach -force "{owner}"'}.items():
            with self.subTest(route=name):
                try:
                    self.mount_owner()
                    wait = self.control(name, cmd, lambda: not self.owner_mounted())
                finally:
                    self.clean()
                for kind in self.profiles:
                    with self.subTest(kind=kind):
                        try:
                            self.mount_owner()
                            r = self.sandboxed(kind, cmd)
                            self.watch(wait, lambda: self.assertTrue(self.owner_mounted(),
                                                                     f"{kind} {name}: {r.stderr}"))
                        finally:
                            self.clean()

    def test_the_raw_device_of_an_image_is_not_read(self):
        """The owner's image mounted over a folder the profile denies reading: its files are refused, and so are its
        blocks through the raw device (an encrypted image's, decrypted)."""
        guarded = profiles(self.ws, deny_read=[self.owner])
        try:
            raw = self.mount_owner().replace("/dev/disk", "/dev/rdisk")
            cmd = (f'/bin/cat "{self.owner}/.git/config" >/dev/null 2>&1 || echo file-refused; '
                   f"/bin/dd if={raw} bs=65536 2>/dev/null | /usr/bin/grep -a -q '{MARK}' && echo raw-read")
            r = self.run_under(OPEN_ALL, cmd)
            self.assertEqual(r.stdout.split(), ["raw-read"], f"control: {r.stdout} {r.stderr}")
            for kind, profile in guarded.items():
                with self.subTest(kind=kind):
                    r = self.run_under(profile, cmd)
                    self.assertEqual(r.stdout.split(), ["file-refused"], f"{kind}: {r.stdout} {r.stderr}")
        finally:
            self.clean()

    def test_ordinary_work_still_runs(self):
        cmd = (f'"{sys.executable}" -c "import json, sqlite3, ssl; sqlite3.connect(\':memory:\')" && '
               "git init -q g && git -C g -c user.name=t -c user.email=t@t commit -q --allow-empty -m m && "
               "df -h . >/dev/null && stat -f %Sd / >/dev/null")
        for kind in self.profiles:
            with self.subTest(kind=kind):
                r = self.sandboxed(kind, cmd)
                self.assertEqual(r.returncode, 0, r.stderr)
                shutil.rmtree(self.ws / "g", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
