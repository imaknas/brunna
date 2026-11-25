"""A platform run logs in as it was declared, and sends that login only where the platform does: what its environment or a
settings file it loads says otherwise doesn't reach it. And an HTTP error never quotes a key."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import completion  # noqa: E402
import hubconf  # noqa: E402
import llm  # noqa: E402
import provider_http  # noqa: E402
import runtime_claude as rc  # noqa: E402
from runtime_ports import Task  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class Environment(Tmp):
    def test_no_other_login_is_left_in_a_run_s_environment(self):
        f = self.tmp / "token"
        f.write_text("t\n")
        base = {"PATH": "/bin", "ANTHROPIC_BASE_URL": "http://gateway", rc.MANAGED_VAR: "theirs", rc.TOKEN_VAR: "theirs",
                **dict.fromkeys(rc.LOGIN_OTHERS, "x")}
        kept = {"PATH": "/bin", "ANTHROPIC_BASE_URL": "http://gateway"}
        with mock.patch.object(rc, "_token_file", lambda: f):
            self.assertEqual(rc.auth_env("token", base)[0], {**kept, rc.TOKEN_VAR: "t", rc.MANAGED_VAR: "1"})
            self.assertEqual(rc.auth_env("subscription", base)[0], kept, "the variable turns the stored login off")

    def test_the_owner_s_own_terminal_keeps_theirs(self):
        self.assertIn("ANTHROPIC_API_KEY", rc.clean_env({"ANTHROPIC_API_KEY": "x"}))


class Held(Tmp):
    SPEC = {"tools": "Read", "shape": "agent"}
    TOKEN = {"PATH": "/bin", rc.TOKEN_VAR: "t", rc.MANAGED_VAR: "1"}

    def setUp(self):
        super().setUp()
        self.mine = {}
        for target, attr, value in ((rc, "user_settings", lambda: self.mine), (rc, "_described_exe", lambda: "claude"),
                                    (hubconf, "private_tmp", lambda: self.tmp)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def flags(self, spec=None, extra=(), env=None):
        return rc._flags(Task(point="x", prompt="p", spec={**self.SPEC, **(spec or {})}, cwd=str(self.tmp), env=env or self.TOKEN, extra=tuple(extra)))

    def held(self, cmd) -> dict:
        self.assertEqual(cmd.count("--settings"), 1)
        text = cmd[cmd.index("--settings") + 1]
        if text.startswith("{"):
            return json.loads(text)
        self.assertEqual(os.stat(text).st_mode & 0o777, 0o600)
        return json.loads(pathlib.Path(text).read_text())

    def test_a_token_run(self):
        """Its login is the host's (MANAGED_VAR): nothing emptied reaches what it starts but the route."""
        doc = self.held(self.flags())
        self.assertEqual((doc["apiKeyHelper"], set(doc["env"])), ("", set(rc.LOGIN_ROUTE)))

    def test_a_subscription_run(self):
        doc = self.held(self.flags(env={"PATH": "/bin"}))
        self.assertEqual({doc["env"][n] for n in (*rc.LOGIN_OTHERS, rc.TOKEN_VAR, "ANTHROPIC_BASE_URL")}, {""})

    def test_the_route_is_the_platform_s_or_the_owner_s(self):
        self.mine = {"env": {"HTTPS_PROXY": "http://mine:1", "NODE_EXTRA_CA_CERTS": "/ca.pem", "ANTHROPIC_BASE_URL": "http://gw"}}
        doc = self.held(self.flags())
        self.assertEqual((doc["env"]["HTTPS_PROXY"], doc["env"]["NODE_EXTRA_CA_CERTS"]), ("http://mine:1", "/ca.pem"))
        doc = self.held(self.flags(env={**self.TOKEN, "HTTPS_PROXY": "http://user:pw@corp:1"}))
        self.assertEqual(doc["env"]["HTTPS_PROXY"], "http://user:pw@corp:1")
        self.assertEqual(self.held(self.flags(env={"PATH": "/bin"}))["env"]["ANTHROPIC_BASE_URL"], "http://gw")

    def test_both_spellings_of_a_proxy(self):
        doc = self.held(self.flags(env={**self.TOKEN, "HTTPS_PROXY": "http://corp"}))
        self.assertEqual((doc["env"]["https_proxy"], doc["env"]["HTTPS_PROXY"], doc["env"]["http_proxy"]), ("http://corp", "http://corp", ""))

    def test_what_the_host_variable_drops_though_it_is_no_login(self):
        self.mine = {"env": {"AWS_PROFILE": "mine", "AWS_REGION": "mine"}}
        (self.tmp / ".claude").mkdir()
        (self.tmp / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "AWS_PROFILE": "project", "GOOGLE_CLOUD_PROJECT": "g", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "m", "ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "x",
            "AWS_BEARER_TOKEN_BEDROCK": "x", "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/evil", "DYLD_INSERT_LIBRARIES": "x", "AWS_N": 1, "NODE_OPTIONS": "x"}}))
        (self.tmp / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"AWS_PROFILE": "local"}}))
        got = rc.carried_env(self.tmp, {**self.TOKEN, "AWS_PROFILE": "process"})
        self.assertEqual({k: v for k, v in got.items() if k not in self.TOKEN},
                         {"AWS_PROFILE": "local", "AWS_REGION": "mine", "GOOGLE_CLOUD_PROJECT": "g", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "m"})
        self.assertEqual(got["PATH"], "/bin")
        self.assertEqual(rc.carried_env(self.tmp, {"PATH": "/bin"}), {"PATH": "/bin"}, "a subscription run's settings files apply as they are")

    def test_a_settings_file_that_is_no_object(self):
        (self.tmp / ".claude").mkdir()
        (self.tmp / ".claude" / "settings.json").write_text("[1]")
        (self.tmp / ".claude" / "settings.local.json").write_text('{"env": [1]}')
        self.assertEqual(rc.carried_env(self.tmp, dict(self.TOKEN)), self.TOKEN)

    def test_no_value_on_the_command_line(self):
        cmd = self.flags(env={**self.TOKEN, "HTTPS_PROXY": "http://user:pw@corp:1"})
        self.assertNotIn("pw@corp", " ".join(cmd))

    def test_a_restricted_run_loads_none(self):
        self.assertNotIn("--settings", self.flags({"restricted": True}))
        with self.assertRaises(ValueError):
            self.flags({"restricted": True}, ("--settings", "{}"))

    def test_a_caller_s_settings_are_kept_whole_under_it(self):
        f = self.tmp / "lease.json"
        f.write_text(json.dumps({"permissions": {"deny": ["Read(//x)"]}, "apiKeyHelper": "/their/helper", "env": {"HTTPS_PROXY": "http://theirs", "A": "1"}}))
        for extra in (("--settings", str(f)), (f"--settings={f}",), ("--settings", f.read_text())):
            doc = self.held(self.flags(extra=("--resume", "s", *extra)))
            self.assertEqual((doc["permissions"], doc["apiKeyHelper"], doc["env"]["HTTPS_PROXY"], doc["env"]["A"]),
                             ({"deny": ["Read(//x)"]}, "", "", "1"))
        self.assertIn("--resume", self.flags(extra=("--resume", "s", "--settings", str(f))))

    def test_one_that_cannot_be_read_stops_the_run(self):
        for extra in (("--settings", str(self.tmp / "none.json")), ("--settings",), ("--settings", "[1]"), ("--settings", "{}", "--settings={}")):
            with self.assertRaises(ValueError):
                self.flags(extra=extra)

    def test_a_caller_does_not_choose_which_settings_load(self):
        for bad in (("--setting-sources", "user"), ("--setting-sources=user",), ("--managed-settings", "{}")):
            with self.assertRaises(ValueError):
                self.flags(extra=bad)

    def test_a_run_on_the_platform_s_settings_takes_no_caller_s(self):
        with mock.patch.object(rc, "project_roots", lambda p: [self.tmp.resolve()]), \
                mock.patch.object(rc, "platform_settings", lambda *a, **k: "/platform.json"), \
                mock.patch.object(rc, "own_state_dir", lambda *a: None), mock.patch.object(rc, "_linked_rules", lambda *a: ([], [])), \
                mock.patch.object(rc, "_notes_rules", lambda *a: []):
            spec, env = {"settings": "platform", "project_env": "PROJ"}, {**self.TOKEN, "PROJ": "p"}
            for bad in (("--settings", "{}"), ("--settings={}",), ("--setting-sources", "user"), ("--managed-settings={}",)):
                with self.assertRaises(ValueError):
                    self.flags(spec, bad, env)
            self.assertEqual(self.flags(spec, (), env).count("--settings"), 1)

    def test_every_declared_point_has_one_settings_object_at_most(self):
        for name, spec in llm.points()["points"].items():
            if spec.get("shape") == "agent" and spec.get("settings") != "platform":
                cmd = self.flags(spec)
                self.assertEqual(cmd.count("--settings"), 0 if spec.get("restricted") else 1, name)


