"""A lease opened about a question the project asked the owner (docs/design/owner-questions.md, part 3).

create's question: an open question of the lease's project, or 409 question_unknown; kept with the lease. The session's
instructions carry the question whole and where its detail is; the handover's answered is taken only for that question,
and closes it with the owner's confirmed answer (done --question, as the project), a failure said and the question left
open.

Never starts an agent or writes the real bus: the subprocesses are replaced.

    bin/py -m unittest tests.test_lease_question
"""
import http.server
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

SID = "88888888-8888-4888-8888-888888888888"
Q = {"id": "m11251913-dfd4", "project": "proj", "ts": "2026-11-25T19:13:45+08:00",
     "text": "Hand over by state now, or wait for the numbers of two machines?\nIt changes how {{owner}} reads it.",
     "context": "The scheduler can pass a model's state from one machine to another.",
     "options": [{"id": "now", "label": "hand over by state now"}, {"id": "wait", "label": "wait for the numbers"}],
     "default": "wait", "see": ["docs/cuts.md"]}


def load_runner(name="lease_runner_q"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / "lease-runner.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.m = load_runner()
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.m.DIR = self.tmp / "state"
        self.addCleanup(self.m.LEASES.clear)


class Lookup(Base):
    def run_with(self, out, code=0):
        return mock.patch.object(self.m.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            returncode=code, stdout=out, stderr="boom" if code else ""))

    def test_an_open_question_of_the_project_is_found(self):
        with self.run_with(json.dumps([{"id": "other", "text": "x"}, {**Q, "from_session": "s"}])):
            got = self.m.open_question("proj", Q["id"])
        self.assertEqual(got, Q, "what it was sent with, nothing else (no session)")

    def test_none_when_it_isnt_there_or_cant_be_read(self):
        with self.run_with(json.dumps([{"id": "other", "text": "x"}])):
            self.assertIsNone(self.m.open_question("proj", Q["id"]))
        with self.run_with("", code=1):
            self.assertIsNone(self.m.open_question("proj", Q["id"]))
        with self.run_with("not json"):
            self.assertIsNone(self.m.open_question("proj", Q["id"]))


