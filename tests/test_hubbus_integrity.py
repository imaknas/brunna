"""hubbus's check of the bus file: only one that is broken is moved aside (the owner is told), and any other error (a full
disk, a lock, a file that can't be opened) leaves it and fails.

    bin/py -m unittest tests.test_hubbus_integrity
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import alert  # noqa: E402


def load():
    loader = importlib.machinery.SourceFileLoader("hubbus_integrity", str(ROOT / "bin" / "hubbus"))
    spec = importlib.util.spec_from_loader("hubbus_integrity", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class Broken:
    """A connection whose quick_check fails with this SQLite error code, or gives these rows."""

    def __init__(self, code=None, rows=()):
        self.code, self.rows = code, rows

    def execute(self, _sql):
        if self.code is not None:
            e = sqlite3.OperationalError("made up")
            e.sqlite_errorcode = self.code
            raise e
        return self

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class Integrity(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.m = load()
        self.m.DB = self.tmp / "bus" / "bus.db"
        self.m.LOG = self.tmp / "bus" / "hubbus.log"
        self.told, self.reaches = [], True

        def once(key, problems, ok_text, ids=None):
            self.told.append((key, problems, ids))
            return self.reaches
        p = mock.patch.object(alert, "once", once)
        p.start()
        self.addCleanup(p.stop)

    def bus(self):
        c = self.m.db()
        c.execute("INSERT INTO events(id, ts, type, source, to_project, ref, payload) VALUES('a-letter','t','message','a','b',NULL,'{}')")
        c.close()

    def letters(self):
        c = self.m.db()
        try:
            return [r["id"] for r in c.execute("SELECT id FROM events WHERE type='message'")]
        finally:
            c.close()

    def aside(self):
        return sorted(p.name for p in self.m.DB.parent.glob("bus.db.corrupt-*"))

    def stays(self, *connect):
        with mock.patch.object(self.m.sqlite3, "connect", *connect):
            with self.assertRaises(sqlite3.DatabaseError):
                self.m.check_integrity()
        self.assertEqual(self.aside(), [])
        self.assertTrue(self.m.DB.exists())
        self.assertIn("bus.db", self.m.LOG.read_text())
        self.assertEqual(self.told, [])

    def test_an_error_that_isnt_the_files_leaves_it(self):
        self.bus()
        for code in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_IOERR_READ,
                     sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_NOMEM):
            with self.subTest(code=code):
                self.stays(lambda *a, code=code, **kw: Broken(code))
        self.assertEqual(self.letters(), ["a-letter"])

    def test_an_error_without_a_code_leaves_it(self):
        self.bus()

        def connect(*_a, **_kw):
            raise sqlite3.DatabaseError("no code")
        self.stays(connect)

    def test_a_page_that_couldnt_be_read_is_not_a_broken_file(self):
        """quick_check reports a read error as a row of its own."""
        self.bus()
        self.stays(lambda *a, **kw: Broken(rows=[("Tree 2 page 9: unable to get the page. error code=266",)]))
        self.assertEqual(self.letters(), ["a-letter"])

    def test_a_locked_bus_stays(self):
        self.bus()
        holder = sqlite3.connect(self.m.DB, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("PRAGMA journal_mode=DELETE")
        holder.execute("BEGIN EXCLUSIVE")
        real = sqlite3.connect
        self.stays(lambda db, **_kw: real(db, timeout=0))
        holder.execute("ROLLBACK")
        self.assertEqual(self.letters(), ["a-letter"])

    def test_the_commands_fail(self):
        """The bridge (a job: its failure reaches the owner) and dispatch exit non-zero, and the bus file stays."""
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "brunna.json").write_text('{"v": 1}')
        self.m.DB.mkdir(parents=True)   # can't be opened, whoever runs this
        env = {**{k: v for k, v in os.environ.items()
                  if not k.startswith(("AGENT_HUB_", "BRUNNA_")) or k in ("AGENT_HUB_PYTHON", "BRUNNA_PYTHON")},
               "HOME": str(self.tmp / "home"), "BRUNNA_CONFIG": str(self.tmp / "config"), "BRUNNA_STATE": str(self.tmp),
               "BRUNNA_LANG_UI": "en"}
        for cmd in ("bridge", "dispatch"):
            with self.subTest(cmd):
                r = subprocess.run([sys.executable, str(ROOT / "bin" / "hubbus"), cmd], env=env, capture_output=True,
                                   text=True, timeout=120)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self.aside(), [])
                self.assertTrue(self.m.DB.is_dir())

    def test_a_broken_bus_is_moved_and_the_owner_told(self):
        self.m.DB.parent.mkdir(parents=True)
        self.m.DB.write_bytes(b"this is no database" * 100)
        self.assertFalse(self.m.check_integrity())
        [name] = self.aside()
        self.assertEqual((self.m.DB.parent / name).read_bytes(), b"this is no database" * 100)
        [(key, [text], ids)] = self.told
        self.assertIn(name, text)
        self.assertEqual(ids, [name])
        self.assertEqual(alert.route(key), "owner", "it can't be told through the mailbox it is about")
        self.assertEqual(self.letters(), [], "a new one is started")
        self.assertTrue(self.m.check_integrity())
        self.assertEqual(len(self.told), 1, "told once")

    def test_a_corrupt_page_is_moved(self):
        self.bus()
        c = sqlite3.connect(self.m.DB)
        c.execute("PRAGMA journal_mode=DELETE")
        c.executemany("INSERT INTO events(id, ts, type, source, payload) VALUES(?,'t','x','a',?)",
                      [(f"e{i}", "x" * 500) for i in range(400)])
        c.commit()
        c.close()
        raw = bytearray(self.m.DB.read_bytes())
        raw[4096 * 4:4096 * 12] = b"\xff" * (4096 * 8)
        self.m.DB.write_bytes(bytes(raw))
        self.assertFalse(self.m.check_integrity())
        self.assertEqual(len(self.aside()), 1)

    def test_a_truncated_file_is_moved(self):
        self.bus()
        c = sqlite3.connect(self.m.DB)
        c.execute("PRAGMA journal_mode=DELETE")
        c.close()
        self.m.DB.write_bytes(self.m.DB.read_bytes()[:4096 * 3])
        self.assertFalse(self.m.check_integrity())
        self.assertEqual(len(self.aside()), 1)

    def test_a_second_one_doesnt_replace_the_first(self):
        self.m.DB.parent.mkdir(parents=True)
        with mock.patch.object(self.m, "now", lambda now=self.m.now(): now):   # both within the same second
            for text in (b"one " * 100, b"two " * 100):
                self.m.DB.write_bytes(text)
                self.assertFalse(self.m.check_integrity())
        self.assertEqual(sorted((self.m.DB.parent / n).read_bytes() for n in self.aside()), [b"one " * 100, b"two " * 100])

    def test_one_another_process_already_replaced_stays(self):
        """The bridge and dispatch find it broken at the same time: the one that moves second leaves the new bus."""
        self.m.DB.parent.mkdir(parents=True)
        self.m.DB.write_bytes(b"this is no database" * 100)

        def connect(*_a, **_kw):   # while this process checks, the other one moves it and letters arrive
            self.m.DB.rename(self.m.DB.with_name("moved-by-the-other"))
            with mock.patch.object(self.m.sqlite3, "connect", real):
                self.bus()
            return Broken(sqlite3.SQLITE_NOTADB)
        real = sqlite3.connect
        with mock.patch.object(self.m.sqlite3, "connect", connect):
            self.assertTrue(self.m.check_integrity())
        self.assertEqual((self.aside(), self.letters(), self.told), ([], ["a-letter"], []))

    def test_an_alert_that_didnt_go_is_sent_by_the_next_bridge(self):
        self.reaches = False
        self.m.DB.parent.mkdir(parents=True)
        self.m.DB.write_bytes(b"this is no database" * 100)
        self.assertFalse(self.m.check_integrity())
        steps = {s: (lambda c: None) for s in ("import_handoffs", "forward", "dispatch_locked", "prune")}
        with mock.patch.multiple(self.m, **steps):
            self.m.cmd_bridge()
            self.reaches = True
            self.m.cmd_bridge()
            self.m.cmd_bridge()
        self.assertEqual([ids for _k, _p, ids in self.told], [self.aside()] * 3, "until it has gone, then no more")

    def test_an_alert_that_breaks_doesnt_stop_the_new_bus(self):
        self.m.DB.parent.mkdir(parents=True)
        self.m.DB.write_bytes(b"this is no database" * 100)
        with mock.patch.object(alert, "once", side_effect=OSError("no state")):
            self.assertFalse(self.m.check_integrity())
        self.assertEqual(self.letters(), [])
        self.told_by_the_bridge([self.aside()])

    BROKEN = b"this is no database" * 100

    def broken(self, text=None):
        self.m.DB.parent.mkdir(parents=True, exist_ok=True)
        self.m.DB.write_bytes(text or self.BROKEN)

    def told_by_the_bridge(self, want):
        steps = {s: (lambda c: None) for s in ("import_handoffs", "forward", "dispatch_locked", "prune")}
        with mock.patch.multiple(self.m, **steps):
            self.m.cmd_bridge()
            self.m.cmd_bridge()
        self.assertEqual([ids for _k, _p, ids in self.told], want or [self.aside()])

    def test_the_move_waits_for_another_process_moving_it(self):
        import fcntl
        import threading
        self.broken()
        with open(self.m.DB.with_name("bus-move.lock"), "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            waiting, got, real = threading.Event(), [], fcntl.flock

            def flock(*a):
                waiting.set()
                return real(*a)
            with mock.patch.object(fcntl, "flock", flock):
                t = threading.Thread(target=lambda: got.append(self.m.check_integrity()))
                t.start()
                self.assertTrue(waiting.wait(60))
            self.m.DB.rename(self.m.DB.with_name("moved-by-the-other"))   # it moves it and starts the new one
            self.bus()
        t.join(60)
        self.assertEqual((got, self.aside(), self.letters()), ([True], [], ["a-letter"]))

    def test_a_log_that_cant_be_written_doesnt_keep_the_owner_from_hearing(self):
        self.broken()
        with mock.patch.object(self.m, "log", side_effect=OSError(28, "No space left on device")):
            self.assertFalse(self.m.check_integrity())
        self.assertEqual([ids for _k, _p, ids in self.told], [self.aside()])

    def test_a_stop_right_after_the_move_is_still_told(self):
        """The note that the owner is to be told is there before the file is moved."""
        self.broken()
        real = pathlib.Path.rename

        def rename(p, dest):
            real(p, dest)
            raise KeyboardInterrupt   # killed here
        with mock.patch.object(pathlib.Path, "rename", rename), self.assertRaises(KeyboardInterrupt):
            self.m.check_integrity()
        self.assertEqual(len(self.aside()), 1)
        self.told_by_the_bridge([self.aside()])

    def test_a_note_that_cant_be_written_leaves_the_bus(self):
        """Nothing is moved that couldn't be told: the disk is full, and a new bus couldn't be made either."""
        self.broken()
        real = os.open

        def full(p, flags, *a, **kw):
            if flags & os.O_CREAT and "moved-" in str(p):
                raise OSError(28, "No space left on device")
            return real(p, flags, *a, **kw)
        with mock.patch.object(os, "open", full), self.assertRaises(OSError):
            self.m.check_integrity()
        self.assertEqual((self.aside(), self.m.DB.read_bytes()), ([], self.BROKEN))

    def test_a_note_of_a_move_that_didnt_happen_isnt_told(self):
        self.broken()
        with mock.patch.object(pathlib.Path, "rename", side_effect=OSError(1, "not permitted")), self.assertRaises(OSError):
            self.m.check_integrity()
        self.told_by_the_bridge(None)   # (the bridge moves it now, and tells that one only)
        self.assertEqual(len(self.aside()), 1)

    def test_a_second_move_doesnt_lose_the_first_ones_note(self):
        self.reaches = False
        self.broken()
        self.m.check_integrity()
        self.broken(b"another broken one " * 50)
        self.m.check_integrity()
        self.reaches = True
        self.told.clear()
        self.told_by_the_bridge([self.aside()])
        self.assertEqual(len(self.aside()), 2)


if __name__ == "__main__":
    unittest.main()