class Keys(Tmp):
    KEY = "sk-ant-api03-abcdefgh"

    def test_an_error_never_quotes_a_header(self):
        for bad in (self.KEY + "\nsecond", self.KEY + "\r\nX: y", self.KEY + "\u00e9\u4e2d"):
            status, _data, _headers, err = provider_http.call("GET", "http://127.0.0.1:9/v1/models", None, {"x-api-key": bad}, 2)
            self.assertIsNone(status)
            self.assertNotIn(self.KEY, err)
            self.assertNotIn("abcdefgh", err)
        _s, _d, _h, err = provider_http.call("GET", "http://127.0.0.1:9/v1/models", None, {"x-api-key": self.KEY}, 2)
        self.assertIn("refused", err.lower(), "an ordinary error is still said")

    def test_a_service_that_quotes_the_key_back(self):
        import http.server
        import threading

        class Echo(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = f"bad key {self.headers.get('x-api-key') or self.headers.get('authorization')}".encode()
                self.send_response(401)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.HTTPServer(("127.0.0.1", 0), Echo)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for headers in ({"x-api-key": self.KEY}, {"Authorization": f"Bearer {self.KEY}"}):
            status, _d, _h, err = provider_http.call("GET", f"http://127.0.0.1:{server.server_port}/", None, headers, 5)
            self.assertEqual(status, 401)
            self.assertNotIn("abcdefgh", err)
            self.assertIn("bad key", err)

    def test_a_file_that_is_no_key_is_never_sent(self):
        with mock.patch.object(hubconf, "secrets_dir", lambda: self.tmp):
            for bad in (self.KEY + "\nsecond", "two words", "ké"):
                (self.tmp / "k").write_text(bad)
                with self.assertRaises(completion.Refused) as e:
                    completion.read_key({"key": {"secret": "k"}})
                self.assertNotIn("abcdefgh", str(e.exception))
            (self.tmp / "k").write_text(self.KEY + "\n")
            self.assertEqual(completion.read_key({"key": {"secret": "k"}}), self.KEY)

    def test_nor_one_from_the_environment(self):
        with mock.patch.dict(os.environ, {"MY_KEY": self.KEY + "\nsecond"}):
            with self.assertRaises(completion.Refused) as e:
                completion.read_key({"key": {"env": "MY_KEY"}})
            self.assertNotIn("abcdefgh", str(e.exception))


if __name__ == "__main__":
    unittest.main()
