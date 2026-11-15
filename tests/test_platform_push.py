"""The platform push (docs/design/platform-push.md, r1 and r2): a sandboxed project's session asks with `brunna push`, the
mailbox dispatch takes the request in (bin/push_queue.py), a detached worker checks and pushes it (bin/git_remote.py serve,
bin/push_checks.py).

On top of tests/test_git_remote.py's harness: a throwaway registry of one stand-in project (hubconf.projects patched), its
remote a bare repo in the test's temp folder, the platform's state and the push request root there too; the owner's git
under a temp global config. The project's job runs through the stand-in isolation.wrap (and, where it matters, the real
Seatbelt profile). Letters and owner to-dos are recorded at push_queue.project_letter / owner_notify. Each refusal comes
with a control: plain git (or the same thing without the trap) does what the platform refused.

    bin/run-tests <a folder holding this module>
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_git_remote import BIN, IDENT, ROOT, Base  # noqa: E402

import git_remote  # noqa: E402
import hubconf  # noqa: E402
import nofollow  # noqa: E402
import policy  # noqa: E402
import push_checks  # noqa: E402
import push_queue  # noqa: E402
import seatbelt  # noqa: E402

AWS = "AKIA" + "QWERTYUIOPASDFGH"          # a key id shape the built-in rules know (not a real key)
LIMITS = {"requests_per_day": 20, "concurrency": 2, "max_commits": 500, "max_total_mb": 200, "scan_timeout_s": 120,
          "wait_s": 0}


class Push(Base):
    def setUp(self):
        super().setUp()
        self.push_root = self.tmp / "push"
        self.letters, self.todos = [], []
        self.names_file = None
        self.limits = dict(LIMITS)
        for p in (mock.patch.object(hubconf, "push_dir", lambda: self.push_root),
                  mock.patch.object(push_queue, "limits", lambda: dict(self.limits)),
                  mock.patch.object(push_queue, "project_letter",
                                    lambda project, text, key: self.letters.append((project, text, key))),
                  mock.patch.object(push_queue, "owner_notify", lambda *a: self.todos.append(list(a))),
                  mock.patch("private_names.names_file", lambda: self.names_file)):
            p.start()
            self.addCleanup(p.stop)
        self.docs["proj"]["enforce"]["git_push"] = {"branches": "all", "emails": ["dev@example.org"]}
        self.base = self.tip()

    # ---------- helpers ----------
    def field(self, **kw):
        self.docs["proj"]["enforce"]["git_push"].update(kw)

    def request(self, branch="main", sha=None, rid=None, **extra) -> str:
        push_queue.ensure_folder("proj")
        rid = rid or git_remote.request_id()
        sha = sha or self.git(self.ws, "rev-parse", f"refs/heads/{branch}").strip()
        (self.push_root / "proj" / f"{rid}.request.json").write_text(
            json.dumps({"v": 1, "id": rid, "branch": branch, "sha": sha, **extra}))
        return rid

    def take(self):
        return push_queue.take_in("proj", letter=lambda p, t, k: self.letters.append((p, t, k)))

    def serve(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            return git_remote.serve("proj")

    def result(self, rid) -> dict:
        return json.loads((self.push_root / "proj" / f"{rid}.result.json").read_text())

    def ask(self, branch="main", sha=None) -> dict:
        rid = self.request(branch, sha)
        self.assertEqual(self.take(), [rid])
        self.serve()
        return self.result(rid)

    def pushes(self) -> list[dict]:
        f = self.owned().parent / "pushes.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def commit_as(self, path, text, msg, **env_over):
        env = {**os.environ, **env_over}
        f = self.ws / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text) if isinstance(text, bytes) else f.write_text(text)
        self.git(self.ws, "add", "--", path, env=env)
        self.git(self.ws, "commit", "-q", "-m", msg, env=env)
        return self.git(self.ws, "rev-parse", "HEAD").strip()

    def refused(self, res, check=None, says=None):
        self.assertEqual(res["state"], "refused", res)
        if check:
            self.assertEqual(res["check"], check, res)
        if says:
            self.assertIn(says, res["reason"])
        self.assertEqual(self.tip(), self.base, "the remote moved")


# ---------- who may request (S4, r2 S-h, S-i, S-j) ----------

class WhoMayRequest(unittest.TestCase):
    """The request folder is rendered only into the project's session settings."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        ws, other = self.tmp / "ws" / "proj", self.tmp / "ws" / "other"
        for d in (ws, other):
            d.mkdir(parents=True)
        self.docs = {"proj": {"name": "proj", "paths": {"workspace": str(ws)}, "enforce": {"sandbox": True}},
                     "other": {"name": "other", "paths": {"workspace": str(other)}, "enforce": {"sandbox": True}}}
        self.ws = ws
        for p in (mock.patch.object(hubconf, "push_dir", lambda: self.tmp / "push"),
                  mock.patch.object(hubconf, "projects", lambda: self.docs)):
            p.start()
            self.addCleanup(p.stop)
        self.pol = policy.policy("proj", self.docs["proj"], {"projects": self.docs})

    def test_only_the_session_rendering_carries_it(self):
        import runtime_claude
        own, theirs = str(self.tmp / "push" / "proj"), str(self.tmp / "push" / "other")
        self.assertEqual([str(p) for p in self.pol["session_write"]], [own])
        session = runtime_claude.session_settings(self.pol)
        allow = [os.path.expanduser(w) for w in session[0]["filesystem"]["allowWrite"]]
        self.assertIn(own, allow)
        self.assertNotIn(theirs, allow)
        self.assertIn(policy.tilde(theirs), session[0]["filesystem"]["denyRead"])
        # nothing a control tree may be built in (r2 S-h): its folder is one more place
        self.assertTrue(any(r.startswith(f"Edit(/{own}/**/") for r in session[1]), "no protect rule under it")
        # the platform's runs (platform_settings builds its sandbox from settings()), leases and the services' profile
        platform = runtime_claude.settings(self.pol)
        self.assertNotIn(own, [os.path.expanduser(w) for w in platform[0]["filesystem"]["allowWrite"]])
        self.assertFalse(any(own in r for r in platform[1]), "the platform's own rules name it")
        self.assertNotIn(own, json.dumps(runtime_claude.lease_settings(self.pol)))
        self.assertNotIn(own, [str(w) for w in self.pol["write"]])
        self.assertNotIn(own, seatbelt.service_profile(self.pol, [self.ws]))
        self.assertIn(theirs, seatbelt.service_profile(self.pol, [self.ws]), "another project's folder is denied there")

    def test_an_unsandboxed_project_has_none(self):
        doc = {"name": "plain", "paths": {"workspace": str(self.ws)}}
        self.assertEqual(policy.policy("plain", doc, {"projects": {**self.docs, "plain": doc}})["session_write"], [])

    def test_codex_says_not_supported(self):
        import runtime_ports
        self.assertIn("session_write", runtime_ports.capabilities("claude"))
        self.assertNotIn("session_write", runtime_ports.capabilities("codex"))
        self.assertIn("Codex", runtime_ports.missing_label("codex", "session_write"))
        self.assertIn("session_write", runtime_ports.OPTIONAL)   # load() of other runtimes is unchanged

    @unittest.skipUnless(seatbelt.can_sandbox() is None, "needs Seatbelt (macOS); Landlock's services list only what "
                         "they allow, and session_write is never in a service's list (policy, checked above)")
    def test_a_service_cant_write_it_under_the_real_profile(self):
        folder = self.tmp / "push" / "proj"
        folder.mkdir(parents=True)
        prof = seatbelt.service_profile(self.pol, [self.ws])

        def attempt(target):
            return subprocess.run([seatbelt.SANDBOX_EXEC, "-p", prof, "/bin/sh", "-c", f'echo x > "{target}"'],
                                  capture_output=True, text=True).returncode == 0
        self.assertFalse(attempt(folder / "r.request.json"))
        self.assertFalse((folder / "r.request.json").exists())
        self.assertTrue(attempt(self.ws / "control.txt"), "the control: the service writes its workspace")


