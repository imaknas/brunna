"""Platform credentials (docs/design/platform-credentials.md): the registry's kind and credentials:, the adapter's
credential_tools port, bin/credentials.py's storage, refresh and owner to-dos, and llm.py's attachment. Local servers and
scratch folders only: no provider, no account.

    bin/run-tests <a folder holding this file>
"""
import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ["AGENT_HUB_LANG_UI"] = os.environ["BRUNNA_LANG_UI"] = "en"   # the messages these tests read are the English ones

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import credentials  # noqa: E402
import mcp_fixture  # noqa: E402
import registry  # noqa: E402
import runtime_claude  # noqa: E402
import runtime_ports  # noqa: E402

SEC = {"name": "upwork", "kind": "oauth", "server": {"id": "upwork", "url": "https://mcp.example.org/mcp"}}
ALLOW = ["mcp__upwork__upwork__get_messages", "mcp__upwork__upwork__list_offers", "ToolSearch", "Read(./.daily/**)"]


def hubbus():
    loader = importlib.machinery.SourceFileLoader("hubbus_t", str(ROOT / "bin" / "hubbus"))
    spec = importlib.util.spec_from_loader("hubbus_t", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class Registry(unittest.TestCase):
    def errs(self, sec, credentials_ok=True):
        return registry.validate_secrets([sec], credentials_ok=credentials_ok)

    def test_a_credential_entry(self):
        self.assertEqual(self.errs(SEC), [])
        self.assertEqual(self.errs({**SEC, "kind": "static", "server": {**SEC["server"], "header": "X-Api-Key"}}), [])

    def test_what_it_may_not_have(self):
        for bad in ({**SEC, "path": "x"}, {**SEC, "expires": "2027-01-01"}, {**SEC, "kind": "token"},
                    {**SEC, "server": {"id": "upwork", "url": "http://mcp.example.org/mcp"}},
                    {**SEC, "server": {"id": "upwork", "url": "https://u:p@mcp.example.org/mcp"}},
                    {**SEC, "server": {"id": "upwork", "url": "https://mcp.example.org/mcp?x=1"}},
                    {**SEC, "server": {"id": "brunna-run", "url": SEC["server"]["url"]}},
                    {**SEC, "server": {**SEC["server"], "header": "X"}},   # header: static only
                    {**SEC, "broken": "page"}, {**SEC, "name": "Up/work"}):
            with self.subTest(bad):
                self.assertNotEqual(self.errs(bad), [])
        self.assertNotEqual(self.errs(SEC, credentials_ok=False), [], "a module has none")
        self.assertTrue(any("server" in e for e in self.errs({"name": "x", "path": "p", "server": {}})))

    def doc(self, **job):
        return {"name": "demo", "secrets": [SEC], "jobs": [{"name": "daily", "kind": "agent", "at": "09:00",
                                                             "allow": ALLOW, "credentials": ["upwork"], **job}]}

    def test_which_jobs_may_list_one(self):
        ok = self.doc()
        self.assertEqual(registry.validate_job_credentials(ok["jobs"][0], ok), [])
        for job in ({"trigger": {"events": ["merge.done"]}}, {"allow": None}, {"connectors": True},
                    {"credentials": ["other"]}, {"credentials": "upwork"}, {"kind": "command"}):
            d = self.doc(**job)
            with self.subTest(job):
                self.assertNotEqual(registry.validate_job_credentials(d["jobs"][0], d), [])
        platform = {**ok, "name": "brunna", "type": "platform"}
        self.assertTrue(any("platform's own jobs" in e for e in registry.validate_job_credentials(ok["jobs"][0], platform)),
                        "a job of the platform's own may write its state, the bridge's status folder included")
        sandboxed = {**ok, "enforce": {"sandbox": True}}
        self.assertTrue(any("sandboxed" in e for e in registry.validate_job_credentials(ok["jobs"][0], sandboxed)))

    def test_owner_only(self):
        cur = self.doc()
        for new in ({**cur, "secrets": []}, {**cur, "secrets": [{**SEC, "server": {**SEC["server"], "url": "https://x.org/mcp"}}]},
                    {**cur, "jobs": [{**cur["jobs"][0], "allow": [*ALLOW, "Bash"]}]},
                    {**cur, "jobs": [{**cur["jobs"][0], "credentials": []}]}):
            with self.subTest(new):
                self.assertNotEqual(registry.platform_owned_changes(new, cur), [])
        plain = {"name": "demo", "secrets": [{"name": "k", "path": ".env"}]}
        self.assertEqual(registry.platform_owned_changes({**plain, "secrets": []}, plain), [], "a project's own secret")

    def test_the_callback_port_is_reserved(self):
        import status_key
        self.assertEqual(registry.RESERVED_LOCAL_PORTS, (credentials.CALLBACK_PORT, status_key.PORT))
        errs = registry.validate_runs({"services": [{"name": "s", "run": "/bin/sh", "expose": {"local_port": 47615}}]},
                                      "demo", {"runtime": None, "workspace": None})
        self.assertTrue(any("47615" in e for e in errs), errs)

    def test_refusals_through_the_adapter(self):
        idx = {"projects": {"demo": self.doc()}}
        self.assertEqual(registry.credential_refusals(idx, runtime_claude), {})
        idx = {"projects": {"demo": self.doc(allow=[*ALLOW, "Bash(ls)"])}}
        self.assertIn("demo/daily", registry.credential_refusals(idx, runtime_claude))


class Adapter(unittest.TestCase):
    def test_tools_named_keeps_the_tools_own_underscores(self):
        self.assertEqual(runtime_claude.rules_tools_named(ALLOW, "upwork"), ["upwork__get_messages", "upwork__list_offers"])
        self.assertEqual(runtime_claude.rules_tools_named(["mcp__my-db__run__query"], "my-db"), ["run__query"])
        self.assertEqual(runtime_claude.rules_tools_named(["mcp__upwork"], "upwork"), [])

    def test_what_a_credentials_job_may_not_name(self):
        p = runtime_claude.credential_rules_problems
        self.assertEqual(p(ALLOW, {"upwork"}), [])
        self.assertEqual(p([*ALLOW, "WebFetch(domain:example.org)", "Edit(./.daily/**)"], {"upwork"}), [])
        for bad in ("Bash", "Bash(ls *)", "Read", "Read(~/x)", "Edit(../y/**)", "mcp__upwork", "mcp__other__x", "Task",
                    "WebFetch", "NotebookEdit"):
            with self.subTest(bad):
                self.assertNotEqual(p([*ALLOW, bad], {"upwork"}), [], bad)
        self.assertTrue(p(["ToolSearch"], {"upwork"}), "a server none of whose tools is named")

    def test_the_port_and_the_fake(self):
        self.assertIn("credential_tools", runtime_ports.capabilities("claude"))
        import runtime_fake
        for fn in runtime_ports.PORTS["credential_tools"]:
            self.assertTrue(callable(getattr(runtime_fake, fn, None)), fn)


class Store(unittest.TestCase):
    """credentials.py against a local authorization server, in scratch folders."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.srv = mcp_fixture.Server(self)
        self.srv.oauth()
        self.sec = {**SEC, "server": {"id": "upwork", "url": self.srv.url}}
        self.doc = {"name": "demo", "secrets": [self.sec], "jobs": [{"name": "daily", "kind": "agent", "allow": ALLOW,
                                                                      "credentials": ["upwork"]}]}
        self.sent, self.alerts = [], []
        b = (self.tmp / "s" / "credentials", self.tmp / "s" / "credentials-kept")
        patches = [mock.patch.object(credentials, "bases", lambda: b),
                   mock.patch.object(credentials, "status_path", lambda p, n: self.tmp / "state" / p / f"{n}.json"),
                   mock.patch.object(credentials, "index", lambda: {"projects": {"demo": self.doc}}),
                   mock.patch.object(credentials, "owner_notify", lambda *a: self.sent.append(a)),
                   mock.patch("alert.once", lambda key, problems, ok, ids=None: self.alerts.append((key, list(problems))))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.idx = {"projects": {"demo": self.doc}}

    def grant(self, **extra):
        cred = {"v": 1, "kind": "oauth", "project": "demo", "name": "upwork", "server_url": self.srv.url,
                "issuer": self.srv.base, "token_endpoint": self.srv.base + "/token",
                "revocation_endpoint": self.srv.base + "/revoke", "resource": self.srv.url,
                "client": {"route": "c", "client_id": "cid", "auth_method": "none"},
                "access_token": "acc-1", "refresh_token": "ref-1", "access_expires_at": time.time() + 3600,
                "issued_at": time.time(), **extra}
        credentials.granted("demo", "upwork", self.sec, cred, "c")
        return cred

    def test_fresh_takes_the_stored_token_while_it_lasts(self):
        self.grant()
        self.assertEqual(credentials.fresh("demo", "upwork", 600, idx=self.idx), "acc-1")
        self.assertEqual(self.srv.posts("/token"), [])
        path = credentials.found("demo", "upwork")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_a_refresh_sends_resource_and_takes_a_short_token(self):
        self.grant(access_expires_at=time.time() + 30)
        self.srv.token_answers = [(200, {"access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 60})]
        self.assertEqual(credentials.fresh("demo", "upwork", 600, idx=self.idx), "acc-2", "never loops on a short one")
        [post] = self.srv.posts("/token")
        form = dict(x.split("=", 1) for x in post["body"].split("&"))
        self.assertEqual((form["grant_type"], form["refresh_token"], form["client_id"]), ("refresh_token", "ref-1", "cid"))
        self.assertIn("resource", form)
        self.assertEqual(credentials.load("demo", "upwork")["refresh_token"], "ref-2")

    def test_two_refreshers_after_one_refused_token_send_one_refresh(self):
        self.grant()
        self.srv.token_answers = [(200, {"access_token": "acc-2", "expires_in": 3600})]
        failed = credentials.token_hash("acc-1")
        out = []
        threads = [threading.Thread(target=lambda: out.append(credentials.refresh("demo", "upwork", failed, idx=self.idx)))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(out, ["acc-2", "acc-2"])
        self.assertEqual(len(self.srv.posts("/token")), 1)
        credentials.refresh("demo", "upwork", credentials.token_hash("an-older-one"), idx=self.idx)
        self.assertEqual(len(self.srv.posts("/token")), 1, "a stale hash refreshes nothing")

    def test_a_pending_file_is_taken_only_when_whole_and_newer(self):
        cred = self.grant(access_expires_at=time.time() - 1)
        path = credentials.found("demo", "upwork")
        credentials._write_private(path.with_name(path.name + ".new"), {**cred, "access_token": "acc-new",
                                                                        "issued_at": time.time() + 5})
        self.assertEqual(credentials.refresh("demo", "upwork", credentials.token_hash("acc-1"), idx=self.idx), "acc-new")
        self.assertFalse(path.with_name(path.name + ".new").exists())
        credentials._write_private(path.with_name(path.name + ".new"), {**cred, "access_token": "acc-old", "issued_at": 1})
        credentials.refresh("demo", "upwork", credentials.token_hash("x"), idx=self.idx)
        self.assertEqual(credentials.load("demo", "upwork")["access_token"], "acc-new", "an older one is dropped")
        self.assertFalse(path.with_name(path.name + ".new").exists())

    def test_answers_that_need_the_owner(self):
        for answer, words in (((400, {"error": "invalid_grant"}), "invalid_grant"),
                              ((401, {"error": "invalid_client"}), "invalid_client")):
            with self.subTest(answer):
                self.grant(access_expires_at=time.time() - 1)
                self.srv.token_answers = [answer]
                with self.assertRaisesRegex(credentials.NeedsGrant, words):
                    credentials.fresh("demo", "upwork", idx=self.idx)
                self.assertEqual(credentials.read_status("demo", "upwork")["state"], "needs_grant")
                with self.assertRaises(credentials.NeedsGrant):   # and stays so, without asking the provider
                    credentials.fresh("demo", "upwork", idx=self.idx)

    def test_a_503_is_a_retry_not_a_break(self):
        self.grant(access_expires_at=time.time() - 1)
        self.srv.token_answers = [(503, {})]
        with self.assertRaises(credentials.Transient):
            credentials.fresh("demo", "upwork", idx=self.idx)
        self.assertEqual(credentials.read_status("demo", "upwork")["state"], "granted")
        self.assertEqual(self.sent, [])

    def test_an_answer_lost_twice_needs_the_owner_once(self):
        self.grant(access_expires_at=time.time() - 1)
        lost = credentials.Transient("timed out")
        lost.lost = True
        with mock.patch.object(credentials, "token_request", side_effect=lost) as tr, \
                mock.patch.object(credentials, "RETRY_BACKOFF_S", 0), \
                self.assertRaisesRegex(credentials.NeedsGrant, "lost twice"):
            credentials.fresh("demo", "upwork", idx=self.idx)
        self.assertEqual(tr.call_count, 2)

    def test_break_regrant_break_gives_two_todos(self):
        hb = hubbus()
        self.grant(access_expires_at=time.time() - 1)
        self.srv.token_answers = [(400, {"error": "invalid_grant"})]
        with self.assertRaises(credentials.NeedsGrant):
            credentials.fresh("demo", "upwork", idx=self.idx)
        credentials.keepalive(self.idx)   # the hourly job doesn't send it again
        self.grant(access_expires_at=time.time() - 1)
        self.srv.token_answers = [(400, {"error": "invalid_grant"})]
        with self.assertRaises(credentials.NeedsGrant):
            credentials.fresh("demo", "upwork", idx=self.idx)
        tasks = [a for a in self.sent if a[1] == "task"]
        dones = [a for a in self.sent if a[1] == "done"]
        self.assertEqual(len(tasks), 2, self.sent)
        self.assertEqual(len(dones), 1)
        keys = [a[3] for a in tasks]
        self.assertEqual(len(set(keys)), 2)
        for k in keys:
            self.assertTrue(hb.KEY_RE.fullmatch(k) and hb.EVENT_ID.fullmatch(k), k)
        self.assertIn("credentials grant demo upwork", tasks[0][5])
        self.assertEqual(dones[0][3], keys[0], "the grant closed the first episode's to-do")

    def test_a_connection_that_never_opened_is_not_lost(self):
        import socket
        with mock.patch.object(socket, "create_connection", side_effect=TimeoutError("timed out")):
            with self.assertRaises(credentials.Transient) as e:
                credentials.http("POST", self.srv.base + "/token", data=b"x")
        self.assertFalse(e.exception.lost)
        self.grant(access_expires_at=time.time() - 1)
        with mock.patch.object(socket, "create_connection", side_effect=TimeoutError("timed out")), \
                self.assertRaises(credentials.Transient):
            credentials.fresh("demo", "upwork", idx=self.idx)
        self.assertEqual(credentials.read_status("demo", "upwork")["state"], "granted", "an outage never needs the owner")

    def test_a_token_that_isnt_bearer_is_refused(self):
        self.grant(access_expires_at=time.time() - 1)
        self.srv.token_answers = [(200, {"access_token": "acc-2", "token_type": "mac"})]
        with self.assertRaisesRegex(credentials.NeedsGrant, "bearer"):
            credentials.fresh("demo", "upwork", idx=self.idx)

    def test_provider_text_is_clamped(self):
        self.assertEqual(credentials.clamp('a"b\\c\nd'), "a?b?c?d")
        self.assertLessEqual(len(credentials.clamp("x" * 500)), 120)

    def test_the_host_warning(self):
        self.assertEqual(credentials.host_lines("mcp.upwork.com", "mcp.upwork.com"), [])
        self.assertIn("different hosts", credentials.host_lines("www.upwork.com", "mcp.upwork.com")[0])
        for a, b in (("auth.example.co.uk", "mcp.other.co.uk"), ("alice.github.io", "bob.github.io"),
                     ("login.example.org", "mcp.example.net")):
            self.assertIn("different sites", credentials.host_lines(a, b)[0], (a, b))
        self.assertIn("different hosts", credentials.host_lines("a.alice.github.io", "b.alice.github.io")[0])

    def test_https_everywhere_and_the_discovery_order(self):
        gets = []
        meta = {"issuer": "https://as.example.com/t1", "authorization_endpoint": "https://as.example.com/a",
                "token_endpoint": "https://as.example.com/t", "code_challenge_methods_supported": ["S256"],
                "registration_endpoint": "http://as.example.com/r", "revocation_endpoint": "http://as.example.com/v"}

        def get_json(url, op=None):
            gets.append(url)
            if url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp":
                return {"resource": "https://mcp.example.com/mcp", "authorization_servers": [issuer[0]]}
            return meta if url == "https://as.example.com/t1/.well-known/openid-configuration" else None
        issuer = ["https://as.example.com/t1"]
        with mock.patch.object(credentials, "http", lambda *a, **k: (401, {}, b"")), \
                mock.patch.object(credentials, "get_json", get_json):
            d = credentials.discover("https://mcp.example.com/mcp")
            self.assertNotIn("registration_endpoint", d["as"])
            self.assertNotIn("revocation_endpoint", d["as"])
            self.assertEqual(gets[1:], ["https://as.example.com/.well-known/oauth-authorization-server/t1",
                                        "https://as.example.com/.well-known/openid-configuration/t1",
                                        "https://as.example.com/t1/.well-known/openid-configuration"])
            issuer[0] = "http://as.example.com"
            with self.assertRaisesRegex(ValueError, "issuer"):
                credentials.discover("https://mcp.example.com/mcp")

    def test_a_long_key_stays_in_the_alphabet(self):
        k = credentials.todo_key("p" * 90, "n" * 48, 12)
        self.assertLessEqual(len(k), 128)
        self.assertTrue(hubbus().KEY_RE.fullmatch(k))
        self.assertNotEqual(k, credentials.todo_key("p" * 90, "n" * 47 + "m", 12))

    def test_not_granted_and_listed_is_one_todo(self):
        credentials.keepalive(self.idx)
        credentials.keepalive(self.idx)
        self.assertEqual([a[1] for a in self.sent], ["task"])
        [row] = credentials.waiting(self.idx)
        self.assertEqual(row["state"], "not_granted")
        self.assertIn("credentials grant demo upwork", credentials.waiting_text(row))
        with mock.patch.dict(self.sec, {"broken": "alert"}):
            self.sent.clear()
            credentials.status_path("demo", "upwork").unlink()
            credentials.keepalive(self.idx)
            self.assertEqual(self.sent, [], "broken: alert raises no to-do")

    def test_revoke_deletes_even_when_the_provider_fails(self):
        self.grant()
        self.srv.revoke_status = 500
        with mock.patch.object(credentials, "REQUIRE_HTTPS", False):
            self.assertEqual(credentials.revoke("demo", "upwork"), "local", "deleted here only")
        self.assertIsNone(credentials.found("demo", "upwork"))
        self.assertTrue(any(k.startswith("credential-revoke") for k, _p in self.alerts))
        posts = self.srv.posts("/revoke")
        self.assertIn("token=ref-1", posts[0]["body"])
        self.assertIn("token=acc-1", posts[1]["body"], "the access token too")
        self.assertEqual(credentials.revoke("demo", "upwork"), "none")

    def test_revoke_takes_the_newest_and_works_without_a_registry_entry(self):
        cred = self.grant()
        path = credentials.found("demo", "upwork")
        credentials._write_private(path.with_name(path.name + ".new"), {**cred, "refresh_token": "ref-newest",
                                                                        "issued_at": time.time() + 5})
        self.doc["secrets"] = []   # an orphan: no entry declares it any more
        [row] = [r for r in credentials.rows(self.idx) if r["state"] == "orphaned"]
        self.assertIn("credentials revoke demo upwork", credentials.waiting_text(row))
        with mock.patch.object(credentials, "REQUIRE_HTTPS", False):
            self.assertEqual(credentials.revoke("demo", "upwork"), "provider")
        self.assertIn("token=ref-newest", self.srv.posts("/revoke")[0]["body"])
        self.assertFalse(path.with_name(path.name + ".new").exists())

    def test_a_revoke_starts_a_new_episode(self):
        self.grant()
        before = credentials.read_status("demo", "upwork").get("episode") or 1
        credentials.revoke("demo", "upwork")
        credentials.keepalive(self.idx)
        tasks = [a for a in self.sent if a[1] == "task"]
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0][3].endswith(f"-{before + 1}"))

    def test_the_hourly_job_closes_a_todo_whose_close_failed(self):
        credentials.keepalive(self.idx)   # not granted: a to-do
        with mock.patch.object(credentials, "owner_notify", side_effect=OSError("bus down")):
            self.grant()   # its close fails
        self.assertFalse(credentials.read_status("demo", "upwork")["todo"]["closed"])
        credentials.keepalive(self.idx)
        self.assertTrue(credentials.read_status("demo", "upwork")["todo"]["closed"])
        self.assertEqual([a[1] for a in self.sent], ["task", "done"])

    def test_check_secrets_counts_credential_files_as_declared(self):
        import hubconf
        spec = importlib.util.spec_from_file_location("check_secrets_t", ROOT / "bin" / "check-secrets.py")
        cs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cs)
        sec = hubconf.secrets_dir()
        f = sec / "credentials" / "demo" / "github-token.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")
        self.addCleanup(shutil.rmtree, sec / "credentials", True)
        listed = cs.undeclared(hubconf.hub_project(), {"name": hubconf.hub_project()}, set())
        self.assertNotIn(f, listed)

    def test_backup_false_stays_out_of_the_backup(self):
        import backup
        import hubconf
        self.assertFalse(backup.covers(hubconf.secrets_dir() / "credentials" / "demo" / "upwork.json"))

    def test_rename_moves_both(self):
        self.grant()
        root = self.tmp / "state-root"
        (root / "credentials" / "demo").mkdir(parents=True)
        (root / "credentials" / "demo" / "upwork.json").write_text("{}")
        with mock.patch.object(credentials.hubconf, "STATE", root):
            self.assertEqual(len(credentials.rename("demo", "demo2", apply=False)), 2)
            (root / "credentials" / "demo2").mkdir()
            with self.assertRaises(ValueError):   # a destination taken: nothing moves
                credentials.rename("demo", "demo2", apply=True)
            self.assertTrue((credentials.bases()[0] / "demo" / "upwork.json").exists())
            (root / "credentials" / "demo2").rmdir()
            credentials.rename("demo", "demo2", apply=True)
        self.assertTrue((credentials.bases()[0] / "demo2" / "upwork.json").exists())
        self.assertTrue((root / "credentials" / "demo2" / "upwork.json").exists(), "the status moves too")

    def test_the_backup_leaves_credentials_out_and_keeps_kept(self):
        import backup
        import hubconf
        import subprocess
        calls = []

        def restic(*args, **k):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        sec = hubconf.secrets_dir()
        sec.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(backup, "ensure_repo", lambda: None), \
                mock.patch.object(backup, "stage", lambda: {"sqlite": [], "bundles": [], "workspace": []}), \
                mock.patch.object(backup, "included", lambda: [sec]), mock.patch.object(backup, "restic", restic), \
                mock.patch.object(backup, "retention_due", lambda *a: False), \
                mock.patch.object(backup, "OUT", self.tmp / "out"), mock.patch.object(backup, "say", lambda m: None):
            backup.cmd_run()
            self.assertTrue(backup.covers(sec / "credentials-kept" / "demo" / "upwork.json"))
            self.assertFalse(backup.covers(sec / "credentials" / "demo" / "upwork.json"))
        [args] = [a for a in calls if a[0] == "backup"]
        i = [j for j, x in enumerate(args) if x == "--exclude" and args[j + 1] == str(sec / "credentials")]
        self.assertTrue(i, args)
        self.assertNotIn(str(sec / "credentials-kept"), args)
        self.assertIn(str(sec), args)

    def test_discovery_checks_what_it_is_told(self):
        with mock.patch.object(credentials, "REQUIRE_HTTPS", False):
            d = credentials.discover(self.srv.url)
            self.assertEqual((d["issuer"], d["resource"]), (self.srv.base, self.srv.url))
            self.assertEqual(self.srv.log[1]["path"], "/.well-known/oauth-protected-resource/mcp",
                             "the 401's resource_metadata first")
            self.srv.metadata["/.well-known/oauth-protected-resource/mcp"]["resource"] = "https://elsewhere/mcp"
            with self.assertRaisesRegex(ValueError, "resource"):
                credentials.discover(self.srv.url)
            self.srv.oauth(issuer="https://other.example")
            with self.assertRaisesRegex(ValueError, "issuer"):
                credentials.discover(self.srv.url)
            self.srv.oauth()
            del self.srv.metadata["/.well-known/oauth-authorization-server"]["code_challenge_methods_supported"]
            with self.assertRaisesRegex(ValueError, "S256"):
                credentials.discover(self.srv.url)
        with self.assertRaises(ValueError):   # over http, outside a test
            credentials.discover(self.srv.url)

    def test_registration_that_isnt_rfc7591_is_unsupported(self):
        meta = {"registration_endpoint": self.srv.base + "/register"}
        self.srv.register = (200, b"<html>sign up</html>", "text/html")
        self.assertIsNone(credentials.register_client(meta, "http://127.0.0.1:47615/callback"))
        self.srv.register = (201, json.dumps({"client_id": "dyn-1"}).encode())
        c = credentials.register_client(meta, "http://127.0.0.1:47615/callback")
        self.assertEqual((c["route"], c["client_id"], c["auth_method"]), ("b", "dyn-1", "none"))

    def test_route_a_is_unavailable_until_published(self):
        """Whatever this checkout ships: an oauth folder of the test's own, without the flag, then with it."""
        oauth = self.tmp / "oauth"
        oauth.mkdir()
        shutil.copy(ROOT / "oauth" / "client.json", oauth / "client.json")
        with mock.patch.object(credentials, "OAUTH", oauth), \
                mock.patch.object(credentials.hubconf, "STATE", self.tmp / "state-root"):
            ok, why = credentials.shared_client_state()
            self.assertFalse(ok)
            self.assertIn("oauth/published", why)
            (oauth / "published").write_text("")
            ok, why = credentials.shared_client_state()
            self.assertFalse(ok, "shipped, but not checked yet")
            self.assertIn("couldn't be checked", why)

    def test_the_tripwire_compares_the_whole_document(self):
        doc = {"client_id": self.srv.base + "/c.json", "client_name": "brunna", "redirect_uris": ["http://127.0.0.1:47615/callback"],
               "grant_types": ["authorization_code"], "token_endpoint_auth_method": "none", "client_uri": "x"}
        self.srv.metadata["/c.json"] = dict(doc)
        with mock.patch.object(credentials, "shared_client", lambda: doc), mock.patch.object(credentials, "REQUIRE_HTTPS", False):
            self.assertEqual(credentials.check_published()[0], "match")
            self.srv.metadata["/c.json"] = {**doc, "client_uri": "y"}
            self.assertEqual(credentials.check_published(), ("mismatch", "its client_uri differ from this install's"))
            self.srv.metadata["/c.json"] = {**doc, "redirect_uris": ["https://evil.example/cb"]}
            result, why = credentials.check_published()
        self.assertEqual(result, "mismatch")
        self.assertIn("redirect_uris", why)

    def test_the_callback_binds_loopback_without_reuse(self):
        srv, _got, _done = credentials.bind_callback(0)
        self.addCleanup(srv.server_close)
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        with self.assertRaises(OSError):
            credentials.bind_callback(srv.server_address[1])
        for good in ("http://127.0.0.1:47615/callback", "http://127.0.0.1/callback"):
            self.assertTrue(credentials.loopback(good), good)
        for bad in ("http://localhost:47615/callback", "https://evil.example/cb", "http://127.0.0.1.evil.example/cb",
                    "http://evil.example\\@127.0.0.1/callback", "http://u:p@127.0.0.1/callback",
                    "http://127.0.0.1/callback#x", "http://127.0.0.1/callback?x=1", "http://127.0.0.1:47615",
                    "http://127.0.0.1\\evil/cb", None):
            self.assertFalse(credentials.loopback(bad), bad)

    def test_pkce(self):
        v, c = credentials.pkce()
        self.assertEqual(c, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode())

    def test_no_redirect_and_no_proxy(self):
        other = mcp_fixture.Server(self)
        self.srv.redirect_to = other.url
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
            status, _h, _b = credentials.http("POST", self.srv.url, data=b"{}",
                                              headers={"Authorization": "Bearer CANARY"})
            self.assertNotIn("HTTP_PROXY", os.environ, "scrubbed")
        self.assertEqual(status, 302)
        self.assertEqual(other.log, [], "the redirect was never followed, the header never sent there")

    # ---- the grant, driven end to end: a fake browser follows the authorization URL to the callback ----
    def grant_cmd(self, browser, answers=("y", "cid"), own_app=True, meta=None):
        self.srv.oauth(**(meta or {}))
        self.srv.token = "tok-granted"
        self.srv.token_answers = [(200, {"access_token": "tok-granted", "refresh_token": "r1", "expires_in": 3600})]
        import runtime_claude
        with mock.patch.object(credentials, "REQUIRE_HTTPS", False), \
                mock.patch.object(credentials, "refuse_unattended", lambda: None), \
                mock.patch.object(credentials, "entry", lambda p, n, idx=None: (self.doc, self.sec)), \
                mock.patch.object(credentials, "ask", side_effect=list(answers)), \
                mock.patch("getpass.getpass", lambda prompt="": ""), mock.patch("webbrowser.open", browser), \
                mock.patch.object(credentials, "learn", lambda *a, **k: None), \
                mock.patch.object(credentials, "CALLBACK_WAIT_S", 2), \
                mock.patch.object(runtime_ports, "load", lambda *a, **k: runtime_claude):
            return credentials.cmd_grant("demo", "upwork", own_app=own_app, port=0)

    def browser(self, **override):
        import urllib.parse
        import urllib.request
        seen = {}

        def open_(url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            seen.update(q)
            back = {"code": "code-1", "state": q["state"], **override}
            urllib.request.urlopen(q["redirect_uri"] + "?" + urllib.parse.urlencode({k: v for k, v in back.items() if v}))
            return True
        return open_, seen

    def test_a_grant_end_to_end(self):
        open_, seen = self.browser()
        self.assertEqual(self.grant_cmd(open_), 0)
        self.assertNotEqual(seen["redirect_uri"], "http://127.0.0.1:0/callback", "--port=0: the port the system gave")
        self.assertTrue(credentials.loopback(seen["redirect_uri"]))
        self.assertEqual((seen["code_challenge_method"], seen["resource"]), ("S256", self.srv.url))
        cred = credentials.load("demo", "upwork")
        self.assertEqual((cred["access_token"], cred["client"]["route"]), ("tok-granted", "c"))
        [post] = self.srv.posts("/token")
        form = dict(x.split("=", 1) for x in post["body"].split("&"))
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertIn("code_verifier", form)
        self.assertIn("resource", form)
        self.assertEqual(credentials.read_status("demo", "upwork")["state"], "granted")
        listed = [e["body"].get("method") for e in self.srv.posts() if isinstance(e["body"], dict)]
        self.assertNotIn("tools/call", listed, "the check lists tools, never calls one")

    def test_a_wrong_state_or_a_missing_iss_is_refused(self):
        open_, _seen = self.browser(state="forged")
        self.assertEqual(self.grant_cmd(open_, answers=("y", "cid", "q")), 1, "the forged answer is ignored; none comes")
        self.assertIsNone(credentials.load("demo", "upwork"))
        open_, _seen = self.browser()
        self.assertEqual(self.grant_cmd(open_, meta={"authorization_response_iss_parameter_supported": True}), 1)
        self.assertIsNone(credentials.load("demo", "upwork"))
        open_, _seen = self.browser(iss=self.srv.base)
        self.assertEqual(self.grant_cmd(open_, meta={"authorization_response_iss_parameter_supported": True}), 0)

    def test_saying_no_stops_before_the_browser(self):
        opened = []
        self.assertEqual(self.grant_cmd(lambda url: opened.append(url), answers=("n",)), 1)
        self.assertEqual(opened, [])
        self.assertEqual(self.srv.posts("/register"), [], "nothing sent before the owner's yes")

    def test_only_the_first_matching_callback_counts(self):
        import urllib.error
        import urllib.request
        want = {"state": "s-1"}
        srv, got, done = credentials.bind_callback(0, want)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}/callback"
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "?code=evil&state=other")
        self.assertFalse(done.is_set())
        urllib.request.urlopen(base + "?code=good&state=s-1")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "?code=late&state=s-1")
        self.assertEqual(got["code"], "good")

    def test_a_pasted_address(self):
        r, w = os.pipe()
        os.write(w, b"http://127.0.0.1:1/callback?code=c-9&state=s-9\n")
        os.close(w)
        with os.fdopen(r) as f, mock.patch.object(sys, "stdin", f):
            got = credentials.wait_callback({}, threading.Event(), timeout_s=5)
        self.assertEqual(got, {"code": "c-9", "state": "s-9"})

    def test_the_owner_todos_go_through_hubbus_itself(self):
        """B1 of the r4 review: hubbus takes each episode's key and publishes each episode, in both of its modes."""
        import contextlib
        import io
        hb = hubbus()
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        hb.DB = tmp / "bus.db"
        hb.log = lambda s: None
        hb.projects = lambda: {"demo": tmp, credentials.hubconf.hub_project(): tmp}
        conn = hb.db()
        hb.db = lambda *a, **k: conn

        def send(*args):
            with mock.patch.object(sys, "argv", ["hubbus", "send", "--from", credentials.hubconf.hub_project(), "--to",
                                                 credentials.hubconf.owner_address(), *args]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    hb.main()
                except SystemExit as e:
                    if e.code:
                        raise OSError(err.getvalue())

        def rows():
            return conn.execute("SELECT count(*) FROM events").fetchone()[0]
        for records in (True, False):
            with self.subTest(records=records), mock.patch.object(hb, "RECORDS", records), \
                    mock.patch.object(credentials, "owner_notify", send):
                credentials.status_path("demo", "upwork").unlink(missing_ok=True)
                before = rows()
                for _ in range(2):   # break, re-grant, break
                    self.grant(access_expires_at=time.time() - 1)
                    self.srv.token_answers = [(400, {"error": "invalid_grant"})]
                    with self.assertRaises(credentials.NeedsGrant):
                        credentials.fresh("demo", "upwork", idx=self.idx)
                self.assertGreaterEqual(rows() - before, 3, "two to-dos and the close between them, each published")

    def test_the_scheduler_names_the_job_for_llm(self):
        idx = {"projects": {"demo": {**self.doc, "paths": {"workspace": str(self.tmp)},
                                     "jobs": [{**self.doc["jobs"][0], "at": "09:00", "cwd": "workspace"}]}},
               "modules": {}}
        jobs, _skipped, _renames = registry.scheduler_jobs(idx, set())
        [job] = [j for j in jobs if j["name"] == "demo/daily"]
        self.assertIn("AGENT_JOB_CREDENTIALS=daily", job["command"])
        self.assertIn("AGENT_JOB_ALLOW=", job["command"])

    def test_the_grant_refuses_inside_a_platform_run(self):
        with mock.patch.dict(os.environ, {"HUB_LLM_POINT": "agent-job-confined"}):
            self.assertIsNotNone(credentials.refuse_unattended())


if __name__ == "__main__":
    unittest.main()