class Create(Base):
    """POST /v1/leases with question, through the real handler; the session start and the lookup replaced."""

    def setUp(self):
        super().setUp()
        m = self.m
        m.token = lambda: "t"
        self.ws = self.tmp / "proj"
        (self.ws / ".git").mkdir(parents=True)
        self.started, self.asked = [], []

        def lookup(project, key):
            self.asked.append((project, key))
            return dict(Q) if (project, key) == ("proj", Q["id"]) else None
        for target, attr, value in [
                (m.hubconf, "workspace", lambda name: self.ws if name == "proj" else None),
                (m.hubconf, "projects", lambda: {"proj": {}}),
                (m, "start_proc", lambda lease, resume: self.started.append((lease.id, lease.question))),
                (m, "refuse_folder", lambda cwd: None),
                (m, "open_question", lookup)]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        reg = types.ModuleType("registry")
        reg.refresh = lambda: {"projects": {}}
        p = mock.patch.dict(sys.modules, {"registry": reg})
        p.start()
        self.addCleanup(p.stop)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), m.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.server_close)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def post(self, body):
        req = urllib.request.Request(self.base + "/v1/leases", data=json.dumps({"v": 2, "project": "proj", "session_id": SID,
                                                                                "thread": "t", **body}).encode(),
                                     method="POST", headers={"Authorization": "Bearer t"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_an_open_question_opens_the_lease_about_it(self):
        code, body = self.post({"question": Q["id"]})
        self.assertEqual(code, 201, body)
        self.assertEqual(self.started, [(SID, Q)], "the session starts knowing the question")
        lease = self.m.LEASES[SID]
        self.assertEqual(self.m.Lease(lease.dump()).question, Q, "kept with the lease: a runner restart keeps it")

    def test_another_projects_or_a_closed_question_is_refused(self):
        code, body = self.post({"question": "m00000000-0000"})
        self.assertEqual((code, body["error"]), (409, "question_unknown"))
        self.assertEqual((self.started, dict(self.m.LEASES)), ([], {}), "nothing started, no lease recorded")

    def test_a_malformed_id_is_refused_before_any_lookup(self):
        for bad in ("--from=x", "", 7, "a b"):
            code, body = self.post({"question": bad})
            self.assertEqual((code, body["error"]), (400, "bad_request"), bad)
        self.assertEqual(self.asked, [])

    def test_without_a_question_nothing_changes_and_a_resume_keeps_it(self):
        self.assertEqual(self.post({"question": Q["id"]})[0], 201)
        lease = self.m.LEASES[SID]
        lease.state = "detached"   # reclaimed; the assistant comes back in that thread
        code, _body = self.post({"resume": True})
        self.assertEqual(code, 201)
        self.assertEqual(self.m.LEASES[SID].question, Q)
        self.assertEqual(self.asked, [("proj", Q["id"])])


class Instructions(Base):
    def test_the_question_whole_goes_into_the_session(self):
        text = self.m.preamble("zz-none", "discuss", Q)
        self.assertTrue(text.startswith(self.m.preamble("zz-none", "discuss")))
        for part in ("m11251913-dfd4", "> Hand over by state now", "> It changes how {{owner}} reads it.",
                     "Context: The scheduler", "now = hand over by state now; wait = wait for the numbers",
                     "Until answered, the project proceeds with: wait", "Detail: docs/cuts.md",
                     ".agent-memory/memory/2026-11-25.md", "say the answer back"):
            self.assertIn(part, text)
        self.assertNotIn("{{", self.m.preamble("zz-none", "discuss"), "the platform's own placeholders are filled")

    def test_the_handover_asks_for_the_answer_only_with_a_question(self):
        base = self.m.handover_prompt()
        self.assertNotIn("answered", base)
        asked = self.m.handover_prompt(None, Q)
        self.assertTrue(asked.startswith(base))
        self.assertIn("answered", asked)
        self.assertIn(Q["id"], asked)


class Answered(Base):
    def setUp(self):
        super().setUp()
        self.lease = self.m.Lease({"lease_id": SID, "project": "proj", "cwd": "/nonexistent", "question": Q})
        self.sent, self.fail = [], False

        def hubbus(*a):
            self.sent.append(a)
            bad = self.fail and a[:1] == ("send",) and "--question" in a
            return types.SimpleNamespace(returncode=1 if bad else 0, stderr="hubbus: not a question proj asked" if bad else "",
                                         stdout="")
        for target, attr, value in [(self.m, "hubbus", hubbus), (self.m.subprocess, "Popen", lambda *a, **k: None)]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_only_an_answer_to_the_leases_own_question_is_taken(self):
        take = self.m.answered
        self.assertEqual(take(self.lease, {"question": Q["id"], "answer": "  wait for the numbers "}),
                         {"question": Q["id"], "answer": "wait for the numbers"})
        for got in ({"question": "m-other", "answer": "x"}, {"question": Q["id"], "answer": "  "},
                    {"question": Q["id"]}, "wait", None):
            self.assertIsNone(take(self.lease, got), got)
        plain = self.m.Lease({"lease_id": SID, "project": "proj", "cwd": "/x"})
        self.assertIsNone(take(plain, {"question": Q["id"], "answer": "x"}), "a lease about no question closes none")

    def h(self):
        return {"summary": "s", "open_questions": [], "tasks_for_owner": [], "notes_markdown": "",
                "answered": {"question": Q["id"], "answer": "wait for the numbers"}}

    def test_a_confirmed_answer_closes_the_question_as_the_project(self):
        h = self.h()
        self.m.deliver(self.lease, h, 1)
        [done] = [a for a in self.sent if a[:1] == ("send",) and "--question" in a]
        self.assertEqual([done[done.index(f) + 1] for f in ("--from", "--type", "--question", "--text", "--id")],
                         ["proj", "done", Q["id"], "wait for the numbers", f"lease-{SID}-1-answered"])
        self.assertTrue(h["answered"]["closed"])
        report = next(a for a in self.sent if "--type" in a and a[a.index("--type") + 1] == "info")
        self.assertIn("wait for the numbers", report[report.index("--text") + 1])
        self.assertFalse([a for a in self.sent if a[:2] == ("publish", "job.failed")])

    def test_a_failed_close_is_said_and_the_question_stays_open(self):
        self.fail = True
        h = self.h()
        self.m.deliver(self.lease, h, 1)
        self.assertFalse(h["answered"]["closed"])
        [failed] = [a for a in self.sent if a[:2] == ("publish", "job.failed")]
        detail = json.loads(failed[failed.index("--payload") + 1])["detail"]
        self.assertIn(Q["id"], detail)
        self.assertIn("not a question proj asked", detail)

    def test_detach_keeps_answered_only_for_its_question(self):
        got = []
        for name, value in [("deny_settings", lambda project: self.tmp / "deny.json"), ("env_for", lambda lease: {}),
                            ("deliver", lambda lease, h, n, earlier=None: got.append(h)),
                            ("earlier_tasks", lambda project: [])]:
            p = mock.patch.object(self.m, name, value)
            p.start()
            self.addCleanup(p.stop)
        import runtime_claude
        p = mock.patch.object(runtime_claude, "transcript", lambda sid: self.tmp / "t.jsonl")
        p.start()
        self.addCleanup(p.stop)
        for answer, kept in ((self.h()["answered"], True), ({"question": "m-other", "answer": "x"}, False)):
            got.clear()
            lease = self.m.Lease({"lease_id": SID, "project": "proj", "cwd": str(self.tmp), "question": Q})
            lease.state = "idle_stopped"
            lease.emit = lambda t, **kw: None
            lease.set_state = lambda s, **kw: None
            so = {"summary": "s", "decisions": [], "open_questions": [], "tasks_for_owner": [], "notes_markdown": "",
                  "answered": answer}
            with mock.patch.object(self.m.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
                    returncode=0, stdout=json.dumps({"structured_output": so}), stderr="")):
                self.m.detach(lease, "user")
            self.assertEqual("answered" in got[0], kept, answer)


if __name__ == "__main__":
    unittest.main()
