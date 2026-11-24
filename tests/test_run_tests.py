"""bin/run-tests: the parallel runner the commit gate uses. Its summary must read like unittest's (people and agents read
it) and every kind of failure must fail the run.

    bin/py -m unittest tests.test_run_tests
"""
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

RUNNER = pathlib.Path(__file__).resolve().parents[1] / "bin" / "run-tests"


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def module(self, name, body):
        (self.tmp / f"{name}.py").write_text("import os\nimport unittest\n\n" + textwrap.dedent(body))

    def run_(self, **env):
        return subprocess.run([sys.executable, str(RUNNER), str(self.tmp)], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "TEST_JOBS": "3", **env})

    def test_all_pass(self):
        self.module("test_a", "class A(unittest.TestCase):\n    def test_1(self): pass\n    def test_2(self): pass\n")
        self.module("test_b", "class B(unittest.TestCase):\n    @unittest.skip('no')\n    def test_1(self): pass\n")
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"(?m)^Ran 3 tests in [\d.]+s$")
        self.assertIn("(2 modules, 2 items, 2 workers)", r.stdout)
        self.assertRegex(r.stdout, r"(?m)^OK \(skipped=1\)$")

    def test_failures_and_crashes_fail_the_run(self):
        self.module("test_ok", "class A(unittest.TestCase):\n    def test_1(self): pass\n")
        self.module("test_bad", "class B(unittest.TestCase):\n    def test_1(self): self.assertEqual(1, 2)\n")
        self.module("test_broken", "import no_such_module_xyz\n")
        r = self.run_()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertRegex(r.stdout, r"(?m)^FAIL: test_1 \(test_bad\.B\.test_1\)$", "FAIL: blocks stay at line start")
        self.assertRegex(r.stdout, r"(?m)^FAILED \(failures=1, errors=1\)$")
        self.assertIn("failed modules: test_bad, test_broken", r.stdout)
        self.assertEqual(r.stdout.count("-" * 70), 3, "one under each FAIL/ERROR header and the final one, as unittest "
                                                      "prints them; not each module's own one before its Ran line")
        self.assertEqual(len([l for l in r.stdout.splitlines() if l.startswith(("OK", "FAILED"))]), 1,
                         "only the overall result line starts with OK/FAILED (people and agents look for them)")

    def test_a_slow_module_runs_test_by_test(self):
        """a module slower than TEST_SPLIT_S last time: each of its tests is its own item; its total is still recorded"""
        self.module("test_slow", "class S(unittest.TestCase):\n    def test_1(self): pass\n    def test_2(self): pass\n"
                                 "    def test_3(self): self.fail('boom')\n")
        durations = self.tmp / "d.json"
        durations.write_text('{"test_slow": 9.0}')
        r = self.run_(TEST_DURATIONS=str(durations), TEST_SPLIT_S="4")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("(1 modules, 3 items,", r.stdout)
        self.assertRegex(r.stdout, r"(?m)^Ran 3 tests in")
        self.assertIn("failed modules: test_slow.S.test_3", r.stdout)
        recorded = __import__("json").loads(durations.read_text())
        self.assertIn("test_slow.S.test_1", recorded)
        self.assertIn("test_slow", recorded, "the module's own total, to split it again next time")

    def test_runs_as_a_zh_hant_instance_unless_told(self):
        """the gate is the zh-Hant side of the language check (CI's plain unittest the English one)"""
        self.module("test_l", "class L(unittest.TestCase):\n    def test_1(self):\n"
                              "        assert os.environ['BRUNNA_LANG_UI'] == os.environ['WANT'], os.environ['BRUNNA_LANG_UI']\n"
                              "        assert os.environ['BRUNNA_LANG_CONTENT'] == os.environ['WANT']\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_HUB_LANG_", "BRUNNA_LANG_"))}
        r = subprocess.run([sys.executable, str(RUNNER), str(self.tmp)], capture_output=True, text=True, timeout=120,
                           env={**env, "TEST_JOBS": "1", "WANT": "zh-Hant"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_(WANT="en", AGENT_HUB_LANG_UI="en", AGENT_HUB_LANG_CONTENT="en")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_golden_modules_also_run_on_english(self):
        body = "class G(unittest.TestCase):\n    def test_lang(self): print(os.environ.get('I18N_GOLDEN_LANG'))\n"
        self.module("test_i18n_golden_x", body)
        self.module("test_i18n_golden_en", "class E(unittest.TestCase):\n    def test_skip(self): pass\n")
        durations = self.tmp / "d.json"
        r = self.run_(TEST_DURATIONS=str(durations))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("(3 modules, 3 items,", r.stdout)
        self.assertIn("test_i18n_golden_x@en", durations.read_text())


class Stopped(unittest.TestCase):
    """A run stopped by a tier's time limit (11-19 and 11-21: thirty minutes, and nothing said what had hung)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def start(self, names):
        """A run over `names`, each of which says it has started, from a copy of bin/ (the nested run's guard keeps its
        folder in that tree's .state, not this one's, which other tests watch)."""
        shutil.copytree(RUNNER.parent, self.tmp / "hub" / "bin", ignore=shutil.ignore_patterns("__pycache__"), symlinks=True)
        run_tmp = self.tmp / "run-tmp"
        run_tmp.mkdir()
        env = {**os.environ, "TEST_JOBS": "3", "TMPDIR": str(run_tmp)}
        env.pop("TEST_DURATIONS", None)   # the tier's own timings: not the nested run's to write
        tests = self.tmp / "suite"
        p = subprocess.Popen([sys.executable, str(self.tmp / "hub" / "bin" / "run-tests"), str(tests)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and os.killpg(p.pid, signal.SIGKILL))
        end = time.monotonic() + 60
        while not all((self.tmp / f"{n}.started").exists() for n in names):
            self.assertLess(time.monotonic(), end, "the suite never got going")
            time.sleep(0.05)
        return p

    def suite(self, **modules):
        (self.tmp / "suite").mkdir()
        for name, body in modules.items():
            (self.tmp / "suite" / f"{name}.py").write_text(
                "import os, pathlib, subprocess, sys, time, unittest\n"
                f"HERE = pathlib.Path({str(self.tmp)!r})\n"
                f"class T(unittest.TestCase):\n    def test_1(self):\n"
                f"        (HERE / '{name}.started').touch()\n        {body}\n")

    def stopped(self, send, names):
        p = self.start(names)
        send(p)
        out, _err = p.communicate(timeout=60)
        self.assertNotEqual(p.returncode, 0, out)
        return out

    HANG = "time.sleep(120)"
    # three workers: the smallest file waits for one of them, so once test_z has started the failed module's result is in
    FOUR = {"test_hang": HANG, "test_hang2": HANG, "test_bad": "self.assertEqual(1, 2)", "test_z": HANG}

    def test_the_time_limit_names_what_was_running_and_what_had_failed(self):
        """TERM to the whole group, as the test runner's stop(): the modules die with it, before the runner can look."""
        self.suite(**self.FOUR)
        out = self.stopped(lambda p: os.killpg(p.pid, signal.SIGTERM), self.FOUR)
        self.assertIn("======== stopped: 1 items had finished, 3 were running", out)
        for name in ("test_hang", "test_hang2", "test_z"):
            self.assertRegex(out, rf"(?m)^ +\d+\.\d s  {name}$")
        self.assertRegex(out, r"(?m)^FAIL: test_1 \(test_bad\.T\.test_1\)$")
        self.assertNotIn("test_hang (exit", out, "what the stop ended has not failed")

    def test_the_runner_alone_signalled(self):
        self.suite(**self.FOUR)
        out = self.stopped(lambda p: p.send_signal(signal.SIGTERM), self.FOUR)
        self.assertIn("======== stopped: 1 items had finished, 3 were running", out)
        self.assertRegex(out, r"(?m)^ +\d+\.\d s  test_hang$")

    def test_an_interrupt_from_the_keyboard(self):
        self.suite(test_hang=self.HANG)
        out = self.stopped(lambda p: os.killpg(p.pid, signal.SIGINT), ["test_hang"])
        self.assertRegex(out, r"(?m)^ +\d+\.\d s  test_hang$")

    def test_a_module_whose_child_keeps_its_output_open(self):
        """A child outside the group outlives the stop and holds the pipe, so the runner never sees the module end."""
        self.suite(test_hang="p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True); "
                             "(HERE / 'child').write_text(str(p.pid)); (HERE / 'child.started').touch(); p.wait()")

        def kill():
            if (self.tmp / "child").exists():
                os.kill(int((self.tmp / "child").read_text()), signal.SIGKILL)
        self.addCleanup(kill)
        out = self.stopped(lambda p: os.killpg(p.pid, signal.SIGTERM), ["test_hang", "child"])
        self.assertRegex(out, r"(?m)^ +\d+\.\d s  test_hang$")
