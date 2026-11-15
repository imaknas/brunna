"""The git broker, slice a (docs/design/git-broker.md, "Step 2 build list"), under the real Seatbelt profiles with real git:
the channel and the nonce, whom it serves, P1 (the git-dir writer) and the init profile, the config op, B2's check, the
from-scratch environment, the wrapper, the owner's canaries and the limits. Every refused probe has a positive control
that lands. Throwaway projects only: the registry is a dict, the state, the shared state and the private temp folder are
the test's own folders, and the owner's git runs with GIT_CONFIG_GLOBAL pointing at a temp file. Skipped where the
sandbox doesn't fence git dirs (Linux's Landlock); the broker's refusal there is tested on any OS (Unsupported).

    bin/run-tests   (or brunna test)
"""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import git_allow  # noqa: E402
import git_broker  # noqa: E402
import hubconf  # noqa: E402
import isolation  # noqa: E402
import names  # noqa: E402
import policy  # noqa: E402
import seatbelt  # noqa: E402

FENCED = seatbelt.can_sandbox() is None and "git-dirs-fenced" in isolation.guarantees()
WRAPPER = BIN / "git-wrapper" / "git"
SOCKET_VAR = names.env_names("GIT_SOCKET")[1]
PY = os.path.realpath(sys.executable)
SECRET = "b-secret-0c1d2e3f"
# a socket's path may be longer than macOS's 104 bytes here (a test's temp folder): reached by its name from its folder
CONNECT = ("import os, socket, sys; d, n = os.path.split(sys.argv[1]); os.chdir(d); s = socket.socket(socket.AF_UNIX); "
           "s.settimeout(5); s.connect(n)")
VIA_LINK = ("import os, socket, sys; os.{how}(sys.argv[1], '{name}'); s = socket.socket(socket.AF_UNIX); "
            "s.settimeout(5); s.connect('{name}')")


def plain_env(home: pathlib.Path, cfg=None) -> dict:
    """The owner's git in a probe: a temp global config (the precondition where it is meant to hold), no system one."""
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "GIT_CONFIG_GLOBAL": str(cfg or os.devnull),
            "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C", "GIT_AUTHOR_NAME": "o", "GIT_AUTHOR_EMAIL": "o@example.invalid",
            "GIT_COMMITTER_NAME": "o", "GIT_COMMITTER_EMAIL": "o@example.invalid"}


