"""Questions the owner can answer (docs/design/owner-questions.md): the check of a question before it is sent
(bin/question_check.py), where it runs (hubbus send from a session; the capability runner for a sandboxed one), what a
person's question carries (context, see, --as-is), --replaces, the review list, and merge-branch taking no question.

    bin/py -m unittest tests.test_owner_questions
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import question_check as qc  # noqa: E402
import runtime_ports  # noqa: E402


_LANG = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": "en"})   # the assertions read the English texts


def setUpModule():
    _LANG.start()


def tearDownModule():
    _LANG.stop()


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "bin" / file))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


F = qc.fields("Ship the fast path (A) or keep the safe one (B)?", "The cache layer is done; this decides the default.",
              [["a", "fast path: 2x speed, rare stale reads"], ["b", "safe path: as today"]], "b", ["docs/cache.md"])
OPEN = [("m1-old", "Which cache default?"), ("m2-other", "Rename the repo?")]


def answer(**over):
    got = {"decisions": ["Ship the fast path (A) or keep the safe one (B)?"], "reviews": [], "defects": [], "repeats": []}
    got.update(over)
    return got


class Verdict(unittest.TestCase):
    def test_a_clear_question_is_sent(self):
        v = qc.verdict(answer(), F, OPEN)
        self.assertEqual((v.sent, v.lines, v.unchecked), (True, [], None))

    def test_nothing_to_decide_is_refused_toward_the_review_list(self):
        v = qc.verdict(answer(decisions=[], reviews=["The cache layer is done"]), F, OPEN)
        self.assertFalse(v.sent)
        self.assertIn("--type review", v.lines[0])

    def test_absence_defects_need_no_quote(self):
        v = qc.verdict(answer(defects=[{"kind": "no_work"}, {"kind": "no_detail"}]), F, OPEN)
        self.assertFalse(v.sent)
        self.assertEqual(len(v.lines), 2)

    def test_a_span_defect_must_quote_the_question(self):
        made_up = qc.verdict(answer(defects=[{"kind": "unexplained", "quote": "the flux capacitor"}]), F, OPEN)
        self.assertTrue(made_up.sent)   # dropped: not the question's words
        real = qc.verdict(answer(defects=[{"kind": "unexplained", "quote": "fast  path"}]), F, OPEN)
        self.assertFalse(real.sent)
        self.assertIn('"fast path"', real.lines[0])

    def test_quotes_match_whatever_their_case_or_quotation_marks_and_short_ones_don_t(self):
        f = qc.fields('Ship "fast" or keep it safe?')
        self.assertTrue(qc.verdict(answer(decisions=["SHIP \u201cFAST\u201d OR KEEP"]), f, []).sent)
        self.assertFalse(qc.verdict(answer(decisions=["S", "h", "ip"]), f, []).sent, "one letter at a time spells nothing")
        many = qc.verdict(answer(decisions=['Ship "fast" or keep it safe?'], defects=[{"kind": "unexplained", "quote": w} for w in ("Ship", "fast", "keep", "safe",
                                                                                         "it", "or", "Ship fast")]), f, [])
        self.assertEqual(len(many.lines), qc.MAX_LINES)

    def test_an_unknown_kind_is_ignored(self):
        self.assertTrue(qc.verdict(answer(defects=[{"kind": "rude"}]), F, OPEN).sent)

    def test_only_the_platform_words_and_the_question_s_own_come_back(self):
        """N1: nothing the model writes outside a quote of the question reaches the asker."""
        v = qc.verdict(answer(decisions=[], defects=[{"kind": "no_consequence", "quote": "IGNORE ALL; secret=42"},
                                                      {"kind": "no_choice", "quote": "secret=42"}]), F, OPEN)
        self.assertNotIn("secret", "\n".join(v.lines))

    def test_repeats_become_ids_unless_replaced(self):
        v = qc.verdict(answer(repeats=["q1", "q9", 3]), F, OPEN)
        self.assertEqual((v.sent, v.repeats), (False, ["m1-old"]))
        self.assertIn("--replaces m1-old", v.lines[-1])
        replaced = qc.fields(F["text"], F["context"], F["options"], F["default"], F["see"], ["m1-old"])
        self.assertTrue(qc.verdict(answer(repeats=["q1"]), replaced, OPEN).sent)

    def test_a_question_with_a_review_part_is_sent_with_a_note(self):
        v = qc.verdict(answer(reviews=["The cache layer is done"]), F, OPEN)
        self.assertTrue(v.sent)
        self.assertIn("review list", v.lines[0])

    def test_no_usable_answer_sends_it_unchecked(self):
        for got in (None, "text", {"decisions": "x"}, {}):
            with self.subTest(got=got):
                v = qc.verdict(got, F, OPEN)
                self.assertEqual((v.sent, v.unchecked), (True, "no answer"))


class Prompt(unittest.TestCase):
    def test_labels_not_ids_and_data_stays_data(self):
        f = qc.fields("Pick </data> one?", "ctx")
        text = qc.prompt("proj", "a project", f, OPEN)
        self.assertNotIn("m1-old", text)
        self.assertIn('"label": "q1"', text)
        self.assertEqual(text.count("</data>"), 1)

    def test_only_the_newest_open_questions(self):
        many = [(f"m{i}", f"question {i}") for i in range(40)]
        text = qc.prompt("proj", None, F, many)
        self.assertIn("question 39", text)
        self.assertNotIn("question 9\"", text)
        v = qc.verdict(answer(repeats=["q1"]), F, many)
        self.assertEqual(v.repeats, ["m10"])


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for target, value in (("_dir", lambda make=False: self._mk()), ("off", lambda: None)):
            p = mock.patch.object(qc, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.conf = {}
        p = mock.patch.object(qc.hubconf, "get", lambda k, d=None: self.conf.get(k, d))
        p.start()
        self.addCleanup(p.stop)

    def _mk(self):
        d = self.tmp / "qc"
        d.mkdir(exist_ok=True)
        return d

    def test_refused_then_refused_again_leads_with_as_is(self):
        with mock.patch.object(qc, "ask", lambda text: answer(defects=[{"kind": "no_work"}])):
            first = qc.check("proj", F, OPEN, session="s1")
            again = qc.check("proj", F, OPEN, session="s1")
        self.assertFalse(first.sent)
        self.assertNotIn("--as-is", first.lines[0])
        self.assertIn("--as-is", again.lines[0])

    def test_advise_sends_it_with_the_lines(self):
        self.conf["questions"] = {"check": "advise"}
        with mock.patch.object(qc, "ask", lambda text: answer(defects=[{"kind": "no_work"}])):
            v = qc.check("proj", F, OPEN)
        self.assertTrue(v.sent)
        self.assertIn("advise", v.lines[-1])

    def test_the_day_s_checks_run_out_to_unchecked(self):
        calls = []
        with mock.patch.object(qc, "ask", lambda text: calls.append(1) or answer()):
            for _ in range(qc.DAILY + 2):
                v = qc.check("proj", F, OPEN)
            other = qc.check("other", F, OPEN)
        self.assertEqual((len(calls), v.unchecked, other.unchecked), (qc.DAILY + 1, "daily", None))

    def test_off_sends_it_unchecked(self):
        with mock.patch.object(qc, "off", lambda: "no model"), mock.patch.object(qc, "ask", lambda t: self.fail("asked")):
            v = qc.check("proj", F, OPEN)
        self.assertEqual((v.sent, v.unchecked), (True, "off"))


class Runner(unittest.TestCase):
    """A sandboxed session's side: the request in its own folder, the runner's verdict read back."""

    def setUp(self):
        self.ws = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_no_runner_sends_it_unchecked(self):
        v = qc.via_runner(self.ws, F, wait_s=0.3)
        self.assertEqual((v.sent, v.unchecked), (True, "runner"))
        self.assertEqual(list((self.ws / ".hub-run" / "requests").iterdir()), [])

    def test_the_runner_s_verdict(self):
        cr = load("cap_runner_q", "cap-runner.py")
        state = self.ws / "state"
        state.mkdir()
        refused = qc.Verdict(False, ["- no"], ["m1"], decisions=["x"])
        projects = {"sbx": {"enforce": {"sandbox": True}}, "open": {}}
        with mock.patch.object(cr, "eligible", lambda: {"sbx": {"dirs": [self.ws], "caps": [], "models": {}}}), \
                mock.patch.object(cr, "DIR", state), mock.patch.object(cr, "LOG", state / "log.jsonl"), \
                mock.patch.object(cr, "exclude_hub_run", lambda project, ws: None), \
                mock.patch.object(cr.hubconf, "projects", lambda: projects), \
                mock.patch.object(cr, "open_questions_of", lambda p: OPEN), \
                mock.patch.object(qc, "ask", lambda text: {"decisions": ["Ship the fast path (A) or keep the safe one (B)?",
                                                                       "keep the safe one"],
                                                         "reviews": [], "defects": [{"kind": "no_work"}], "repeats": ["q1"]}), \
                mock.patch.object(qc, "_dir", lambda make=False: self._qc_dir()), mock.patch.object(qc, "off", lambda: None):
            runner = cr.Runner()
            import threading
            got = {}
            t = threading.Thread(target=lambda: got.setdefault("v", qc.via_runner(self.ws, F, "s1", wait_s=10)))
            t.start()
            t0 = time.time()
            while t.is_alive() and time.time() - t0 < 10:
                runner.tick()
                time.sleep(0.1)
            t.join()
        v = got["v"]
        self.assertEqual((v.sent, v.repeats, len(v.decisions)), (False, ["m1-old"], 1), "at most one of the model's quotes")
        self.assertIn("what the work is", v.lines[-2] + v.lines[-1] + " ".join(v.lines))
        del refused

    def _qc_dir(self):
        d = self.ws / "qc"
        d.mkdir(exist_ok=True)
        return d

    def test_one_question_at_a_time_per_project(self):
        cr = load("cap_runner_q3", "cap-runner.py")
        state = self.ws / "state"
        state.mkdir()
        with mock.patch.object(cr, "eligible", lambda: {"sbx": {"dirs": [self.ws], "caps": [], "models": {}}}), \
                mock.patch.object(cr, "DIR", state), mock.patch.object(cr, "LOG", state / "log.jsonl"), \
                mock.patch.object(cr, "exclude_hub_run", lambda project, ws: None), \
                mock.patch.object(cr.hubconf, "projects", lambda: {"sbx": {"enforce": {"sandbox": True}}}), \
                mock.patch.object(qc, "left", lambda project: True):
            runner = cr.Runner()
            runner.qbusy.add("sbx")   # one is being checked
            d = self.ws / ".hub-run" / "requests"
            d.mkdir(parents=True)
            for i in range(50):
                (d / f"q{i}.json").write_text(json.dumps({"v": 1, "id": f"q{i}", "kind": "question-check", "fields": F}))
            runner.tick()
        states = {json.loads((self.ws / ".hub-run" / "jobs" / f"q{i}" / "state.json").read_text())["state"] for i in range(50)}
        self.assertEqual((states, runner.threads), ({"rejected"}, []), "refused at once, no thread, nothing held")

    def test_only_a_sandboxed_project_is_checked_by_the_runner(self):
        cr = load("cap_runner_q2", "cap-runner.py")
        state = self.ws / "state"
        state.mkdir()
        with mock.patch.object(cr, "eligible", lambda: {"open": {"dirs": [self.ws], "caps": [], "models": {"x": {}}}}), \
                mock.patch.object(cr, "DIR", state), mock.patch.object(cr, "LOG", state / "log.jsonl"), \
                mock.patch.object(cr, "exclude_hub_run", lambda project, ws: None), \
                mock.patch.object(cr.hubconf, "projects", lambda: {"open": {}}):
            d = self.ws / ".hub-run" / "requests"
            d.mkdir(parents=True)
            (d / "q1.json").write_text(json.dumps({"v": 1, "id": "q1", "kind": "question-check", "fields": F}))
            cr.Runner().tick()
        st = json.loads((self.ws / ".hub-run" / "jobs" / "q1" / "state.json").read_text())
        self.assertEqual(st["state"], "rejected")


class Send(unittest.TestCase):
    """hubbus send: a person's question checked and composed; --replaces, --see, --as-is; the review list."""

    def setUp(self):
        m = self.m = load("hubbus_owner_q", "hubbus")
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        m.DB = self.tmp / "bus.db"
        m.log = lambda s: None
        m.projects = lambda: {"proj": self.tmp, "other": self.tmp}
        self.c = m.db()
        m.db = lambda *a, **k: self.c
        m.RECORDS = True
        self.owner = m.hubconf.owner_address()
        self.session = "claude:s1"
        p = mock.patch.object(runtime_ports, "current_session", lambda env=None: self.session)
        p.start()
        self.addCleanup(p.stop)
        self.verdict = qc.Verdict(True)
        self.checked = []
        p = mock.patch.object(qc, "check", lambda project, f, opens, session=None, mark=True:
                              self.checked.append((project, f["text"])) or self.verdict)
        p.start()
        self.addCleanup(p.stop)

    def send(self, *argv, frm="proj"):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["hubbus", "send", "--from", frm, "--to", self.owner, *argv]), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(err):
            try:
                self.m.main()
                code = 0
            except SystemExit as e:   # a refusal exits with its words
                code = e.code if isinstance(e.code, int) else 1
                if isinstance(e.code, str):
                    err.write(e.code)
        return code, out.getvalue().strip(), err.getvalue()

    def recs(self):
        return [self.m.payload(r)["record"] for r in self.c.execute("SELECT * FROM events WHERE type='record' ORDER BY seq")]

    def test_a_refused_question_writes_nothing(self):
        self.verdict = qc.Verdict(False, ["Nothing says what the work is"])
        code, _out, err = self.send("--type", "question", "--text", "A or B?")
        self.assertNotEqual(code, 0)
        self.assertIn("Nothing says what the work is", err)
        self.assertIn("--as-is", err)
        self.assertEqual(self.recs(), [])

    def test_as_is_skips_the_check_and_says_so(self):
        self.verdict = qc.Verdict(False, ["no"])
        code, _out, _err = self.send("--type", "question", "--text", "A or B?", "--context", "The cache work.",
                                     "--see", "docs/cache.md", "--as-is", "it is clear to the owner")
        self.assertEqual(code, 0)
        [r] = self.recs()
        text = r["terms"]["text"]
        for part in ("A or B?", "The cache work.", "docs/cache.md", "it is clear to the owner"):
            self.assertIn(part, text)
        self.assertNotIn("context", r)   # in the text, said once

    def test_platform_senders_keep_their_context_and_aren_t_checked(self):
        self.session = None
        self.verdict = qc.Verdict(False, ["no"])
        code, _o, _e = self.send("--type", "question", "--text", "A or B?", "--context", "lease:abc")
        self.assertEqual(code, 0)
        [r] = self.recs()
        self.assertEqual((r["terms"]["text"], r["context"]), ("A or B?", "lease:abc"))

    def test_platform_runs_aren_t_checked(self):
        self.verdict = qc.Verdict(False, ["no"])
        with mock.patch.dict(os.environ, {"HUB_LLM_POINT": "lease-session"}):
            code, _o, _e = self.send("--type", "question", "--text", "A or B?")
        self.assertEqual(code, 0)

    def test_replaces_closes_the_old_question(self):
        _c, old, _e = self.send("--type", "question", "--text", "Old A or B?")
        code, new, _e = self.send("--type", "question", "--text", "A or B, now with numbers?", "--replaces", old)
        self.assertEqual(code, 0)
        self.assertEqual([q["id"] for q in self.m.open_questions(self.c, "proj")], [new])
        withdrawn = [r for r in self.recs() if r["type"] == "withdrawn"]
        self.assertEqual((withdrawn[0]["key"], withdrawn[0]["note"]), (old, f"replaced by {new}"))

    def test_replaces_only_this_project_s_open_questions(self):
        _c, theirs, _e = self.send("--type", "question", "--text", "Their question?", frm="other")
        code, _o, err = self.send("--type", "question", "--text", "Mine?", "--replaces", theirs)
        self.assertNotEqual(code, 0)
        self.assertIn("not an open question", err)
        self.assertEqual(len(self.m.open_questions(self.c, "other")), 1)

    def test_see_names_a_file_inside_the_project(self):
        for bad in ("/etc/passwd", "../other/x", "~/x", "a\nb"):
            with self.subTest(bad=bad):
                code, _o, err = self.send("--type", "question", "--text", "A?", "--see", bad)
                self.assertNotEqual(code, 0)
                self.assertIn("--see", err)

    def test_a_bad_flag_is_refused_before_a_check_is_spent(self):
        code, _o, _err = self.send("--type", "question", "--text", "A?", "--see", "/etc/passwd")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.checked, [])

    def test_see_and_as_is_reach_the_owner_from_any_sender(self):
        self.session = None
        code, _o, err = self.send("--type", "question", "--text", "A or B?", "--see", "docs/x.md", "--as-is", "clear")
        self.assertEqual(code, 0)
        self.assertIn("not checked", err)
        [r] = self.recs()
        self.assertIn("docs/x.md", r["terms"]["text"])
        self.assertIn("clear", r["terms"]["text"])

    def test_an_mcp_session_is_checked(self):
        self.session = None
        self.verdict = qc.Verdict(False, ["no"])
        with mock.patch.dict(os.environ, {"BRUNNA_MCP_ASKER": "1"}):
            code, _o, _e = self.send("--type", "question", "--text", "A or B?")
        self.assertNotEqual(code, 0)

    def test_a_task_is_only_told_when_it_asks_to_decide(self):
        self.verdict = qc.Verdict(False, ["no"], decisions=["choose A or B"])
        code, _o, err = self.send("--type", "task", "--text", "Choose A or B")
        self.assertEqual(code, 0)
        self.assertIn("--type question", err)

    def review(self, *argv):
        return self.send("--type", "review", *argv)

    def test_the_review_list(self):
        code, _o, err = self.review("--text", "- docs/cut.md, the new cut")
        self.assertEqual(code, 0)
        self.assertIn("revision 1", err)
        code, _o, err = self.review("--text", "- docs/cut.md\n- docs/slots.md")
        self.assertNotEqual(code, 0)   # no --rev: the list as it stands comes back
        self.assertIn("docs/cut.md, the new cut", err)
        code, _o, err = self.review("--text", "- docs/cut.md\n- docs/slots.md", "--rev", "1")
        self.assertEqual(code, 0)
        self.assertIn("revision 2", err)
        code, _o, _e = self.review("--text", "- stale", "--rev", "1")
        self.assertNotEqual(code, 0)
        keys = {r["key"] for r in self.recs()}
        self.assertEqual(keys, {"review-proj"})
        amended = [r for r in self.recs() if r["type"] == "amended"]
        self.assertIn("- docs/cut.md\n- docs/slots.md", amended[-1]["context"], "its lines kept, in the context")
        self.assertTrue(all("\n" not in r["terms"]["text"] for r in self.recs()), "an Acts item's text is one line")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["hubbus", "review", "--project", "proj"]), redirect_stdout(out):
            self.m.main()
        self.assertTrue(out.getvalue().rstrip().endswith("- docs/cut.md\n- docs/slots.md"))
        self.assertEqual(out.getvalue().count("(its review list)"), 0, "the session gets its own text back, no header")

    def test_a_review_list_needs_records(self):
        self.m.RECORDS = False
        code, _o, err = self.review("--text", "- x")
        self.assertNotEqual(code, 0)
        self.assertIn("mailbox.records", err)

    def test_review_keys_of_projects_with_dashes(self):
        self.assertEqual(self.m.review_n("review-proj-3", "proj"), 3)
        self.assertEqual(self.m.review_n("review-proj", "proj"), 1)
        self.assertIsNone(self.m.review_n("review-other", "proj"))
        with mock.patch.object(self.m, "own_names", lambda p: [p]):
            self.assertEqual(self.m.review_n("review-a-b-2", "a-b"), 2)
            self.assertEqual(self.m.review_n("review-a-b-2", "a-b-2"), 1)
            self.assertIsNone(self.m.review_n("review-a-b", "a"))

    def test_a_stale_review_list_from_an_outbox_comes_back_as_a_request(self):
        self.review("--text", "- first")
        entry = {"v": 1, "op": "send", "id": "m11260900-0123456789ab", "type": "review", "to": self.owner,
                 "text": "- second", "rev": None}
        self.c.execute("BEGIN IMMEDIATE")
        self.m.take(self.c, "proj", entry)
        self.c.execute("COMMIT")
        [back] = [self.m.payload(r) for r in self.c.execute("SELECT * FROM events WHERE type='message' AND to_project='proj'")]
        self.assertEqual(back["type"], "request")
        self.assertIn("- first", back["text"])


