"""The platform's bridge to a remote MCP server (bin/credential_bridge.py; docs/design/platform-credentials.md C5), and
its attachment to a run in llm.py: against a local MCP server, in scratch folders.

    bin/run-tests <a folder holding this file>
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
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

    def test_a_403_is_not_a_refused_credential(self):
        b, out = self.bridge()
        self.session(b)
        refreshed = []
        with mock.patch.object(credential_bridge.Bridge, "refresh", lambda self: refreshed.append(1) or False), \
                mock.patch.object(credential_bridge.Upstream, "send",
                                  side_effect=credential_bridge.Forbidden("403 insufficient_scope")):
            b.handle(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}))
        self.assertEqual(self.replies(out)[-1]["error"]["code"], -32003)
        self.assertEqual(refreshed, [], "no refresh for a 403")
        st = self.status()
        self.assertFalse(st["unrecovered"])
        import llm
        (self.tmp / "credential-upwork.json").write_text("{}")
        self.assertIn("refused a call the job needs", llm.bridge_failure(self.tmp))

    def test_an_answer_that_breaks_off_is_refused(self):
        import http.client
        b, out = self.bridge()
        self.session(b)
        with mock.patch.object(credential_bridge.Upstream, "_deliver", side_effect=http.client.IncompleteRead(b"{")):
            b.handle(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}))
        self.assertEqual(self.replies(out)[-1]["error"]["code"], -32002)
        self.assertIn("broke off", self.status()["last_error"])

    def test_a_session_id_must_be_visible_ascii(self):
        self.assertTrue(credential_bridge.SESSION_ID.fullmatch("s-1"))
        for bad in ("a\r\nX-Evil: 1", "a b", "", "é"):
            self.assertIsNone(credential_bridge.SESSION_ID.fullmatch(bad), bad)

    def test_anything_else_becomes_an_error_and_a_crash_fails_the_run(self):
        b, out = self.bridge()
        with mock.patch.object(credential_bridge.Bridge, "forward", side_effect=KeyError("x")):
            b.handle(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}))
        self.assertEqual(self.replies(out)[-1]["error"]["code"], -32603)
        self.assertIn("KeyError", self.status()["last_error"])
        import llm
        (self.tmp / "credential-upwork.json").write_text("{}")
        st = self.status()
        (self.tmp / "credential-upwork.status.json").write_text(json.dumps({**st, "initialized": True, "ended": "crashed"}))
        self.assertIn("crashed", llm.bridge_failure(self.tmp))
        (self.tmp / "credential-upwork.status.json").write_text(json.dumps({**st, "initialized": True, "ended": "terminated"}))
        self.assertIsNone(llm.bridge_failure(self.tmp), "the agent closing its servers is how a run ends")

    def test_redaction_forms_and_the_token_refreshed_away(self):
        import base64
        import urllib.parse
        b, out = self.bridge()
        b.hide({"access_token": "new-token-123456"})
        for form in (TOKEN, urllib.parse.quote("tok/en+" + TOKEN, safe=""), base64.b64encode(TOKEN.encode()).decode(),
                     base64.b64encode(f"Bearer {TOKEN}".encode()).decode(), "new-token-123456"):
            b.write({"jsonrpc": "2.0", "id": 1, "result": {"text": form}})
        text = out.getvalue()
        for secret in (TOKEN, base64.b64encode(TOKEN.encode()).decode().rstrip("="), "new-token-123456"):
            self.assertNotIn(secret, text)

    def test_a_later_success_doesnt_clear_a_refused_credential(self):
        self.cred["access_token"] = "a-wrong-one"
        b, out = self.bridge()
        with mock.patch.object(credential_bridge.Bridge, "refresh", lambda self: False):
            b.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
        b.cred = {**b.cred, "access_token": TOKEN}
        b.up.header = credential_bridge.header_for(b.run.sec, b.cred)
        b.handle(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}))
        self.assertTrue(self.status()["unrecovered"])

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
            b.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
        [reply] = self.replies(out)
        self.assertEqual(reply["error"]["code"], -32001)
        self.assertEqual(len(self.srv.posts()), 1, "the refused token is sent once, never again")
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


class DetachedRefresh(Scratch):
    """Bridge.refresh starts credentials.py refresh with an environment of its own (no agent login, no proxy); that
    command, run as it is started, refreshes against the server, or marks a refused static value as needing the
    owner."""

    def started(self, cred, run_child):
        self.cred = cred
        b, _out = self.bridge()
        seen = {}

        class P:
            def __init__(self, argv, env=None, **kw):
                seen.update(argv=argv, env=env, kw=kw)
                run_child(argv)

            def wait(self, timeout=None):
                return 0
        agent_env = {"CLAUDE_CODE_OAUTH_TOKEN": "agent-login", "HTTPS_PROXY": "http://127.0.0.1:9", "HOME": "/h"}
        with mock.patch.dict(os.environ, agent_env), mock.patch("subprocess.Popen", P):
            got = b.refresh()
        return got, seen, b

    def test_its_argv_and_env(self):
        import hashlib
        got, seen, _b = self.started(dict(self.cred), lambda argv: None)
        self.assertFalse(got, "nothing new to retry with")
        argv = seen["argv"]
        self.assertEqual(argv[1:3], ["-I", str(ROOT / "bin" / "credentials.py")])
        self.assertEqual(argv[3:7], ["refresh", "demo", "upwork", "--failed"])
        self.assertEqual(argv[7], hashlib.sha256(TOKEN.encode()).hexdigest())
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen["env"])
        self.assertNotIn("HTTPS_PROXY", seen["env"])
        self.assertTrue(seen["kw"].get("start_new_session"))

    def test_the_command_refreshes_and_the_bridge_retries_with_it(self):
        self.srv.oauth()
        tmp = self.tmp
        sec = self.sec
        doc = self.doc
        bases = (tmp / "creds", tmp / "creds-kept")
        old = {"v": 1, "kind": "oauth", "access_token": "old-token", "refresh_token": "r1", "issued_at": 1,
               "access_expires_at": None, "token_endpoint": self.srv.base + "/token", "resource": self.srv.url,
               "client": {"route": "c", "client_id": "cid", "auth_method": "none"}}
        self.srv.token_answers = [(200, {"access_token": TOKEN, "expires_in": 3600})]
        with mock.patch.object(credentials, "bases", lambda: bases), \
                mock.patch.object(credentials, "status_path", lambda p, n: tmp / "st" / f"{n}.json"), \
                mock.patch.object(credentials, "entry", lambda p, n, idx=None: (doc, sec)), \
                mock.patch.object(credentials, "owner_notify", lambda *a: None):
            credentials._write_private(bases[0] / "demo" / "upwork.json", old)

            def child(argv):
                self.assertEqual(credentials.main(argv[3:]), 0)
            got, _seen, b = self.started(old, child)
        self.assertTrue(got)
        self.assertEqual(b.up.header, {"Authorization": f"Bearer {TOKEN}"})
        [post] = self.srv.posts("/token")
        self.assertIn("refresh_token=r1", post["body"])

    def test_a_static_value_refused_once_is_retried_not_marked(self):
        static = {"v": 1, "kind": "static", "value": TOKEN}   # the server takes it: the ping goes through
        started = []
        self.cred = static
        b, _out = self.bridge()
        with mock.patch("subprocess.Popen", lambda *a, **k: started.append(a)):
            self.assertTrue(b.refresh(), "one retry with the same value")
        self.assertEqual(started, [], "nothing marked")

    def test_a_refused_static_value_needs_the_owner(self):
        tmp, sec, doc, sent = self.tmp, {**self.sec, "kind": "static"}, self.doc, []
        bases = (tmp / "creds", tmp / "creds-kept")
        static = {"v": 1, "kind": "static", "value": "static-value-1"}
        with mock.patch.object(credentials, "bases", lambda: bases), \
                mock.patch.object(credentials, "status_path", lambda p, n: tmp / "st" / f"{n}.json"), \
                mock.patch.object(credentials, "entry", lambda p, n, idx=None: (doc, sec)), \
                mock.patch.object(credentials, "index", lambda: {"projects": {"demo": doc}}), \
                mock.patch.object(credentials, "owner_notify", lambda *a: sent.append(a)), \
                mock.patch("alert.once", lambda *a, **k: None):
            credentials._write_private(bases[0] / "demo" / "upwork.json", static)
            got, _seen, _b = self.started(static, lambda argv: credentials.main(argv[3:]))
            self.assertFalse(got)
            self.assertEqual(credentials.read_status("demo", "upwork")["state"], "needs_grant")
        self.assertEqual([a[1] for a in sent], ["task"], "the owner is told: the bridge's word holds for static too")


class Adapter(Scratch):
    """What llm.py attaches reaches the runtime's command: the bridge in --mcp-config, its tools allowed, no token."""

    def test_the_bridge_in_the_runs_config(self):
        import runtime_claude
        task, why = Attach.attach(self, extra=("--strict-mcp-config",))
        self.assertIsNone(why)
        task = task._replace(spec={"permission_mode": "dontAsk"})
        with mock.patch.object(runtime_claude, "_exe", lambda: "claude"):
            cmd = runtime_claude._flags(task)
        cfg = json.loads(pathlib.Path(cmd[cmd.index("--mcp-config") + 1]).read_text())["mcpServers"]
        self.assertEqual(cfg["upwork"]["args"][:2], ["-I", str(hubconf.ROOT / "bin" / "credential_bridge.py")])
        self.assertIn("mcp__upwork__upwork__get_messages", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn(TOKEN, json.dumps(cfg) + " ".join(cmd))


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

    def load_run(self, path):
        import runtime_claude
        with mock.patch.object(credential_bridge.hubconf, "projects", lambda: {"demo": self.doc}), \
                mock.patch.object(runtime_ports, "load", lambda *a, **k: runtime_claude):
            return credential_bridge.Run(str(path))

    def body(self, **kw):
        return {"project": "demo", "job": "daily", "credential": "upwork",
                "tools": ["upwork__get_messages", "echo_auth"], **kw}

    def test_a_good_one(self):
        r = self.load_run(self.write(self.body()))
        self.assertIsNone(r.refusal)
        self.assertEqual(set(r.tools), {"upwork__get_messages", "echo_auth"})

    def test_bad_ones(self):
        cases = [(self.write(self.body()), 0o644), (self.write(self.body(tools=["upwork__send_message"])), 0o600),
                 (self.write(self.body(job="other")), 0o600)]
        for path, mode in cases:
            path.chmod(mode)
            with self.subTest(path.read_text()):
                self.assertIsNotNone(self.load_run(path).refusal)
        outside = self.tmp / "credential-upwork.json"
        outside.write_text(json.dumps(self.body()))
        outside.chmod(0o600)
        self.assertIsNotNone(self.load_run(outside).refusal)
        link = cases[0][0].with_name("link.json")
        link.symlink_to(cases[0][0])
        cases[0][0].chmod(0o600)
        self.assertIsNotNone(self.load_run(link).refusal)

    def test_a_refused_run_says_so_where_llm_looks(self):
        import llm
        path = self.write(self.body(job="other"))
        credential_bridge.Bridge(self.load_run(path), out=io.StringIO())
        self.assertTrue((path.parent / "credential-upwork.status.json").exists())
        self.assertIn("never connected", llm.bridge_failure(path.parent))


class Attach(Scratch):
    """llm.py adds one bridge per credential, after the mail server, only to a sealed run whose rules are the registry's."""

    def attach(self, extra=(), allow=ALLOW, fresh=None, prompt="daily.md", cwd=None):
        import llm
        import registry
        import runtime_claude
        (self.tmp / "daily.md").write_text("p")
        doc = {**self.doc, "paths": {"workspace": str(self.tmp)},
               "jobs": [{**self.doc["jobs"][0], "cwd": "workspace", "prompt": "jobs/daily.md"}]}
        spec = {"settings": "platform", "project_env": "AGENT_JOB_PROJECT", "allow_from_env": "AGENT_JOB_ALLOW",
                "credentials_from_env": "AGENT_JOB_CREDENTIALS"}
        env = {"AGENT_JOB_PROJECT": "demo", "AGENT_JOB_ALLOW": json.dumps(allow), "AGENT_JOB_CREDENTIALS": "daily"}
        mail = runtime_ports.ToolServer("brunna-run", "py", (), {}, ("send",))
        task = runtime_ports.Task("agent-job-confined", "p", spec, env=env, extra=tuple(extra), servers=(mail,),
                                  run_dir=str(self.tmp), cwd=str(cwd or self.tmp))
        with mock.patch("registry.refresh", lambda: {"projects": {"demo": doc}}), \
                mock.patch.object(registry, "job_prompt", lambda stem, job: self.tmp / "daily.md"), \
                mock.patch.object(credentials, "fresh", fresh or (lambda *a, **k: TOKEN)):
            return llm.attach_credentials("agent-job-confined", spec, runtime_claude, task, self.tmp,
                                          prompt_file=str(self.tmp / prompt))

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
        other = self.tmp / "elsewhere"
        other.mkdir()
        self.assertIn("folder", self.attach(extra=("--strict-mcp-config",), cwd=other)[1], "the frozen job's folder")
        (self.tmp / "other.md").write_text("p")
        self.assertIn("prompt", self.attach(extra=("--strict-mcp-config",), prompt="other.md")[1], "and its prompt")


class LlmRun(Scratch):
    """A credentials job through llm.run itself, on a stand-in runtime: the ports it loads, the bridge attached after
    the rule check and before the run, and the bridge's status read after it (a dry run, then stubbed runs)."""

    def run_llm(self, status=None, dry=False):
        import argparse
        import types
        import llm
        import registry
        ws = self.tmp / "ws"
        ws.mkdir(exist_ok=True)
        (self.tmp / "daily.md").write_text("probe")
        doc = {**self.doc, "paths": {"workspace": str(ws)},
               "jobs": [{**self.doc["jobs"][0], "cwd": "workspace", "prompt": "jobs/daily.md"}]}
        seen = {"loaded": [], "order": []}

        def run(task):
            seen["order"].append("run")
            seen["task"] = task
            if status is not None:
                (pathlib.Path(task.run_dir) / "credential-upwork.status.json").write_text(json.dumps(status))
            return runtime_ports.TaskResult(ok=True, text="done")

        def rule_problems(task, **k):
            seen["order"].append("rules")
            return []
        import runtime_claude
        rt = types.SimpleNamespace(
            run=run, describe=lambda task: seen.update(task=task) or ["described"], rule_problems=rule_problems,
            run_server=lambda task: task, sealed=runtime_claude.sealed, tools_named=runtime_claude.tools_named,
            rules_tools_named=runtime_claude.rules_tools_named,
            credential_rules_problems=runtime_claude.credential_rules_problems,
            ambient_servers=lambda task: None, servers_named=lambda task: [])

        def load(name=None, ports=(), **k):
            seen["loaded"].append(tuple(ports))
            return rt
        env = {"AGENT_JOB_PROJECT": "demo", "AGENT_JOB_ALLOW": json.dumps(ALLOW), "AGENT_JOB_CREDENTIALS": "daily"}
        a = argparse.Namespace(cmd="run", point="agent-job-confined", cwd=str(ws), model=None,
                               prompt_file=str(self.tmp / "daily.md"), dry_run=dry, extra=["--strict-mcp-config"])
        failed = []
        with mock.patch.dict(os.environ, env), mock.patch.object(hubconf, "STATE", self.tmp / "state"), \
                mock.patch.object(runtime_ports, "load", load), \
                mock.patch.object(runtime_ports, "capabilities", lambda name=None: {"agent", "confine", "rule_check"}), \
                mock.patch("registry.refresh", lambda: {"projects": {"demo": doc}}), \
                mock.patch.object(registry, "job_prompt", lambda stem, job: self.tmp / "daily.md"), \
                mock.patch.object(credentials, "fresh", lambda *a, **k: TOKEN), \
                mock.patch.object(llm, "job_failed", lambda point, reason: failed.append(reason)), \
                mock.patch("sys.stdout", io.StringIO()):
            code = llm.run(a)
        return code, seen, failed

    def test_the_dry_run_describes_a_run_with_the_bridge(self):
        code, seen, failed = self.run_llm(dry=True)
        self.assertEqual((code, failed), (0, []))
        self.assertTrue(any("credential_tools" in p and "run_server" in p for p in seen["loaded"]), seen["loaded"])
        self.assertEqual([s.id for s in seen["task"].servers], ["upwork"])

    def test_a_run_whose_bridge_connected_passes_and_one_whose_didnt_fails(self):
        code, seen, failed = self.run_llm(status={"initialized": True, "ended": "terminated"})
        self.assertEqual((code, failed), (0, []))
        self.assertEqual(seen["order"], ["rules", "run"], "the rules checked before, the run after the attachment")
        self.assertEqual([s.id for s in seen["task"].servers], ["upwork"])
        code, _seen, failed = self.run_llm(status={"initialized": False, "last_error": "x"})
        self.assertEqual(code, 1)
        self.assertIn("never connected", failed[0], "the model's success doesn't make the run green")
        code, _seen, failed = self.run_llm(status=None)
        self.assertIn("never started", failed[0])


# llm.py in a temp hub: the real one, run as run-agent-job.sh starts it (its argv, its folder, its environment), with
# only what reaches outside stood in for: the registry entry (from $FIXTURE), the credential's freshness, the runtime
# (it records the task instead of starting an agent, and writes the bridge's status as a connected bridge would) and
# the bus
REAL_LLM = r"""import importlib.util, json, os, pathlib, sys, types
from unittest import mock
real_bin = pathlib.Path(os.environ["REAL_BIN"])
sys.path.insert(0, str(real_bin))
import credentials, registry, runtime_claude, runtime_ports
fx = json.loads(pathlib.Path(os.environ["FIXTURE"]).read_text())
spec = importlib.util.spec_from_file_location("llm", real_bin / "llm.py")
llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llm)
seen = {}

def run(task):
    seen.update(cwd=task.cwd, pwd=os.getcwd(), servers=[s.id for s in task.servers])
    (pathlib.Path(task.run_dir) / "credential-upwork.status.json").write_text(json.dumps({"initialized": True}))
    return runtime_ports.TaskResult(ok=True, text="done")
rt = types.SimpleNamespace(run=run, describe=lambda t: [], rule_problems=lambda t, **k: [], run_server=lambda t: t,
                           sealed=runtime_claude.sealed, tools_named=runtime_claude.tools_named,
                           rules_tools_named=runtime_claude.rules_tools_named,
                           credential_rules_problems=runtime_claude.credential_rules_problems,
                           ambient_servers=lambda t: None, servers_named=lambda t: [])
failed = []
with mock.patch.object(runtime_ports, "load", lambda *a, **k: rt), \
        mock.patch.object(runtime_ports, "capabilities", lambda name=None: {"agent", "confine", "rule_check"}), \
        mock.patch.object(registry, "refresh", lambda: {"projects": {"demo": fx["doc"]}}), \
        mock.patch.object(registry, "job_prompt", lambda stem, job: pathlib.Path(fx["prompt"])), \
        mock.patch.object(credentials, "fresh", lambda *a, **k: "t"), \
        mock.patch.object(llm, "job_failed", lambda point, reason: failed.append(reason)):
    sys.argv = ["llm.py", *sys.argv[1:]]
    code = llm.main()
pathlib.Path(os.environ["RECORD_TO"]).write_text(json.dumps({"code": code, "failed": failed, **seen}))
sys.exit(code)
"""


class RunAgentJob(Scratch):
    """The job as the scheduler starts it: run-agent-job.sh under its legacy state name, its folder the registered
    workspace reached through a link, then llm.py's own run (registry.scheduler_jobs's command, the real script and the
    real llm.py; the first run on the owner's machine was refused here, 11-15: llm.py got no --cwd)."""

    def setUp(self):
        super().setUp()
        import registry
        self.hub = self.tmp / "hub"
        (self.hub / "bin").mkdir(parents=True)
        bin_ = ROOT / "bin"
        for f in ("py", "hubpaths.sh", "i18n.py", "hubconf.py", "run-agent-job.sh", "names.py"):
            (self.hub / "bin" / f).symlink_to(bin_ / f)
        (self.hub / "bin" / "llm.py").write_text(REAL_LLM)
        (self.hub / "bin" / "gen-status-page.py").write_text("pass\n")
        (self.hub / "contracts").symlink_to(ROOT / "contracts")
        for d in ("home", "config", "data", "secrets", "state", "ws"):
            (self.tmp / d).mkdir(exist_ok=True)
        (self.tmp / "config" / "hub.json").write_text(json.dumps({"owner": {"name": "alex", "tag": "Alex"}}))
        (self.tmp / "link").symlink_to(self.tmp / "ws")   # the registered workspace, spelled another way
        self.prompt = self.tmp / "daily.md"
        self.prompt.write_text("do it\n")
        job = {**self.doc["jobs"][0], "at": "09:00", "cwd": "workspace", "legacy_name": "demo-daily-check",
               "prompt": "jobs/daily.md"}
        self.doc = {**self.doc, "paths": {"workspace": str(self.tmp / "link")}, "jobs": [job]}
        (self.tmp / "fixture.json").write_text(json.dumps({"doc": self.doc, "prompt": str(self.prompt)}))
        with mock.patch.object(registry, "job_prompt", lambda stem, j: self.prompt):
            [sched] = [j for j in registry.scheduler_jobs({"projects": {"demo": self.doc}, "modules": {}}, set())[0]
                       if j["name"] == "demo/daily"]
        self.command = sched["command"]
        self.record = self.tmp / "record.json"

    def run_job(self, command: str) -> dict:
        py = os.environ.get("AGENT_HUB_PYTHON") or os.environ.get("BRUNNA_PYTHON") or sys.executable
        env = {**{k: v for k, v in os.environ.items()
                  if not k.startswith(("BRUNNA_", "AGENT_HUB_", "LLM_AUTH", "AGENT_JOB_", "CLAUDE_CODE_OAUTH"))},
               "AGENT_HUB_PYTHON": py, "AGENT_HUB_ROOT": str(self.hub), "HOME": str(self.tmp / "home"),
               "AGENT_HUB_CONFIG": str(self.tmp / "config"), "AGENT_HUB_DATA": str(self.tmp / "data"),
               "AGENT_HUB_SECRETS": str(self.tmp / "secrets"), "AGENT_HUB_STATE": str(self.tmp / "state"),
               "AGENT_HUB_LANG_UI": "en", "RUN_AGENT_JOB_FOREGROUND": "1", "RECORD_TO": str(self.record),
               "REAL_BIN": str(ROOT / "bin"), "FIXTURE": str(self.tmp / "fixture.json")}
        # the scheduler's command, with this test's hub in place of the platform's
        command = command.replace(str(ROOT / "bin" / "run-agent-job.sh"), str(self.hub / "bin" / "run-agent-job.sh"))
        r = subprocess.run(["/bin/bash", "-c", command], env=env, capture_output=True, text=True, timeout=120)
        # run-agent-job.sh keeps a job's state in the hub's .state, under the job's state name
        status = json.loads((self.hub / ".state" / "agent-jobs" / "demo-daily-check" / "status.json").read_text())
        return {**json.loads(self.record.read_text()), "status": status, "rc": r.returncode}

    def test_the_registered_folder_runs_with_its_bridge(self):
        self.assertIn("AGENT_JOB_CREDENTIALS=daily", self.command)
        self.assertIn("demo-daily-check", self.command, "the state folder is the legacy name's")
        rec = self.run_job(self.command)
        self.assertEqual((rec["code"], rec["failed"]), (0, []), rec)
        self.assertEqual(rec["servers"], ["upwork"])
        self.assertIsNone(rec["cwd"], "run-agent-job.sh passes no --cwd: llm.py runs in the job's folder")
        self.assertEqual(pathlib.Path(rec["pwd"]).resolve(), (self.tmp / "ws").resolve())
        self.assertEqual(rec["status"]["state"], "ok")

    def test_another_folder_is_refused(self):
        other = self.tmp / "other"
        other.mkdir()
        rec = self.run_job(self.command.replace(f" {self.tmp / 'link'} ", f" {other} "))
        self.assertEqual(rec["code"], 1)
        self.assertIn("folder", rec["failed"][0])
        self.assertEqual(rec["status"]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