def listen_unix(path: pathlib.Path) -> socket.socket:
    """A Unix socket accepting (and closing) connections, bound by a path relative to its folder (macOS's 104 bytes)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.getcwd()
    os.chdir(path.parent)
    try:
        s.bind(path.name)
    finally:
        os.chdir(old)
    s.listen(8)

    def loop():
        while True:
            try:
                c, _a = s.accept()
            except OSError:
                return
            c.close()
    threading.Thread(target=loop, daemon=True).start()
    return s


class Fixture(unittest.TestCase):
    """Throwaway sandboxed projects: pa (switched on, its repo made by the broker), pb (sandboxed, not switched on),
    pc (switched on, another project's sockets); a broker serving in a thread; the policy each profile is built from."""
    on = {"sandbox": True, "git": "platform"}

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="g")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        t = self.tmp
        self.state, self.shared, self.priv, self.t, self.home = t / "s", t / "sh", t / "p", t / "t", t / "h"
        self.ws, self.wsb, self.wsc = t / "a", t / "b", t / "c"
        for d in (self.shared, self.priv, self.t, self.home, self.ws, self.wsb, self.wsc):
            d.mkdir()
        self.owner_cfg = t / "gitconfig"
        self.owner_cfg.write_text("[safe]\n\tbareRepository = explicit\n[user]\n\tname = Owner\n"
                                  "\temail = owner@example.invalid\n")
        # the messages are read in English here (each language's: tests/test_i18n_golden_git_broker.py)
        for p in (mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.owner_cfg), "GIT_CONFIG_NOSYSTEM": "1",
                                               "AGENT_HUB_LANG_UI": "en", "BRUNNA_LANG_UI": "en"}),
                  mock.patch.object(seatbelt, "TMP", [str(self.t)])):
            p.start()
            self.addCleanup(p.stop)
        self.docs = {
            "pa": {"name": "pa", "paths": {"workspace": str(self.ws), "origin": "https://example.invalid/pa.git"},
                   "enforce": dict(self.on)},
            "pb": {"name": "pb", "paths": {"workspace": str(self.wsb)}, "enforce": {"sandbox": True}},
            "pc": {"name": "pc", "paths": {"workspace": str(self.wsc)}, "enforce": dict(self.on)},
        }
        (self.wsb / "secret").write_text(SECRET + "\n")
        self.settings = {}
        sp = mock.patch.object(git_broker, "setting", lambda key, default: self.settings.get(key, default))
        sp.start()
        self.addCleanup(sp.stop)
        self.broker = git_broker.Broker(self.state, self.shared, projects=lambda: self.docs, policy_for=self.pol,
                                        tmp=lambda: self.priv)
        self.broker.start()
        self.serving()

    def serving(self):
        self.stop = threading.Event()
        th = threading.Thread(target=self.broker.serve, args=(self.stop,), daemon=True)
        th.start()
        self.addCleanup(self.broker.close)
        self.addCleanup(th.join, 5)
        self.addCleanup(self.stop.set)

    def pol(self, name, doc, projects=None):
        doc = {**doc, "name": name}
        ws = pathlib.Path(doc["paths"]["workspace"])
        others = [pathlib.Path(d["paths"]["workspace"]) for n, d in (projects or self.docs).items() if n != name]
        return {"workspace": ws, "write": [], "env": {}, "protect": [], "protect_roots": [], "other_projects": others,
                "deny_write": [], "deny_write_files": [], "deny_read": [self.state, hubconf.secrets_dir()],
                "credentials": [], "credential_files": [], "platform_state": [], "read": [self.shared],
                "network": [], "local_bind": True, "platform_commands": [], "platform_cli": [], "capabilities": [],
                "git_fence": policy.git_fence(doc, [ws]), "git_broker": policy.git_broker(doc, name, self.state)}

    def sock(self, name="pa", kind="service") -> str:
        return str(self.state / "git-broker" / name / kind)

    # ---------- the owner's side ----------

    def owner_git(self, *args, cwd=None, check=True, inp=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.ws), capture_output=True, text=inp is None or
                              isinstance(inp, str), timeout=60, env=plain_env(self.home, self.owner_cfg), check=check,
                              input=inp)

    def made(self, name="pa", ws=None):
        """The project's repo made by the broker, with one commit the owner made (f, .gitignore)."""
        ws = ws or self.ws
        self.broker.make_repo(name, ws)
        (ws / "f").write_text("x\n")
        (ws / ".gitignore").write_text(".hub-run/\nlnk\n")
        self.owner_git("add", "f", ".gitignore", cwd=ws)
        self.owner_git("commit", "-q", "-m", "init", cwd=ws)

    def head(self) -> str:
        return self.owner_git("rev-parse", "HEAD").stdout.strip()

    def has_object(self, text: str) -> bool:
        oid = self.owner_git("hash-object", "--stdin", inp=text).stdout.strip()
        return self.owner_git("cat-file", "-e", oid, check=False).returncode == 0

    def log(self) -> list[dict]:
        f = self.state / "git-broker" / "log.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    # ---------- the project's side ----------

    def service_profile(self, name="pa", rule=True, fenced=True):
        doc = self.docs[name]
        pol = self.pol(name, doc if fenced else {**doc, "enforce": {"sandbox": True}})
        if not rule:
            pol["git_broker"] = None
        return seatbelt.service_profile(pol, [pathlib.Path(doc["paths"]["workspace"])])

    def project_env(self, name="pa", extra=None, path=None):
        keep = {k: v for k, v in os.environ.items() if k.startswith(("AGENT_HUB_", "BRUNNA_", "PYTHON"))}
        return {**keep, "PATH": path or f"{WRAPPER.parent}:/usr/bin:/bin", "HOME": str(self.home),
                "TMPDIR": str(self.t), "LANG": "C", SOCKET_VAR: self.sock(name), **(extra or {})}

    def as_project(self, argv, name="pa", cwd=None, stdin=None, profile=None, env=None, timeout=60):
        """argv inside the project's own service profile (fenced, with the socket rule), as its services run."""
        prof = profile if profile is not None else self.service_profile(name)
        return subprocess.run(["/usr/bin/sandbox-exec", "-p", prof, *argv], timeout=timeout, input=stdin,
                              cwd=str(cwd or self.docs[name]["paths"]["workspace"]), env=env or self.project_env(name),
                              capture_output=True)

    def wrapped(self, *args, **kw):
        return self.as_project([str(WRAPPER), *args], **kw)

    def client(self, argv, name="pa", kind="service", cwd=None, env=None, prove=True, nonce_dir=None, stdin=b"",
               setup=None):
        """An unsandboxed caller speaking the protocol (it can prove any project: it has the owner's authority)."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        git_broker.unix_connect(s, self.state / "git-broker" / name / kind)
        out, err, code = b"", b"", None
        try:
            while (got := git_broker.recv(s)) is not None:
                k, data = got
                if k == b"N":
                    msg = json.loads(data)
                    d = pathlib.Path(nonce_dir or msg["dir"])
                    if setup:
                        setup()
                    if prove:
                        d.mkdir(parents=True, exist_ok=True)
                        (d / msg["nonce"]).write_text("")
                    git_broker.send(s, b"P")
                    git_broker.send(s, b"R", json.dumps({"argv": argv, "cwd": str(cwd or self.ws),
                                                         "env": env or {}}).encode())
                    if stdin:
                        git_broker.send(s, b"I", stdin)
                    git_broker.send(s, b"I")
                elif k == b"O":
                    out += data
                elif k == b"E":
                    err += data
                elif k == b"X":
                    code = json.loads(data)["code"]
        finally:
            s.close()
        return code, out.decode(errors="replace"), err.decode(errors="replace")

    def p1(self, progs=(), guard=True, gd=None) -> str:
        """P1 for pa's repo, as the broker builds it; progs: more programs the exec guard lets run (a probe's)."""
        prof = seatbelt.git_writer_profile(self.pol("pa", self.docs["pa"]), [gd or (self.ws / ".git")],
                                           [self.ws, self.shared], [*self.broker.git_paths, *progs])
        if not guard:
            prof = prof[:prof.index("; the exec guard")]
        return prof

    def run_in(self, prof, argv, cwd=None, env=None, stdin=None):
        return subprocess.run(["/usr/bin/sandbox-exec", "-p", prof, *argv] if prof else argv, cwd=str(cwd or self.ws),
                              env=env or plain_env(self.home), capture_output=True, text=True, timeout=60, input=stdin)


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced (the broker refuses elsewhere)")
class EndToEnd(Fixture):
    def test_this_slices_operations_through_the_wrapper(self):
        """From inside the fenced service profile: add, commit -m, commit -F -, commit -a, hash-object -w --stdin with
        binary data, apply --cached (stdin and a file), rm --cached, notes add -m, gc."""
        self.made()
        (self.ws / "g").write_text("y\n")
        for args, stdin, content in ((("add", "g"), None, None), (("commit", "-q", "-m", "second"), None, None),
                                     (("add", "g"), None, "z\n"), (("commit", "-q", "-F", "-"), b"third\n", None)):
            if content:
                (self.ws / "g").write_text(content)
            r = self.wrapped(*args, stdin=stdin)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.owner_git("log", "--format=%s").stdout.split(), ["third", "second", "init"])
        self.assertEqual(self.owner_git("log", "-1", "--format=%an").stdout.strip(), "Owner")
        (self.ws / "f").write_text("changed\n")
        r = self.wrapped("commit", "-q", "-a", "-m", "all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.owner_git("show", "HEAD:f").stdout, "changed\n")
        blob = bytes(range(256)) * 1200
        r = self.wrapped("hash-object", "-w", "--stdin", stdin=blob)
        self.assertEqual(r.returncode, 0, r.stderr)
        oid = r.stdout.decode().strip()
        self.assertEqual(subprocess.run(["git", "cat-file", "blob", oid], cwd=self.ws, capture_output=True,
                                        env=plain_env(self.home, self.owner_cfg)).stdout, blob)
        patch = ("diff --git a/new b/new\nnew file mode 100644\n--- /dev/null\n+++ b/new\n@@ -0,0 +1 @@\n+new\n")
        r = self.wrapped("apply", "--cached", stdin=patch.encode())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("new", self.owner_git("ls-files").stdout.split())
        (self.ws / "p.diff").write_text(patch.replace("a/new", "a/newer").replace("b/new", "b/newer"))
        r = self.wrapped("apply", "--cached", "p.diff")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("newer", self.owner_git("ls-files").stdout.split())
        r = self.wrapped("rm", "-q", "--cached", "newer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("newer", self.owner_git("ls-files").stdout.split())
        r = self.wrapped("notes", "add", "-m", "a note")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.owner_git("notes", "show").stdout.strip(), "a note")
        r = self.wrapped("gc", "--quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.owner_git("fsck", "--no-progress").returncode, 0)
        self.assertTrue({x["path"] for x in self.log()} == {"p1"}, self.log())
        self.assertFalse((self.ws / ".git" / "index.lock").exists())

    def test_not_built_yet_and_refused_say_why_and_run_nothing(self):
        self.made()
        before = self.head()
        for args, says in ((("switch", "-c", "x"), "isn't built yet"), (("push",), "isn't built yet"),
                           (("commit", "--exe", "x"), "unknown option --exe"),
                           (("submodule", "add", "u"), "submodules"), (("commit", "-S", "-m", "x"), "signing")):
            r = self.wrapped(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn(says, r.stderr.decode())
            self.assertIn("nothing was run", r.stderr.decode())
        self.assertEqual(self.head(), before)


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced")
class WriterProfile(Fixture):
    def control(self, gd) -> str:
        """Test-only: a git-dir writer with no read denies, the shape r3 S-b forbids (never built by the platform)."""
        return "\n".join(["(version 1)", "(allow default)", '(deny file-write* (subpath "/"))',
                          f'(allow file-write* (subpath "{gd}") (literal "/dev/null"))', ""])

    def test_s_b_four_reads_refused_and_the_control_takes_the_secret(self):
        self.made()
        os.symlink(self.wsb, self.ws / "lnk")
        bpatch = self.wsb / "b.diff"
        bpatch.write_text(f"diff --git a/s b/s\nnew file mode 100644\n--- /dev/null\n+++ b/s\n@@ -0,0 +1 @@\n+{SECRET}\n")
        probes = {"hash-object": ["hash-object", "-w", str(self.wsb / "secret")],
                  "through-a-folder-link": ["hash-object", "-w", "lnk/secret"],
                  "commit -F": ["commit", "--allow-empty", "-F", str(self.wsb / "secret")],
                  "apply --cached": ["apply", "--cached", str(bpatch)]}
        for name, args in probes.items():
            with self.subTest(name):
                r = self.wrapped(*args)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertEqual(self.log()[-1]["path"], "p1", "refused before P1 ran: the probe tested nothing")
                self.assertFalse(self.took(self.ws), "B's secret entered A's repo")
                ctl = self.tmp / f"ctl-{len(name)}-{name[:4].strip()}"   # control: a fresh plain repo per probe
                self.owner_git("init", "-q", str(ctl))
                self.owner_git("commit", "-q", "--allow-empty", "-m", "init", cwd=ctl)
                os.symlink(self.wsb, ctl / "lnk")
                env = {**plain_env(self.home), "GIT_DIR": str(ctl / ".git"), "GIT_WORK_TREE": str(ctl)}
                r = self.run_in(self.control(ctl / ".git"), ["git", *args], env=env, cwd=ctl)
                self.assertEqual(r.returncode, 0, f"control: {r.stderr}")
                self.assertTrue(self.took(ctl), "the control took nothing")

    def took(self, repo) -> bool:
        """B's secret is in repo: as a blob, or as a commit's message."""
        oid = self.owner_git("hash-object", "--stdin", inp=SECRET + "\n").stdout.strip()
        blob = self.owner_git("cat-file", "-e", oid, cwd=repo, check=False).returncode == 0
        return blob or SECRET in self.owner_git("log", "--all", "--format=%B", cwd=repo, check=False).stdout

    def test_p1_carries_no_write_but_the_git_dir_and_no_network(self):
        self.made()
        gd = self.ws / ".git"
        lst = socket.socket()
        lst.bind(("127.0.0.1", 0))
        lst.listen(4)
        self.addCleanup(lst.close)
        port = lst.getsockname()[1]
        unix = listen_unix(self.t / "l.sock")
        self.addCleanup(unix.close)
        writes = {"worktree": self.ws / "w", "temp area": self.t / "w", "config": gd / "config",
                  "hooks/": gd / "hooks" / "pre-commit", "info/attributes": gd / "info" / "attributes",
                  "alternates": gd / "objects" / "info" / "alternates",
                  "http-alternates": gd / "objects" / "info" / "http-alternates", "commondir": gd / "commondir",
                  "gitdir": gd / "gitdir", "config.lock": gd / "CONFIG.lock", "shallow": gd / "shallow",
                  "info/grafts": gd / "info" / "grafts"}
        prof = self.p1(progs=[PY])
        for name, path in writes.items():
            code = f"import os; os.makedirs({str(path.parent)!r}, exist_ok=True); open({str(path)!r}, 'a').write('x')"
            with self.subTest(name):
                r = self.run_in(prof, [PY, "-c", code])
                self.assertNotEqual(r.returncode, 0, f"{name} written under P1")
                r = self.run_in(None, [PY, "-c", code])
                self.assertEqual(r.returncode, 0, f"control: {r.stderr}")
                os.unlink(path)
        ok = self.run_in(prof, [PY, "-c", f"open({str(gd / 'objects' / 'probe')!r}, 'w').write('x')"])
        self.assertEqual(ok.returncode, 0, f"P1 writes its git dir: {ok.stderr}")
        tcp = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=5)"
        for name, code, arg in (("tcp", tcp, []), ("unix", CONNECT, [str(self.t / "l.sock")])):
            with self.subTest(name):
                self.assertNotEqual(self.run_in(prof, [PY, "-c", code, *arg]).returncode, 0)
                self.assertEqual(self.run_in(None, [PY, "-c", code, *arg]).returncode, 0)
                r = self.run_in(self.service_profile(), [PY, "-c", code, *arg])
                self.assertEqual(r.returncode, 0, f"control under the service profile: {r.stderr}")

    def test_the_exec_guard(self):
        """With the argument filter off (git run directly under P1): grep -O<cmd> and a ! alias run no canary; P1 without
        the guard runs both."""
        self.made()
        gd = self.ws / ".git"
        env = {**plain_env(self.home), "GIT_DIR": str(gd), "GIT_WORK_TREE": str(self.ws)}
        for guard in (True, False):
            prof = self.p1(guard=guard)
            for name, args in (("grep -O", ["grep", f"-Otouch {gd}/canary-grep", "x"]),
                               ("alias", ["-c", f"alias.c=!touch {gd}/canary-alias", "c"])):
                with self.subTest(name, guard=guard):
                    canary = gd / f"canary-{name.split()[0]}"
                    self.run_in(prof, [self.broker.git, *args], env=env)
                    self.assertEqual(canary.exists(), not guard, name)
                    canary.unlink(missing_ok=True)

    def test_init_profile(self):
        """make_repo's repo holds only allowlisted keys and the presets; under the init profile a write outside the new
        .git is refused, and lands with no profile."""
        import git_fence
        real = self.broker.make_repo("pa", self.ws / "sub" / "r")
        text = (real / ".git" / "config").read_text()
        pairs = git_fence.config_pairs(real / ".git" / "config", self.t)
        self.assertTrue(pairs)
        self.assertEqual([k for k, v in pairs if git_fence.check_key(k, v, set())], [], text)
        self.assertIn(("branch.autosetupmerge", "false"), pairs)
        self.assertTrue((real / ".git" / names.current("repo_dir")).is_dir())
        self.assertFalse((real / ".git" / "hooks").exists(), "--template= copies no hooks")
        prof = seatbelt.git_init_profile(self.pol("pa", self.docs["pa"]), self.ws / "n" / ".git", [self.ws],
                                         [*self.broker.git_paths, PY])
        code = f"open({str(self.ws / 'outside')!r}, 'w').write('x')"
        self.assertNotEqual(self.run_in(prof, [PY, "-c", code]).returncode, 0)
        self.assertEqual(self.run_in(None, [PY, "-c", code]).returncode, 0)
        rec = json.loads(git_broker.repos_file(self.state, "pa").read_text())["repos"]
        self.assertIn(str(real / ".git"), [r["git"] for r in rec])

    def test_the_environment_is_built_from_scratch(self):
        """A canary http.extraHeader in the broker's own environment never reaches its git (until slice d's https
        server: git config --get under P1 finds nothing); plain git with the same environment finds it."""
        self.made()
        canary = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": "X-Canary: 1"}
        with mock.patch.dict(os.environ, canary):
            env = self.broker.env({}, {"GIT_DIR": str(self.ws / ".git")})
            r = self.run_in(self.p1(), [self.broker.git, "config", "--get", "http.extraHeader"], env=env)
            self.assertEqual(r.stdout.strip(), "")
            r = subprocess.run(["git", "config", "--get", "http.extraHeader"], cwd=self.ws, capture_output=True,
                               text=True, env={**os.environ}, timeout=30)
            self.assertEqual(r.stdout.strip(), "X-Canary: 1")
        self.assertFalse([k for k in env if k.startswith("GIT_CONFIG") and k not in ("GIT_CONFIG_GLOBAL",
                                                                                        "GIT_CONFIG_NOSYSTEM")])


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced")
class B2(Fixture):
    PATCH = "diff --git a/x/HEAD b/x/HEAD\nnew file mode 100644\n--- /dev/null\n+++ b/x/HEAD\n@@ -0,0 +1 @@\n+ref\n"

    def test_apply_cached_of_a_head_path_is_refused(self):
        self.made()
        index, head = (self.ws / ".git" / "index").read_bytes(), self.head()
        r = self.wrapped("apply", "--cached", stdin=self.PATCH.encode())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HEAD", r.stderr.decode())
        self.assertEqual((self.ws / ".git" / "index").read_bytes(), index)
        self.assertEqual(self.head(), head)
        self.assertFalse(list((self.ws / ".git").glob("index.*")), "a copy or a lock was left")
        for folded in ("x/head", "x/He‌AD"):
            r = self.wrapped("apply", "--cached", stdin=self.PATCH.replace("x/HEAD", folded).encode())
            self.assertNotEqual(r.returncode, 0, folded)
        ctl = self.owner_git("apply", "--cached", inp=self.PATCH)   # control: plain git takes it
        self.assertEqual(ctl.returncode, 0)
        self.assertIn("x/HEAD", self.owner_git("ls-files").stdout)

    def test_a_commit_of_an_index_holding_head_is_refused(self):
        self.made()
        oid = self.owner_git("hash-object", "-w", "--stdin", inp="ref\n").stdout.strip()
        self.owner_git("update-index", "--add", "--cacheinfo", f"100644,{oid},x/HEAD")
        index, head = (self.ws / ".git" / "index").read_bytes(), self.head()
        r = self.wrapped("commit", "-q", "-m", "plant")
        self.assertEqual(r.returncode, 2)
        self.assertIn("x/HEAD", r.stderr.decode())
        self.assertEqual((self.ws / ".git" / "index").read_bytes(), index)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.owner_git("commit", "-q", "-m", "plant").returncode, 0)   # control
        self.assertNotEqual(self.head(), head)


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced")
class Identity(Fixture):
    def test_another_projects_socket_is_refused_by_the_rule(self):
        self.made()
        for rule, want in ((True, False), (False, True)):
            r = self.as_project([PY, "-c", CONNECT, self.sock("pc")], profile=self.service_profile(rule=rule))
            self.assertEqual(r.returncode == 0, want, f"rule={rule}: {r.stderr}")

    def test_its_own_service_cant_reach_its_own_session_socket(self):
        """Directly, through a symbolic link it made in its workspace, through a hard link it tries to make there. The
        controls: without the socket rule the first two connect; the hard link isn't made under any profile of the
        project (the platform state is outside its writes), so its control is no profile at all, where it is made and
        connects."""
        self.made()
        sess = self.sock("pa", "session")
        ways = {"direct": CONNECT, "symlink": VIA_LINK.format(how="symlink", name="sl"),
                "hardlink": VIA_LINK.format(how="link", name="hl")}
        for name, code in ways.items():
            with self.subTest(name):
                for f in ("sl", "hl"):
                    (self.ws / f).unlink(missing_ok=True)
                r = self.as_project([PY, "-c", code, sess])
                self.assertNotEqual(r.returncode, 0, name)
                for f in ("sl", "hl"):
                    (self.ws / f).unlink(missing_ok=True)
                if name == "hardlink":
                    ctl = subprocess.run([PY, "-c", code, sess], cwd=self.ws, capture_output=True, text=True, timeout=30)
                else:
                    ctl = self.as_project([PY, "-c", code, sess], profile=self.service_profile(rule=False))
                self.assertEqual(ctl.returncode, 0, f"control: {ctl.stderr}")
        own = self.as_project([PY, "-c", CONNECT, self.sock("pa", "service")])
        self.assertEqual(own.returncode, 0, own.stderr)

    def test_an_unrelated_unix_socket_stays_reachable(self):
        other = listen_unix(self.t / "db.sock")
        self.addCleanup(other.close)
        r = self.as_project([PY, "-c", CONNECT, str(self.t / "db.sock")])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_proof_and_proof_elsewhere_are_refused(self):
        self.made()
        before = self.head()
        code, _o, err = self.client(["commit", "--allow-empty", "-m", "x"], prove=False)
        self.assertEqual(code, 2)
        self.assertIn("proof", err)
        code, _o, err = self.client(["commit", "--allow-empty", "-m", "x"], nonce_dir=self.wsb / ".hub-run" / "git")
        self.assertEqual(code, 2)
        self.assertEqual(self.head(), before)
        code, _o, err = self.client(["commit", "--allow-empty", "-q", "-m", "x"])   # control: the right folder
        self.assertEqual(code, 0, err)
        self.assertNotEqual(self.head(), before)

    def test_a_sandboxed_process_cant_prove_another_project(self):
        """pa's service, connected to pc's socket (the rule off, as a session that reaches any socket), can't write the
        nonce into pc's folder; pc's own does."""
        self.made()
        self.made("pc", self.wsc)
        env = self.project_env("pa", {SOCKET_VAR: self.sock("pc")})
        r = self.as_project([str(WRAPPER), "-C", str(self.wsc), "commit", "--allow-empty", "-m", "x"], env=env,
                            profile=self.service_profile(rule=False))
        self.assertEqual(r.returncode, 2)
        self.assertIn("can't prove", r.stderr.decode())
        r = self.as_project([str(WRAPPER), "commit", "-q", "--allow-empty", "-m", "x"], name="pc")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_fifo_or_a_link_at_hub_run_or_git_is_refused_without_blocking(self):
        self.made()
        real = self.tmp / "elsewhere" / "git"
        real.mkdir(parents=True)
        hub = self.ws / ".hub-run"

        def fifo_top():
            shutil.rmtree(hub, ignore_errors=True)
            os.mkfifo(hub)

        def fifo_git():
            shutil.rmtree(hub, ignore_errors=True)
            hub.mkdir()
            os.mkfifo(hub / "git")

        def link_top():
            shutil.rmtree(hub, ignore_errors=True)
            os.symlink(real.parent, hub)

        def link_git():
            shutil.rmtree(hub, ignore_errors=True)
            hub.mkdir()
            os.symlink(real, hub / "git")
        for name, setup in (("fifo .hub-run", fifo_top), ("fifo git", fifo_git), ("link .hub-run", link_top),
                            ("link git", link_git)):
            with self.subTest(name):
                t0 = time.monotonic()
                code, _o, err = self.client(["commit", "--allow-empty", "-m", "x"], setup=setup, nonce_dir=real,
                                            prove=True)
                self.assertEqual(code, 2, err)
                self.assertLess(time.monotonic() - t0, 10)
                if hub.is_symlink() or not hub.is_dir():
                    hub.unlink()
                else:
                    shutil.rmtree(hub)

    def test_nested_and_overlapping_projects_arent_served(self):
        self.made()
        base = dict(self.docs)
        cases = {"inside another's workspace": {"pb": {**base["pb"], "paths": {"workspace": str(self.tmp)}}},
                 "inside another's allow_write": {"pb": {**base["pb"], "allow_write": [str(self.ws / ".hub-run")]}},
                 "a shared root": {"pb": {**base["pb"], "paths": {"workspace": str(self.ws)}}}}
        for name, change in cases.items():
            with self.subTest(name):
                self.docs = {**base, **change}
                code, _o, err = self.client(["commit", "--allow-empty", "-m", "x"])
                self.assertEqual(code, 2, err)
                self.assertIn("isn't served", err)
        self.docs = base
        code, _o, err = self.client(["commit", "-q", "--allow-empty", "-m", "x"])   # control: no overlap
        self.assertEqual(code, 0, err)

    def test_a_cwd_outside_the_projects_folders(self):
        self.made()
        other = self.tmp / "outside"
        other.mkdir()
        self.owner_git("init", "-q", str(other))
        for cwd, argv in ((other, ["commit", "--allow-empty", "-m", "x"]),
                          (self.ws, ["-C", str(other), "commit", "--allow-empty", "-m", "x"])):
            code, _o, err = self.client(argv, cwd=cwd)
            self.assertEqual(code, 2)
            self.assertIn("worktree inside the project", err)


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced")
class Answers(Fixture):
    def test_not_switched_on(self):
        self.made()
        self.docs["pa"] = {**self.docs["pa"], "enforce": {"sandbox": True}}
        code, _o, err = self.client(["commit", "--allow-empty", "-m", "x"])
        self.assertEqual(code, 2)
        self.assertIn("isn't on", err)

    def test_switched_on_not_rebuilt(self):
        self.made()
        head = self.head()
        f = git_broker.repos_file(self.state, "pa")
        saved = f.read_text()
        f.unlink()
        code, _o, err = self.client(["commit", "--allow-empty", "-m", "x"])
        self.assertEqual(code, 2)
        self.assertIn("isn't rebuilt", err)
        f.write_text(saved)
        shutil.copytree(self.ws / ".git", self.tmp / "copy.git", symlinks=True)
        shutil.rmtree(self.ws / ".git")
        shutil.copytree(self.tmp / "copy.git", self.ws / ".git", symlinks=True)   # same path, another inode
        code, _o, err = self.client(["commit", "--allow-empty", "-m", "x"])
        self.assertEqual(code, 2)
        self.assertIn("isn't rebuilt", err)
        self.assertEqual(self.head(), head)
        self.assertNotIn("p1", [x["path"] for x in self.log()])
        self.assertIsNotNone(git_broker.rebuilt("pa", self.state))
        self.assertIsNone(git_broker.rebuilt("pc", self.state))


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced")
class Wrapper(Fixture):
    def test_outside_a_sandbox_argv_env_and_exit_pass_unchanged(self):
        fake = self.tmp / "fakebin"
        fake.mkdir()
        out = self.tmp / "seen"
        (fake / "git").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {out}\nenv > {out}.env\nexit 7\n')
        (fake / "git").chmod(0o755)
        env = {**self.project_env(path=f"{WRAPPER.parent}:{fake}:/usr/bin:/bin"), "GIT_DIR": "x", "ODD": "a b"}
        r = subprocess.run([str(WRAPPER), "commit", "-m", "two words", "--", "--x"], env=env, capture_output=True,
                           timeout=60, cwd=self.ws)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(out.read_text().splitlines(), ["commit", "-m", "two words", "--", "--x"])
        seen = dict(line.split("=", 1) for line in (out.parent / "seen.env").read_text().splitlines() if "=" in line)
        for k, v in env.items():
            self.assertEqual(seen.get(k), v, k)

    def test_inside_reads_run_in_place_and_a_selecting_environment_fails_loudly(self):
        self.made()
        n = len(self.log())
        r = self.wrapped("status", "--porcelain")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.wrapped("log", "--oneline")
        self.assertIn("init", r.stdout.decode())
        self.assertEqual(len(self.log()), n, "a read went through the broker")
        r = self.as_project([str(WRAPPER), "commit", "--allow-empty", "-m", "x"],
                            env=self.project_env(extra={"GIT_DIR": str(self.ws / ".git")}))
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stderr.decode(), "(?i)not permitted|unable|cannot|fatal")
        self.assertEqual(len(self.log()), n, "a selecting environment was forwarded")

    def test_with_the_broker_down_writes_say_so_and_reads_work(self):
        self.made()
        self.stop.set()
        self.broker.close()
        time.sleep(0.5)
        r = self.wrapped("commit", "--allow-empty", "-m", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("can't be reached", r.stderr.decode())
        r = self.wrapped("log", "--oneline")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_brunna_git_is_the_same_program(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("agenthub_cli", str(BIN / "agenthub"))
        spec = importlib.util.spec_from_loader("agenthub_cli", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        self.assertIn("git", mod.COMMANDS)
        self.assertNotIn("git", policy.SESSION_SUBCOMMANDS)


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced")
class ConfigOp(Fixture):
    def config(self) -> str:
        return (self.ws / ".git" / "config").read_text()

    def test_the_allowlist_and_value_rules(self):
        self.made()
        refused = [("config", "core.fsmonitor", "touch x"), ("config", "core.bare", "true"),
                   ("config", "filter.x.clean", "cat"), ("config", "remote.origin.pushurl", "https://e.invalid/x"),
                   ("config", "remote.origin.url", "https://example.invalid/other.git"),
                   ("config", "branch.main.merge", "refs/heads/main"), ("config", "submodule.s.url", "https://e/x"),
                   ("config", "--global", "user.name", "x"), ("remote", "add", "up", "https://e.invalid/up.git"),
                   ("config", "--unset", "core.repositoryformatversion")]
        for args in refused:
            with self.subTest(args):
                before = self.config()
                r = self.wrapped(*args)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(self.config(), before)
        for args, key, want in ((("config", "user.name", "Proj"), "user.name", "Proj"),
                                (("remote", "add", "origin", "https://example.invalid/pa.git"), "remote.origin.url",
                                 "https://example.invalid/pa.git"),
                                (("config", "branch.w.merge", "refs/heads/w"), "branch.w.merge", "refs/heads/w")):
            with self.subTest(args):
                r = self.wrapped(*args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.owner_git("config", "--get", key).stdout.strip(), want)
        r = self.wrapped("config", "--unset", "user.name")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.owner_git("update-ref", "refs/remotes/origin/main", "HEAD")
        r = self.wrapped("remote", "remove", "origin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("origin", self.config())
        self.assertEqual(self.owner_git("for-each-ref", "refs/remotes/").stdout, "")
        self.assertEqual({x["path"] for x in self.log()} - {"answer"}, {"config"})
        self.assertFalse(list(self.priv.iterdir()), "the config op left its copy")

    def test_the_presets(self):
        self.made()
        self.assertEqual(self.owner_git("config", "--get", "branch.autosetupmerge").stdout.strip(), "false")

    def test_an_existing_config_lock_fails_cleanly(self):
        self.made()
        lock = self.ws / ".git" / "config.lock"
        lock.write_text("the owner's\n")
        before = self.config()
        r = self.wrapped("config", "user.name", "Proj")
        self.assertEqual(r.returncode, 255, r.stderr)
        self.assertIn("config.lock", r.stderr.decode())
        self.assertEqual(lock.read_text(), "the owner's\n")
        self.assertEqual(self.config(), before)
        lock.unlink()
        self.assertEqual(self.wrapped("config", "user.name", "Proj").returncode, 0)

    def test_a_fifo_include_doesnt_hang_it(self):
        self.made()
        fifo = self.tmp / "fifo"
        os.mkfifo(fifo)
        with (self.ws / ".git" / "config").open("a") as f:
            f.write(f"[include]\n\tpath = {fifo}\n")
        t0 = time.monotonic()
        r = self.wrapped("config", "user.name", "Proj", timeout=60)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(r.returncode, 0, r.stderr)


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced")
class OwnerCanaries(Fixture):
    """Section 6: the plants written from inside, then the owner's git in the repo and in a nested one."""

    def plant(self) -> str:
        """Each plant twice where git can write it: through `git` (the wrapper, under the fence) and directly."""
        c, ws = self.tmp / "canary", self.ws
        return " ; ".join([
            f"printf '#!/bin/sh\\ntouch {c}-filter\\ncat\\n' > can-filter.sh",
            f"printf '#!/bin/sh\\ntouch {c}-fsmonitor\\n' > can-fs.sh",
            f"printf '#!/bin/sh\\ntouch {c}-hook\\n' > can-hook.sh",
            f"printf '#!/bin/sh\\ntouch {c}-nested\\n' > can-nested.sh",
            "chmod +x can-*.sh",
            "echo 'f filter=x' > .gitattributes",
            f"git config filter.x.clean {ws}/can-filter.sh",
            f"printf '[filter \"x\"]\\n\\tclean = {ws}/can-filter.sh\\n' >> .git/config",
            f"git config core.fsmonitor {ws}/can-fs.sh",
            f"git -c core.fsmonitor={ws}/can-fs.sh status >/dev/null",
            f"printf '[core]\\n\\tfsmonitor = {ws}/can-fs.sh\\n' >> .git/config",
            "mkdir -p .git/hooks; cp can-hook.sh .git/hooks/post-index-change",
            "mkdir -p .git/info; echo '* filter=x' > .git/info/attributes",
            "git init -q nested",
            f"printf '[core]\\n\\tfsmonitor = {ws}/can-nested.sh\\n' >> nested/.git/config",
            "true",
        ])

    def owner_runs(self):
        (self.ws / "f").write_text("changed\n")
        for args in (("status",), ("add", "f"), ("commit", "-q", "-m", "o"), ("diff", "HEAD~1")):
            self.owner_git(*args, check=False)
        if (self.ws / "nested").is_dir():
            self.owner_git("status", cwd=self.ws / "nested", check=False)
        return sorted(p.name for p in self.tmp.glob("canary-*"))

    def test_none_run_after_the_fenced_plants_and_all_run_after_the_unfenced(self):
        self.made()
        r = self.as_project(["/bin/sh", "-c", self.plant()])
        ran = self.owner_runs()
        self.assertEqual(ran, [], r.stderr)
        for p in list(self.ws.iterdir()):
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        self.made()
        r = self.as_project(["/bin/sh", "-c", self.plant()], profile=self.service_profile(fenced=False, rule=False),
                            env=self.project_env(path="/usr/bin:/bin"))
        ran = self.owner_runs()
        self.assertTrue({"canary-filter", "canary-fsmonitor", "canary-hook", "canary-nested"} <= set(ran),
                        f"control: {ran} {r.stderr}")


