"""The librarian's anchor admission inside process() (bin/librarian.py; docs/design/state-anchors.md, "Admission"):
after the model's call, an anchor it added that does not resolve on the project's main loses its sign. Pinned here:

  hook-mixed  the project's hook already committed the model's edit (good and bad anchors) → admission is committed on
              top, no second commit, the checkpoint advances to it, the review-log entry names it
  hook-only   the hook committed an edit whose only change was a bad anchor → admission's commit gives the file back;
              the run still counts as done (checkpoint advances, no misreport warning, no review-log entry)
  only        the same edit with no hook → nothing committed, nothing pushed, the run counts as done
  mixed       no hook, good and bad anchors → one "librarian: update STATE.md" commit with the bad one unanchored
In every case the eval outcome records what the model wrote, before admission.

process() runs in-process on a real memory copy (bare repo + clone) and a real project repo; the model call, the
shadow comparison, the to-do repair and the bus are stand-ins, the push a recording script.

    bin/py -m unittest tests.test_librarian_admission
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
BEFORE = "# STATE\n\n- The job runs in ⚓`src/job.py`.\n- The parser is in `pkg/parse.py`.\n"
GOOD_AND_BAD = BEFORE + "- Retries go through ⚓`src/job.py#run_job`.\n- Settings live in ⚓`core/settings.py`.\n"
ONLY_BAD = BEFORE.replace("`pkg/parse.py`", "⚓`pkg/parse.py`")
HOOK_SUBJECT = "agent-hub: sync memory/STATE.md"


def load():
    spec = importlib.util.spec_from_file_location("librarian_admission_t", ROOT / "bin" / "librarian.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Admission(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.dict(os.environ, GIT_ENV)
        p.start()
        self.addCleanup(p.stop)
        self.lib = load()
        # the project's code: src/job.py with run_job on main; core/settings.py and pkg/parse.py exist nowhere
        self.ws = self.tmp / "ws"
        self.git(self.tmp, "init", "-q", "-b", "main", str(self.ws))
        (self.ws / "src").mkdir()
        (self.ws / "src" / "job.py").write_text("def run_job():\n    return 1\n")
        self.git(self.ws, "add", "-A")
        self.git(self.ws, "commit", "-qm", "init")
        # the memory copy: the checkpoint at init, one new note pushed (the librarian's pull brings it in)
        bare, self.d = self.tmp / "demo-memory.git", self.tmp / "copies" / "demo"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "agent-memory", str(bare))
        self.git(self.tmp, "clone", "-q", str(bare), str(self.d))
        (self.d / "memory").mkdir()
        (self.d / "AGENT.md").write_text("agent\n")
        (self.d / "memory" / "2026-10-20.md").write_text("- old note\n")
        (self.d / "STATE.md").write_text(BEFORE)
        self.git(self.d, "add", "-A")
        self.git(self.d, "commit", "-qm", "init")
        self.git(self.d, "push", "-q", "origin", "HEAD")
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "demo.sha").write_text(self.git(self.d, "rev-parse", "HEAD").strip() + "\n")
        (self.d / "memory" / "2026-10-27.md").write_text("- moved the settings\n")
        self.git(self.d, "add", "-A")
        self.git(self.d, "commit", "-qm", "new note")
        self.git(self.d, "push", "-q", "origin", "HEAD")
        self.git(self.d, "reset", "-q", "--hard", "HEAD~1")
        # where process() writes, and the push stand-in
        self.pushes = self.tmp / "pushes"
        push = self.tmp / "safe-push.sh"
        push.write_text(f"#!/bin/sh\necho \"$PWD\" >> '{self.pushes}'\n")
        push.chmod(0o755)
        self.case = self.tmp / "case"
        self.case.mkdir()
        self.outcomes = []
        for name, value in {"STATE_DIR": self.state, "LOG_DIR": self.tmp, "LOG": self.tmp / "librarian.log",
                            "REVIEW_LOG": self.tmp / "review-log.md", "SAFE_PUSH": push}.items():
            setattr(self.lib, name, value)
        real_outcome = self.lib.record_outcome
        def outcome(case, o, files):
            self.outcomes.append(o)
            real_outcome(case, o, files)
        for name, value in {"load_records": lambda proj: [], "shadow_state": lambda *a, **k: None,
                            "repair_state_todo": lambda proj, d: None,
                            "record_state_case": lambda *a, **k: self.case, "record_outcome": outcome}.items():
            p = mock.patch.object(self.lib, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(self.lib.hubconf, "roster", return_value={"demo": {"workspace": str(self.ws)}})
        p.start()
        self.addCleanup(p.stop)

    def git(self, cwd, *args) -> str:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def model(self, text: str, hook: bool):
        """the stand-in for the librarian-state call: writes STATE.md (and, as the project's SessionEnd hook would,
        commits it while the call is still running)"""
        def llm(point, prompt, cwd, project=None):
            self.assertEqual(point, "librarian-state")
            (pathlib.Path(cwd) / "STATE.md").write_text(text)
            if hook:
                self.git(cwd, "add", "STATE.md")
                self.git(cwd, "commit", "-qm", HOOK_SUBJECT)
            obj = {"type": "result", "is_error": False, "structured_output": {
                "state_updated": True, "summary": "summary demo", "reason": "reason demo", "worth_digesting": True}}
            return 0, obj, json.dumps(obj)
        return llm

    def process(self, text: str, hook: bool) -> list[str]:
        digest: list[str] = []
        with mock.patch.object(self.lib, "llm", self.model(text, hook)):
            self.lib.process("demo", self.d, [], digest)
        return digest

    def subjects(self) -> list[str]:
        return self.git(self.d, "log", "--format=%s").splitlines()

    def head(self) -> str:
        return self.git(self.d, "rev-parse", "HEAD").strip()

    def log(self) -> str:
        return (self.tmp / "librarian.log").read_text()

    def pushed(self) -> int:
        return len(self.pushes.read_text().splitlines()) if self.pushes.exists() else 0

    def review_log(self) -> str:
        f = self.tmp / "review-log.md"
        return f.read_text() if f.exists() else ""

    def assert_done(self):
        """the run counts as done: checkpoint at HEAD, clean copy, no misreport warning, not retried"""
        self.assertEqual((self.state / "demo.sha").read_text().strip(), self.head())
        self.assertEqual(self.git(self.d, "status", "--porcelain"), "")
        _ = self.lib._
        self.assertNotIn(_("{project}: this result is not trustworthy; the checkpoint is not advanced, retrying next "
                           "time", project="demo"), self.log())
        self.assertNotIn(_("{project}: warning: the agent reported state_updated={reported}, but the actual file "
                           "change={actually} (going by the actual files)", project="demo", reported="true",
                           actually="false"), self.log())

    def unanchored_line(self, raws: str) -> str:
        return self.lib._("{project}: anchors that do not resolve on the project's main were turned back into plain "
                          "code: {anchors}", project="demo", anchors=raws)

    def undid_line(self) -> str:
        return self.lib._("{project}: admission undid the model's only edit (anchors that do not resolve)",
                          project="demo")

    # --- somebody's merge, begun while the run goes on -------------------------------------------------------------
    def begin(self):
        """a person's revert (or a merge: it is told by its files): STATE.md as they left it, another file staged"""
        (self.d / ".git" / "REVERT_HEAD").write_text(self.head() + "\n")
        (self.d / "STATE.md").write_text("a person's\n")
        (self.d / "theirs.md").write_text("half done\n")
        self.git(self.d, "add", "theirs.md")

    def untouched(self, head, digest=()):
        self.assertEqual((self.d / "STATE.md").read_text(), "a person's\n")
        self.assertEqual(self.git(self.d, "status", "--porcelain"), " M STATE.md\nA  theirs.md\n")
        self.assertTrue((self.d / ".git" / "REVERT_HEAD").exists())
        self.assertFalse((self.d / ".git" / "agent-hub-op.lock").exists())
        self.assertEqual(self.head(), head)
        self.assertEqual(self.pushed(), 0)
        self.assertNotEqual((self.state / "demo.sha").read_text().strip(), head, "tried again the next night")
        self.assertIn("under way", self.log())
        self.assertEqual(list(digest), [])
        self.assertEqual(self.review_log(), "")

    def blocked_at(self, where):
        """the person begins right before this step of process()"""
        real = getattr(self.lib, where)

        def step(*a, **k):
            if not (self.d / ".git" / "REVERT_HEAD").exists():
                self.begin()
            return real(*a, **k)
        return mock.patch.object(self.lib, where, step)

    def test_nothing_is_written_over_a_merge_begun_before_any_step(self):
        for where, text, hook in (("quarantine_stray", GOOD_AND_BAD, False), ("admit_anchors", ONLY_BAD, False),
                                  ("commit_state", GOOD_AND_BAD, False)):
            with self.subTest(where):
                self.setUp()
                with self.blocked_at(where):
                    digest = self.process(text, hook=hook)
                self.untouched(self.head(), digest)
                self.assertEqual(self.subjects(), ["new note", "init"])

    def test_the_admission_commit_is_not_made_over_one_either(self):
        with mock.patch.object(self.lib, "admit_anchors", lambda *a: self.begin() or ["x"]):
            digest = self.process(GOOD_AND_BAD, hook=True)
        self.untouched(self.head(), digest)
        self.assertEqual(self.subjects(), [HOOK_SUBJECT, "new note", "init"])

    def test_a_failed_call_restores_nothing_over_one(self):
        def llm(*a, **k):
            self.begin()
            return 1, None, ""
        with mock.patch.object(self.lib, "llm", llm):
            self.lib.process("demo", self.d, [], [])
        self.untouched(self.head())

    def test_no_model_is_started_in_one(self):
        """one begun between the pull and the call"""
        self.begin_after_pull()
        with mock.patch.object(self.lib, "llm", side_effect=AssertionError("started")):
            self.lib.process("demo", self.d, [], [])
        self.untouched(self.head())

    def begin_after_pull(self):
        real = self.lib.stale_anchors

        def then(*a):
            self.begin()
            return real(*a)
        p = mock.patch.object(self.lib, "stale_anchors", then)
        p.start()
        self.addCleanup(p.stop)

    def test_the_completion_versions_write_nothing_over_one(self):
        self.begin_after_pull()
        answer = {"structured_output": {"state_updated": True, "edits": []}}
        with mock.patch.dict(self.lib.COMPLETION, {"state": True}), \
                mock.patch.object(self.lib, "_edits_call", lambda *a, **k: (0, answer, "x", "the model's\n")):
            self.lib.process("demo", self.d, [], [])
        self.untouched(self.head())
        with mock.patch.object(self.lib, "_edits_call", lambda *a, **k: (0, answer, "x", "the model's\n")), \
                self.assertRaises(self.lib.memory_paths.Blocked):
            self.lib.todo_by_edits("demo", self.d, "problems")
        self.assertEqual((self.d / "STATE.md").read_text(), "a person's\n")

    def test_a_merge_from_before_is_left_and_said_by_the_pull(self):
        self.begin()
        self.lib.process("demo", self.d, [], [])
        self.assertEqual((self.d / "STATE.md").read_text(), "a person's\n")
        self.assertIn("git pull failed, skip", self.log())

    def test_a_lock_that_cannot_be_had_leaves_the_project_and_names_it(self):
        (self.d / ".git" / "agent-hub-op.lock").mkdir()
        with mock.patch.object(self.lib.memory_paths, "LOCK_WAIT_S", 0):
            self.lib.process("demo", self.d, [], [])
        self.assertIn(f"rmdir '{(self.d / '.git').resolve() / 'agent-hub-op.lock'}'", self.log())
        self.assertNotIn("changed memory files", self.log())

    def test_the_owner_is_told_once_for_a_project_and_when_it_is_over(self):
        told = []
        alert = __import__("alert")
        with mock.patch.object(alert, "once", lambda key, p, ok, ids=None: told.append((key, bool(p), ids)) or True), \
                mock.patch.object(alert, "open_keys", lambda prefix: [k for k, p, _i in told[-1:] if p]), \
                mock.patch.object(self.lib.memory_paths, "LOCK_WAIT_S", 0):
            (self.d / ".git" / "agent-hub-op.lock").mkdir()
            for _i in range(2):
                self.lib.process("demo", self.d, [], [])
            (self.d / ".git" / "agent-hub-op.lock").rmdir()
            for _i in range(2):
                with mock.patch.object(self.lib, "llm", self.model(BEFORE, False)):
                    self.lib.process("demo", self.d, [], [])
        self.assertEqual(told, [("memory-copy-blocked:demo", True, ["blocked"])] * 2 + [("memory-copy-blocked:demo", False, [])])

    def test_what_could_not_be_committed_is_left_for_the_next_night(self):
        real = self.lib.quarantine_stray

        def index_held(*a):
            got = real(*a)
            (self.d / ".git" / "index.lock").write_text("")
            return got
        with mock.patch.object(self.lib, "quarantine_stray", index_held):
            digest = self.process(GOOD_AND_BAD, hook=False)
        self.assertEqual(self.subjects(), ["new note", "init"])
        self.assertNotEqual((self.state / "demo.sha").read_text().strip(), self.head())
        self.assertIn("could not be committed", self.log())
        self.assertEqual((digest, self.review_log(), self.pushed()), ([], "", 0))

    def test_only_state_md_is_committed_and_no_hook_runs(self):
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        ran = self.tmp / "ran"
        for h in ("pre-commit", "commit-msg", "post-commit", "post-checkout"):
            (hooks / h).write_text(f"#!/bin/sh\necho {h} >>'{ran}'\n")
            (hooks / h).chmod(0o755)
        real = self.lib.quarantine_stray

        def staged_elsewhere(*a):
            got = real(*a)
            (self.d / "memory" / "other.md").write_text("somebody's\n")
            self.git(self.d, "add", "memory/other.md")
            self.git(self.d, "config", "core.hooksPath", str(hooks))
            return got
        with mock.patch.object(self.lib, "quarantine_stray", staged_elsewhere):
            self.process(GOOD_AND_BAD, hook=False)
        self.assertEqual(self.git(self.d, "show", "--format=%s", "--name-only", "HEAD").split("\n"),
                         ["librarian: update STATE.md", "", "STATE.md", ""])
        self.assertEqual(self.git(self.d, "status", "--porcelain"), "A  memory/other.md\n")
        self.assertFalse(ran.exists(), ran.read_text() if ran.exists() else "")
        self.assertEqual(self.pushed(), 1)

    def test_one_project_left_does_not_stop_the_run(self):
        done = []
        with mock.patch.object(self.lib, "_process", lambda proj, *a: done.append(proj) or (_ for _ in ()).throw(
                self.lib.memory_paths.Blocked("under way")) if proj == "a" else done.append(proj)):
            for proj in ("a", "b"):
                self.lib.process(proj, self.d, [], [])
        self.assertEqual(done, ["a", "b"])

    def test_hook_committed_mixed_edit_gets_admission_on_top(self):
        digest = self.process(GOOD_AND_BAD, hook=True)
        admitted = GOOD_AND_BAD.replace("⚓`core/settings.py`", "`core/settings.py`")
        self.assertEqual(self.subjects(), ["librarian: admit STATE.md anchors", HOOK_SUBJECT, "new note", "init"],
                         "admission is committed on top of the hook's commit, and nothing after it")
        self.assertEqual(self.git(self.d, "show", "HEAD:STATE.md"), admitted)
        self.assertEqual(self.git(self.d, "show", "HEAD~1:STATE.md"), GOOD_AND_BAD, "the hook's commit is left as is")
        self.assertEqual(self.pushed(), 1)
        self.assert_done()
        self.assertIn(self.unanchored_line("core/settings.py"), self.log())
        self.assertNotIn(self.undid_line(), self.log())
        rl = self.review_log()
        self.assertIn(f"**commit**: `{self.head()}`", rl)
        self.assertIn("+- Settings live in `core/settings.py`.", rl)
        self.assertIn("+- Retries go through ⚓`src/job.py#run_job`.", rl)
        self.assertEqual(digest, ["## demo\n\nsummary demo\n\n"])
        self.assertEqual((self.outcomes[0]["changed"], self.outcomes[0]["unanchored"]), (True, ["core/settings.py"]))
        self.assertEqual((self.case / "after.STATE.md").read_text(), GOOD_AND_BAD, "the eval case keeps what the model "
                                                                                 "wrote, before admission")

    def test_hook_committed_only_a_bad_anchor_still_completes(self):
        digest = self.process(ONLY_BAD, hook=True)
        self.assertEqual(self.subjects(), ["librarian: admit STATE.md anchors", HOOK_SUBJECT, "new note", "init"])
        self.assertEqual(self.git(self.d, "show", "HEAD:STATE.md"), BEFORE, "admission gives the file back")
        self.assertEqual(self.git(self.d, "diff", "HEAD~2", "HEAD", "--", "STATE.md"), "")
        self.assertEqual(self.pushed(), 1)
        self.assert_done()
        self.assertIn(self.unanchored_line("pkg/parse.py"), self.log())
        self.assertIn(self.undid_line(), self.log())
        self.assertEqual(self.review_log(), "", "no empty review-log entry")
        self.assertEqual(digest, ["## demo\n\nsummary demo\n\n"])
        self.assertEqual((self.outcomes[0]["changed"], self.outcomes[0]["unanchored"]), (False, ["pkg/parse.py"]))
        self.assertEqual((self.case / "after.STATE.md").read_text(), ONLY_BAD)

    def test_only_a_bad_anchor_without_the_hook_still_completes(self):
        digest = self.process(ONLY_BAD, hook=False)
        self.assertEqual(self.subjects(), ["new note", "init"], "nothing to commit")
        self.assertEqual((self.d / "STATE.md").read_text(), BEFORE)
        self.assertEqual(self.pushed(), 0)
        self.assert_done()
        self.assertIn(self.undid_line(), self.log())
        self.assertEqual(self.review_log(), "")
        self.assertEqual(digest, ["## demo\n\nsummary demo\n\n"])
        self.assertEqual((self.outcomes[0]["changed"], self.outcomes[0]["unanchored"]), (False, ["pkg/parse.py"]))
        self.assertEqual((self.case / "after.STATE.md").read_text(), ONLY_BAD)

    def test_mixed_edit_without_the_hook_is_one_commit(self):
        self.process(GOOD_AND_BAD, hook=False)
        self.assertEqual(self.subjects(), ["librarian: update STATE.md", "new note", "init"])
        self.assertEqual(self.git(self.d, "show", "HEAD:STATE.md"),
                         GOOD_AND_BAD.replace("⚓`core/settings.py`", "`core/settings.py`"))
        self.assertEqual(self.pushed(), 1)
        self.assert_done()
        self.assertNotIn(self.undid_line(), self.log())
        self.assertIn(f"**commit**: `{self.head()}`", self.review_log())


if __name__ == "__main__":
    unittest.main()
