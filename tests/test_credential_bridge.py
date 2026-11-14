"""The platform's bridge to a remote MCP server (bin/credential_bridge.py; docs/design/platform-credentials.md C5), and
its attachment to a run in llm.py: against a local MCP server, in scratch folders.

    bin/run-tests <a folder holding this file>
"""
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ["AGENT_HUB_LANG_UI"] = os.environ["BRUNNA_LANG_UI"] = "en"   # the messages these tests read are the English ones

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import credential_bridge  # noqa: E402
import credentials  # noqa: E402
import hubconf  # noqa: E402
import mcp_fixture  # noqa: E402
import runtime_ports  # noqa: E402

TOKEN = "canary-token-5e1d9a"
ALLOW = ["mcp__upwork__upwork__get_messages", "mcp__upwork__echo_auth", "ToolSearch"]


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.srv = mcp_fixture.Server(self, token=TOKEN)
        self.sec = {"name": "upwork", "kind": "oauth", "server": {"id": "upwork", "url": self.srv.url}}
        self.doc = {"name": "demo", "secrets": [self.sec],
                    "jobs": [{"name": "daily", "kind": "agent", "allow": ALLOW, "credentials": ["upwork"]}]}
        self.cred = {"kind": "oauth", "access_token": TOKEN, "refresh_token": "refresh-canary-77"}

    def bridge(self, tools=("upwork__get_messages", "echo_auth")):
        run = credential_bridge.Run.__new__(credential_bridge.Run)
        run.refusal, run.project, run.job, run.name, run.sec, run.tools = None, "demo", "daily", "upwork", self.sec, tools
        run.dir, run.id = self.tmp, "upwork"
        out = io.StringIO()
        with mock.patch.object(credentials, "load", lambda p, n: dict(self.cred)):
            b = credential_bridge.Bridge(run, out=out)
        return b, out

    def replies(self, out) -> list:
        return [json.loads(x) for x in out.getvalue().splitlines() if x]

    def status(self) -> dict:
        return json.loads((self.tmp / "credential-upwork.status.json").read_text())


class Bridge(Scratch):
    def session(self, b):
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {"roots": {}, "sampling": {}, "elicitation": {}},
            "clientInfo": {"name": "c"}}}))
        b.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_a_session_through_it(self):
        b, out = self.bridge()
        self.session(b)
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "upwork__get_messages"}}))
        r = self.replies(out)
        self.assertEqual([x["id"] for x in r], [1, 2, 3])
        self.assertEqual([t["name"] for t in r[1]["result"]["tools"]], ["upwork__get_messages", "echo_auth"],
                         "only the job's tools are listed")
        self.assertEqual(r[2]["result"]["content"][0]["text"], "called upwork__get_messages")
        init = self.srv.posts()[0]["body"]
        self.assertEqual(init["params"]["capabilities"], {}, "no roots, sampling or elicitation offered upstream")
        for e in self.srv.posts():
            self.assertEqual(e["headers"]["Authorization"], f"Bearer {TOKEN}")
        st = self.status()
        self.assertTrue(st["initialized"])
        self.assertEqual(st["tools_listed"], 2)

    def test_what_it_refuses(self):
        b, out = self.bridge()
        self.session(b)
        before = len(self.srv.posts())
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "upwork__send_message"}}))
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 6, "method": "resources/list"}))
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "server/discover"}))
        b.handle(json.dumps([{"jsonrpc": "2.0", "id": 8, "method": "ping"}]))
        b.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"}))
        r = self.replies(out)[1:]
        self.assertEqual([x.get("error", {}).get("code") for x in r], [-32602, -32601, -32601, -32600])
        self.assertEqual(len(self.srv.posts()), before, "none of them reached the server")

    def test_a_token_echoed_back_is_redacted_everywhere(self):
        b, out = self.bridge()
        self.session(b)
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "echo_auth"}}))
        self.assertIn("[redacted]", out.getvalue())
        for text in (out.getvalue(), (self.tmp / "credential-upwork.status.json").read_text(),
                     (self.tmp / "credential-upwork.log").read_text()):
            self.assertNotIn(TOKEN, text)

    def test_sse_and_server_requests(self):
        self.srv.sse = self.srv.server_request = True
        b, out = self.bridge()
        self.session(b)
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        r = self.replies(out)
        self.assertEqual([x["id"] for x in r], [1, 2], "only the answers, never the server's sampling request")
        declined = [e for e in self.srv.posts() if isinstance(e["body"], dict) and e["body"].get("id") == "s1"]
        self.assertTrue(declined and all("error" in e["body"] for e in declined), "answered upstream with an error")
        self.assertEqual(self.srv.posts()[-1]["headers"].get("Mcp-Session-Id"), "s-1")

    def test_a_redirect_is_never_followed(self):
        other = mcp_fixture.Server(self, token=TOKEN)
        self.srv.redirect_to = other.url
        b, out = self.bridge()
        self.session(b)
        self.assertIn("redirect", self.replies(out)[0]["error"]["message"])
        self.assertEqual(other.log, [])
        self.assertFalse(self.status()["initialized"])

    def test_a_refused_credential_fails_the_run(self):
        self.cred["access_token"] = "a-wrong-one"
        b, out = self.bridge()
        with mock.patch.object(credential_bridge.Bridge, "refresh", lambda self: False):
            self.session(b)
        self.assertEqual(self.replies(out)[0]["error"]["code"], -32001)
        st = self.status()
        self.assertTrue(st["unrecovered"])
        import llm
        (self.tmp / "credential-upwork.json").write_text("{}")   # the run file llm.py wrote
        self.assertIn("refused", llm.bridge_failure(self.tmp))

    def test_a_refresh_recovers(self):
        self.cred["access_token"] = "a-wrong-one"
        b, out = self.bridge()

        def refresh(bridge):
            bridge.cred = {**bridge.cred, "access_token": TOKEN}
            bridge.up.header = credential_bridge.header_for(bridge.run.sec, bridge.cred)
            return True
        with mock.patch.object(credential_bridge.Bridge, "refresh", refresh):
            self.session(b)
        self.assertIn("result", self.replies(out)[0])
        st = self.status()
        self.assertEqual((st["auth_failures"], st["unrecovered"]), (1, False))

    def test_a_bridge_that_never_started_fails_the_run(self):
        import llm
        (self.tmp / "credential-upwork.json").write_text("{}")
        self.assertIn("never started", llm.bridge_failure(self.tmp))
        (self.tmp / "credential-upwork.status.json").write_text(json.dumps({"initialized": False, "last_error": "x"}))
        self.assertIn("never connected", llm.bridge_failure(self.tmp))
        (self.tmp / "credential-upwork.status.json").write_text(json.dumps({"initialized": True}))
        self.assertIsNone(llm.bridge_failure(self.tmp))

    def test_static_headers(self):
        self.assertEqual(credential_bridge.header_for({"server": {}}, {"kind": "static", "value": "v"}),
                         {"Authorization": "Bearer v"})
        self.assertEqual(credential_bridge.header_for({"server": {"header": "X-Api-Key"}}, {"kind": "static", "value": "v"}),
                         {"X-Api-Key": "v"})