@unittest.skipUnless(FENCED, "needs Seatbelt with git-dirs-fenced")
class Limits(Fixture):
    def test_a_timeout_kills_the_group(self):
        self.made()
        os.mkfifo(self.ws / "fifo")
        self.settings["timeout_s"] = 2
        t0 = time.monotonic()
        r = self.wrapped("hash-object", "-w", "fifo", timeout=60)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertIn("stopped after", r.stderr.decode())
        time.sleep(0.5)
        ps = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True, timeout=30).stdout
        self.assertFalse([x for x in ps.splitlines() if "hash-object -w fifo" in x], "the killed call left a process")

    def test_the_concurrency_cap_queues(self):
        self.made()
        os.mkfifo(self.ws / "fifo")
        self.settings["concurrency"] = 1
        self.broker.slots.clear()
        a = subprocess.Popen(["/usr/bin/sandbox-exec", "-p", self.service_profile(), str(WRAPPER), "hash-object",
                              "-w", "fifo"], cwd=self.ws, env=self.project_env(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        time.sleep(1.5)
        b = subprocess.Popen(["/usr/bin/sandbox-exec", "-p", self.service_profile(), str(WRAPPER), "hash-object",
                              "-w", "--stdin"], cwd=self.ws, env=self.project_env(), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        b.stdin.write(b"queued\n")
        b.stdin.close()
        time.sleep(2)
        self.assertIsNone(b.poll(), "the second call ran past the cap")
        with open(self.ws / "fifo", "w") as f:
            f.write("first\n")
        for p in (a, b):
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)

    def test_an_owners_index_lock_fails_it_as_a_second_terminal_would(self):
        self.made()
        lock = self.ws / ".git" / "index.lock"
        lock.write_text("")
        (self.ws / "g").write_text("y\n")
        for args, stdin in ((("add", "g"), None), (("apply", "--cached"), B2.PATCH.replace("x/HEAD", "ok").encode())):
            r = self.wrapped(*args, stdin=stdin)
            self.assertEqual(r.returncode, 128, r.stderr)
            self.assertIn("index.lock", r.stderr.decode())
        self.assertTrue(lock.exists())
        lock.unlink()
        self.assertEqual(self.wrapped("add", "g").returncode, 0)

    def test_the_log_holds_no_argument_values(self):
        self.made()
        self.assertEqual(self.wrapped("commit", "-q", "--allow-empty", "-m", "VALUE-9f8e7d").returncode, 0)
        text = (self.state / "git-broker" / "log.jsonl").read_text()
        self.assertNotIn("VALUE-9f8e7d", text)
        rec = json.loads(text.splitlines()[-1])
        self.assertEqual({k: rec[k] for k in ("project", "socket", "sub", "path", "exit")},
                         {"project": "pa", "socket": "service", "sub": "commit", "path": "p1", "exit": 0})
        self.assertIn("ms", rec)


class Unsupported(unittest.TestCase):
    """Any OS: where the sandbox can't fence git dirs, the broker refuses cleanly (exit 2) and runs nothing."""

    def test_the_broker_refuses_where_git_dirs_cant_be_fenced(self):
        fake = type("A", (), {"can_sandbox": staticmethod(lambda: None), "exec_service": None,
                              "guarantees": staticmethod(lambda: set(isolation.REQUIRED))})
        with mock.patch.object(isolation, "adapter", lambda: ("landlock", fake)), \
                mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": "en", "BRUNNA_LANG_UI": "en"}), \
                mock.patch.object(git_broker, "git_programs", side_effect=AssertionError("ran")):
            b = git_broker.Broker(pathlib.Path("/nonexistent/s"), pathlib.Path("/nonexistent/sh"), projects=dict)
            with self.assertRaises(git_broker.Answer) as e:
                b.start()
            self.assertEqual(e.exception.code, 2)
            self.assertIn("isn't supported", str(e.exception))

    def test_a_socket_path_over_macos_limit_is_bound_and_reached(self):
        """macOS limits a socket's path to 104 bytes; a longer one (a long instance or test folder) is bound and
        reached by its name from its folder, and the working folder is put back."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="g")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        b = git_broker.Broker(tmp / ("x" * 120), tmp / "sh", projects=dict)
        self.addCleanup(b.close)
        here = os.getcwd()
        b.bind("pa")
        self.assertEqual(os.getcwd(), here)
        path = tmp / ("x" * 120) / "git-broker" / "pa" / "service"
        self.assertGreater(len(str(path)), git_broker.SOCKET_MAX)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        git_broker.unix_connect(c, path)
        self.assertEqual(os.getcwd(), here)

    def test_the_folding_matches_heads(self):
        for p in (b"HEAD", b"x/HEAD", b"x/head", b"x/He\xe2\x80\x8cAD", b"a/b/HEAD.", b"HEAD "):
            self.assertTrue(git_broker.folds_to_head(p), p)
        for p in (b"HEADS", b"x/HEAD/y", b"xHEAD", b"README"):
            self.assertFalse(git_broker.folds_to_head(p), p)

    def test_the_wrapper_folder_holds_only_git_under_the_hub_root(self):
        """Every sandbox already denies writes under the hub root (policy deny_write), and no PATH of the owner's holds
        it (open point 14)."""
        self.assertEqual(sorted(os.listdir(WRAPPER.parent)), ["git"])
        self.assertTrue(os.access(WRAPPER, os.X_OK))
        self.assertEqual(WRAPPER.parent.parent, BIN)

    def test_the_wrapper_finds_the_real_git_not_itself(self):
        import git_wrapper
        with mock.patch.dict(os.environ, {"PATH": f"{WRAPPER.parent}:/usr/bin:/bin"}):
            self.assertNotEqual(os.path.realpath(git_wrapper.real_git()), os.path.realpath(WRAPPER))
            self.assertNotIn("git-wrapper", git_wrapper.real_git())

    def test_classification_is_the_brokers_own(self):
        """The broker classifies every request again; a wrapper's word counts for nothing."""
        self.assertEqual(git_allow.classify(["commit", "--exe", "x"], {}).kind, git_allow.REFUSED)


if __name__ == "__main__":
    unittest.main()
