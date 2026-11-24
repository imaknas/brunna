"""Long-running services follow the platform's code (docs/design/service-code.md).

D1: hubconf.code_rev() reads the commit HEAD names from git's files, in a main checkout, a linked worktree, with packed
refs and detached. D2: a process whose code the checkout has moved on from doesn't write the live registry index (the
11-24 reproduction: an old validation that rejects what the new code accepts, refreshing after the merge). D3:
code_watch.Watch, and cap-runner draining before it exits (lease-runner's drain is in test_lease_handover.FollowCode).
D4: each following service preloads every platform module it imports lazily. D5: every keepalive service of the
platform's is in render-services' FOLLOWS_CODE, and one left on old code past the limit is a problem.

    bin/py -m unittest tests.test_service_code
"""
import ast
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import code_watch  # noqa: E402
import gitdirs  # noqa: E402
import hubconf  # noqa: E402
import yaml  # noqa: E402


def load(script: str, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / script))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class CodeRev(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "hub"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a").write_text("1")
        git(self.repo, "add", "a")
        git(self.repo, "commit", "-qm", "one")

    def rev(self, root):
        with mock.patch.object(hubconf, "ROOT", pathlib.Path(root)):
            return hubconf.code_rev()

    def test_main_checkout_follows_its_branch(self):
        first = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.rev(self.repo), first)
        (self.repo / "a").write_text("2")
        git(self.repo, "commit", "-qam", "two")
        self.assertEqual(self.rev(self.repo), git(self.repo, "rev-parse", "HEAD"))
        self.assertNotEqual(self.rev(self.repo), first)

    def test_packed_refs(self):
        git(self.repo, "pack-refs", "--all")
        self.assertFalse((self.repo / ".git" / "refs" / "heads" / "main").exists())
        self.assertEqual(self.rev(self.repo), git(self.repo, "rev-parse", "HEAD"))

    def test_detached_head_as_upgrade_leaves_it(self):
        first = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "tag", "v1")
        (self.repo / "a").write_text("2")
        git(self.repo, "commit", "-qam", "two")
        git(self.repo, "checkout", "-q", "--detach", "v1")
        self.assertEqual(self.rev(self.repo), first)

    def test_linked_worktree_reads_its_own_head(self):
        wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(wt))
        (wt / "a").write_text("3")
        git(wt, "commit", "-qam", "side")
        self.assertEqual(self.rev(wt), git(wt, "rev-parse", "HEAD"))
        self.assertNotEqual(self.rev(wt), self.rev(self.repo))

    def test_not_a_checkout(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(self.rev(plain))
        self.assertIsNone(self.rev(self.repo / "sub-does-not-matter"), "a folder inside a repo isn't its checkout")

    def test_unborn_or_garbage_head(self):
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/nothing\n")
        self.assertIsNone(self.rev(self.repo))
        (self.repo / ".git" / "HEAD").write_text("not a sha\n")
        self.assertIsNone(self.rev(self.repo))
        (self.repo / ".git" / "HEAD").write_text("ref: refs/../../etc\n")
        self.assertIsNone(gitdirs.head(gitdirs.dirs(self.repo)))


# The 11-24 reproduction, in a process of its own with a scratch config and state: the index is first written by the
# current code; then the same process plays one started before a merge (LOADED_REV another commit than the checkout's)
# whose validation rejects the project (as the old code rejected modules using found_exit) and refreshes.
SCRIPT = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    import hubconf, registry
    stale = sys.argv[2] == "stale"
    hubconf.LOADED_REV = "a" * 40
    hubconf.code_rev = lambda: "a" * 40
    registry.refresh()                                   # the new code's index
    if stale:
        hubconf.code_rev = lambda: "b" * 40              # then the checkout moves on (the merge)
    old_validate = registry.validate
    registry.validate = lambda doc, stem: (["unknown field"] if stem == "proj" else old_validate(doc, stem))
    (registry.CACHE / "proj.json").unlink()              # no last valid copy to fall back on, as for a new module
    got = registry.refresh()
    print(json.dumps({"returned": sorted(got["projects"]), "errors": sorted(got["errors"])}))
""")


class OldCodeDoesNotWriteTheIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        cfg = self.tmp / "config"
        (cfg / "projects").mkdir(parents=True)
        (cfg / "hub.json").write_text(json.dumps({"v": 1, "registry_dir": "projects",
                                                  "owner": {"address": "owner", "tag": "owner"}}))
        ws = self.tmp / "ws" / "proj"
        ws.mkdir(parents=True)
        (cfg / "projects" / "proj.yaml").write_text(f"v: 1\nname: proj\npaths:\n  workspace: {ws}\n")
        self.env = {**{k: v for k, v in os.environ.items()
                       if not k.startswith(("AGENT_HUB_", "BRUNNA_")) or k in ("AGENT_HUB_PYTHON", "BRUNNA_PYTHON")},
                    "HOME": str(self.tmp / "home"), "AGENT_HUB_CONFIG": str(cfg), "AGENT_HUB_STATE": str(self.state),
                    "AGENT_HUB_LANG_UI": "en"}

    def run_as(self, which):
        r = subprocess.run([sys.executable, "-c", SCRIPT, str(BIN), which], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        index = json.loads((self.state / "shared" / "registry" / "index.json").read_text())
        return json.loads(r.stdout.splitlines()[-1]), index, r.stderr

    def test_reproduction_current_code_does_write(self):
        # the control: the same refresh by a process on the checkout's code writes what it validated
        got, index, _err = self.run_as("current")
        self.assertNotIn("proj", got["returned"])
        self.assertNotIn("proj", index["projects"])

    def test_superseded_code_leaves_the_index_alone(self):
        got, index, err = self.run_as("stale")
        self.assertNotIn("proj", got["returned"], "the old process gets its own view")
        self.assertIn("proj", index["projects"], "the index the new code wrote is left as it was")
        self.assertNotIn("proj", index["errors"])
        self.assertEqual(err.count("the live index is left to the new code"), 1, err)


class Watching(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = "a" * 40
        for p in (mock.patch.object(hubconf, "STATE", self.tmp), mock.patch.object(hubconf, "LOADED_REV", "a" * 40),
                  mock.patch.object(hubconf, "code_rev", lambda: self.now)):
            p.start()
            self.addCleanup(p.stop)

    def at(self, t):
        return mock.patch.object(code_watch.time, "time", return_value=t)

    def test_follows_the_commit_once_it_is_landed(self):
        w = code_watch.Watch("svc")
        self.assertEqual(code_watch.record("svc")["rev"], "a" * 40)
        self.assertEqual(code_watch.record("svc")["pid"], os.getpid())
        self.assertFalse(w.stale())
        self.now = "b" * 40           # merge-branch fast-forwarded; land is still running its steps
        self.assertFalse(w.stale())
        hubconf.mark_landed()         # land is done
        self.assertTrue(w.stale())
        self.assertEqual(hubconf.landed_rev(), "b" * 40)

    def test_with_no_land_it_moves_after_the_grace(self):
        with self.at(1000.0):
            w = code_watch.Watch("svc")
            self.now = "b" * 40
            self.assertFalse(w.stale())
        with self.at(1000.0 + code_watch.LAND_GRACE_S - 1):
            self.assertFalse(w.stale())
        with self.at(1000.0 + code_watch.LAND_GRACE_S):
            self.assertTrue(w.stale())

    def test_its_own_start_not_a_later_read(self):
        self.now = "b" * 40           # a merge between the process's start (LOADED_REV a) and the watch
        hubconf.mark_landed()
        self.assertTrue(code_watch.Watch("svc").stale())

    def test_an_unreadable_checkout_mid_change_is_not_stale(self):
        w = code_watch.Watch("svc")
        self.now = None
        self.assertFalse(w.stale())

    def test_an_error_is_reported_not_raised(self):
        seen = []
        w = code_watch.Watch("svc", on_error=seen.append)
        with mock.patch.object(hubconf, "code_rev", side_effect=OSError("gone")):
            self.assertFalse(w.stale())
        self.assertEqual([type(e) for e in seen], [OSError])

    def test_draining_is_recorded_once_with_when_it_began(self):
        w = code_watch.Watch("svc")
        with self.at(500.0):
            w.draining("lease 1234 in a turn")
        with self.at(600.0):
            w.draining("lease 5678 in a turn")
        rec = code_watch.record("svc")
        self.assertEqual((rec["stale_since"], rec["draining"]), (500.0, "lease 5678 in a turn"))

    def test_not_a_checkout_falls_back_to_bin_times_with_a_settle(self):
        with mock.patch.object(hubconf, "LOADED_REV", None), mock.patch.object(hubconf, "code_stamp", return_value=100.0):
            w = code_watch.Watch("svc")
        with mock.patch.object(hubconf, "code_stamp", return_value=200.0):
            with self.at(200.0 + code_watch.SETTLE_S - 1):
                self.assertFalse(w.stale(), "still being changed")
            with self.at(200.0 + code_watch.SETTLE_S):
                self.assertTrue(w.stale())


class CapRunnerDrains(unittest.TestCase):
    def test_exits_only_once_nothing_runs_or_waits(self):
        m = load("cap-runner.py", "cap_runner_follow")
        ticks = []

        class Runner:
            running, queue = ["job"], ["queued"]

            def recover(self):
                pass

            def tick(self):
                ticks.append(1)
                if len(ticks) == 2:
                    self.queue = []
                if len(ticks) == 3:
                    self.running = []

        events = []
        with mock.patch.object(m, "Runner", Runner), mock.patch.object(m, "log", lambda **k: events.append(k)), \
                mock.patch.object(m.code_watch, "Watch", lambda *a, **k: mock.Mock(stale=lambda: True)), \
                mock.patch.object(m.code_watch, "preload", lambda names: None), \
                mock.patch.object(m.time, "sleep", lambda s: None), mock.patch.object(sys, "argv", ["cap-runner.py"]):
            self.assertEqual(m.main(), 0)
        self.assertEqual(len(ticks), 3, "it kept running its jobs (and taking requests) until none was left")
        self.assertEqual(events[-1]["event"], "code_changed_restart")


def platform_services() -> list[tuple[str, dict]]:
    """(entry, service) for every service of the platform's own entry and of every module, from the repo's files."""
    out = []
    for f in [ROOT / "projects" / "brunna.yaml", *sorted((ROOT / "modules").glob("*.yaml"))]:
        doc = yaml.safe_load(f.read_text()) or {}
        out += [(f.stem, svc) for svc in doc.get("services") or []]
    return out


def lazy_platform_imports(src: str) -> set[str]:
    """Platform modules (bin/<name>.py) a script imports inside a function or method."""
    found = set()
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
            found |= {n.split(".")[0] for n in names if (BIN / f"{n.split('.')[0]}.py").is_file()}
    return found


def preload_list(src: str) -> set[str] | None:
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PRELOAD" for t in node.targets):
            return set(ast.literal_eval(node.value))
    return None


class EveryServiceFollows(unittest.TestCase):
    def setUp(self):
        self.rs = load("render-services.py", "render_services_follow")

    def test_every_keepalive_platform_service_is_declared(self):
        keepalive = sorted(svc["name"] for _entry, svc in platform_services() if svc.get("keepalive"))
        self.assertTrue(keepalive)
        missing = [n for n in keepalive if n not in self.rs.FOLLOWS_CODE]
        self.assertEqual(missing, [], "say in render-services.FOLLOWS_CODE how each follows the platform's code "
                                      "(docs/design/service-code.md D5)")

    def test_each_following_service_watches_and_preloads(self):
        for _entry, svc in platform_services():
            record = self.rs.FOLLOWS_CODE.get(svc["name"])
            if not record:
                continue
            with self.subTest(svc["name"]):
                script = next(w for w in svc["run"].split() if w.startswith("bin/") and w != "bin/py")
                src = (ROOT / script).read_text()
                self.assertIn(f'code_watch.Watch("{record}"', src)
                self.assertIn("code_watch.preload(PRELOAD)", src)
                preload = preload_list(src)
                self.assertIsNotNone(preload, "a PRELOAD tuple at the top level")
                self.assertEqual(sorted(lazy_platform_imports(src) - preload), [],
                                 "imported lazily but not preloaded (docs/design/service-code.md D4)")
                for name in preload:
                    self.assertTrue((BIN / f"{name}.py").is_file(), name)

    def test_the_scan_finds_lazy_imports(self):
        src = "import os\ndef f():\n    import registry\n    from policy import x\n    import json\n"
        self.assertEqual(lazy_platform_imports(src), {"registry", "policy"})


class OldCodeIsReported(unittest.TestCase):
    def setUp(self):
        self.rs = load("render-services.py", "render_services_old_code")
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(hubconf, "STATE", self.tmp),
                  mock.patch.object(self.rs, "OLD_CODE", self.tmp / "old-code.json"),
                  mock.patch.object(hubconf, "code_rev", return_value="b" * 40),
                  mock.patch.object(self.rs, "manager_pid", lambda uid, label: self.manager)):
            p.start()
            self.addCleanup(p.stop)
        self.manager = os.getpid()
        self.index = {"projects": {"brunna": {"type": "platform", "services": [{"name": "status-page", "keepalive": True}]},
                                   "proj": {"services": [{"name": "lease-runner", "keepalive": True}]}},
                      "modules": {"assistant": {"services": [{"name": "lease-runner", "label": "local.brunna.lease-runner",
                                                              "keepalive": True}]}}}

    def write(self, **rec):
        hubconf.write_atomic(code_watch.record_path("lease-runner"), json.dumps(rec))

    def at(self, t):
        return mock.patch.object(self.rs.time, "time", return_value=t)

    def test_old_code_past_the_limit_is_a_problem(self):
        self.write(pid=os.getpid(), rev="a" * 40, started=1.0)
        with self.at(1000.0):
            self.assertEqual(self.rs.old_code(self.index), [], "first seen: it may still be draining")
        with self.at(1000.0 + self.rs.wait_limit_s() - 1):
            self.assertEqual(self.rs.old_code(self.index), [])
        with self.at(1000.0 + self.rs.wait_limit_s()):
            got = self.rs.old_code(self.index)
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith("local.brunna.lease-runner"), got)
        self.assertIn("aaaaaaaa", got[0])
        self.assertIn(self.rs._("it says nothing holds it: its check may have stopped"), got[0])
        self.write(pid=os.getpid(), rev="a" * 40, started=1.0, draining="lease 1234 in a turn", stale_since=5.0)
        with self.at(1000.0 + self.rs.wait_limit_s()):
            self.assertIn("lease 1234 in a turn", self.rs.old_code(self.index)[0], "it names what holds it")

    def test_current_code_clears_it(self):
        self.write(pid=os.getpid(), rev="a" * 40, started=1.0)
        with self.at(1000.0):
            self.rs.old_code(self.index)
        self.write(pid=os.getpid(), rev="b" * 40, started=2.0)
        with self.at(1000.0 + self.rs.wait_limit_s()):
            self.assertEqual(self.rs.old_code(self.index), [])
        self.assertEqual(json.loads((self.tmp / "old-code.json").read_text()), {})

    def test_no_record_or_not_the_managers_process_says_nothing(self):
        with self.at(time.time()):
            self.assertEqual(self.rs.old_code(self.index), [], "a service started before code_watch writes none")
        self.manager = None   # not running (reported as such elsewhere), or a pid reused after a reboot
        self.write(pid=os.getpid(), rev="a" * 40, started=1.0)
        with self.at(1000.0):
            self.rs.old_code(self.index)
        with self.at(1000.0 + self.rs.wait_limit_s() + 1):
            self.assertEqual(self.rs.old_code(self.index), [])


if __name__ == "__main__":
    unittest.main()