# ---------- the session request folder (r2 K1) and taking in (S2, r2 S-f, S-g) ----------

class TakingIn(Push):
    def test_a_link_at_the_folder_is_never_followed(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        rid = git_remote.request_id()
        planted = outside / f"{rid}.request.json"
        planted.write_text(json.dumps({"v": 1, "id": rid, "branch": "main", "sha": self.base}))
        (outside / "keep.txt").write_text("mine\n")
        self.push_root.mkdir(mode=0o700)
        (self.push_root / "proj").symlink_to(outside)
        self.assertEqual(self.take(), [])
        self.assertTrue(planted.exists(), "a file through the link was taken (and removed)")
        self.assertEqual((outside / "keep.txt").read_text(), "mine\n")
        self.assertFalse((self.push_root / "proj").is_symlink())
        self.assertTrue((self.push_root / "proj").is_dir())
        # a result written for an id is never written through a link either
        os.rmdir(self.push_root / "proj")
        (self.push_root / "proj").symlink_to(outside)
        push_queue.write_result("proj", push_queue.result("proj", rid, "refused", "x"))
        self.assertFalse((outside / f"{rid}.result.json").exists())
        self.assertTrue((self.push_root / "proj" / f"{rid}.result.json").is_file())
        self.assertFalse((self.push_root / "proj").is_symlink())
        # control: named as the base, the link is followed
        shutil.rmtree(self.push_root / "proj")
        (self.push_root / "proj").symlink_to(outside)
        self.assertIsNotNone(nofollow.read(self.push_root / "proj", planted.name))

    def test_only_request_files_are_touched(self):
        push_queue.ensure_folder("proj")
        other = self.push_root / "proj" / "notes.txt"
        other.write_text("x\n")
        self.take()
        self.assertTrue(other.exists())

    def test_a_request_becomes_a_platform_made_file(self):
        rid = self.request(project="someone-else", extra="ignored")
        self.assertEqual(self.take(), [rid])
        self.assertFalse((self.push_root / "proj" / f"{rid}.request.json").exists())
        q = push_queue.queued("proj")
        self.assertEqual([x["id"] for x in q], [rid])
        self.assertEqual(q[0]["project"], "proj", "a project named in the file is never read")
        self.assertNotIn("extra", q[0])
        self.assertEqual(oct((push_queue.broker("proj") / "requests" / f"{rid}.json").stat().st_mode & 0o777), "0o600")

    def test_a_hard_link_is_refused(self):
        push_queue.ensure_folder("proj")
        rid = git_remote.request_id()
        src = self.tmp / "elsewhere.json"
        src.write_text(json.dumps({"v": 1, "id": rid, "branch": "main", "sha": self.base}))
        os.link(src, self.push_root / "proj" / f"{rid}.request.json")
        self.take()
        self.assertEqual(self.result(rid)["state"], "refused")
        self.assertEqual(push_queue.queued("proj"), [])

    def test_malformed(self):
        push_queue.ensure_folder("proj")
        rid = git_remote.request_id()
        (self.push_root / "proj" / f"{rid}.request.json").write_text(json.dumps({"v": 1, "id": rid, "branch": "main",
                                                                                 "sha": "nothex"}))
        (self.push_root / "proj" / "bad name.request.json").write_text("{}")
        self.take()
        self.assertEqual(self.result(rid)["state"], "refused")
        self.assertIn("full commit id", self.result(rid)["reason"])
        self.assertEqual(len(self.letters), 1, "an id that can't be read gets a letter")
        self.assertEqual(os.listdir(self.push_root / "proj"), [f"{rid}.result.json"])

    def test_one_at_a_time_and_the_same_branch_replaced(self):
        self.git(self.ws, "branch", "feature")
        first = self.request("main")
        self.take()
        other = self.request("feature")
        self.take()
        self.assertEqual(self.result(other)["state"], "refused")
        self.assertIn(first, self.result(other)["reason"])
        newer = self.request("main", sha=self.commit(self.ws, "a.txt", "a\n", "a"))
        self.take()
        self.assertEqual(self.result(first)["state"], "superseded")
        q = push_queue.queued("proj")
        self.assertEqual([x["id"] for x in q], [newer])
        self.assertEqual(q[0]["supersedes"]["id"], first)
        # a started one is never replaced
        push_queue._write_json(push_queue.broker("proj") / "worker.json",
                               {"pid": os.getpid(), "start": None, "id": newer, "started": newer})
        later = self.request("main")
        self.take()
        self.assertEqual(self.result(later)["state"], "refused")

    def test_the_same_id_twice_does_nothing(self):
        rid = self.request()
        self.take()
        before = list(push_queue.queued("proj"))
        self.request(rid=rid)
        self.assertEqual(self.take(), [])
        self.assertEqual(push_queue.queued("proj"), before)
        self.assertFalse((self.push_root / "proj" / f"{rid}.request.json").exists())

    def test_the_daily_cap(self):
        self.limits["requests_per_day"] = 2
        ids = []
        for _i in range(3):
            ids.append(self.request())
            self.take()
            for f in (push_queue.broker("proj") / "requests").glob("*.json"):
                f.unlink()
        self.assertEqual(self.result(ids[2])["state"], "refused")
        self.assertEqual(self.result(ids[2])["fix"], "wait")
        self.assertFalse((self.push_root / "proj" / f"{ids[1]}.result.json").exists(), "the second was queued")

    def test_old_results_go(self):
        push_queue.ensure_folder("proj")
        old = self.push_root / "proj" / "m01010000-000000000000.result.json"
        old.write_text("{}")
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        self.take()
        self.assertFalse(old.exists())


# ---------- the worker (r1 S2, S10, r2 S-k) ----------

class Worker(Push):
    def test_a_request_is_pushed_pinned_to_its_sha(self):
        a = self.commit(self.ws, "a.txt", "a\n", "add a")
        rid = self.request("main", sha=a)
        self.take()
        self.commit(self.ws, "b.txt", "b\n", "add b, after the request")
        self.serve()
        res = self.result(rid)
        self.assertEqual(res["state"], "pushed", res)
        self.assertEqual(self.tip(), a, "the requested sha is pushed, not the branch's tip (r2 S-g)")
        self.assertEqual(push_queue.queued("proj"), [])
        self.assertFalse((push_queue.broker("proj") / "worker.json").exists())
        [(proj, text, _key)] = self.letters
        self.assertEqual(proj, "proj")
        self.assertIn("pushed", text)
        rec = self.pushes()[-1]
        self.assertEqual((rec["new"], rec["state"], rec["checks"]), (a, "pushed", "passed"))
        self.assert_owner_git_only_in_owned_repos()

    def test_a_sha_no_longer_on_the_branch(self):
        a = self.commit(self.ws, "a.txt", "a\n", "add a")
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        self.commit(self.ws, "b.txt", "b\n", "rewritten")
        self.refused(self.ask("main", sha=a), check="fast-forward", says="not on main")

    def test_the_field(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.field(branches="none")
        res = self.ask()
        self.refused(res, check="field")
        self.assertEqual(res["fix"], "owner")
        self.assertTrue(res["todo"].startswith("push-proj-branch-"))
        self.field(branches="others")
        self.refused(self.ask("main"), check="field")
        self.git(self.ws, "checkout", "-q", "-b", "feature")
        f = self.commit(self.ws, "f.txt", "f\n", "feature")
        self.assertEqual(self.ask("feature")["state"], "pushed")
        self.assertEqual(self.tip("refs/heads/feature"), f)
        self.assertEqual(len([t for t in self.todos if t[:2] == ["--type", "task"]]), 1, "one to-do per episode")

    def test_the_branch_todo_closes_when_the_owner_pushes_that_sha(self):
        a = self.commit(self.ws, "a.txt", "a\n", "add a")
        self.field(branches="none")
        key = self.ask()["todo"]
        self.assertFalse([t for t in self.todos if "done" in t])
        code, _out, err = self.push("main", stdin_tty=True, answer="y")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tip(), a)
        self.assertIn(["--type", "done", "--task", key, "--text", "pushed", "--context", "push"], self.todos)

    def test_no_push_key_for_a_remote_origin(self):
        """Owner decision 1: an automatic push never uses the owner's credential; without the platform's key it refuses
        before any network step. Control: the owner form contacts the remote with the owner's environment."""
        self.docs["proj"]["paths"]["origin"] = "https://127.0.0.1:9/x.git"
        self.commit(self.ws, "a.txt", "a\n", "add a")
        with mock.patch.dict(sys.modules, {"git_push_grant": None}):
            res = self.ask()
        self.assertEqual((res["state"], res["check"], res["fix"]), ("refused", "credential", "owner"))
        self.assertIn("push setup", res["reason"])
        self.assertFalse([c for c in self.calls if "ls-remote" in c[0]], "a network step ran")
        self.calls.clear()
        self.push("main", "--yes")
        self.assertTrue([c for c in self.calls if "ls-remote" in c[0]], "the control contacted nothing")

    def test_the_grants_environment_and_nothing_of_the_owners(self):
        envs = []
        real = git_remote.run_git
        fake = types.ModuleType("git_push_grant")

        class NotSetUp(Exception):
            pass
        fake.NotSetUp = NotSetUp
        fake.ssh_env = lambda project: {"GIT_SSH_COMMAND": "ssh -F /dev/null -o IdentitiesOnly=yes", "GRANT_MARK": project}
        self.docs["proj"]["paths"]["origin"] = "https://127.0.0.1:9/x.git"
        self.commit(self.ws, "a.txt", "a\n", "add a")
        with mock.patch.dict(sys.modules, {"git_push_grant": fake}), \
                mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/owner/agent", "GIT_ASKPASS": "/owner/askpass"}), \
                mock.patch.object(git_remote, "run_git", lambda argv, cwd, env, **kw: (envs.append((argv, env)),
                                                                                       real(argv, cwd, env, **kw))[1]):
            res = self.ask()
        net = [e for a, e in envs if "ls-remote" in a]
        self.assertTrue(net)
        for e in net:
            self.assertEqual(e.get("GRANT_MARK"), "proj")
            self.assertNotIn("SSH_AUTH_SOCK", e)
            self.assertNotIn("GIT_ASKPASS", e)
            self.assertNotEqual(e.get("GIT_CONFIG_GLOBAL"), str(self.gitconfig), "the owner's global config")
        self.assertEqual(res["state"], "failed")
        self.assertNotIn("127.0.0.1", res["reason"], "git's own words reached the project (r1 S8)")
        fake.ssh_env = lambda project: (_ for _ in ()).throw(NotSetUp("no key"))
        with mock.patch.dict(sys.modules, {"git_push_grant": fake}):
            res = self.ask()
        self.assertEqual((res["state"], res["check"]), ("refused", "credential"))

    def test_doctors_credential_probe(self):
        """doctor runs ls-remote as the worker would: no key → its row says so; with the grant's environment it reaches
        the remote. Control: a local origin needs no key."""
        self.docs["proj"]["paths"]["origin"] = "https://127.0.0.1:9/x.git"
        with mock.patch.dict(sys.modules, {"git_push_grant": None}):
            rows = git_remote.doctor_lines()
        self.assertIn("bad", [lvl for lvl, what, _f in rows if "proj" in what])
        fake = types.ModuleType("git_push_grant")
        fake.NotSetUp = type("NotSetUp", (Exception,), {})
        fake.ssh_env = lambda project: {}
        self.docs["proj"]["paths"]["origin"] = f"file://{self.remote}"
        with mock.patch.dict(sys.modules, {"git_push_grant": fake}):
            rows = git_remote.doctor_lines()
        self.assertEqual([lvl for lvl, what, _f in rows if "proj" in what], ["ok"])
        self.assertEqual(git_remote.credential_probe("proj", f"file://{self.remote}", "x")[0], "ok")
        self.assertEqual(git_remote.credential_probe("proj", str(self.tmp / "missing.git"), "x")[0], "bad")

    def test_the_remotes_own_words_reach_only_the_owner(self):
        """S8: a canary in the remote's refusal never reaches the result or the letter; pushes.jsonl and the to-do have
        it. Control: plain git shows it."""
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'CANARY-5e1 protected branch' >&2\nexit 1\n")
        hook.chmod(0o755)
        self.commit(self.ws, "a.txt", "a\n", "add a")
        res = self.ask()
        self.assertEqual((res["state"], res["fix"]), ("failed", "owner"))
        blob = json.dumps(res) + json.dumps(self.letters)
        self.assertNotIn("CANARY-5e1", blob)
        self.assertIn("CANARY-5e1", json.dumps(self.pushes()[-1]))
        self.assertTrue(any("CANARY-5e1" in " ".join(t) for t in self.todos))
        r = subprocess.run(["git", "-C", str(self.ws), "push", "origin", "main"], capture_output=True, text=True)
        self.assertIn("CANARY-5e1", r.stderr)

    def test_an_exception_is_a_classified_answer(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        with mock.patch.object(git_remote, "prepare", side_effect=RuntimeError(f"boom {hubconf.private_tmp()}")):
            res = self.ask()
        self.assertEqual((res["state"], res["check"]), ("failed", "platform"))
        self.assertNotIn(str(hubconf.private_tmp()), json.dumps(res) + json.dumps(self.letters))

    def test_the_lock_waits_for_the_owner(self):
        a = self.commit(self.ws, "a.txt", "a\n", "add a")
        rid = self.request()
        self.take()
        hold = threading.Event()
        done = []

        def owner():
            with git_remote.locked(push_queue.broker("proj")):
                hold.set()
                time.sleep(2)
        t = threading.Thread(target=owner)
        t.start()
        hold.wait()
        t0 = time.monotonic()
        done.append(self.serve())
        t.join()
        self.assertGreaterEqual(time.monotonic() - t0, 1.5, "the worker didn't wait for the owner's push")
        self.assertEqual(self.result(rid)["state"], "pushed")
        self.assertEqual(self.tip(), a)

    def test_a_dead_worker_is_replaced_once_then_lost(self):
        rid = self.request()
        self.take()
        spawned = []
        dead = subprocess.Popen(["true"])
        dead.wait()
        spawn = lambda project: (spawned.append(project), (dead.pid, "gone"))[1]  # noqa: E731
        push_queue.tick(spawn=spawn)
        self.assertEqual(spawned, ["proj"])
        self.assertEqual(push_queue.worker("proj")["attempts"], 1)
        push_queue.tick(spawn=spawn)   # its worker died: started again
        self.assertEqual(push_queue.worker("proj")["attempts"], 2)
        push_queue.tick(spawn=spawn, letter=lambda p, t, k: self.letters.append((p, t, k)))
        self.assertEqual(len(spawned), 2)
        res = self.result(rid)
        self.assertEqual((res["state"], res["check"]), ("failed", "worker"))
        self.assertEqual(push_queue.queued("proj"), [])
        # a live worker is left alone, and no request means nothing is started
        push_queue.tick(spawn=spawn)
        self.assertEqual(len(spawned), 2)

    def test_concurrency(self):
        self.limits["concurrency"] = 1
        self.docs["two"] = {**self.docs["proj"], "name": "two"}
        for project in ("proj", "two"):
            push_queue.ensure_folder(project)
            rid = git_remote.request_id()
            (self.push_root / project / f"{rid}.request.json").write_text(
                json.dumps({"v": 1, "id": rid, "branch": "main", "sha": self.base}))
            push_queue.take_in(project)
        spawned = []
        push_queue.tick(spawn=lambda project: (spawned.append(project), (os.getpid(), None))[1])
        self.assertEqual(len(spawned), 1)


# ---------- the checks (section 4, r1 B1 B2 S3 S9, r2 S-b..S-e, S-l, S-m) ----------

class Checks(Push):
    def fake_gitleaks(self, version="8.21.0", stdin_exit=0, report="[]"):
        d = self.tmp / "gl"
        d.mkdir(exist_ok=True)
        g = d / "gitleaks"
        g.write_text(f'#!/bin/sh\ncase "$1" in version) echo {version}; exit 0;; esac\n'
                     f'cat > /dev/null\nwhile [ $# -gt 0 ]; do [ "$1" = --report-path ] && echo \'{report}\' > "$2"; '
                     f'shift; done\nexit {stdin_exit}\n')
        g.chmod(0o755)
        p = mock.patch.dict(os.environ, {"PATH": f"{d}:{os.environ['PATH']}"})
        p.start()
        self.addCleanup(p.stop)

    def log_p(self) -> str:
        return self.git(self.ws, "log", "-p", "main", "--not", "origin/main")

    def test_a_secret_in_a_merge_commits_own_blob(self):
        self.git(self.ws, "checkout", "-q", "-b", "side")
        self.commit(self.ws, "s.txt", "s\n", "side")
        self.git(self.ws, "checkout", "-q", "main")
        self.commit(self.ws, "m.txt", "m\n", "main")
        self.git(self.ws, "merge", "-q", "--no-commit", "side")
        (self.ws / "evil.txt").write_text(f"key = {AWS}\n")
        self.git(self.ws, "add", "evil.txt")
        self.git(self.ws, "commit", "-q", "-m", "merge")
        self.assertNotIn(AWS, self.log_p(), "the control: git log -p shows a merge's own change")
        self.refused(self.ask(), check="secrets", says="aws-access-key-id")

    def test_a_secret_behind_a_nul(self):
        self.commit_as("data.bin", b"\0" * 10 + f"\n{AWS}\n".encode(), "binary-looking")
        self.assertNotIn(AWS, self.log_p())
        self.refused(self.ask(), check="secrets")

    def test_a_secret_added_then_removed(self):
        self.commit(self.ws, "k.txt", f"{AWS}\n", "add")
        self.git(self.ws, "rm", "-q", "k.txt")
        self.git(self.ws, "commit", "-q", "-m", "remove")
        self.assertNotIn(AWS, self.git(self.ws, "diff", "origin/main", "main"))
        self.refused(self.ask(), check="secrets")

    def test_utf16_and_a_message(self):
        self.commit_as("w.txt", ("﻿" + f"key {AWS}\n").encode("utf-16-le"), "utf-16")
        self.refused(self.ask(), check="secrets")
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        self.commit(self.ws, "a.txt", "a\n", f"message with {AWS}")
        self.refused(self.ask(), check="secrets", says="message")

    def test_gitleaks_allow_and_ignore_files_change_nothing(self):
        self.commit(self.ws, ".gitleaksignore", "*\n", "ignore all")
        self.commit(self.ws, ".gitleaks.toml", "[allowlist]\npaths = ['.*']\n", "allow all")
        self.commit(self.ws, "k.txt", f"{AWS}  # gitleaks:allow\n", "marked")
        self.refused(self.ask(), check="secrets")

    def test_a_new_env_file_with_gitleaks_installed(self):
        self.fake_gitleaks()
        self.commit(self.ws, ".env", "PLAIN=1\n", "env")
        self.refused(self.ask(), check="secrets", says="env-file")
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        self.commit(self.ws, ".env.example", "PLAIN=1\n", "example")
        self.assertEqual(self.ask()["state"], "pushed", "the control: an example file passes")

    def test_a_renamed_old_blob_becomes_a_key_file(self):
        self.commit(self.ws, "notes.txt", "plain\n", "notes")
        self.assertEqual(self.ask()["state"], "pushed")
        self.base = self.tip()
        self.git(self.ws, "mv", "notes.txt", "id_rsa")
        self.git(self.ws, "commit", "-q", "-m", "rename")
        self.refused(self.ask(), check="secrets", says="ssh-key-file")

    def test_gitleaks_failing_or_too_old_refuses(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        self.fake_gitleaks(stdin_exit=3)
        res = self.ask()
        self.refused(res, check="secrets", says="scanner failed")
        self.fake_gitleaks(version="8.0.0")
        res = self.ask()
        self.refused(res, check="secrets", says="older than 8.19")
        self.assertEqual(res["fix"], "owner")
        self.fake_gitleaks()
        self.assertEqual(self.ask()["state"], "pushed", "the control: a gitleaks that finds nothing")

    def test_gitleaks_findings_name_the_file(self):
        self.fake_gitleaks(stdin_exit=1, report='[{"RuleID": "generic-api-key", "StartLine": 2}]')
        self.commit(self.ws, "conf.txt", "token here\n", "conf")
        res = self.ask()
        self.refused(res, check="secrets", says="generic-api-key")

    def test_declared_values(self):
        """S3: a declared secret at an absolute path, at ~/… or behind a link is never read (nor said to the project);
        the control, declared at a relative path in its own folder, is found by name; r2 S-e: and stays known once the
        declaration goes."""
        value = "zq8-" + "x7" * 10
        (self.ws / "local.key").write_text(value + "\n")
        outside = self.tmp / "outside.key"
        outside.write_text(value + "\n")
        (self.ws / "linked.key").symlink_to(outside)
        for i, path in enumerate((str(outside), "~/outside.key", "linked.key")):
            with self.subTest(path=path):
                self.git(self.ws, "checkout", "-q", "-b", f"b{i}", self.base)
                self.commit(self.ws, "a.txt", f"v={value}\n", "uses the value")
                self.docs["proj"]["secrets"] = [{"name": "k", "path": path}]
                res = self.ask(f"b{i}")
                self.assertEqual(res["state"], "pushed", res)
        self.git(self.ws, "checkout", "-q", "-b", "c1", self.base)
        self.commit(self.ws, "a.txt", f"v={value}\n", "uses the value")
        self.docs["proj"]["secrets"] = [{"name": "the-key", "path": "local.key"}]
        self.refused(self.ask("c1"), check="secrets", says="the-key")
        self.docs["proj"]["secrets"] = []
        self.refused(self.ask("c1"), check="secrets", says="the-key")

    def test_objects(self):
        # an LFS pointer
        self.commit(self.ws, "big.bin", "version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 1\n", "lfs")
        self.refused(self.ask(), check="objects", says="LFS")
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        # a gitlink
        tree = self.git(self.ws, "rev-parse", "HEAD^{tree}").strip()
        listing = self.git(self.ws, "ls-tree", tree) + f"160000 commit {self.base}\tsub\n"
        t2 = subprocess.run(["git", "-C", str(self.ws), "mktree"], input=listing, capture_output=True, text=True,
                            check=True).stdout.strip()
        c = self.git(self.ws, "commit-tree", "-p", "HEAD", "-m", "a gitlink", t2).strip()
        self.git(self.ws, "update-ref", "refs/heads/main", c)
        self.refused(self.ask(), check="objects", says="gitlink")
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        # a signed commit (a gpgsig header)
        raw = self.git(self.ws, "cat-file", "commit", "HEAD")
        idents = "\n".join(x for x in raw.partition("\n\n")[0].splitlines() if x.startswith(("author ", "committer ")))
        signed = (f"tree {tree}\nparent {self.base}\n{idents}\ngpgsig -----BEGIN PGP SIGNATURE-----\n x\n "
                  "-----END PGP SIGNATURE-----\n\nsigned\n")
        c = subprocess.run(["git", "-C", str(self.ws), "hash-object", "-t", "commit", "-w", "--stdin"], input=signed,
                           capture_output=True, text=True, check=True).stdout.strip()
        child = self.git(self.ws, "commit-tree", "-p", c, "-m", "on top", tree).strip()
        self.git(self.ws, "update-ref", "refs/heads/main", child)
        self.refused(self.ask(), check="objects", says="gpgsig")
        self.assertEqual(self.bare(self.owned(), "cat-file", "-t", child), "", "a refused push left objects (S9)")

    def test_size_before_the_scans(self):
        self.field(max_file_mb=1)
        self.commit_as("big.bin", os.urandom(3 << 20), "big")
        with mock.patch.object(push_checks, "scan", side_effect=AssertionError("scanned")):
            self.refused(self.ask(), check="size")
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        (self.ws / "a.txt").write_text("a\n")
        (self.tmp / "msg").write_text("huge message " + "m" * (1 << 20))
        self.git(self.ws, "add", "a.txt")
        self.git(self.ws, "commit", "-q", "-F", str(self.tmp / "msg"))
        self.refused(self.ask(), check="size", says="as an object")

    def test_the_bundle_cap(self):
        self.limits["max_total_mb"] = 1
        self.commit_as("big.bin", os.urandom(3 << 20), "big")
        self.refused(self.ask(), check="size")

    def test_identity(self):
        self.commit_as("a.txt", "a\n", "elsewhere", GIT_AUTHOR_EMAIL="other@example.org")
        self.refused(self.ask(), check="identity", says="author")
        self.field(emails=[])
        res = self.ask()
        self.refused(res, check="identity")
        self.assertEqual(res["fix"], "owner")
        self.assertTrue(res["todo"].startswith("push-proj-emails-"))

    def test_a_public_remote(self):
        self.commit(self.ws, "a.txt", f"see {pathlib.Path.home()}/x\n", "a")
        with mock.patch.object(git_remote, "visibility", lambda origin: "public"):
            res = self.ask()
        self.refused(res)
        self.assertIn(res["check"], ("identity", "names"))
        # with a noreply address, the home path is what remains
        self.field(emails=["1+dev@users.noreply.github.com"])
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        self.commit_as("a.txt", f"see {pathlib.Path.home()}/x\n", "a",
                       GIT_AUTHOR_EMAIL="1+dev@users.noreply.github.com",
                       GIT_COMMITTER_EMAIL="1+dev@users.noreply.github.com")
        with mock.patch.object(git_remote, "visibility", lambda origin: "public"):
            self.refused(self.ask(), check="names", says="home path")
        self.assertEqual(self.ask()["state"], "pushed", "the control: the same on a private remote")

    def test_private_names_on_a_public_remote(self):
        self.names_file = self.tmp / "private-names.txt"
        self.names_file.write_text("secretproj\n")
        self.field(emails=["dev@example.org"], public_emails=["dev@example.org"])
        self.commit(self.ws, "secretproj/readme.txt", "hello\n", "a")
        with mock.patch.object(git_remote, "visibility", lambda origin: "unknown"):   # can't tell: public
            self.refused(self.ask(), check="names", says="secretproj")
        self.names_file = self.tmp / "missing.txt"
        with mock.patch("private_names.names_file", side_effect=__import__("private_names").ListError("x")), \
                mock.patch.object(git_remote, "visibility", lambda origin: "public"):
            self.refused(self.ask(), check="names", says="can't be read")

    def test_a_ci_change(self):
        self.commit(self.ws, ".gitlab-ci.yml", "x: 1\n", "ci")
        self.refused(self.ask(), check="ci", says=".gitlab-ci.yml")
        self.docs["proj"]["enforce"]["git_ci"] = True
        self.assertEqual(self.ask()["state"], "pushed")

    def test_visibility_probe(self):
        self.assertEqual(git_remote.visibility(str(self.remote)), "private")
        self.assertEqual(git_remote.https_form("git@example.org:a/b.git"), "https://example.org/a/b.git")
        self.assertEqual(git_remote.https_form("ssh://git@example.org:22/a/b.git"), "https://example.org/a/b.git")
        self.assertEqual(git_remote.visibility("https://127.0.0.1:9/x.git"), "unknown")


@unittest.skipUnless(shutil.which("git"), "needs git")
class Probe(unittest.TestCase):
    """check 8's anonymous probe against a real http transport: open → public, 401 → private."""

    def test_open_and_closed(self):
        import base64
        import http.server
        tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(tmp / "r.git")], check=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
        need = {"auth": True}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if need["auth"] and self.headers.get("Authorization") != "Basic " + base64.b64encode(b"a:b").decode():
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="t"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path, _q, query = self.path.partition("?")
                r = subprocess.run(["git", "http-backend"], capture_output=True, env={
                    "PATH": os.environ["PATH"], "GIT_PROJECT_ROOT": str(tmp), "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query, "GIT_CONFIG_GLOBAL": "/dev/null"})
                head, _s, body = r.stdout.partition(b"\r\n\r\n")
                self.send_response(200)
                for line in head.decode().splitlines():
                    k, _c, v = line.partition(":")
                    if k.lower() != "status":
                        self.send_header(k, v.strip())
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_port}/r.git"
        self.assertEqual(git_remote.visibility(url), "private")
        need["auth"] = False
        self.assertEqual(git_remote.visibility(url), "public")


# ---------- the owner form (r1 S1, S5) ----------

class OwnerForm(Push):
    def test_flags_and_the_override(self):
        self.commit(self.ws, "k.txt", f"{AWS}\n", "a key")
        code, out, err = self.push("main", "--yes")
        self.assertEqual(code, git_remote.REFUSED, err)
        self.assertIn("never --yes", err)
        self.assertIn("secrets !!", out)
        self.assertIn("origin ✓", out)
        self.assertEqual(self.tip(), self.base)
        self.assertEqual(self.bare(self.owned(), "cat-file", "-t", self.git(self.ws, "rev-parse", "HEAD").strip()), "",
                         "the refused commits reached the owned repo")
        code, out, err = self.push("main", stdin_tty=True, answer="y")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(self.tip(), self.base)
        self.assertTrue(self.pushes()[-1]["overridden"])

    def test_setup_is_never_a_project(self):
        self.docs["setup"] = dict(self.docs["proj"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = git_remote.main(["push", "setup"])
        self.assertEqual(code, git_remote.REFUSED)
        self.assertIn("usage", err.getvalue())

    def test_refused_where_the_platforms_folder_cant_be_written(self):
        self.commit(self.ws, "a.txt", "a\n", "add a")
        d = push_queue.broker("proj")
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o500)
        self.addCleanup(d.chmod, 0o700)
        self.calls.clear()
        code, _out, err = self.push("main", "--yes")
        self.assertEqual(code, git_remote.REFUSED)
        self.assertIn("your own terminal", err)
        self.assertEqual(self.calls, [], "it contacted something first")
        self.assertEqual(self.wrapped, [])


# ---------- the forms and the client ----------

class Forms(Push):
    def main(self, args, cwd):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = git_remote.main(["push", *args], cwd=cwd)
        return code, out.getvalue(), err.getvalue()

    def test_a_session_requests_and_waits(self):
        a = self.commit(self.ws, "a.txt", "a\n", "add a")
        push_queue.ensure_folder("proj")
        code, out, _err = self.main(["--no-wait"], self.ws)
        self.assertEqual(code, git_remote.PENDING)
        [name] = [n for n in os.listdir(self.push_root / "proj") if n.endswith(".request.json")]
        req = json.loads((self.push_root / "proj" / name).read_text())
        self.assertEqual((req["branch"], req["sha"]), ("main", a))
        self.take()
        self.serve()
        self.assertEqual(git_remote.await_result("proj", req["id"], 0), git_remote.OK)
        self.assertEqual(self.tip(), a)

    def test_in_an_unsandboxed_project(self):
        del self.docs["proj"]["enforce"]
        code, _out, err = self.main(["main"], self.ws)
        self.assertEqual(code, git_remote.REFUSED)
        self.assertIn("push with git as usual", err)

    def test_a_confined_run_cant_ask(self):
        import names
        push_queue.ensure_folder("proj")
        env = {}
        names.env_set(env, "CONFINED", "1")
        with mock.patch.dict(os.environ, env):
            code, _out, err = self.main(["main", "--no-wait"], self.ws)
        self.assertEqual(code, git_remote.REFUSED)
        self.assertEqual([n for n in os.listdir(self.push_root / "proj") if n.endswith(".json")], [])

    def test_the_folder_isnt_there_yet(self):
        code, _out, err = self.main(["main", "--no-wait"], self.ws)
        self.assertEqual(code, git_remote.FAILED)
        self.assertIn("within a minute", err)


# ---------- the registry field ----------

class Field(unittest.TestCase):
    def test_validation(self):
        import registry
        ok = {"name": "p", "paths": {"origin": "git@example.org:a/b.git"},
              "enforce": {"sandbox": True, "git_push": {"branches": "all", "emails": ["a@example.org"]}}}
        self.assertEqual(registry.validate_enforce(ok), [])
        bad = [{"branches": "some"}, {"branches": "all", "emails": "a@example.org"},
               {"branches": "all", "emails": ["not an email"]}, {"branches": "all", "max_file_mb": 0},
               {"branches": "all", "max_file_mb": 101}, {"branches": "all", "unknown": 1},
               {"branches": "all", "visibility": "maybe"},
               {"branches": "all", "emails": ["a@example.org"], "public_emails": ["b@example.org"]}]
        for gp in bad:
            with self.subTest(gp=gp):
                self.assertTrue(registry.validate_enforce({**ok, "enforce": {"sandbox": True, "git_push": gp}}))
        self.assertTrue(registry.validate_enforce({**ok, "enforce": {"git_push": {"branches": "all"}}}))
        for origin in (None, "(native)", "ext::sh -c x", "relative/path"):
            with self.subTest(origin=origin):
                self.assertTrue(registry.validate_enforce({**ok, "paths": {"origin": origin}}))
        self.assertEqual(registry.validate_enforce({**ok, "paths": {}, "enforce": {"sandbox": True,
                                                                                  "git_push": {"branches": "none"}}}), [])
        with mock.patch.object(hubconf, "get", lambda k, d=None: ["p"] if k == "service_sandbox_exempt" else d):
            self.assertTrue(registry.validate_enforce(ok))

    def test_a_projects_own_manifest_update_cant_set_it(self):
        import registry
        cur = {"name": "proj", "enforce": {"sandbox": True}}
        said = registry.platform_owned_changes(
            {**cur, "enforce": {"sandbox": True, "git_push": {"branches": "all"}}}, cur)
        self.assertTrue(any("enforce" in s for s in said), said)


# ---------- end to end: the session's command, the real dispatch, a detached worker ----------

class EndToEnd(unittest.TestCase):
    """`brunna push` typed in a sandboxed project's folder, in a throwaway instance; the real `hubbus dispatch` takes it
    in and starts a detached worker, which outlives the dispatch run; the project's job goes through the real
    bin/sandboxed-run (skipped where this machine can't sandbox it)."""

    def test_the_whole_way(self):
        tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for d in ("home", "config/projects", "data", "secrets", "bus", "state", "ws", "t"):
            (tmp / d).mkdir(parents=True)
        (tmp / "gitconfig").write_text("[safe]\n\tbareRepository = explicit\n")
        (tmp / "config" / "hub.json").write_text(json.dumps({
            "owner": {"name": "alex", "tag": "Alex"}, "bus_dir": str(tmp / "bus"), "registry_dir": "projects",
            "workspace_root": str(tmp / "ws"), "memory_copies": str(tmp / "copies"), "services_root": str(tmp / "svc"),
            "language": {"ui": "en", "content": "en"}, "push_dir": str(tmp / "push"), "git_push": {"wait_s": 5}}))
        env = {**{k: v for k, v in os.environ.items() if not k.startswith(("BRUNNA_", "AGENT_HUB_", "GIT_"))
                  or k in ("AGENT_HUB_PYTHON", "BRUNNA_PYTHON")},
               "AGENT_HUB_ROOT": str(ROOT), "HOME": str(tmp / "home"), "AGENT_HUB_CONFIG": str(tmp / "config"),
               "AGENT_HUB_DATA": str(tmp / "data"), "AGENT_HUB_SECRETS": str(tmp / "secrets"), "TMPDIR": str(tmp / "t"),
               "AGENT_HUB_STATE": str(tmp / "state"), "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
               "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C", **IDENT}
        remote, ws, seed = tmp / "remote.git", tmp / "ws" / "proj", tmp / "seed"
        real = [r for r in git_remote.temp_roots() if r != pathlib.Path(tempfile.gettempdir())]
        if any(remote.is_relative_to(r.resolve()) or remote.is_relative_to(r) for r in real):
            self.skipTest(f"this run's temp folder is in a system temp area ({tmp}): no remote outside one")

        def g(cwd, *args):
            return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, text=True,
                                  check=True).stdout
        g(tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        g(tmp, "init", "-q", "-b", "main", str(seed))
        (seed / "README").write_text("r\n")
        g(seed, "add", "README")
        g(seed, "commit", "-q", "-m", "init")
        g(seed, "push", "-q", str(remote), "main")
        g(tmp, "clone", "-q", str(remote), str(ws))
        (tmp / "config" / "projects" / "proj.yaml").write_text(
            f"v: 1\nname: proj\npaths:\n  workspace: {ws}\n  origin: {remote}\n"
            f"enforce:\n  sandbox: true\n  git_push:\n    branches: all\n    emails: [{IDENT['GIT_AUTHOR_EMAIL']}]\n")
        r = subprocess.run([sys.executable, str(BIN / "registry.py"), "check"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (ws / "a.txt").write_text("a\n")
        g(ws, "add", "a.txt")
        g(ws, "commit", "-q", "-m", "add a")
        head = g(ws, "rev-parse", "HEAD").strip()

        def dispatch():
            return subprocess.run([sys.executable, str(BIN / "hubbus"), "dispatch"], env=env, capture_output=True,
                                  text=True, timeout=120, cwd=tmp)
        d = dispatch()   # makes the project's request folder
        self.assertTrue((tmp / "push" / "proj").is_dir(), d.stdout + d.stderr)
        c = subprocess.run([sys.executable, str(BIN / "agenthub"), "push", "--no-wait"], env=env, cwd=ws,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(c.returncode, git_remote.PENDING, c.stdout + c.stderr)
        rid = next(n for n in os.listdir(tmp / "push" / "proj")).split(".")[0]
        t0 = time.monotonic()
        d = dispatch()
        self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
        res_f = tmp / "push" / "proj" / f"{rid}.result.json"
        self.assertFalse(res_f.exists() and time.monotonic() - t0 < 0.5, "the push ran inside the dispatch tick")
        deadline = time.monotonic() + 180
        while not res_f.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        log = tmp / "state" / "git-broker" / "proj" / "worker.log"
        res = json.loads(res_f.read_text()) if res_f.exists() else {}
        rec = tmp / "state" / "git-broker" / "proj" / "pushes.jsonl"
        if "the sandbox refused" in (rec.read_text() if rec.exists() else ""):
            self.skipTest("this machine can't sandbox the project's job")
        self.assertEqual(res.get("state"), "pushed", (res, log.read_text() if log.exists() else ""))
        tip = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"], env=env, capture_output=True,
                             text=True).stdout.strip()
        self.assertEqual(tip, head)
        w = subprocess.run([sys.executable, str(BIN / "agenthub"), "push", "--wait", "1"], env=env, cwd=ws,
                           capture_output=True, text=True, timeout=60)
        self.assertIn(w.returncode, (git_remote.PENDING, git_remote.OK))
        # wait for that second request's worker too (nothing to push), so no process outlives the test
        dispatch()
        deadline = time.monotonic() + 60
        while (tmp / "state" / "git-broker" / "proj" / "worker.json").exists() and time.monotonic() < deadline:
            time.sleep(0.5)


if __name__ == "__main__":
    unittest.main()
