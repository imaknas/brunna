"""`schedulectl run` starts a job outside a tick: its result clears or sets the job's failing mark as a scheduled run's
would (11-16: a monthly job fixed and run by hand stayed "failing", with a daily alert, until its next month).

    bin/py -m unittest discover -s tests
"""
import importlib.machinery
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


def load(name: str, file: str):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "bin" / file))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class ManualRuns(unittest.TestCase):
    def setUp(self):
        self.m = load("scheduler_mr", "scheduler.py")
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.m.FAILING_FILE = self.tmp / "failing.json"
        self.m.FAILING_FILE.write_text(json.dumps({"p/monthly": {"since": "2026-11-01T10:32:39", "detail": "exit 1"}}))

    def failing(self):
        return json.loads(self.m.FAILING_FILE.read_text())

    def test_a_manual_success_clears_and_a_failure_marks(self):
        self.m.record_run("p/monthly", 0)
        self.assertEqual(self.failing(), {})
        self.m.record_run("p/monthly", 3)
        self.assertEqual(self.failing()["p/monthly"]["detail"], "exit 3 (run by hand)")

    def test_the_started_command_records_and_keeps_the_jobs_exit_code(self):
        """What launchd / systemd run for a manual run: the job, then --record-run with its exit code; the whole
        command exits with the job's code. The recorder is a stand-in that logs its arguments (the real one writes the
        platform's state)."""
        sc = load("schedulectl_mr", "schedulectl")
        stand_in = self.tmp / "scheduler.py"
        stand_in.write_text('#!/bin/sh\necho "$@" >> "$(dirname "$0")/recorded"\n')
        stand_in.chmod(0o755)
        real = sc.hubconf.sh_py(sc.HUB / "bin" / "scheduler.py")
        for code in (0, 3):
            with self.subTest(code=code):
                cmd = sc.recorded("p/monthly", {"command": f"exit {code}"})
                self.assertIn(real, cmd)
                r = subprocess.run(["/bin/sh", "-c", cmd.replace(real, str(stand_in))], capture_output=True, text=True)
                self.assertEqual(r.returncode, code, r.stderr)
                self.assertEqual((self.tmp / "recorded").read_text().splitlines()[-1], f"--record-run p/monthly {code}")


if __name__ == "__main__":
    unittest.main()
