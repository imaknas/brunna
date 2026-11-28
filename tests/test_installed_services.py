"""Installed services (docs/design/service-ecosystem.md, step 2a): entries, names, the fetch's rules, and a fixture
service added from a local repository, set up and run under its own read model (where this machine can sandbox)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import installed_services as svc  # noqa: E402
import isolation  # noqa: E402
import names  # noqa: E402
import service_fetch  # noqa: E402

COMMIT = "a" * 40


def entry(**kw) -> dict:
    e = {"v": 1, "name": "echo-svc", "url": "https://example.org/x.git", "ref": None, "commit": COMMIT,
         "interface": 1, "run": {"cli": "sh cli.sh", "mcp": "sh cli.sh mcp", "setup": []},
         "granted": {"models": {}, "secrets": ["ECHO_KEY"], "principal": False, "scripts": False, "network": []},
         "added": "2026-11-28"}
    e.update(kw)
    return e


class Entries(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(svc.entry_problems(entry(), "echo-svc"), [])

    def test_invalid(self):
        for kw in ({"v": 2}, {"name": "Echo"}, {"commit": "abc"}, {"interface": 0}, {"interface": True},
                   {"url": "-x"}, {"run": {"cli": "a"}}, {"granted": {"secrets": ["lower"]}},
                   {"granted": {"principal": "yes"}}, {"port": 1}):
            with self.subTest(kw=kw):
                self.assertTrue(svc.entry_problems(entry(**kw), "echo-svc"))
        self.assertTrue(svc.entry_problems(entry(), "other"))
        self.assertTrue(svc.entry_problems([], None))


class Fetch(unittest.TestCase):
    def test_urls(self):
        for url in ("https://github.com/a/b.git", "ssh://git@host/a/b", "git@github.com:a/b.git"):
            self.assertIsNone(service_fetch.url_problem(url), url)
        for url in ("-u", "--upload-pack=x", "ext::sh -c x", "/local/path", "file:///x", "http://a/b", "a b"):
            self.assertIsNotNone(service_fetch.url_problem(url), url)
        self.assertIsNone(service_fetch.url_problem("/local/path", allow_local=True))

    def test_refs(self):
        for ref in ("v1.2.3", "main", "release/2", "a" * 40):
            self.assertIsNone(service_fetch.ref_problem(ref), ref)
        for ref in ("-x", "a..b", "a b", "x.lock"):
            self.assertIsNotNone(service_fetch.ref_problem(ref), ref)


MANIFEST = """v: 1
name: echo-svc
what: "A test service"
interface: 1
setup: ["echo ok > built.txt"]
cli: {run: "sh cli.sh"}
mcp: {run: "sh cli.sh mcp"}
secrets: [{name: ECHO_KEY, what: "a key", optional: true}]
"""
CLI = r"""case "$1" in
--help) echo "echo-svc help";;
read) if cat "$2" >/dev/null 2>&1 || ls "$2/" >/dev/null 2>&1; then echo "ALLOWED"; else echo "REFUSED"; fi;;
write) if (echo x > "$2") 2>/dev/null; then echo "ALLOWED"; else echo "REFUSED"; fi;;
env) env;;
swap) if rm -rf "$2" 2>/dev/null && ln -s "$3" "$2" 2>/dev/null; then echo "SWAPPED"; else echo "REFUSED"; fi;;
esac
"""
DRIVER = """import importlib.util, sys
sys.path.insert(0, sys.argv[1] + "/bin")
spec = importlib.util.spec_from_file_location("svccmd", sys.argv[1] + "/bin/service-cmd.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.guard = lambda: None
sys.exit(m.main(sys.argv[2:]))
"""


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


class Instance(unittest.TestCase):
    """A throwaway instance (config, state, secrets, services root of its own) and a fixture repo."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ("config/projects", "state", "secrets", "services", "src"):
            (self.tmp / d).mkdir(parents=True)
        (self.tmp / "config" / "brunna.json").write_text(json.dumps(
            {"v": 1, "services_root": str(self.tmp / "services"), "registry_dir": "projects"}))
        self.src = self.tmp / "src"
        (self.src / "brunna-service.yaml").write_text(MANIFEST)
        (self.src / "cli.sh").write_text(CLI)
        git("init", "-q", cwd=self.src)
        git("add", "-A", cwd=self.src)
        git("commit", "-qm", "init", cwd=self.src)
        (self.tmp / "drive.py").write_text(DRIVER)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(names.ENV_NEW)}
        for var, sub in (("CONFIG", "config"), ("STATE", "state"), ("SECRETS", "secrets")):
            names.env_set(self.env, var, str(self.tmp / sub))
        names.env_set(self.env, "SERVICE_ALLOW_LOCAL", "1")
        self.env["CALLER_SECRET_TOKEN"] = "leak"

    def cli(self, *args, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.tmp / "drive.py"), str(ROOT), *args], env=self.env,
                              input=stdin, text=True, capture_output=True, timeout=300)

    def code(self) -> pathlib.Path:
        return next((self.tmp / "services" / "echo-svc" / "code").iterdir())