class Outbox(Send):
    """A sandboxed project's send: what goes into its outbox, and what the platform does with it."""
    # Send's tests run here too, through the outbox; a review list's revision is told at take-in, not by the session's send
    test_the_review_list = None

    def setUp(self):
        super().setUp()
        self.entries = []
        for name, value in (("mail_box", lambda a, here: "proj"), ("view_db", lambda p: self.c),
                            ("outbox_put", lambda p, e: self.entries.append(e)), ("queued", lambda p: None)):
            p = mock.patch.object(self.m, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(qc, "via_runner", lambda ws, f, session=None, mark=True: self.verdict)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.m.hubconf, "workspace", lambda name: self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def take(self, entry):
        self.c.execute("BEGIN IMMEDIATE")
        self.m.take(self.c, "proj", {"v": 1, **entry})
        self.c.execute("COMMIT")

    def test_the_entry_carries_the_person_s_question_once(self):
        code, _o, _e = self.send("--type", "question", "--text", "A or B?", "--context", "the cache work",
                                 "--option", "a", "fast", "--see", "docs/x.md")
        self.assertEqual(code, 0)
        [e] = self.entries
        self.assertEqual((e["context"], e["options"], e["text"]), (None, [{"id": "a", "label": "fast"}],
                                                                  "A or B?\n\nthe cache work"))
        self.c.execute("DELETE FROM events")   # the view's copy: the bus starts from the outbox
        self.take(e)
        [r] = [x for x in self.recs() if x["type"] == "raised"]
        self.assertEqual(r["terms"]["text"], "A or B?\n\nthe cache work\nDetail: docs/x.md", "composed once")

    def test_a_replaced_question_closed_meanwhile_doesn_t_lose_the_new_one(self):
        self.session = None
        self.m.mail_box = lambda a, here: None
        _c, old, _e = self.send("--type", "question", "--text", "Old A or B?")
        self.send("--type", "drop", "--question", old, "--text", "answered elsewhere")
        self.take({"op": "send", "id": "m11260900-0123456789ab", "type": "question", "to": self.owner, "text": "New?",
                   "replaces": [old]})
        self.assertEqual([q["id"] for q in self.m.open_questions(self.c, "proj")], ["m11260900-0123456789ab"])

    def test_replaces_through_the_outbox_closes_once(self):
        self.session = None
        self.m.mail_box = lambda a, here: None
        _c, old, _e = self.send("--type", "question", "--text", "Old A or B?")
        entry = {"op": "send", "id": "m11260901-0123456789ab", "type": "question", "to": self.owner, "text": "New?",
                 "replaces": [old]}
        self.take(entry)
        self.assertEqual([q["id"] for q in self.m.open_questions(self.c, "proj")], [entry["id"]])
        withdrawn = [r for r in self.recs() if r["type"] == "withdrawn"]
        self.assertEqual(len(withdrawn), 1)


class Mcp(unittest.TestCase):
    def test_ask_owner_carries_the_new_fields(self):
        ms = load("mcp_server_q", "mcp_server.py")
        args = ms.mail_args("proj", "ask_owner", {"text": "A or B?", "context": "the cache", "see": ["docs/c.md"],
                                                  "replaces": ["m1"], "as_is": "clear"}, None)
        for flag in ("--context=the cache", "--see=docs/c.md", "--replaces=m1", "--as-is=clear"):
            self.assertIn(flag, args)
        with self.assertRaises(ms.BadArgs):
            ms.mail_args("proj", "ask_owner", {"text": "A?", "see": "docs/c.md"}, None)


class MergeBranch(unittest.TestCase):
    def test_a_question_is_refused_before_anything(self):
        mb = load("merge_branch_q", "merge-branch")
        with mock.patch.object(sys, "argv", ["merge-branch", "--question", "A or B?"]), \
                mock.patch.object(mb, "project_of", lambda wt: self.fail("went on")):
            with self.assertRaises(SystemExit) as e:
                mb.main()
        self.assertIn("brunna send", str(e.exception.code))


if __name__ == "__main__":
    unittest.main()
