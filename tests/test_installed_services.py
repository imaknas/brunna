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
         "granted": {"models": {}, "secrets": ["ECHO_KEY"], "principal": False, "network": []},
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
link) if ln "$2" "$3" 2>/dev/null; then echo "LINKED"; else echo "REFUSED"; fi;;
sleep) sleep "$2";;
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

    def test_no_hard_link_to_a_file_it_cant_read(self):
        self.assertEqual(self.cli("add", str(self.src), "--yes").returncode, 0)
        victim = self.tmp / "victim.txt"
        victim.write_text("owner's")
        state = self.tmp / "services" / "echo-svc" / "state"
        out = self.cli("run", "echo-svc", "--", "link", str(victim), str(state / "l")).stdout
        self.assertIn("REFUSED", out)
        self.assertFalse((state / "l").exists())

    def test_a_running_cli_holds_off_remove(self):
        self.assertEqual(self.cli("add", str(self.src), "--yes").returncode, 0)
        names.env_set(self.env, "SERVICE_LOCK_WAIT", "1")
        running = subprocess.Popen([sys.executable, str(self.tmp / "drive.py"), str(ROOT), "run", "echo-svc", "--",
                                    "sleep", "4"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(running.wait)
        import time
        time.sleep(1.5)
        r = self.cli("remove", "echo-svc")
        self.assertEqual(r.returncode, 1)
        self.assertTrue((self.tmp / "config" / "services" / "echo-svc.yaml").exists())

    def test_required_secrets_are_checked(self):
        (self.src / "brunna-service.yaml").write_text(MANIFEST.replace(
            "secrets: [{name: ECHO_KEY, what: \"a key\", optional: true}]",
            "secrets: [{name: ECHO_KEY, what: \"a key\", optional: true}, {name: NEED_KEY, what: x}]"))
        git("commit", "-qam", "need", cwd=self.src)
        # no terminal: a required secret can't be asked, so add refuses; the entry is made by hand here instead
        self.assertEqual(self.cli("add", str(self.src), "--yes").returncode, 1)
        code = subprocess.run([sys.executable, "-c", """
import sys; sys.path.insert(0, sys.argv[1]); import installed_services as s
e = {"secrets_required": ["NEED_KEY"]}
print(len(s.secret_problems("echo-svc", e)), len(s.secret_problems("echo-svc", {"secrets_required": []})))
""", str(ROOT / "bin")], env=self.env, text=True, capture_output=True)
        self.assertEqual(code.stdout.split(), ["1", "0"], code.stderr)

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


@unittest.skipIf(isolation.can_sandbox(), "this machine can't sandbox")
class Updates(Instance):
    """Step 2b: following release tags."""

    def tag(self, name: str, cli_word: str | None = None, manifest: str | None = None, force: bool = False):
        if manifest is not None:
            (self.src / "brunna-service.yaml").write_text(manifest)
        if cli_word is not None:
            (self.src / "cli.sh").write_text(CLI.replace('--help) echo "echo-svc help"', f'--help) echo "{cli_word}"'))
        git("commit", "-qam", name, "--allow-empty", cwd=self.src)
        git("tag", *(["-f"] if force else []), name, cwd=self.src)

    def entry(self) -> dict:
        import yaml
        return yaml.safe_load((self.tmp / "config" / "services" / "echo-svc.yaml").read_text())

    def test_follows_release_tags_and_stops_where_it_should(self):
        self.tag("v1.0.0", "one")
        self.assertEqual(self.cli("add", str(self.src), "--ref", "v1.0.0", "--yes").returncode, 0)
        first = self.entry()
        self.assertEqual((first["follow"], list(first["tags"])), ("tags", ["v1.0.0"]))
        self.tag("v1.1.0-rc1", "rc")
        self.tag("v1.1.0", "one-one")
        self.cli("update", "--all")
        e = self.entry()
        self.assertEqual((e["ref"], e["previous"]), ("v1.1.0", first["commit"]))
        self.assertIn("one-one", self.cli("run", "echo-svc", "--", "--help").stdout)
        code = self.tmp / "services" / "echo-svc" / "code"
        self.assertEqual({c.name for c in code.iterdir()}, {e["commit"], first["commit"]})   # the previous one kept

        # asks for more (a new required secret): not taken, said
        self.tag("v1.2.0", "more", MANIFEST.replace("secrets: [{name: ECHO_KEY, what: \"a key\", optional: true}]",
                                  "secrets: [{name: ECHO_KEY, what: \"a key\", optional: true}, {name: NEW_KEY, what: x}]"))
        r = self.cli("update", "--all")
        self.assertIn("NEW_KEY", r.stderr)
        self.assertEqual(self.entry()["ref"], "v1.1.0")

        # a failing setup: not taken
        self.tag("v1.3.0", manifest=MANIFEST.replace("echo ok > built.txt", "exit 4"))
        self.cli("update", "--all")
        self.assertEqual(self.entry()["ref"], "v1.1.0")
        self.assertEqual({c.name for c in code.iterdir()}, {e["commit"], first["commit"]})

        # a moved tag stops everything
        git("tag", "-d", "v1.2.0", "v1.3.0", cwd=self.src)
        git("tag", "-f", "v1.1.0", "HEAD~3", cwd=self.src)
        self.tag("v1.4.0", "four", manifest=MANIFEST)
        r = self.cli("update", "--all")
        self.assertIn("v1.1.0", r.stderr)
        self.assertEqual(self.entry()["ref"], "v1.1.0")

    def test_a_ref_by_hand_pins_it(self):
        self.tag("v1.0.0", "one")
        self.assertEqual(self.cli("add", str(self.src), "--ref", "v1.0.0", "--yes").returncode, 0)
        git("branch", "dev", cwd=self.src)
        self.tag("v1.1.0", "one-one")
        git("tag", "-f", "v1.0.0", "v1.1.0", cwd=self.src)   # moved
        r = self.cli("update", "echo-svc", "--ref", "dev", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        e = self.entry()
        self.assertEqual((e["ref"], e["follow"], e["tags"]), ("dev", "none", {}))
        self.cli("update", "--all")   # a pin isn't followed: still at dev
        self.assertEqual(self.entry()["ref"], "dev")
        # taking a release tag by hand after a move: follows again, with only that tag recorded
        r = self.cli("update", "echo-svc", "--ref", "v1.1.0", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        e = self.entry()
        self.assertEqual((e["follow"], list(e["tags"])), ("tags", ["v1.1.0"]))

    def test_two_spellings_of_one_version(self):
        self.tag("v1.0.0", "one")
        self.tag("1.0.0", "other")
        r = self.cli("add", str(self.src), "--ref", "v1.0.0", "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.tmp / "services" / "echo-svc").exists())

    def test_backup_takes_state_and_secrets_only(self):
        self.assertEqual(self.cli("add", str(self.src), "--yes").returncode, 0)
        self.cli("secret", "echo-svc", "ECHO_KEY", stdin="v\n")
        out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import backup; "
                              "print([str(f) for _n, f, _x in backup.services()])", str(ROOT / "bin")],
                             env=self.env, text=True, capture_output=True)
        base = self.tmp / "services" / "echo-svc"
        self.assertEqual(eval(out.stdout.strip().splitlines()[-1]), [str(base / "state"), str(base / "secrets")],
                         out.stderr)


class Lock(Instance):
    def test_a_shared_lock_holds_off_an_exclusive_one(self):
        out = subprocess.run([sys.executable, "-c", """
import sys; sys.path.insert(0, sys.argv[1]); import installed_services as s
with s.lock("echo-svc", shared=True):
    with s.lock("echo-svc", shared=True, wait=0.5):
        pass
    try:
        with s.lock("echo-svc", wait=0.5):
            print("TAKEN")
    except s.Locked:
        print("LOCKED")
""", str(ROOT / "bin")], env=self.env, text=True, capture_output=True)
        self.assertEqual(out.stdout.strip(), "LOCKED", out.stderr)


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
