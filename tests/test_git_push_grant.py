"""The platform's push grant (bin/git_push_grant.py; docs/design/platform-push.md, "If the grant" and "The owner's answers
(11-15)"): one write deploy key per repo, held by the platform, and what onboarding asks.

Throwaway everything: the secrets directory, the state and the registry are the test's temp folder (hubconf patched); `gh`
and `ssh` are stand-ins first on PATH (gh records every call and keeps its deploy keys in a JSON file; ssh accepts only
a key the stand-in gh holds with write access, and then serves `git upload-pack` from a local bare repo), with GH_CONFIG_DIR
in the temp folder and no GH_TOKEN, so nothing can reach the real gh's sign-in, GitHub, ~/.ssh or a real repo. ssh-keygen
is the real one (skipped where it isn't installed). Each refusal has a control that passes.

    bin/py -m unittest tests.test_git_push_grant
"""
import base64
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import git_push_grant as gpg  # noqa: E402
import hubconf  # noqa: E402
import isolation  # noqa: E402
import names  # noqa: E402

HAS_KEYGEN = shutil.which("ssh-keygen") is not None

FAKE_GH = r'''
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_DIR"]) / "gh.jsonl"
with log.open("a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
state = pathlib.Path(os.environ["FAKE_DIR"]) / "deploy-keys.json"
keys = json.loads(state.read_text()) if state.exists() else []
a = sys.argv[1:]
def opt(name):
    return a[a.index(name) + 1] if name in a else None
if a[:2] == ["api", "user"]:
    print('"42+octo@users.noreply.github.com"')
    sys.exit(0)
if a[:2] != ["repo", "deploy-key"]:
    print(f"fake gh: {a}", file=sys.stderr)
    sys.exit(2)
if os.environ.get("FAKE_GH_FAIL") == a[2]:
    print("HTTP 422: Deploy keys are disabled for this repository", file=sys.stderr)
    sys.exit(1)
repo = opt("--repo")
if a[2] == "list":
    print(json.dumps([{k: v for k, v in d.items() if k != "repo"} for d in keys if d["repo"] == repo]))
elif a[2] == "add":
    pub = pathlib.Path(a[3]).read_text().strip()
    new = {"id": 100 + sum('"add"' in line for line in log.read_text().splitlines()), "key": " ".join(pub.split()[:2]), "title": opt("--title"), "repo": repo,
           "readOnly": "--allow-write" not in a or bool(os.environ.get("FAKE_GH_READONLY"))}
    keys.append(new)
    print(f"✓ Deploy key added to {repo}")
elif a[2] == "delete":
    before = len(keys)
    keys = [d for d in keys if not (str(d["id"]) == a[3] and d["repo"] == repo)]
    if len(keys) == before:
        print("HTTP 404: Not Found", file=sys.stderr)
        sys.exit(1)
state.write_text(json.dumps(keys))
'''

FAKE_SSH = r'''
import json, os, pathlib, re, subprocess, sys
d = pathlib.Path(__file__).parent   # its env is git's, which the grant builds from scratch: nothing of the test's in it
with (d / "ssh.jsonl").open("a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:]
key = a[a.index("-i") + 1] if "-i" in a else None
pub = subprocess.run(["ssh-keygen", "-y", "-f", key], capture_output=True, text=True).stdout.split()[:2] if key else []
keys = json.loads((d / "deploy-keys.json").read_text()) if (d / "deploy-keys.json").exists() else []
m = re.fullmatch(r"git-upload-pack '/?(.+)'", a[-1])
ok = m and any(k["key"].split() == pub and not k["readOnly"] and f"{k['repo']}.git" == m.group(1) for k in keys)
if (d / "deny").exists() or not ok:
    print("git@github.com: Permission denied (publickey).", file=sys.stderr)
    sys.exit(255)
os.execvp("git", ["git", "upload-pack", str(pathlib.Path((d / "remotes").read_text()) / m.group(1))])
'''

PUBLISHED = {"SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU", "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
             "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s"}


