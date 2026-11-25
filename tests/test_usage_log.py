"""Every model run the platform starts leaves one usage row (bin/usage_log.py): opened before it starts, settled after it,
counted when it was killed, and with its own cost when the tool reports a whole conversation's."""
from __future__ import annotations

import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import completion  # noqa: E402
import runtime_claude as rc  # noqa: E402
import usage_log  # noqa: E402
from runtime_ports import CompletionResult  # noqa: E402

SID = "01234567-89ab-cdef-0123-456789abcdef"


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "state" / "llm-usage.jsonl"
        p = mock.patch.object(completion, "USAGE", self.log)
        p.start()
        self.addCleanup(p.stop)

    def rows(self) -> list[dict]:
        return usage_log.rows(self.log)

    def open_files(self) -> list:
        return list(usage_log.open_dir(self.log).glob("*.json"))

    def killed(self, rid: str) -> None:
        f = usage_log.open_dir(self.log) / f"{rid}.json"
        f.write_text(json.dumps({**json.loads(f.read_text()), "pid": dead_pid()}))


class Ledger(Base):
    def test_one_row_for_a_run_and_nothing_left_open(self):
        rid = usage_log.begin(self.log, "p", "claude", "m", "subscription", "proj")
        self.assertEqual((len(self.open_files()), self.rows()), (1, []))
        completion.record_run("p", "claude", "m", "subscription", 0.5, "end", "proj", 10, 2, rid)
        self.assertEqual(self.open_files(), [])
        (r,) = self.rows()
        self.assertEqual((r["cost_usd"], r["input_tokens"], r["output_tokens"], r["project"]), (0.5, 10, 2, "proj"))

    def test_a_run_that_never_started_leaves_no_row(self):
        usage_log.drop(self.log, usage_log.begin(self.log, "p", "claude", "m", "subscription"))
        self.assertEqual((self.open_files(), self.rows()), ([], []))

    def test_another_s_running_run_isn_t_adopted_for_dead(self):
        other = self.tmp / "eval" / "llm-usage.jsonl"
        rid = usage_log.begin(other, "run", "claude", None, "metered")
        usage_log.adopt(self.log, other, "eval")
        self.assertEqual(self.rows(), [])
        usage_log.settle(other, rid, {"ts": usage_log.now(), "point": "run", "cost_usd": 1.5, "billing": "metered"})
        usage_log.adopt(self.log, other, "eval")
        self.assertEqual([r["cost_usd"] for r in self.rows()], [1.5])

    def test_a_folder_someone_else_wrote_in(self):
        """A check's world is written by the tool under check: whatever is there stops nothing."""
        other = self.tmp / "world" / "llm-usage.jsonl"
        usage_log.begin(other, "run", "claude", None, "subscription")
        d = usage_log.open_dir(other)
        (d / "a.json").write_bytes(b"\xff\xfe")
        (d / "b.json").write_text("[1]")
        os.mkfifo(d / "c.json")
        (d / "d.json").symlink_to("/etc/hosts")
        (d / "e.json").mkdir()
        other.write_bytes(b'\xff\n{"ts": "\\ud800", "point": "x"}\n{"cost_usd": NaN, "billing": "metered", "ts": "9"}\n')
        usage_log.adopt(self.log, other, "conformance/x", over=True)
        self.assertEqual(completion.today_cost(), 0.0)
        self.assertTrue(all(r["ts"] <= usage_log.now() for r in self.rows()))
        usage_log.begin(other, "run", "claude", None, "subscription")
        os.mkfifo(other)
        usage_log.adopt(self.log, other, "conformance/x", over=True)

    def test_an_open_folder_that_is_a_link_to_the_live_one(self):
        rid = usage_log.begin(self.log, "live", "claude", None, "metered")
        other = self.tmp / "world" / "llm-usage.jsonl"
        other.parent.mkdir()
        usage_log.open_dir(other).symlink_to(usage_log.open_dir(self.log))
        usage_log.adopt(self.log, other, "conformance/x", over=True)
        usage_log.settle(self.log, rid, {"ts": usage_log.now(), "point": "live", "cost_usd": 1.5, "billing": "metered"})
        self.assertEqual([(r["point"], r["cost_usd"]) for r in self.rows()], [("live", 1.5)])

    def test_a_cut_row_does_not_take_the_next_one(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text('{"ts": "2026-01-01T00:00:00", "poi')
        usage_log.append(self.log, {"ts": usage_log.now(), "point": "p", "cost_usd": 10 ** 400, "billing": "metered"})
        self.assertEqual([r["point"] for r in self.rows()], ["p"])
        self.assertEqual(completion.today_cost(), 0.0)

    def test_a_row_that_could_not_be_written_is_kept_for_later(self):
        rid = usage_log.begin(self.log, "p", "claude", "m", "subscription")
        with mock.patch.object(usage_log, "append", lambda *_a: False):
            usage_log.settle(self.log, rid, {"ts": usage_log.now(), "point": "p", "cost_usd": 0.5, "billing": "subscription"})
        self.assertEqual(self.rows(), [])
        usage_log.sweep(self.log)
        self.assertEqual(([r["cost_usd"] for r in self.rows()], self.open_files()), ([0.5], []))

    def test_killed_while_it_settled(self):
        rid = usage_log.begin(self.log, "p", "claude", "m", "subscription")
        long_ago = datetime.datetime.now().timestamp() - 300   # a run of five minutes
        os.utime(self.open_files()[0], (long_ago, long_ago))
        claim = usage_log._claim(self.log, rid)
        usage_log.sweep(self.log)
        self.assertEqual(self.rows(), [], "a claim of now is somebody's")
        old = datetime.datetime.now().timestamp() - usage_log.CLAIM_S - 10
        os.utime(claim, (old, old))
        usage_log.sweep(self.log)
        usage_log.sweep(self.log)
        self.assertEqual([r["stop"] for r in self.rows()], ["no-answer"])

    def test_odd_values_fail_no_run(self):
        rid = usage_log.begin(self.log, "p", "claude", object(), "subscription")
        completion.record_run("p", "claude", object(), "subscription", 0.5, "end", run=rid)
        completion.record_run("\ud800", "claude", None, "subscription", 0.5, "end")

    def test_a_killed_run_counts_once(self):
        rid = usage_log.begin(self.log, "p", "claude", "m", "metered", "proj", gated=True)
        self.killed(rid)
        usage_log.sweep(self.log)
        usage_log.sweep(self.log)
        (r,) = self.rows()
        self.assertEqual((r["stop"], r["cost_usd"], r["cost_unknown"], r["project"]), ("no-answer", None, True, "proj"))
        self.assertTrue(completion.unknown_cost_today())
        completion.record_run("p", "claude", "m", "metered", 9.0, "end", None, run=rid)   # it was alive after all: not twice
        self.assertEqual(len(self.rows()), 1)

    def test_a_killed_run_counts_what_was_reserved_for_it(self):
        self.killed(usage_log.begin(self.log, "p", "claude", "m", "metered", reserved=0.75))
        self.assertEqual(completion.today_cost(), 0.75)
        self.assertFalse(completion.unknown_cost_today())
        self.assertTrue(self.rows()[0]["cost_assumed"])

    def test_a_running_one_is_left_alone(self):
        usage_log.begin(self.log, "p", "claude", "m", "subscription")
        usage_log.sweep(self.log)
        self.assertEqual((len(self.open_files()), self.rows()), (1, []))

    def test_a_pid_that_is_another_process_by_now(self):
        usage_log.begin(self.log, "p", "claude", "m", "subscription")
        old = datetime.datetime.now().timestamp() - usage_log.DEAD_AFTER_S - 10
        os.utime(self.open_files()[0], (old, old))
        usage_log.sweep(self.log)
        self.assertEqual((self.open_files(), [r["stop"] for r in self.rows()]), ([], ["no-answer"]))

    def test_lines_that_are_no_rows_are_skipped(self):
        self.log.parent.mkdir(parents=True)
        now = usage_log.now()
        self.log.write_text('[1]\nnot json\n"x"\n' + json.dumps({"ts": now, "cost_usd": "a lot", "billing": "metered"}) + "\n"
                            + json.dumps({"ts": now, "cost_usd": 2.0, "billing": "metered"}) + "\n")
        self.assertEqual(completion.today_cost(), 2.0)

    def test_a_folder_that_cannot_be_written_fails_no_run(self):
        self.tmp.joinpath("state").write_text("a file where the folder goes")
        rid = usage_log.begin(self.log, "p", "claude", "m", "subscription")
        self.assertIsNone(rid)
        completion.record_run("p", "claude", "m", "subscription", 0.5, "end", run=rid)

    def test_a_check_s_own_rows_are_kept(self):
        other = self.tmp / "world" / "llm-usage.jsonl"
        usage_log.append(other, {"ts": usage_log.now(), "point": "run", "cost_usd": 0.1, "billing": "subscription", "odd": 1})
        usage_log.begin(other, "run", "claude", None, "subscription")   # the check was killed: this process is alive, the run over
        usage_log.adopt(self.log, other, "conformance/models", over=True)
        self.assertEqual([(r["point"], r["cost_usd"]) for r in self.rows()],
                         [("conformance/models/run", 0.1), ("conformance/models/run", None)])
        self.assertNotIn("odd", self.rows()[0])
        usage_log.adopt(self.log, other, "conformance/models", over=True)
        self.assertEqual(len(self.rows()), 2, "adopted once")
        usage_log.adopt(self.log, self.log, "x")
        self.assertEqual(len(self.rows()), 2, "never its own")

    def test_work(self):
        self.assertEqual([usage_log.work({"point": p}) for p in ("consolidate", "proj/review", "conformance/a/run", "eval/x")],
                         ["consolidate", "project-calls", "conformance", "eval"])

    def test_summary(self):
        s = usage_log.summary([{"point": "a", "cost_usd": 1.0, "billing": "metered", "input_tokens": 5},
                               {"point": "a", "cost_usd": 0.5, "billing": "subscription"}, {"point": "a", "cost_usd": None, "billing": "unknown"},
                               {"point": "b", "cost_usd": None, "billing": "local", "output_tokens": 3}], usage_log.work)
        self.assertEqual((s["a"]["charged"], s["a"]["estimate"], s["a"]["unknown"], s["a"]["runs"]), (1.0, 0.5, 1, 3))
        self.assertEqual((s["b"]["unknown"], s["b"]["output_tokens"]), (0, 3))


class Gate(Base):
    def test_only_a_run_whose_cost_must_be_told_stops_the_day(self):
        completion.record_run("p", "codex", "m", "metered", None, "end", gated=False)
        self.killed(usage_log.begin(self.log, "p", "codex", "m", "metered"))
        self.assertFalse(completion.unknown_cost_today())
        completion.record_run("p", "codex", "m", "metered", None, "end")
        self.assertTrue(completion.unknown_cost_today())

    def test_nor_an_adopted_one(self):
        other = self.tmp / "eval" / "llm-usage.jsonl"
        usage_log.append(other, {"ts": usage_log.now(), "point": "x", "billing": "metered", "cost_usd": None, "cost_unknown": True})
        usage_log.adopt(self.log, other, "eval")
        self.assertEqual((len(self.rows()), completion.unknown_cost_today()), (1, False))


class Completions(Base):
    TARGET = {"provider": "p", "model": "m", "impl": "anthropic", "conf": {"key": {"secret": "x"}}, "location": "cloud", "price": None}

    def test_one_killed_at_its_time_settles_the_request_it_has_open(self):
        completion.begin("route", self.TARGET)
        completion.record("route", self.TARGET, CompletionResult(False, stop="timeout"))
        self.assertEqual(([r["stop"] for r in self.rows()], self.open_files()), (["timeout"], []))

    def test_one_that_never_started(self):
        completion.begin("route", self.TARGET)
        completion.record("route", self.TARGET, CompletionResult(False, stop="error", started=False))
        self.assertEqual((self.rows(), self.open_files()), ([], []))


class OwnCost(unittest.TestCase):
    """Claude Code's totals of a resumed or forked session include what it had cost before."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(rc, "PROJECTS", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def record(self, *lines) -> None:
        d = self.tmp / "-p"
        d.mkdir(exist_ok=True)
        (d / f"{SID}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json totalCostUSD\n")

    def test_what_the_session_had_cost(self):
        self.assertIsNone(rc.session_cost(SID))
        self.record({"type": "user"})
        self.assertEqual(rc.session_cost(SID), 0.0)
        self.record({"totalCostUSD": 1.0}, {"type": "x"}, {"totalCostUSD": 2.5})
        self.assertEqual(rc.session_cost(SID), 2.5)

    def test_only_a_record_s_own_field_counts(self):
        """A message that quotes the field is not the session's total."""
        self.record({"totalCostUSD": 1.0}, {"type": "user", "message": {"content": '"totalCostUSD": 900', "totalCostUSD": 900}})
        self.assertEqual(rc.session_cost(SID), 1.0)

    def test_the_resumed_session(self):
        self.assertEqual([rc._resumed(a) for a in (["-p"], ["--resume", SID, "--fork-session"], [f"--resume={SID}"], ["--continue"])],
                         [None, SID, SID, ""])

    def test_a_fresh_run(self):
        out = {"total_cost_usd": 0.5, "modelUsage": {"a": {"inputTokens": 1, "cacheReadInputTokens": 2, "cacheCreationInputTokens": 4, "outputTokens": 8},
                                                     "b": {"inputTokens": 16, "outputTokens": 32}}, "usage": {"input_tokens": 999}}
        self.assertEqual(rc._used(out, None, None), {"cost_usd": 0.5, "input_tokens": 23, "output_tokens": 40})

    def test_a_resumed_run(self):
        out = {"total_cost_usd": 3.0, "modelUsage": {"a": {"inputTokens": 900, "outputTokens": 900}},
               "usage": {"input_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 4, "output_tokens": 8}}
        self.assertEqual(rc._used(out, SID, 2.5), {"cost_usd": 0.5, "input_tokens": 7, "output_tokens": 8})
        self.assertIsNone(rc._used(out, SID, None)["cost_usd"], "what it had cost couldn't be read")
        self.assertIsNone(rc._used(out, "", None)["cost_usd"])
        self.assertEqual(rc._used({"total_cost_usd": 0.1}, SID, 2.5)["cost_usd"], 0.0)

    def test_no_result(self):
        self.assertEqual(rc._used(None, None, None), {"cost_usd": None})


class Leases(Base):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("lease_runner_usage", ROOT / "bin" / "lease-runner.py")
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def lease(self, spent):
        return types.SimpleNamespace(id="x", proc=types.SimpleNamespace(meter=self.m.Meter(spent)), mode="discuss", project="proj")

    def turns(self, lease, *data):
        with mock.patch.object(self.m, "turn_billing", lambda: "unknown"):
            for d in data:
                self.m.turn_open(lease)
                self.m.turn_used(lease, lease.proc.meter, d)

    def test_a_turn_costs_what_the_total_rose_by(self):
        self.turns(self.lease(1.0), *({"cost": c} for c in (1.25, 1.75, None, 3.0, 3.5)), None)
        self.assertEqual([r["cost_usd"] for r in self.rows()], [0.25, 0.5, None, None, 0.5, None])
        self.assertEqual([r["stop"] for r in self.rows()][-1], "no-answer")
        self.assertEqual({(r["point"], r["project"]) for r in self.rows()}, {("lease-session", "proj")})

    def test_a_session_whose_total_before_could_not_be_read(self):
        self.turns(self.lease(None), {"cost": 5.0}, {"cost": 5.5})
        self.assertEqual([r["cost_usd"] for r in self.rows()], [None, 0.5])

    def test_stopped_in_the_middle_of_a_turn_is_one_row(self):
        """The process stopped (its result still comes), another started and in a turn: each turn is its own process's."""
        lease = self.lease(0.0)
        with mock.patch.object(self.m, "turn_billing", lambda: "unknown"):
            self.m.turn_open(lease)
            old, lease.proc.meter = lease.proc.meter, self.m.Meter(0.5)
            self.m.turn_open(lease)
            self.m.turn_used(lease, old, {"cost": 0.5})   # the old one's reader
            self.m.turn_used(lease, old, None)            # and its end
            self.assertEqual([(r["stop"], r["cost_usd"]) for r in self.rows()], [("end", 0.5)])
            self.m.turn_used(lease, lease.proc.meter, {"cost": 0.75})
        self.assertEqual([(r["stop"], r["cost_usd"]) for r in self.rows()], [("end", 0.5), ("end", 0.25)])
        self.assertEqual(self.open_files(), [])

    def test_a_turn_with_none_open_is_still_a_row(self):
        """Two messages queued in one turn give two results; the session can go on by itself."""
        lease = self.lease(0.0)
        with mock.patch.object(self.m, "turn_billing", lambda: "unknown"):
            self.m.turn_open(lease)
            self.m.turn_open(lease)
            for total in (0.5, 2.0):
                self.m.turn_used(lease, lease.proc.meter, {"cost": total, "num_turns": 1})
        self.assertEqual([r["cost_usd"] for r in self.rows()], [0.5, 1.5])

    def test_a_process_that_failed_at_start_is_no_row(self):
        lease = self.lease(0.0)
        self.m.turn_used(lease, lease.proc.meter, {"cost": 0, "num_turns": 0, "is_error": True})
        self.assertEqual(self.rows(), [])


class Command(Base):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("agenthub_usage", str(ROOT / "bin" / "agenthub"))
        spec = importlib.util.spec_from_loader("agenthub_usage", loader)
        cls.hub = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.hub)

    def run_it(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = self.hub.cmd_llm(["usage", *argv])
        return code, out.getvalue()

    def test_nothing_yet(self):
        self.assertEqual(self.run_it()[0], 0)

    def test_the_table(self):
        completion.record_run("consolidate", "claude", "m", "subscription", 2.5, "end", None, 1000, 10)
        completion.record_run("consolidate", "claude", "m", "metered", 1.25, "end", None, 1000, 10)
        completion.record_run("lease-session", "claude", None, "unknown", None, "end")
        code, out = self.run_it("--days", "7")
        self.assertEqual(code, 0)
        line = next(x for x in out.splitlines() if x.startswith("consolidate")).split()
        self.assertEqual(line, ["consolidate", "2", "2,000", "20", "$1.25", "$2.50"])
        self.assertIn("lease-session", out)

    def test_bad_days(self):
        for argv in (("--days", "0"), ("--days", "x"), ("--days",), ("7",), ("--days", "999999999"), ("--days", "²")):
            self.assertEqual(self.run_it(*argv)[0], 2)


if __name__ == "__main__":
    unittest.main()
