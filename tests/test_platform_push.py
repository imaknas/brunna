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
import contextlib
import datetime
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
                  mock.patch.object(hubconf, "secrets_dir", lambda: self.tmp / "secrets"),
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
        # a lease only denies it (review S2): it loads the session rendering, and never asks for a push
        lease = runtime_claude.lease_settings(self.pol)["permissions"]["deny"]
        self.assertTrue(all(r.startswith("Edit(") for r in lease if own in r))
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

    def test_a_link_at_the_result_files_name_is_replaced(self):
        push_queue.ensure_folder("proj")
        rid = git_remote.request_id()
        target = self.tmp / "elsewhere.txt"
        target.write_text("mine\n")
        (self.push_root / "proj" / f"{rid}.result.json").symlink_to(target)
        push_queue.write_result("proj", push_queue.result("proj", rid, "refused", "x"))
        self.assertEqual(target.read_text(), "mine\n", "written through the link")
        self.assertFalse((self.push_root / "proj" / f"{rid}.result.json").is_symlink())
        self.assertEqual(self.result(rid)["state"], "refused")
        # control: a plain write follows a link
        (self.tmp / "l").symlink_to(target)
        (self.tmp / "l").write_text("through\n")
        self.assertEqual(target.read_text(), "through\n")

    def test_what_else_is_in_the_folder(self):
        """Only requests are read; a result and a client's young dot-file stay, an old dot-file and anything else go
        (the re-review's nit), never followed."""
        push_queue.ensure_folder("proj")
        other = self.push_root / "proj" / "notes.txt"
        other.write_text("x\n")
        young, old = self.push_root / "proj" / ".m1.tmp", self.push_root / "proj" / ".m2.tmp"
        young.write_text("{")
        old.write_text("{")
        os.utime(old, (time.time() - 7200,) * 2)
        res = self.push_root / "proj" / "m11160000-cccccccccccc.result.json"
        res.write_text("{}")
        self.take()
        self.assertFalse(other.exists())
        self.assertTrue(young.exists())
        self.assertFalse(old.exists())
        self.assertTrue(res.exists())

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
                               {"pid": os.getpid(), "start": None, "id": newer, "started": newer,
                                "spawned_at": push_queue.now_iso()})
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
        b = self.commit(self.ws, "b.txt", "b\n", "add b, after the request")
        self.serve()
        res = self.result(rid)
        self.assertEqual(res["state"], "pushed", res)
        self.assertEqual(self.tip(), a, "the requested sha is pushed, not the branch's tip (r2 S-g)")
        self.assertEqual(self.bare(self.owned(), "cat-file", "-t", b), "", "an unchecked commit reached the owned repo")
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
        fake.ssh_env = lambda project, run_dir=None: {"GIT_SSH_COMMAND": "ssh -F /dev/null -o IdentitiesOnly=yes", "GRANT_MARK": project}
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
        fake.ssh_env = lambda project, run_dir=None: (_ for _ in ()).throw(NotSetUp("no key"))
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
        fake.ssh_env = lambda project, run_dir=None: {}
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

    def test_the_worker_is_detached(self):
        """start_worker: the worker runs in a session of its own, not a child of the caller, and outlives it; its pid
        and start time are what tick records. Control: a plain child is in the caller's session."""
        marker = self.tmp / "started"
        with mock.patch.object(hubconf, "launch",
                               lambda script: ["/bin/sh", "-c", f'echo $$ > "{marker}"; exec sleep 30', "worker"]):
            pid, start = push_queue.start_worker("proj")
        self.addCleanup(lambda: subprocess.run(["kill", str(pid)], capture_output=True))
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(int(marker.read_text()), pid)
        self.assertNotEqual(os.getsid(pid), os.getsid(0), "in the caller's session")
        ppid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout)
        self.assertNotEqual(ppid, os.getpid(), "still the caller's child")
        self.assertTrue(push_queue.alive({"pid": pid, "start": start, "spawned_at": push_queue.now_iso()}))
        child = subprocess.Popen(["sleep", "5"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(os.getsid(child.pid), os.getsid(0), "the control")

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
        self.gl_log = d / "calls.log"
        g.write_text(f'#!/bin/sh\ncase "$1" in version) echo {version}; exit 0;; esac\n'
                     f'{{ echo "cwd=$PWD"; echo "args=$*"; env | sed "s/^/env:/"; ls -A; }} >> "{self.gl_log}"\n'
                     f'cat > /dev/null\nwhile [ $# -gt 0 ]; do [ "$1" = --report-path ] && echo \'{report}\' > "$2"; '
                     f'shift; done\nexit {stdin_exit}\n')
        g.chmod(0o755)
        p = mock.patch.dict(os.environ, {"PATH": f"{d}:{os.environ['PATH']}", "GITLEAKS_CONFIG": "/owner/leaks.toml"})
        p.start()
        self.addCleanup(p.stop)

    def test_gitleaks_runs_hardened(self):
        """B2: a fresh empty folder as its cwd (nothing of the project's to read), an empty ignore file named, no baseline
        or config from the environment. Control: the caller's environment has GITLEAKS_CONFIG."""
        self.fake_gitleaks()
        self.commit(self.ws, ".gitleaksignore", "*\n", "ignore all")
        self.assertEqual(self.ask()["state"], "pushed")
        log = self.gl_log.read_text()
        cwd = next(x for x in log.splitlines() if x.startswith("cwd=")).removeprefix("cwd=")
        self.assertFalse(pathlib.Path(cwd).resolve().is_relative_to(self.ws.resolve()))
        self.assertIn("--gitleaks-ignore-path", log)
        self.assertIn("--redact", log)
        self.assertNotIn("--baseline-path", log)
        self.assertNotIn("env:GITLEAKS_CONFIG", log)
        self.assertEqual(os.environ["GITLEAKS_CONFIG"], "/owner/leaks.toml", "the control: the caller has it")
        listed = log.split("env:")[-1].splitlines()[1:]
        self.assertNotIn(".gitleaksignore", listed, "the scanner's folder held the project's files")

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
            res = self.ask()
        self.refused(res, check="names", says="private project")
        # which name: the owner's record only (review S3), never the result or the letter
        self.assertNotIn("secretproj", json.dumps(res) + json.dumps(self.letters))
        self.assertIn("secretproj", json.dumps(self.pushes()[-1]))
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        self.commit(self.ws, "notes/readme.txt", "see secretproj for it\n", "b")
        with mock.patch.object(git_remote, "visibility", lambda origin: "public"):
            res = self.ask()
        self.refused(res, check="names")
        self.assertNotIn("secretproj", json.dumps(res) + json.dumps(self.letters))
        self.names_file = self.tmp / "missing.txt"
        with mock.patch("private_names.names_file", side_effect=__import__("private_names").ListError("x")), \
                mock.patch.object(git_remote, "visibility", lambda origin: "public"):
            self.refused(self.ask(), check="names", says="can't be read")

    def test_a_ci_change(self):
        self.commit(self.ws, ".gitlab-ci.yml", "x: 1\n", "ci")
        self.refused(self.ask(), check="ci", says=".gitlab-ci.yml")
        self.docs["proj"]["enforce"]["git_ci"] = True
        self.assertEqual(self.ask()["state"], "pushed")

    def test_visibility_is_the_grants_probe(self):
        """One probe (git_push_grant.visibility, tested with the grant); a local path is private here, and without the
        grant module every remote counts as unknown (public)."""
        self.assertEqual(git_remote.visibility(str(self.remote)), "private")
        self.assertEqual(git_remote.visibility(f"file://{self.remote}"), "private")
        with mock.patch.dict(sys.modules, {"git_push_grant": None}):
            self.assertEqual(git_remote.visibility("git@example.org:a/b.git"), "unknown")
        import git_push_grant
        with mock.patch.object(git_push_grant, "visibility", lambda origin: f"probed {origin}"):
            self.assertEqual(git_remote.visibility("git@example.org:a/b.git"), "probed git@example.org:a/b.git")


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

    def test_a_failed_push_is_recorded_in_any_language(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.commit(self.ws, "a.txt", "a\n", "add a")
        with mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": "zh-Hant"}):
            code, _out, err = self.push("main", "--yes")
        self.assertEqual(code, git_remote.FAILED, err)
        self.assertEqual(self.pushes()[-1]["ok"], False)

    def test_setup_is_never_a_project(self):
        self.docs["setup"] = dict(self.docs["proj"])
        ran = []
        with mock.patch.object(git_remote.subprocess, "run",
                               lambda argv, **kw: (ran.append(argv), subprocess.CompletedProcess(argv, 2))[1]):
            code = git_remote.main(["push", "setup", "proj"])
        self.assertEqual(code, 2)
        self.assertEqual(ran[0][-3:], [str(BIN / "git_push_grant.py"), "setup", "proj"], "setup is the grant's")
        self.assertEqual(self.calls, [], "it was read as a project")

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

    # the plain owner form (11-16): a sandboxed project named first, from a folder no sandboxed project owns

    def test_the_plain_owner_form_from_the_hub(self):
        a = self.commit(self.ws, "a.txt", "a\n", "add a")
        code, out, err = self.main(["proj", "main", "--yes"], hubconf.ROOT)
        self.assertEqual(code, git_remote.OK, err)
        self.assertEqual(self.tip(), a)
        self.assertIn("pushed main", out)
        self.assertEqual(self.pushes()[-1]["by"], "owner")
        self.assert_owner_git_only_in_owned_repos()

    def test_the_plain_owner_form_from_an_unsandboxed_project(self):
        other = self.tmp / "ws" / "notes"
        other.mkdir(parents=True)
        self.docs["notes"] = {"name": "notes", "paths": {"workspace": str(other)}}
        a = self.commit(self.ws, "a.txt", "a\n", "add a")
        code, _out, err = self.main(["proj", "--yes"], other)   # no branch: the one checked out
        self.assertEqual(code, git_remote.OK, err)
        self.assertEqual(self.tip(), a)
        # a first argument that is no sandboxed project is still refused there, with the plain form named
        code, _out, err = self.main(["main"], other)
        self.assertEqual(code, git_remote.REFUSED)
        self.assertIn("push with git as usual; for a sandboxed project use brunna push <project>", err)
        code, _out, err = self.main(["notes"], hubconf.ROOT)   # an unsandboxed project's name from the hub
        self.assertEqual(code, git_remote.REFUSED)
        self.assertIn("is not sandboxed", err)

    def test_inside_a_sandboxed_project_a_project_name_stays_a_branch(self):
        ws2 = self.tmp / "ws" / "proj2"
        self.git(self.tmp, "clone", "-q", str(self.remote), str(ws2))
        self.docs["proj2"] = {"name": "proj2", "paths": {"workspace": str(ws2), "origin": str(self.remote)},
                              "enforce": {"sandbox": True}}
        self.git(self.ws, "branch", "proj2")   # a branch of proj that shares the other project's name
        push_queue.ensure_folder("proj")
        sub = self.ws / ".claude" / "worktrees" / "w1"   # where its sessions' worktrees start: still its folder
        sub.mkdir(parents=True)
        for cwd in (self.ws, sub):
            with self.subTest(cwd=cwd):
                self.calls.clear()
                code, _out, err = self.main(["proj2", "--no-wait"], cwd)
                self.assertEqual(code, git_remote.PENDING, err)
                reqs = [json.loads((self.push_root / "proj" / n).read_text())
                        for n in os.listdir(self.push_root / "proj") if n.endswith(".request.json")]
                self.assertIn("proj2", [r["branch"] for r in reqs], "read as a branch of proj, a session's request")
                self.assertEqual(self.calls, [], "the owner's git ran: it was read as the owner form")
        self.assertEqual(self.tip(), self.base)

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


# ---------- the code review's findings (B1, B2, S1, S2, S3 and the nits) ----------

def mktree(repo, entries) -> str:
    inp = "".join(f"{m} {t} {o}\t{n}\n" for m, t, o, n in entries)
    return subprocess.run(["git", "-C", str(repo), "mktree"], input=inp, capture_output=True, text=True,
                          check=True).stdout.strip()


class Review(Push):
    VALUE = "Zq7xK2mPa9Lr4tWv"   # letters and digits only

    def declare(self, value: str):
        (self.ws / ".env").write_text(f"DB_PASSWORD={value}\n")
        self.docs["proj"]["secrets"] = [{"name": "db", "path": ".env", "env_key": "DB_PASSWORD"}]

    def shape(self, i: int, text: str) -> dict:
        self.git(self.ws, "checkout", "-q", "-b", f"s{i}", self.base)
        self.commit_as(f"f{i}.txt", text, f"shape {i}")
        return self.ask(branch=f"s{i}")

    def test_b1_a_declared_value_inside_other_text(self):
        """A declared value is found wherever it sits in a text, not only as a whole word; the control is the bare
        value, which was caught before too."""
        v = self.VALUE
        self.declare(v)
        for i, text in enumerate([f"url = 'https://user:{v}@db.example.org/x'\n", f"q = 'a={v}&b=1'\n",
                                  f"Use the password {v}.\n", f"x = '{v}'\n"]):
            with self.subTest(text=text):
                res = self.shape(i, text)
                self.assertEqual((res["state"], res["check"]), ("refused", "secrets"), res)
                self.assertIn("db", res["reason"])
                self.assertNotIn(v, json.dumps(res) + json.dumps(self.letters), "the value itself was told")

    def test_b1_a_value_with_characters_a_word_splits_on(self):
        v = "p@ss(word)2024!xy'z\"Q"
        self.declare(v)
        res = self.shape(0, f'DB = "{v}"\n')
        self.assertEqual((res["state"], res["check"]), ("refused", "secrets"), res)
        # the values live where no sandbox reads (the platform's secrets), owner-only
        f = push_checks.declared_file("proj")
        self.assertTrue(f.is_relative_to(hubconf.secrets_dir()))
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(f.parent.stat().st_mode & 0o777), "0o700")
        # control: a push without the value passes
        self.assertEqual(self.shape(1, "DB = 'something else'\n")["state"], "pushed")

    def bomb(self, k: int) -> str:
        blob = subprocess.run(["git", "-C", str(self.ws), "hash-object", "-w", "--stdin"], input="x\n",
                              capture_output=True, text=True, check=True).stdout.strip()
        t = mktree(self.ws, [("100644", "blob", blob, "f")])
        for _i in range(k):
            t = mktree(self.ws, [("040000", "tree", t, "a"), ("040000", "tree", t, "b")])
        root = mktree(self.ws, [("040000", "tree", t, "bomb"), ("100644", "blob", blob, "README")])
        c = self.git(self.ws, "commit-tree", root, "-p", self.base, "-m", "bomb").strip()
        self.git(self.ws, "update-ref", "refs/heads/main", c)
        return c

    def test_b2_a_tree_bomb_is_walked_once_per_tree(self):
        """k levels of a tree naming one subtree twice: 2^k paths, k+2 trees. Each tree is walked once, fast; the control
        (the same shape, walked per path as before) lists 2^k entries."""
        k = 18
        c = self.bomb(k)

        def g(*a, input=None):
            return subprocess.run(["git", "-C", str(self.ws), *a], input=input, capture_output=True, check=True).stdout
        objs = push_checks.Objects(g, c, [self.base])
        t0 = time.monotonic()
        entries, _gl, _f = objs.walk(time.monotonic() + 60)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertLess(len(entries), 10 * k)
        res = self.ask()
        self.assertEqual(res["state"], "pushed", res)   # nothing in it is refused: the bomb is only big when listed
        # control: walked per path, as before the fix, it lists every path
        n, stack = 0, [(objs.commit_header(c, "tree")[0], 0)]
        while stack and n <= 1 << 12:
            tree, depth = stack.pop()
            for mode, _name, oid in objs.tree_entries(tree):
                n += 1
                if mode == "40000":
                    stack.append((oid, depth + 1))
        self.assertGreater(n, 1 << 12, "the control: per path it would have listed thousands")

    def test_b2_caps_are_size_findings(self):
        with mock.patch.object(push_checks, "MAX_ENTRIES", 50):
            self.commit_as("many/" + "x" * 3 + ".txt", "x\n", "one")
            for i in range(60):
                (self.ws / "many" / f"f{i}").write_text(f"{i}\n")
            self.git(self.ws, "add", "many")
            self.git(self.ws, "commit", "-q", "-m", "many")
            res = self.ask()
        self.refused(res, check="size", says="entries")
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        deep = "/".join(f"d{i}" for i in range(12)) + "/f.txt"
        with mock.patch.object(push_checks, "MAX_DEPTH", 8):
            self.commit_as(deep, "x\n", "deep")
            self.refused(self.ask(), check="size", says="deeper")
        self.assertEqual(self.ask()["state"], "pushed", "the control: the same deep tree within the default caps")

    def test_s1_a_flood_is_bounded(self):
        self.limits["requests_per_day"] = 5
        push_queue.ensure_folder("proj")
        for i in range(400):
            rid = f"m11161200-{i:012x}"
            (self.push_root / "proj" / f"{rid}.request.json").write_text(
                json.dumps({"v": 1, "id": rid, "branch": "main", "sha": self.base}))
        reads = []
        real = push_queue.nofollow.read
        with mock.patch.object(push_queue.nofollow, "read", lambda *a, **k: (reads.append(a[1]), real(*a, **k))[1]):
            t0 = time.monotonic()
            self.take()
        self.assertLess(time.monotonic() - t0, 20)
        self.assertLessEqual(len([r for r in reads if r.endswith(".request.json")]), 6)
        self.assertEqual([n for n in os.listdir(self.push_root / "proj") if n.endswith(".request.json")], [])
        self.assertEqual(len([x for x in self.letters if "over what one day allows" in x[1]]), 1)

    def test_s1_taken_is_pruned(self):
        f = push_queue.broker("proj") / "taken.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"at": "2020-01-01T00:00:00+00:00", "id": "m01010000-000000000000"}) + "\n")
        self.request()
        self.take()
        self.assertNotIn("2020-01-01", f.read_text())

    def test_nit_a_folder_by_a_requests_name(self):
        push_queue.ensure_folder("proj")
        bad = self.push_root / "proj" / "m11160000-aaaaaaaaaaaa.request.json"
        bad.mkdir()
        (bad / "inside").write_text("x\n")
        os.utime(bad, (1, 1))
        (self.push_root / "proj" / "m11160000-bbbbbbbbbbbb.result.json").mkdir()
        self.commit(self.ws, "a.txt", "a\n", "a")
        rid = self.request()
        self.assertEqual(self.take(), [rid])
        self.assertFalse(bad.exists())
        self.assertFalse((self.push_root / "proj" / "m11160000-bbbbbbbbbbbb.result.json").exists())

    def test_nit_no_start_time_is_not_forever(self):
        now = push_queue.now_iso()
        self.assertTrue(push_queue.alive({"pid": os.getpid(), "start": None, "spawned_at": now}))
        self.assertFalse(push_queue.alive({"pid": os.getpid(), "start": None,
                                           "spawned_at": "2020-01-01T00:00:00+00:00"}))

    def test_s2_a_lease_never_pushes(self):
        """A lease loads the workspace's local settings (the session rendering, with the push rule and folder): its own
        --settings ask for the command (ask beats allow: the owner's relay) and deny the folder. Control: the session
        rendering allows them."""
        import importlib.util
        import runtime_claude
        pol = policy.policy("proj", self.docs["proj"], {"projects": self.docs})
        folder = str(self.push_root / "proj")
        lease = runtime_claude.lease_settings(pol)["permissions"]
        self.assertIn("Bash(brunna push *)", lease["ask"])
        self.assertNotIn("Bash(brunna push *)", lease["deny"])
        self.assertTrue(any(r.startswith("Edit(") and folder in r for r in lease["deny"]))
        session = runtime_claude.session_settings(pol)
        self.assertIn("Bash(brunna push *)", session[2])
        self.assertNotIn("Bash(brunna push *)", session[1])
        # the lease's real command line names the file that holds them
        spec = importlib.util.spec_from_file_location("lease_runner_t", BIN / "lease-runner.py")
        lr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lr)
        with mock.patch.object(lr, "DIR", self.tmp / "leases"), \
                mock.patch.object(policy, "for_project", lambda name: pol):
            f = lr.deny_settings("proj")
        self.assertIn("Bash(brunna push *)", json.loads(f.read_text())["permissions"]["ask"])

    def test_grant_probe_shapes(self):
        import git_push_grant
        self.assertEqual(git_push_grant.https_url("http://127.0.0.1:9/x.git"), "http://127.0.0.1:9/x.git")
        self.assertIsNone(git_push_grant.https_url("git://example.org/x.git"))
        self.assertEqual(git_push_grant.visibility("git://example.org/x.git"), "unknown")
        self.assertEqual(git_push_grant.visibility(str(self.remote)), "private")
        with mock.patch.object(git_push_grant, "anonymous_ls_remote",
                               lambda url: subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(git_push_grant.visibility("http://127.0.0.1:9/x.git"), "public")


class ReReview(Review):
    """The re-review of the code review's fixes: B1 (the store), B2 (path bytes), 3 (a bounded match), 4 (leases), 5
    (the visibility to-do), 6 (no commit named) and the nits."""

    def test_b1_a_store_that_cant_be_read_whole_fails_closed(self):
        v = self.VALUE
        self.declare(v)
        self.assertEqual(self.shape(0, f"x = '{v}'\n")["check"], "secrets", "the control: it is found")
        f = push_checks.declared_file("proj")
        before = f.read_bytes()
        with mock.patch.object(push_checks, "STORE_CAP", 10):
            res = self.shape(1, "nothing secret\n")
        self.assertEqual((res["state"], res["check"], res["fix"]), ("refused", "secrets", "owner"), res)
        self.assertTrue(res.get("todo"))
        self.assertEqual(f.read_bytes(), before, "the store was rewritten or shrunk")
        # and an entry rewritten many times never drops the old values
        for i in range(5):
            self.declare(f"{v}-{i}")
            self.shape(10 + i, "ok\n")
        self.declare("unrelated-value-0000")
        self.assertEqual(self.shape(20, f"x = '{v}-0'\n")["check"], "secrets")

    def test_b1_the_count_cap_refuses_for_the_owner(self):
        self.declare(self.VALUE)
        with mock.patch.object(push_checks, "MAX_VALUES", 0):
            res = self.shape(0, "ok\n")
        self.assertEqual((res["state"], res["fix"]), ("refused", "owner"), res)

    def test_b1_a_long_value_is_kept_as_its_hash(self):
        """A value over MAX_VALUE is stored as its hash, matched against a whole file or a long word in one; the
        store holds no value that long."""
        token = "Ab3" * 400
        (self.ws / "cert.txt").write_text("-----BEGIN THING-----\n" + token + "\n")
        self.docs["proj"]["secrets"] = [{"name": "cert", "path": "cert.txt"}]
        self.git(self.ws, "checkout", "-q", "-b", "c0", self.base)
        self.commit_as("copy/x.txt", f'key = "{token}"\n', "copy")
        res = self.ask("c0")
        self.assertEqual(res["check"], "secrets", res)
        self.assertIn("cert", res["reason"])
        rec = [json.loads(x) for x in push_checks.declared_file("proj").read_text().splitlines()]
        self.assertTrue(all("v" not in r or len(r["v"]) <= push_checks.MAX_VALUE for r in rec))

    def test_b1_dotenv_comments_and_nfc(self):
        (self.ws / ".env").write_text("TOKEN=Zq7xK2mPa9Lr4tWvé   # a comment\n")
        self.docs["proj"]["secrets"] = [{"name": "tok", "path": ".env", "env_key": "TOKEN"}]
        import unicodedata
        decomposed = unicodedata.normalize("NFD", "Zq7xK2mPa9Lr4tWvé")
        res = self.shape(0, f"x = {decomposed}\n")
        self.assertEqual(res["check"], "secrets", res)
        stored = push_checks.declared_file("proj").read_text()
        self.assertNotIn("comment", stored)

    def test_3_the_match_is_bounded(self):
        import random
        import string
        rnd = random.Random(7)
        vals = ["".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(rnd.randint(12, 64)))
                for _ in range(4096)]
        d = push_checks.Declared.__new__(push_checks.Declared)
        d.names, d.hashes, d.by_key = {v: "n" for v in vals}, {}, {}
        for v in vals:
            d.by_key.setdefault(v[:d.KEY], []).append(v)
        import re as _re
        d.rx = _re.compile("(?=(" + push_checks._trie_pattern(sorted(d.by_key)) + "))")
        text = "".join(rnd.choice(string.ascii_letters + " \n") for _ in range(10 << 20)) + vals[7]
        t0 = time.monotonic()
        self.assertEqual(d.hits([text], time.monotonic() + 120), ["n"])
        self.assertLess(time.monotonic() - t0, 60)
        with self.assertRaises(push_checks.Failure):
            d.hits([text], time.monotonic() - 1)

    def deep(self, depth: int, namelen: int, width: int) -> str:
        src = self.tmp / f"blobs-{width}"
        src.mkdir(exist_ok=True)
        for i in range(width):
            (src / str(i)).write_text(f"{i}\n")
        blobs = subprocess.run(["git", "-C", str(self.ws), "hash-object", "-w", "--stdin-paths"],
                               input="".join(f"{src / str(i)}\n" for i in range(width)), capture_output=True, text=True,
                               check=True).stdout.split()
        t = mktree(self.ws, [("100644", "blob", b, f"{i:0{namelen}d}"[-namelen:]) for i, b in enumerate(blobs)])
        for i in range(depth):
            t = mktree(self.ws, [("040000", "tree", t, ("d" * namelen)[:namelen - 3] + f"{i:03d}")])
        c = self.git(self.ws, "commit-tree", t, "-p", self.base, "-m", "deep").strip()
        self.git(self.ws, "update-ref", "refs/heads/main", c)
        return c

    def test_b2_long_paths_are_refused_fast(self):
        """250 folders of 255-character names over 3000 files: 190 MB of paths if each were built and joined. Refused
        as a size finding while walking, quickly, without growing the process much; rev-list prints no names. Control: a
        tree as deep with short names passes."""
        import resource
        c = self.deep(250, 255, 3000)

        def g(*a, input=None):
            r = subprocess.run(["git", "-C", str(self.ws), *a], input=input, capture_output=True, check=True).stdout
            self.assertLess(len(r), 16 << 20, f"git {a[0]} printed {len(r)} bytes")
            return r
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        t0 = time.monotonic()
        objs = push_checks.Objects(g, c, [self.base])
        with self.assertRaises(push_checks.Failure) as got:
            objs.walk(time.monotonic() + 60)
        self.assertEqual(got.exception.check, "size")
        self.assertLess(time.monotonic() - t0, 30)
        grew = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
        self.assertLess(grew, (300 << 20) if sys.platform == "darwin" else 300 << 10, "the walk held too much")
        self.refused(self.ask(), check="size")
        self.git(self.ws, "reset", "-q", "--hard", self.base)
        self.deep(250, 4, 20)
        self.assertEqual(self.ask()["state"], "pushed", "the control: as deep with short names")

    def test_b2_a_wide_tree_is_refused_unread(self):
        self.commit_as("w/a.txt", "a\n", "wide")
        with mock.patch.object(push_checks, "MAX_TREE", 10), \
                mock.patch.object(push_checks.Objects, "tree_entries", side_effect=AssertionError("parsed")):
            self.refused(self.ask(), check="size", says="as an object")

    def test_4_a_lease_runs_no_session_command_unasked(self):
        import runtime_claude
        pol = policy.policy("proj", self.docs["proj"], {"projects": self.docs})
        allow = set(runtime_claude.session_settings(pol)[2])
        ask = set(runtime_claude.lease_settings(pol)["permissions"]["ask"])
        self.assertTrue(allow)
        self.assertEqual(sorted(allow - ask), [], "an allow the lease loads that its own settings don't ask for")

    def test_5_the_visibility_todo_stays_open(self):
        self.field(visibility="public", emails=["1+d@users.noreply.github.com"])
        for i in range(2):
            self.git(self.ws, "checkout", "-q", "-b", f"v{i}", self.base)
            self.commit_as(f"v{i}.txt", "x\n", "v", GIT_AUTHOR_EMAIL="1+d@users.noreply.github.com",
                           GIT_COMMITTER_EMAIL="1+d@users.noreply.github.com")
            self.assertEqual(self.ask(f"v{i}")["state"], "pushed")
        kinds = [(t[1], t[3]) for t in self.todos]
        self.assertEqual([k for k in kinds if "visibility" in k[1]], [("task", "push-proj-visibility-1")],
                         "the to-do closed or was sent again")

    def test_6_a_private_name_names_no_commit(self):
        self.names_file = self.tmp / "private-names.txt"
        self.names_file.write_text("secretproj\n")
        self.field(public_emails=["dev@example.org"])
        c = self.commit(self.ws, "a.txt", "a\n", "about secretproj")
        with mock.patch.object(git_remote, "visibility", lambda origin: "public"):
            res = self.ask()
        self.refused(res, check="names")
        self.assertEqual(res["fix"], "owner")
        told = json.dumps(res) + json.dumps(self.letters)
        self.assertNotIn(c[:12], told)
        self.assertNotIn("secretproj", told)
        self.assertTrue(any("secretproj" in " ".join(t) and c[:12] in " ".join(t) for t in self.todos), self.todos)

    def test_nits_the_folder_holds_only_requests_and_letters_go_once(self):
        push_queue.ensure_folder("proj")
        for i in range(5):
            (self.push_root / "proj" / f"other-{i}.txt").write_text("x")
            (self.push_root / "proj" / f"bad {i}.request.json").write_text("{}")
        self.take()
        self.assertEqual(os.listdir(self.push_root / "proj"), [])
        self.assertEqual(len([x for x in self.letters if "not a push request" in x[1]]), 1)
        self.limits["requests_per_day"] = 1
        keys = []
        for _round in range(2):
            for i in range(4):
                rid = git_remote.request_id()
                (self.push_root / "proj" / f"{rid}.request.json").write_text(
                    json.dumps({"v": 1, "id": rid, "branch": "main", "sha": self.base}))
            push_queue.take_in("proj", letter=lambda p, t, k: keys.append(k))
        flood = [k for k in keys if k.startswith("push-flood-")]
        self.assertEqual(len(flood), 1, "one flood letter a day")


class ThirdReview(Review):
    """The third re-review: B-1 (no joined path under the request root), B-2 (a record a name can't split), S-1 (a
    matcher that can't fail), S-2 (a long value inside a longer word), S-3 (one batch for the object checks), S-4 (path
    bytes) and the nits."""

    def test_b1_a_swapped_folder_never_leads_outside(self):
        """A session swaps its folder for a link to another folder and back, as fast as it can, while take-in clears
        entries: nothing outside is removed. Control: the old fallback (a joined path) removes it."""
        outside = self.tmp / "outside"
        (outside / "Documents").mkdir(parents=True)
        (outside / "Documents" / "precious").write_text("mine\n")
        push_queue.ensure_folder("proj")
        folder = self.push_root / "proj"
        stop = threading.Event()

        def swapper():
            real = self.push_root / "proj-real"
            while not stop.is_set():
                try:
                    if folder.is_symlink():
                        folder.unlink()
                        real.rename(folder)
                    else:
                        (folder / "Documents").mkdir(exist_ok=True)
                        folder.rename(real)
                        folder.symlink_to(outside)
                except OSError:
                    pass
        t = threading.Thread(target=swapper)
        t.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with contextlib.suppress(OSError, ValueError):
                    push_queue.take_in("proj")
        finally:
            stop.set()
            t.join()
        self.assertEqual((outside / "Documents" / "precious").read_text(), "mine\n")
        # control: removing by a joined path follows the link
        if folder.is_symlink():
            folder.unlink()
        elif folder.exists():
            shutil.rmtree(folder)
        folder.symlink_to(outside)
        shutil.rmtree(folder / "Documents")
        self.assertFalse((outside / "Documents").exists())

    def test_b2_a_name_with_line_separators(self):
        v = self.VALUE
        (self.ws / ".env").write_text(f"DB_PASSWORD={v}\n")
        self.docs["proj"]["secrets"] = [{"name": "db x y\u0085z\r", "path": ".env", "env_key": "DB_PASSWORD"}]
        self.shape(0, "ok\n")
        self.docs["proj"]["secrets"] = []
        self.assertEqual(self.shape(1, f"x = {v}\n")["check"], "secrets", "the value was forgotten")
        raw = push_checks.declared_file("proj").read_bytes()
        self.assertTrue(raw.isascii())

    def test_b2_a_cut_line_costs_only_itself(self):
        self.declare(self.VALUE)
        self.shape(0, "ok\n")
        f = push_checks.declared_file("proj")
        with open(f, "ab") as fh:
            fh.write(b'{"v": "cut-off-in-the-midd')   # a crash mid-append
        self.declare("Another-Value-2024x")
        self.shape(1, "ok\n")
        self.assertEqual(self.shape(2, "a Another-Value-2024x b\n")["check"], "secrets")
        self.assertEqual(self.shape(3, f"x {self.VALUE}\n")["check"], "secrets")

    def test_s1_values_sharing_long_prefixes(self):
        """Values nested 500 deep by their prefixes: a regex of the values would overflow sre's recursion; the match
        can't fail on them."""
        (self.ws / "keys.txt").write_text("\n".join("K" + "x" * i + "1" for i in range(20, 500, 4)) + "\n")
        self.docs["proj"]["secrets"] = [{"name": "keys", "path": "keys.txt"}]
        res = self.shape(0, "plain text\n")
        self.assertEqual(res["state"], "pushed", res)
        self.assertEqual(self.shape(1, "K" + "x" * 300 + "1\n")["check"], "secrets")

    def test_s2_a_long_value_inside_a_longer_word(self):
        v = "Zz9" * 200   # 600 bytes
        (self.ws / ".env").write_text(f"BIG={v}\n")
        self.docs["proj"]["secrets"] = [{"name": "big", "path": ".env", "env_key": "BIG"}]
        for i, text in enumerate([f"TOKEN={v}\n", f"https://x.example/?k={v}&a=1\n"]):
            with self.subTest(text=text[:20]):
                res = self.shape(i, text)
                self.assertEqual(res["check"], "secrets", res)
        recs = [json.loads(x) for x in push_checks.declared_file("proj").read_bytes().split(b"\n") if x.strip()]
        self.assertTrue(all(len(r["v"].encode()) <= push_checks.MAX_VALUE for r in recs if "v" in r))

    def test_s3_one_batch_for_the_object_checks(self):
        src = self.tmp / "many"
        src.mkdir()
        for i in range(300):
            (self.ws / "many" / f"f{i}").parent.mkdir(exist_ok=True)
            (self.ws / "many" / f"f{i}").write_text(f"{i}\n")
        self.git(self.ws, "add", "many")
        self.git(self.ws, "commit", "-q", "-m", "many")
        c = self.git(self.ws, "rev-parse", "HEAD").strip()
        calls = []

        def g(*a, input=None):
            calls.append(a[0])
            return subprocess.run(["git", "-C", str(self.ws), *a], input=input, capture_output=True, check=True).stdout
        objs = push_checks.Objects(g, c, [self.base])
        entries, gl, _f = objs.walk(time.monotonic() + 60)
        calls.clear()
        push_checks.objects_check(objs, entries, gl, time.monotonic() + 60)
        self.assertLessEqual(len(calls), 1, calls[:5])
        with self.assertRaises(push_checks.Failure):
            objs.raw.clear()
            push_checks.objects_check(objs, entries, gl, time.monotonic() - 1)

    def test_s4_paths_are_counted_in_bytes(self):
        name = "\U0001F600" * 50   # 50 characters, 200 bytes
        with mock.patch.object(push_checks, "MAX_PATH", 150):
            self.commit_as(f"{name}/a.txt", "a\n", "astral")
            self.refused(self.ask(), check="size", says="bytes")

    def test_n1_dotenv(self):
        got = push_checks.dotenv_values('export\tA="x\\"y-0123456789"\nB="multi\nline-0123456789"\nC=plain0123456789 # c\n'
                                        "D='q # x 0123456789'\n")
        self.assertEqual(got, {"A": ['x"y-0123456789'], "B": ["multi\nline-0123456789"], "C": ["plain0123456789"],
                               "D": ["q # x 0123456789"]})

    def test_n3_a_letter_key_carries_the_take_in(self):
        rid = self.request()
        self.take()
        self.serve()
        [(_p, _t, key)] = self.letters
        self.assertTrue(key.startswith(f"push-{rid}-") and key != f"push-{rid}", key)


class FourthReview(Review):
    """The fourth review: an entry that can't be removed never blocks take-in; dotenv's bad statements and repeated keys;
    the deadline inside the match; raw name bytes; a stuck worker."""

    def test_an_unremovable_entry_blocks_nothing(self):
        d = self.push_root / "proj"
        push_queue.ensure_folder("proj")
        (d / "junk" / "locked").mkdir(parents=True)
        (d / "junk" / "locked" / "f").write_text("x")
        os.chmod(d / "junk" / "locked", 0)
        self.addCleanup(lambda: os.path.isdir(d / "junk" / "locked") and os.chmod(d / "junk" / "locked", 0o700))
        rid = self.request()
        said = []
        got = push_queue.take_in("proj", log=said.append)
        self.assertEqual(got, [rid])
        self.serve()
        self.assertEqual(self.result(rid)["state"], "nothing")
        self.assertEqual(len(said), 1, said)
        push_queue.take_in("proj", log=said.append)
        self.assertEqual(len(said), 1, "said once")

    def test_dotenv_bad_statements_and_repeated_keys(self):
        v = self.VALUE
        for text in (f"A='oops\nDB_PASSWORD={v}\n", f'A="oops\nDB_PASSWORD={v}\n'):
            with self.subTest(text=text[:8]):
                self.assertEqual(push_checks.dotenv_values(text).get("DB_PASSWORD"), [v])
        self.assertEqual(push_checks._values(f"DB_PASSWORD={v}\nDB_PASSWORD={v}2\n".encode(), "DB_PASSWORD"),
                         [v, f"{v}2"])
        self.assertEqual(push_checks._values(f"DB_PASSWORD={v}\nDB_PASSWORD=\n".encode(), "DB_PASSWORD"), [v])

    def test_the_deadline_inside_the_match(self):
        d = push_checks.Declared.__new__(push_checks.Declared)
        vals = ["k" * 12 + "x" * i for i in range(1, 300)]
        d.names, d.hashes, d.by_key = {v: "n" for v in vals}, {}, {"k" * 12: vals}
        import re as _re
        d.rx = _re.compile("(?=(" + push_checks._trie_pattern(["k" * 12]) + "))")
        text = "k" * (1 << 20)
        t0 = time.monotonic()
        with self.assertRaises(push_checks.Failure):
            d.hits([text], time.monotonic() + 0.5)
        self.assertLess(time.monotonic() - t0, 5, "it ran a whole chunk past the deadline")

    def test_name_bytes_as_stored(self):
        """A name that isn't UTF-8 counts as its bytes (one each), not as the replacement characters it is shown as."""
        blob = subprocess.run(["git", "-C", str(self.ws), "hash-object", "-w", "--stdin"], input=b"x\n",
                              capture_output=True, check=True).stdout.decode().strip()
        name = b"\xff" * 100
        tree = subprocess.run(["git", "-C", str(self.ws), "mktree", "-z"],
                              input=b"100644 blob " + blob.encode() + b"\t" + name + b"\0",
                              capture_output=True, check=True).stdout.decode().strip()
        c = self.git(self.ws, "commit-tree", tree, "-p", self.base, "-m", "raw").strip()

        def g(*a, input=None):
            return subprocess.run(["git", "-C", str(self.ws), *a], input=input, capture_output=True, check=True).stdout
        objs = push_checks.Objects(g, c, [self.base])
        [(_m, shown, _o, n)] = objs.tree_entries_raw(objs.commit_header(c, "tree")[0])
        self.assertEqual(n, 100)
        self.assertEqual(len(shown.encode()), 300, "the control: shown, it is three bytes a character")
        with mock.patch.object(push_checks, "MAX_PATH", 150):
            objs.walk(time.monotonic() + 30)   # 100 bytes: within

    def test_a_stuck_worker_is_stopped_and_its_slot_freed(self):
        rid = self.request()
        self.take()
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(sleeper.wait)
        self.addCleanup(lambda: sleeper.poll() is None and sleeper.kill())
        import runtime_ports
        old = (datetime.datetime.now().astimezone() - datetime.timedelta(seconds=push_queue.WORKER_MAX_S + 60))
        push_queue._write_json(push_queue.broker("proj") / "worker.json",
                               {"pid": sleeper.pid, "start": runtime_ports.process_start(sleeper.pid), "id": rid,
                                "attempts": 1, "spawned_at": old.isoformat()})
        self.assertFalse(push_queue.alive(push_queue.worker("proj")))
        spawned = []
        push_queue.tick(spawn=lambda project: (spawned.append(project), (os.getpid(), None))[1])
        self.assertEqual(sleeper.wait(10), -9, "the stuck worker was not stopped")
        self.assertEqual(spawned, ["proj"], "its slot was not freed")


# ---------- merge-branch's push_on_merge in a sandboxed project (section 5) ----------

from test_merge_memory import Project  # noqa: E402


class MergeRequests(Project):
    """A sandboxed project's merge-branch, run for real, files a push request for the default branch instead of running
    git push; with no request folder the merge still lands and is recorded."""

    def setUp(self):
        super().setUp()
        self.index({"demo": {"v": 2, "name": "demo", "paths": {"workspace": str(self.ws)}, "push_on_merge": True,
                             "enforce": {"sandbox": True}}})
        self.folder = self.tmp / "state" / "push-requests" / "demo"

    def test_a_request_for_the_default_branch(self):
        self.folder.mkdir(parents=True)
        wt = self.worktree("feat")
        self.commit(wt, "b", "feat: b")
        r = self.merge(wt, "--note", "n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [req] = [json.loads(f.read_text()) for f in self.folder.glob("*.request.json")]
        self.assertEqual((req["branch"], req["sha"]), ("main", self.git(self.ws, "rev-parse", "main")))
        self.assertTrue(self.events("merge.done"))
        [report] = self.events("session.report")
        self.assertTrue(report["push_requested"])

    def test_no_folder_is_no_failure_of_the_merge(self):
        wt = self.worktree("feat")
        self.commit(wt, "b", "feat: b")
        r = self.merge(wt, "--note", "n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git(self.ws, "log", "-1", "--format=%s", "main"), "feat: b")
        self.assertTrue(self.events("merge.done"))
        [report] = self.events("session.report")
        self.assertTrue(report["merged"])
        self.assertIn("push_error", report)


# ---------- end to end: the session's command, the real dispatch, a detached worker ----------

class EndToEnd(unittest.TestCase):
    """`brunna push` typed in a sandboxed project's folder, in a throwaway instance; the real `hubbus dispatch` takes it
    in and starts a detached worker, which outlives the dispatch run; the project's job goes through the real
    bin/sandboxed-run (skipped where this machine can't sandbox it)."""

    def test_the_whole_way(self):
        import isolation
        if (why := isolation.can_sandbox()):
            self.skipTest(f"this machine can't sandbox the project's job: {why}")
        tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        def workers_done():   # runs before the removal: no worker of this instance outlives the test
            deadline = time.monotonic() + 120
            while (tmp / "state" / "git-broker" / "proj" / "worker.json").exists() and time.monotonic() < deadline:
                time.sleep(0.5)
        self.addCleanup(workers_done)
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
        d = dispatch()
        self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
        res_f = tmp / "push" / "proj" / f"{rid}.result.json"
        deadline = time.monotonic() + 180
        while not res_f.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        log = tmp / "state" / "git-broker" / "proj" / "worker.log"
        res = json.loads(res_f.read_text()) if res_f.exists() else {}
        rec = tmp / "state" / "git-broker" / "proj" / "pushes.jsonl"
        self.assertEqual(res.get("state"), "pushed", (res, log.read_text() if log.exists() else "",
                                                      rec.read_text() if rec.exists() else ""))
        self.assertNotIn("could not be sent", log.read_text() if log.exists() else "")
        letters = self.bus(tmp, "SELECT payload FROM events WHERE type='message' AND to_project='proj'")
        self.assertTrue(any(p.get("type") == "info" and rid in p.get("text", "") for p in letters), letters)
        # the owner's to-do and its close through the real hubbus of this instance
        todo = subprocess.run([sys.executable, "-c", (
            "import sys; sys.path.insert(0, sys.argv[1]); import push_queue; "
            "k = push_queue.raise_todo('proj', 'emails', 'list the emails'); "
            "assert k and push_queue.raise_todo('proj', 'emails', 'again') == k; "
            "print(k, push_queue.close_todos('proj'))"), str(BIN)], env=env, capture_output=True, text=True,
            timeout=60, cwd=tmp)
        self.assertEqual(todo.returncode, 0, todo.stdout + todo.stderr)
        key = todo.stdout.split()[0]
        self.assertIn(key, todo.stdout.split(" ", 1)[1], "not closed")
        sent = [p for p in self.bus(tmp, "SELECT payload FROM events WHERE type='message'") if p.get("type") in
                ("task", "done")]
        self.assertTrue([p for p in sent if p.get("type") == "task" and "list the emails" in p.get("text", "")], sent)
        self.assertTrue([p for p in sent if p.get("type") == "done" and key in json.dumps(p)], sent)
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

    def bus(self, tmp, sql) -> list[dict]:
        import contextlib
        import sqlite3
        with contextlib.closing(sqlite3.connect(tmp / "bus" / "bus.db")) as c:
            return [json.loads(p) for (p,) in c.execute(sql)]


if __name__ == "__main__":
    unittest.main()