def tool(path: pathlib.Path, script: pathlib.Path) -> None:
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    path.chmod(0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="pushgrant-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = self.tmp / "fake"
        fakebin = self.tmp / "fakebin"
        for d in (self.fake, fakebin, self.tmp / "home", self.tmp / "gh-config", self.tmp / "reg"):
            d.mkdir()
        (self.fake / "gh.py").write_text(FAKE_GH)
        (self.fake / "ssh.py").write_text(FAKE_SSH)
        tool(fakebin / "gh", self.fake / "gh.py")
        tool(fakebin / "ssh", self.fake / "ssh.py")
        (self.fake / "remotes").write_text(str(self.tmp / "remotes"))
        self.gitconfig = self.tmp / "owner-gitconfig"
        self.gitconfig.write_text("[user]\n\tname = Owner Name\n\temail = owner@example.org\n")
        self.remotes = self.tmp / "remotes"
        self.remote = self.remotes / "o" / "proj.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.ws = self.tmp / "ws" / "proj"
        self.git(self.tmp, "init", "-q", "-b", "main", str(self.ws))
        (self.ws / "README").write_text("hi\n")
        self.git(self.ws, "add", "README")
        self.git(self.ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        self.git(self.ws, "push", "-q", str(self.remote), "main")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "GH_", "GITHUB_", "SSH_")) and k not in ("HOME",)}
        env.update(HOME=str(self.tmp / "home"), PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
                   GH_CONFIG_DIR=str(self.tmp / "gh-config"), FAKE_DIR=str(self.fake), FAKE_REMOTES=str(self.remotes),
                   GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM="1", AGENT_HUB_LANG_UI="en",
                   SSH_AUTH_SOCK=str(self.tmp / "owner-agent.sock"))   # the owner's agent: never used
        self.docs = {"proj": {"name": "proj", "paths": {"workspace": str(self.ws), "origin": "git@github.com:o/proj.git"},
                              "enforce": {"sandbox": True}}}
        self.todos: list[tuple] = []
        self.commits: list[tuple] = []
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(hubconf, "secrets_dir", lambda: self.tmp / "secrets"),
                  mock.patch.object(hubconf, "STATE", self.tmp / "state"),
                  mock.patch.object(hubconf, "projects", lambda: self.docs),
                  mock.patch.object(hubconf, "registry_dir", lambda: self.tmp / "reg"),
                  mock.patch("registry.refresh", lambda *a, **k: {}),
                  mock.patch.object(gpg, "owner_notify", lambda *a: self.todos.append(a)),
                  mock.patch.object(gpg, "commit_registry", lambda *a: self.commits.append(a)),
                  mock.patch.object(gpg, "visibility", lambda origin: self.vis),
                  mock.patch.object(isolation, "wrap", lambda project, cwd, argv, port=None: ["env", *argv])):
            p.start()
            self.addCleanup(p.stop)
        self.vis = "private"

    def git(self, cwd, *args) -> str:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r.stdout.strip()

    def gh_calls(self) -> list[list[str]]:
        f = self.fake / "gh.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def ssh_calls(self) -> list[list[str]]:
        f = self.fake / "ssh.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def deploy_keys(self) -> list[dict]:
        f = self.fake / "deploy-keys.json"
        return json.loads(f.read_text()) if f.exists() else []

    def setup_grant(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: False)):
            code = gpg.setup("proj", **{"yes": True, "branches": "all", **kw})
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def denied(self):
        """GitHub refuses every key while this holds."""
        (self.fake / "deny").write_text("")
        try:
            yield
        finally:
            (self.fake / "deny").unlink()

    def gdir(self) -> pathlib.Path:
        return self.tmp / "secrets" / "git-push" / "proj"