@unittest.skipIf(isolation.can_sandbox(), "this machine can't sandbox")
class AddAndRun(Instance):
    def test_add_run_and_the_read_model(self):
        r = self.cli("add", str(self.src), "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.tmp / "config" / "services" / "echo-svc.yaml").exists())
        code, base = self.code(), self.tmp / "services" / "echo-svc"
        self.assertEqual((code / "built.txt").read_text().strip(), "ok")   # setup wrote its code folder
        self.assertEqual(self.cli("secret", "echo-svc", "ECHO_KEY", stdin="s3cret\n").returncode, 0)
        out = self.cli("run", "echo-svc", "--", "--help")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("echo-svc help", out.stdout)

        def probe(kind, path):
            return self.cli("run", "echo-svc", "--", kind, str(path)).stdout.strip().splitlines()[-1]
        self.assertEqual(probe("read", code / "cli.sh"), "ALLOWED")
        self.assertEqual(probe("write", base / "state" / "x"), "ALLOWED")
        for path in (self.tmp / "config" / "brunna.json", base / "secrets" / "ECHO_KEY", base / "repo.git" / "HEAD",
                     self.tmp / "secrets", pathlib.Path.home(), "/tmp"):
            with self.subTest(read=path):
                self.assertEqual(probe("read", path), "REFUSED")
        # Landlock has no git fence (a stated limit, docs/design/service-ecosystem.md 2a): only Seatbelt refuses a .git
        fenced = [base / "state" / ".git"] if isolation.adapter()[0] == "seatbelt" else []
        for path in (code / "x", base / "secrets" / "x", *fenced, self.tmp / "config" / "x",
                     "/tmp/brunna-service-probe"):
            with self.subTest(write=path):
                self.assertEqual(probe("write", path), "REFUSED")
        env = self.cli("run", "echo-svc", "--", "env").stdout
        self.assertNotIn("CALLER_SECRET_TOKEN", env)   # nothing of the caller's environment
        self.assertIn("ECHO_KEY=s3cret", env)
        self.assertIn(f"{svc.STATE_ENV}={base / 'state'}", env)
        self.assertIn(f"{svc.CALLER_ENV}=owner", env)

        r = self.cli("remove", "echo-svc")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((base / "code").exists())
        self.assertTrue((base / "state").exists())
        self.assertFalse((self.tmp / "config" / "services" / "echo-svc.yaml").exists())

    def test_its_folders_stay_its_own(self):
        self.assertEqual(self.cli("add", str(self.src), "--yes").returncode, 0)
        base, victim = self.tmp / "services" / "echo-svc", self.tmp / "victim"
        victim.mkdir()
        (victim / "secret.txt").write_text("owner's")
        out = self.cli("run", "echo-svc", "--", "swap", str(base / "state"), str(victim)).stdout
        if isolation.adapter()[0] == "seatbelt":   # Landlock: removing state/ needs a right on base it doesn't have
            self.assertIn("REFUSED", out)
        # and a link put there some other way is refused before anything runs or is made
        shutil.rmtree(base / "state")
        (base / "state").symlink_to(victim)
        r = self.cli("run", "echo-svc", "--", "read", str(victim / "secret.txt"))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("ALLOWED", r.stdout)
        self.assertFalse((victim / "home").exists())

    def test_nothing_setup_starts_outlives_it(self):
        (self.src / "brunna-service.yaml").write_text(MANIFEST.replace(
            "echo ok > built.txt", "(sleep 2; echo late > late.txt) & echo ok > built.txt"))
        git("commit", "-qam", "late", cwd=self.src)
        self.assertEqual(self.cli("add", str(self.src), "--yes").returncode, 0)
        import time
        time.sleep(3)
        self.assertFalse((self.code() / "late.txt").exists())

    def test_a_failing_setup_leaves_nothing(self):
        (self.src / "brunna-service.yaml").write_text(MANIFEST.replace("echo ok > built.txt", "exit 3"))
        git("commit", "-qam", "fail", cwd=self.src)
        r = self.cli("add", str(self.src), "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.tmp / "services" / "echo-svc").exists())
        self.assertFalse((self.tmp / "config" / "services" / "echo-svc.yaml").exists())


class Refused(Instance):
    def test_bad_manifest_refused_before_checkout(self):
        (self.src / "brunna-service.yaml").write_text("v: 1\nname: echo-svc\n")
        git("commit", "-qam", "bad", cwd=self.src)
        r = self.cli("add", str(self.src), "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.tmp / "services" / "echo-svc").exists())

    def test_remove_takes_only_a_service_name(self):
        (self.tmp / "config" / "projects" / "foo.yaml").write_text("v: 2\n")
        r = self.cli("remove", "../projects/foo")
        self.assertEqual(r.returncode, 1)
        self.assertTrue((self.tmp / "config" / "projects" / "foo.yaml").exists())

    def test_local_source_needs_the_test_switch(self):
        names.env_set(self.env, "SERVICE_ALLOW_LOCAL", "")
        r = self.cli("add", str(self.src), "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.tmp / "services" / "echo-svc").exists())

    def test_name_clashes(self):
        (self.tmp / "config" / "projects" / "Echo-Svc.yaml").write_text("v: 2\nname: Echo-Svc\n")
        out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                              "import installed_services as s; "
                              "print(s.clash('echo-svc', {'Echo-Svc': {}}), s.clash('idea-3', {}), "
                              "s.clash('fine-name', {}))", str(ROOT / "bin")],
                             env=self.env, text=True, capture_output=True)
        first, idea, fine = out.stdout.strip().rsplit(" ", 2)[0], out.stdout, out.stdout.strip().split()[-1]
        self.assertNotEqual(first, "None", out.stderr)
        self.assertIn("idea", idea)
        self.assertEqual(fine, "None")


class RegistryRefuses(Instance):
    def validate(self) -> list:
        out = subprocess.run([sys.executable, "-c", "import json, sys; sys.path.insert(0, sys.argv[1]); import registry; "
                              "print(json.dumps(registry.validate_v2({'v': 2, 'name': 'Echo-Svc', "
                              "'paths': {'workspace': '/x'}}, 'Echo-Svc')))", str(ROOT / "bin")],
                             env=self.env, text=True, capture_output=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_project_named_like_an_installed_service(self):
        before = self.validate()
        (self.tmp / "config" / "services").mkdir()
        (self.tmp / "config" / "services" / "echo-svc.yaml").write_text("v: 1\nname: echo-svc\n")
        added = [e for e in self.validate() if e not in before]
        self.assertEqual(len(added), 1, added)
        self.assertIn("Echo-Svc", added[0])


if __name__ == "__main__":
    unittest.main()