class RunFile(Scratch):
    """The run file: read once, the platform's own, then checked again against the registry."""

    def write(self, body, mode=0o600) -> pathlib.Path:
        d = hubconf.STATE / "agent-jobs" / ".settings" / f"demo.t.{os.getpid()}"
        d.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, d, True)
        p = d / "credential-upwork.json"
        p.write_text(json.dumps(body))
        p.chmod(mode)
        return p

    def run(self, path):
        import runtime_claude
        with mock.patch.object(credentials, "index", lambda: {"projects": {"demo": self.doc}}), \
                mock.patch.object(runtime_ports, "load", lambda *a, **k: runtime_claude):
            return credential_bridge.Run(str(path))

    def body(self, **kw):
        return {"project": "demo", "job": "daily", "credential": "upwork",
                "tools": ["upwork__get_messages", "echo_auth"], **kw}

    def test_a_good_one(self):
        r = self.run(self.write(self.body()))
        self.assertIsNone(r.refusal)
        self.assertEqual(set(r.tools), {"upwork__get_messages", "echo_auth"})

    def test_bad_ones(self):
        cases = [(self.write(self.body()), 0o644), (self.write(self.body(tools=["upwork__send_message"])), 0o600),
                 (self.write(self.body(job="other")), 0o600)]
        for path, mode in cases:
            path.chmod(mode)
            with self.subTest(path.read_text()):
                self.assertIsNotNone(self.run(path).refusal)
        outside = self.tmp / "credential-upwork.json"
        outside.write_text(json.dumps(self.body()))
        outside.chmod(0o600)
        self.assertIsNotNone(self.run(outside).refusal)
        link = cases[0][0].with_name("link.json")
        link.symlink_to(cases[0][0])
        cases[0][0].chmod(0o600)
        self.assertIsNotNone(self.run(link).refusal)


class Attach(Scratch):
    """llm.py adds one bridge per credential, after the mail server, only to a sealed run whose rules are the registry's."""

    def attach(self, extra=(), allow=ALLOW, fresh=None):
        import llm
        import runtime_claude
        spec = {"settings": "platform", "project_env": "AGENT_JOB_PROJECT", "allow_from_env": "AGENT_JOB_ALLOW",
                "credentials_from_env": "AGENT_JOB_CREDENTIALS"}
        env = {"AGENT_JOB_PROJECT": "demo", "AGENT_JOB_ALLOW": json.dumps(allow), "AGENT_JOB_CREDENTIALS": "daily"}
        mail = runtime_ports.ToolServer("brunna-run", "py", (), {}, ("send",))
        task = runtime_ports.Task("agent-job-confined", "p", spec, env=env, extra=tuple(extra), servers=(mail,),
                                  run_dir=str(self.tmp))
        with mock.patch("registry.refresh", lambda: {"projects": {"demo": self.doc}}), \
                mock.patch.object(credentials, "fresh", fresh or (lambda *a, **k: TOKEN)):
            return llm.attach_credentials("agent-job-confined", spec, runtime_claude, task, self.tmp)

    def test_appended_with_its_file(self):
        task, why = self.attach(extra=("--strict-mcp-config",))
        self.assertIsNone(why)
        self.assertEqual([s.id for s in task.servers], ["brunna-run", "upwork"])
        bridge = task.servers[1]
        self.assertEqual(set(bridge.tools), {"upwork__get_messages", "echo_auth"})
        self.assertEqual(bridge.args[0], "-I")
        body = json.loads((self.tmp / "credential-upwork.json").read_text())
        self.assertEqual((body["project"], body["job"], body["credential"]), ("demo", "daily", "upwork"))
        everything = json.dumps([list(s) for s in task.servers]) + json.dumps(body)
        self.assertNotIn(TOKEN, everything, "the token is never in the run's servers or files")

    def test_refusals(self):
        self.assertIn("load", self.attach()[1], "an unsealed run")
        self.assertIn("rules", self.attach(extra=("--strict-mcp-config",), allow=[*ALLOW, "Read"])[1])

        def needs(*a, **k):
            raise credentials.NeedsGrant("demo/upwork needs the owner: run brunna credentials grant demo upwork")
        self.assertIn("credentials grant", self.attach(extra=("--strict-mcp-config",), fresh=needs)[1])


if __name__ == "__main__":
    unittest.main()