@unittest.skipUnless(HAS_KEYGEN, "no ssh-keygen (OpenSSH's client) on this machine")
class Grant(Base):
    def test_setup_makes_a_private_key_and_a_write_deploy_key(self):
        with mock.patch.object(gpg, "field_supported", lambda: False):
            code, out, err = self.setup_grant()
        self.assertEqual(code, 0, err)
        top = self.tmp / "secrets" / "git-push"
        for p, mode in ((top, 0o700), (self.gdir(), 0o700), (self.gdir() / "key", 0o600),
                        (self.gdir() / "key.pub", 0o600), (self.gdir() / "grant.json", 0o600)):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), mode, p)
        self.assertEqual(sorted(x.name for x in self.gdir().iterdir()), ["grant.json", "key", "key.pub"])
        g = json.loads((self.gdir() / "grant.json").read_text())
        self.assertEqual((g["repo"], g["host"], g["push_url"]), ("o/proj", "github.com", "git@github.com:o/proj.git"))
        self.assertEqual(g["fingerprint"], gpg.fingerprint((self.gdir() / "key.pub").read_text()))
        self.assertTrue(g["public_key"].startswith("ssh-ed25519 "))
        add = [c for c in self.gh_calls() if c[:3] == ["repo", "deploy-key", "add"]]
        self.assertEqual(len(add), 1)
        self.assertIn("--allow-write", add[0])
        self.assertEqual(add[0][add[0].index("--repo") + 1], "o/proj")
        self.assertEqual(add[0][add[0].index("--title") + 1], f"{names.current('cli')} push: proj")
        self.assertEqual(g["deploy_key_id"], self.deploy_keys()[0]["id"])
        self.assertFalse(self.deploy_keys()[0]["readOnly"])
        # outside credentials.py's grants, which it walks as <secrets>/credentials/*/*.json
        self.assertFalse((self.tmp / "secrets" / "credentials").exists())
        # checked through the grant: one ls-remote by the stand-in ssh, with this key
        self.assertTrue(any(c[c.index("-i") + 1] == str(self.gdir() / "key") for c in self.ssh_calls()))
        self.assertIn("enforce.git_push", out, "the registry has no field yet: what to set is said")
        self.assertEqual(self.commits, [])

    def test_ssh_env_uses_only_the_key_and_the_pinned_host_keys(self):
        self.setup_grant()
        env = gpg.ssh_env("proj")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(pathlib.Path(env["GIT_CONFIG_GLOBAL"]).read_text(), "", "no owner helper or insteadOf")
        cmd = shlex.split(env["GIT_SSH_COMMAND"])
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[1:3], ["-F", "/dev/null"])
        self.assertEqual(cmd[cmd.index("-i") + 1], str(self.gdir() / "key"))
        opts = {cmd[i + 1].split("=", 1)[0]: cmd[i + 1].split("=", 1)[1] for i, x in enumerate(cmd) if x == "-o"}
        self.assertEqual(opts["IdentitiesOnly"], "yes")
        self.assertEqual(opts["IdentityAgent"], "none")
        self.assertEqual(opts["StrictHostKeyChecking"], "yes")
        self.assertEqual(opts["UserKnownHostsFile"], str(BIN / "known_hosts" / "github.com"))
        self.assertEqual(opts["GlobalKnownHostsFile"], "/dev/null")
        self.assertEqual(opts["BatchMode"], "yes")
        self.assertEqual(gpg.push_url("proj"), "git@github.com:o/proj.git")
        # an https origin: the push still goes to the ssh form, which uses the key (https never would)
        self.docs["proj"]["paths"]["origin"] = "https://github.com/o/proj.git"
        self.assertEqual(gpg.push_url("proj"), "git@github.com:o/proj.git")
        # the env works for a real git ls-remote through the stand-in ssh, which sees no agent and this key only
        r = gpg.ls_remote(gpg.push_url("proj"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("refs/heads/main", r.stdout)

    def test_the_pinned_host_keys_are_githubs_published_ones(self):
        got = set()
        for line in (BIN / "known_hosts" / "github.com").read_text().splitlines():
            if line and not line.startswith("#"):
                host, _alg, b64 = line.split()
                self.assertEqual(host, "github.com")
                got.add("SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(b64)).digest()).decode().rstrip("="))
        self.assertEqual(got, PUBLISHED)

    def test_not_set_up_names_why_and_a_working_grant_is_the_control(self):
        self.setup_grant()
        gpg.ssh_env("proj")   # the control
        key, rec = self.gdir() / "key", self.gdir() / "grant.json"
        good_key, good_rec = key.read_bytes(), rec.read_text()

        def restore():
            for p in (key, rec):
                if p.is_symlink() or p.exists():
                    p.unlink()
            key.write_bytes(good_key)
            key.chmod(0o600)
            rec.write_text(good_rec)
            rec.chmod(0o600)
            self.docs["proj"]["paths"]["origin"] = "git@github.com:o/proj.git"
            gpg.ssh_env("proj")   # the control holds again

        other = self.tmp / "other-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
        cases = {
            "key missing": lambda: key.unlink(),
            "key readable by others": lambda: key.chmod(0o644),
            "grant record missing": lambda: rec.unlink(),
            "grant record not JSON": lambda: rec.write_text("{"),
            "key is a link": lambda: (key.unlink(), key.symlink_to(other)),
            "another key in its place": lambda: (key.unlink(), key.write_bytes(other.read_bytes()), key.chmod(0o600)),
            "origin is another repo now": lambda: self.docs["proj"]["paths"].update(origin="git@github.com:o/else.git"),
            "origin is another forge now": lambda: self.docs["proj"]["paths"].update(origin="git@gitlab.com:o/proj.git"),
            "origin gone": lambda: self.docs["proj"]["paths"].update(origin="(native)"),
        }
        for label, breaks in cases.items():
            with self.subTest(label):
                breaks()
                with self.assertRaises(gpg.NotSetUp) as cm:
                    gpg.ssh_env("proj")
                self.assertTrue(str(cm.exception))
                with self.assertRaises(gpg.NotSetUp):
                    gpg.push_url("proj")
                self.assertEqual(gpg.status("proj")["state"], {"origin is another forge now": "unsupported",
                                                               "origin gone": "not_needed"}.get(label, "not_set_up"))
                restore()
        shutil.rmtree(self.gdir())
        with self.assertRaises(gpg.NotSetUp):
            gpg.ssh_env("proj")

    def test_a_key_that_does_not_work_is_removed_again(self):
        with self.denied(), self.assertRaises(gpg.Failed) as cm:
            self.setup_grant()
        self.assertIn("removed again", str(cm.exception))
        self.assertEqual(self.deploy_keys(), [], "the deploy key added is deleted at GitHub")
        self.assertFalse((self.gdir() / "key").exists())
        self.assertFalse((self.gdir() / "grant.json").exists())

    def test_read_only_or_disabled_deploy_keys_get_no_platform_push(self):
        with mock.patch.dict(os.environ, {"FAKE_GH_READONLY": "1"}), self.assertRaises(gpg.Failed) as cm:
            self.setup_grant()
        self.assertIn("read-only", str(cm.exception))
        self.assertEqual(self.deploy_keys(), [])
        with mock.patch.dict(os.environ, {"FAKE_GH_FAIL": "add"}), self.assertRaises(gpg.Failed) as cm:
            self.setup_grant()
        self.assertIn("deploy keys off", str(cm.exception))
        self.assertFalse((self.gdir() / "grant.json").exists())
        self.setup_grant()   # the control
        self.assertTrue((self.gdir() / "grant.json").exists())

    def test_a_rerun_keeps_a_working_grant_and_replaces_a_stale_one(self):
        self.setup_grant()
        first = json.loads((self.gdir() / "grant.json").read_text())
        self.setup_grant()
        self.assertEqual(len([c for c in self.gh_calls() if c[2:3] == ["add"]]), 1, "kept, not added again")
        self.assertEqual(json.loads((self.gdir() / "grant.json").read_text()), first)
        # a run that stopped after adding its key at GitHub: the next one deletes that key, not piling them up
        (self.gdir() / "grant.json").unlink()
        self.setup_grant()
        self.assertEqual(len(self.deploy_keys()), 1)
        self.assertNotEqual(self.deploy_keys()[0]["id"], first["deploy_key_id"])

    def test_other_forges_and_local_paths_are_refused_before_anything(self):
        for origin in ("git@gitlab.com:o/proj.git", "https://codeberg.org/o/proj", "gh-work:o/proj.git",
                       "ssh://me@github.com/o/proj.git", "/srv/git/proj.git", "file:///srv/proj.git", "(native)",
                       "git@github.com:o/a/b.git"):
            with self.subTest(origin):
                self.docs["proj"]["paths"]["origin"] = origin
                with self.assertRaises(gpg.Refused):
                    self.setup_grant()
                self.assertEqual(self.gh_calls(), [])
                self.assertFalse(self.gdir().exists())
        for origin in ("git@github.com:o/proj.git", "ssh://git@github.com/o/proj", "https://github.com/o/proj.git",
                       "github.com:o/proj"):
            with self.subTest(control=origin):
                self.assertEqual(gpg.parse_origin(origin), ("github.com", "o/proj"))

    def test_an_unsandboxed_project_has_nothing_to_set_up(self):
        del self.docs["proj"]["enforce"]
        with self.assertRaises(gpg.Refused):
            self.setup_grant()
        self.assertEqual(self.gh_calls(), [])

    def test_revoke_deletes_the_deploy_key_and_the_key(self):
        self.setup_grant()
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(gpg.main(["setup", "proj", "--revoke"]), 0)
        self.assertEqual(self.deploy_keys(), [])
        self.assertFalse(self.gdir().exists())
        with self.assertRaises(gpg.NotSetUp):
            gpg.ssh_env("proj")
        # GitHub refusing the delete keeps the key here, to run again
        self.setup_grant()
        with mock.patch.dict(os.environ, {"FAKE_GH_FAIL": "delete"}), mock.patch("sys.stdout", io.StringIO()), \
                self.assertRaises(gpg.Failed):
            gpg.revoke("proj")
        self.assertTrue((self.gdir() / "key").exists())

    def test_the_policy_is_written_only_once_the_grant_works(self):
        reg = self.tmp / "reg" / "proj.yaml"
        reg.write_text("# project registry\nv: 1\nname: proj\nenforce:\n  sandbox: true\n")
        with mock.patch.object(gpg, "field_supported", lambda: True), mock.patch("registry.validate", lambda d, s: []), \
                mock.patch.object(gpg, "set_identity", lambda *a: self.ident.append(a)):
            self.ident = []
            with self.denied(), self.assertRaises(gpg.Failed):
                self.setup_grant()
            self.assertNotIn("git_push", reg.read_text(), "no working grant: nothing written")
            code, out, err = self.setup_grant(emails=["42+octo@users.noreply.github.com"])
        self.assertEqual(code, 0, err)
        import yaml
        doc = yaml.safe_load(reg.read_text())
        self.assertEqual(doc["enforce"]["git_push"], {"branches": "all", "emails": ["42+octo@users.noreply.github.com"]})
        self.assertTrue(reg.read_text().startswith("# project registry\n"))
        self.assertEqual(self.commits[0][1:], ("projects/proj.yaml",))
        self.assertEqual(len(self.ident), 1)

    def test_default_emails_by_visibility(self):
        self.assertEqual(gpg.default_emails("public"), ["42+octo@users.noreply.github.com"])
        self.assertEqual(gpg.default_emails("unknown"), ["42+octo@users.noreply.github.com"])
        self.assertEqual(gpg.default_emails("private"), ["owner@example.org", "42+octo@users.noreply.github.com"])

    def test_non_interactive_defaults_follow_the_visibility(self):
        with mock.patch.object(gpg, "field_supported", lambda: False):
            self.vis = "public"
            code, out, _ = self.setup_grant(branches=None)
            self.assertIn("'branches': 'none'".replace("'", '"'), out)
            self.assertIn("public", out)
            self.assertEqual(self.gh_calls(), [], "none: no grant made")
            self.vis = "private"
            code, out, _ = self.setup_grant(branches=None)
            self.assertIn('"branches": "all"', out)
            self.assertIn('"emails": ["owner@example.org", "42+octo@users.noreply.github.com"]', out)

    def test_interactive_setup_asks_branches_and_emails(self):
        answers = iter(["others", "a@b.example, 42+octo@users.noreply.github.com"])
        out = io.StringIO()
        with mock.patch.object(gpg, "field_supported", lambda: False), mock.patch("sys.stdout", out), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch("builtins.input", lambda prompt="": next(answers)):
            self.assertEqual(gpg.setup("proj"), 0)
        self.assertIn('"branches": "others"', out.getvalue())
        self.assertIn('"emails": ["a@b.example", "42+octo@users.noreply.github.com"]', out.getvalue())

    def test_status_and_the_probe(self):
        self.assertEqual(gpg.status("proj")["state"], "not_needed")
        self.docs["proj"]["enforce"]["git_push"] = {"branches": "all", "emails": []}
        self.assertTrue(gpg.needed(self.docs["proj"]))
        self.assertEqual(gpg.status("proj")["state"], "not_set_up")
        self.assertIn(f"{names.current('cli')} push setup proj", gpg.status("proj")["detail"])
        self.setup_grant()
        st = gpg.status("proj")
        self.assertEqual((st["state"], st["repo"]), ("ok", "o/proj"))
        self.assertEqual(gpg.probe("proj"), (True, ""))
        # the deploy key removed at GitHub (or gh's sign-in that added it revoked): the probe says so
        (self.fake / "deploy-keys.json").write_text("[]")
        ok, why = gpg.probe("proj")
        self.assertFalse(ok)
        self.assertIn("refused the deploy key", why)
        self.docs["proj"]["paths"]["origin"] = "/srv/local.git"
        self.assertFalse(gpg.needed(self.docs["proj"]), "a local origin has nothing to grant")


class Visibility(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="pushvis-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(hubconf, "secrets_dir", lambda: self.tmp / "secrets"),
                  mock.patch.object(hubconf, "STATE", self.tmp / "state")):
            p.start()
            self.addCleanup(p.stop)

    def test_the_anonymous_probe(self):
        cases = ((0, "", "public"), (128, "fatal: could not read Username for 'https://github.com'", "private"),
                 (128, "remote: Repository not found.", "private"),
                 (128, "fatal: unable to access: Could not resolve host: github.com", "unknown"))
        for rc, err, want in cases:
            with self.subTest(want=want, err=err), \
                    mock.patch.object(gpg, "anonymous_ls_remote", lambda url, rc=rc, err=err:
                                      subprocess.CompletedProcess([url], rc, "", err)):
                self.assertEqual(gpg.visibility("git@github.com:o/proj.git"), want)
        self.assertEqual(gpg.visibility("/srv/local.git"), "private")
        self.assertEqual(gpg.https_url("git@github.com:o/proj.git"), "https://github.com/o/proj.git")
        self.assertEqual(gpg.https_url("ssh://git@github.com:22/o/proj"), "https://github.com/o/proj")

    def test_the_probe_carries_no_credential(self):
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, **kw)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/x", "GH_TOKEN": "t", "GIT_ASKPASS": "/owner/askpass"}), \
                mock.patch.object(gpg.subprocess, "run", run):
            gpg.anonymous_ls_remote("https://github.com/o/proj.git")
        env = seen["env"]
        self.assertEqual((env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"], env["GIT_TERMINAL_PROMPT"]),
                         ("/dev/null", "1", "0"))
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotEqual(env["HOME"], os.environ.get("HOME"))
        self.assertEqual(env["GIT_ASKPASS"], "/bin/false")
        self.assertEqual(seen["argv"][:3], ["git", "-c", "credential.helper="])


class Onboard(Base):
    """git_push_grant.py onboard: what registration's next step decides, with setup replaced (Grant tests it)."""

    def run_onboard(self, push=None, agent=False, fails=None):
        calls = []

        def setup(project, **kw):
            calls.append(kw)
            if fails:
                raise gpg.Failed(fails)
            return 0
        with mock.patch.object(gpg, "setup", setup), mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(gpg.onboard("proj", push, None, agent), 0)
        return calls

    def test_defaults_and_choices(self):
        for vis, push, agent, want_setup, want_todo in (
                ("private", None, False, "all", False),
                ("public", None, False, None, True),
                ("unknown", None, False, None, True),
                ("public", "others", False, "others", False),
                ("private", "none", False, None, False),   # a person chose none: no to-do
                ("private", "none", True, None, True)):    # an agent's none (connect_project, the lease): the to-do
            with self.subTest(vis=vis, push=push, agent=agent):
                self.vis, self.todos[:] = vis, []
                (self.tmp / "state" / "git-push-grant" / "proj.json").unlink(missing_ok=True)
                calls = self.run_onboard(push, agent)
                self.assertEqual([c["branches"] for c in calls], [want_setup] if want_setup else [])
                self.assertEqual(bool(self.todos), want_todo, self.todos)
                if want_todo:
                    self.assertEqual(self.todos[0][:2], ("--type", "task"))
                    self.assertIn(f"{names.current('cli')} push setup proj", self.todos[0][5])

    def test_a_grant_that_fails_leaves_none_and_a_todo_saying_why(self):
        calls = self.run_onboard("all", fails="HTTP 422: Deploy keys are disabled")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.todos), 1)
        self.assertIn("Deploy keys are disabled", self.todos[0][5])

    def test_nothing_for_an_unsandboxed_project_or_one_without_a_github_remote(self):
        del self.docs["proj"]["enforce"]
        self.assertEqual(self.run_onboard("all"), [])
        self.docs["proj"]["enforce"] = {"sandbox": True}
        self.docs["proj"]["paths"]["origin"] = "(native)"
        self.assertEqual(self.run_onboard(), [])
        self.docs["proj"]["paths"]["origin"] = "git@gitlab.com:o/proj.git"
        self.assertEqual(self.run_onboard(), [])
        self.assertEqual(self.todos, [])

    def test_the_todo_is_once_per_episode_and_closes(self):
        p = mock.patch("sys.stdout", io.StringIO())
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(gpg.raise_todo("proj"))
        self.assertFalse(gpg.raise_todo("proj"), "one per episode")
        gpg.close_todo("proj")
        self.assertEqual(self.todos[-1][:4], ("--type", "done", "--task", "git-push-setup-proj-1"))
        self.assertTrue(gpg.raise_todo("proj"), "a new episode after it was closed")
        self.assertEqual(self.todos[-1][3], "git-push-setup-proj-2")

    def test_extra_merges_the_sandbox_into_what_the_caller_registers(self):
        self.assertEqual(json.loads(gpg.extra('{"type": "idea", "enforce": {"work": true}}', True)),
                         {"type": "idea", "enforce": {"work": True, "sandbox": True}})
        self.assertEqual(json.loads(gpg.extra("", True)), {"enforce": {"sandbox": True}})
        self.assertEqual(gpg.extra('{"layout_intent": {"split": true}}', False), '{"layout_intent": {"split": true}}')
        self.assertEqual(gpg.extra("", False), "")
        with self.assertRaises(ValueError):
            gpg.extra("{", True)

    def test_field_supported_asks_the_registry(self):
        import registry
        with mock.patch.object(registry, "validate_enforce", lambda doc: []):
            self.assertTrue(gpg.field_supported())
        with mock.patch.object(registry, "validate_enforce", lambda doc: ["enforce can only be …"]):
            self.assertFalse(gpg.field_supported())


class Identity(Base):
    def test_set_identity_runs_as_the_project_in_its_own_config(self):
        with mock.patch("sys.stdout", io.StringIO()):
            gpg.set_identity("proj", self.docs["proj"], ["owner@example.org", "42+octo@users.noreply.github.com"])
        self.assertEqual(self.git(self.ws, "config", "--local", "user.email"), "42+octo@users.noreply.github.com")
        self.assertEqual(self.git(self.ws, "config", "--local", "user.name"), "Owner Name")
        r = subprocess.run([sys.executable, str(BIN / "project-git-job.py"), "set-identity", str(self.ws), "x",
                            "no-at-sign"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, str(BIN / "project-git-job.py"), "set-identity", str(self.ws), "x\ny",
                            "a@b.example"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.git(self.ws, "config", "--local", "user.name"), "Owner Name")


class Wiring(Base):
    def test_brunna_push_setup_goes_to_the_grant(self):
        loader = importlib.machinery.SourceFileLoader("agenthub_t", str(BIN / "agenthub"))
        spec = importlib.util.spec_from_loader("agenthub_t", loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        with mock.patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            m.COMMANDS["push"](["setup", "proj", "--branches", "all"])
            self.assertEqual(run.call_args.args[0][-5:], [str(BIN / "git_push_grant.py"), "setup", "proj", "--branches",
                                                          "all"])
            m.COMMANDS["push"](["proj", "main"])
            self.assertEqual(run.call_args.args[0][-4:], [str(BIN / "git_remote.py"), "push", "proj", "main"])

    def test_backup_never_takes_the_grants(self):
        import backup
        secrets = self.tmp / "secrets"
        (secrets / "git-push" / "proj").mkdir(parents=True)
        (secrets / "git-push" / "proj" / "key").write_text("k")
        (secrets / "b2-env").write_text("x")
        with mock.patch.object(backup, "included", lambda: [secrets]):
            self.assertTrue(backup.covers(secrets / "b2-env"), "the control: the secrets directory is backed up")
            self.assertFalse(backup.covers(secrets / "git-push" / "proj" / "key"))
        seen = []

        class Stop(Exception):
            pass

        def restic(*args, **kw):
            seen.extend(args)
            raise Stop
        with mock.patch.object(backup, "ensure_repo", lambda: None), mock.patch.object(backup, "say", lambda *a: None), \
                mock.patch.object(backup, "stage", lambda: {"sqlite": [], "bundles": [], "workspace": []}), \
                mock.patch.object(backup, "included", lambda: [secrets]), \
                mock.patch.object(backup, "restic", restic), contextlib.suppress(Stop, SystemExit):
            backup.cmd_run()
        excluded = {seen[i + 1] for i, a in enumerate(seen) if a == "--exclude"}
        self.assertIn(str(secrets / "git-push"), excluded)
        self.assertIn(str(secrets / "credentials"), excluded, "the control: credentials/ was already excluded")

    @unittest.skipUnless(HAS_KEYGEN, "no ssh-keygen (OpenSSH's client) on this machine")
    def test_check_secrets_knows_the_derived_grant(self):
        loader = importlib.machinery.SourceFileLoader("check_secrets_t", str(BIN / "check-secrets.py"))
        spec = importlib.util.spec_from_loader("check_secrets_t", loader)
        cs = importlib.util.module_from_spec(spec)
        loader.exec_module(cs)
        self.docs["proj"]["enforce"]["git_push"] = {"branches": "all", "emails": ["a@b.example"]}
        with mock.patch("sys.stdout", io.StringIO()):
            probs, notes = cs.check_push_grant("proj")
        self.assertEqual(len(probs), 1)
        self.assertIn(f"{names.current('cli')} push setup proj", probs[0])
        self.assertEqual(len(self.todos), 1, "the owner's to-do to set it up")
        self.setup_grant()
        probs, notes = cs.check_push_grant("proj")
        self.assertEqual(probs, [])
        self.assertIn("not backed up on purpose", notes[0])
        (self.fake / "deploy-keys.json").write_text("[]")   # removed at GitHub
        with mock.patch("sys.stdout", io.StringIO()):
            probs, notes = cs.check_push_grant("proj")
        self.assertIn("refused the deploy key", probs[0])


if __name__ == "__main__":
    unittest.main()
