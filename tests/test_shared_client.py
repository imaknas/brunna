"""brunna's shared OAuth client (docs/design/platform-credentials.md C2a): the document the repository ships as the pin,
the template of the organization's Pages repository and its staging step, and the tripwire. Local servers and scratch
folders only: nothing is fetched from the published site.

    bin/run-tests <a folder holding this file>
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

import yaml

os.environ["AGENT_HUB_LANG_UI"] = os.environ["BRUNNA_LANG_UI"] = "en"   # the messages these tests read are the English ones

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import credentials  # noqa: E402
import mcp_fixture  # noqa: E402

CLIENT = ROOT / "oauth" / "client.json"
TEMPLATE = ROOT / "oauth" / "pages-repo"
WORKFLOW = TEMPLATE / ".github" / "workflows" / "pages.yml"


class Document(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(CLIENT.read_text())

    def test_its_url_is_its_own(self):
        u = urllib.parse.urlsplit(self.doc["client_id"])
        self.assertEqual((u.scheme, u.path, u.query, u.fragment), ("https", "/oauth/client.json", "", ""))
        self.assertTrue(u.hostname.endswith(".github.io"))

    def test_a_public_client_with_loopback_redirects_only(self):
        self.assertEqual(self.doc["redirect_uris"][0], f"http://127.0.0.1:{credentials.CALLBACK_PORT}/callback")
        self.assertTrue(all(credentials.loopback(u) for u in self.doc["redirect_uris"]))
        self.assertEqual((self.doc["token_endpoint_auth_method"], self.doc["token_endpoint_auth_methods_supported"]),
                         ("none", ["none"]))
        self.assertEqual(self.doc["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(self.doc["response_types"], ["code"])
        self.assertEqual(self.doc["client_name"], "brunna")
        for forbidden in ("client_secret", "client_secret_expires_at", "jwks", "jwks_uri"):
            self.assertNotIn(forbidden, self.doc)
        for k in credentials.SECURITY_FIELDS:
            self.assertIn(k, self.doc)

    def test_the_flag_isnt_shipped_until_the_owner_has_published(self):
        self.assertFalse((ROOT / "oauth" / "published").exists())
        self.assertFalse(credentials.shipped())
        self.assertFalse(credentials.shared_client_state()[0])

    def test_the_release_checklist_says_so(self):
        text = (ROOT / "CONTRIBUTING.md").read_text()
        self.assertIn("brunna credentials check-published", text)
        self.assertIn("oauth/published", text)


class Template(unittest.TestCase):
    def setUp(self):
        self.wf = yaml.safe_load(WORKFLOW.read_text())

    def test_it_runs_only_on_main_and_deploys_through_the_environment(self):
        self.assertEqual(self.wf[True], {"push": {"branches": ["main"]}})   # YAML reads a bare `on` as true
        [job] = self.wf["jobs"].values()
        self.assertEqual(job["environment"]["name"], "github-pages")
        self.assertEqual(self.wf["permissions"], {"contents": "read", "pages": "write", "id-token": "write"})

    def test_every_action_is_pinned_by_sha(self):
        [job] = self.wf["jobs"].values()
        uses = [s["uses"] for s in job["steps"] if "uses" in s]
        self.assertEqual([u.split("@")[0] for u in uses],
                         ["actions/checkout", "actions/upload-pages-artifact", "actions/deploy-pages"])
        for u in uses:
            self.assertRegex(u, r"@[0-9a-f]{40}$", u)
        upload = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-pages-artifact"))
        self.assertEqual(upload["with"]["path"], "_site")

    def stage(self, setup) -> tuple[int, pathlib.Path]:
        """The workflow's staging step, run as it is in a copy of the organization repository's layout."""
        [job] = self.wf["jobs"].values()
        script = next(s["run"] for s in job["steps"] if s.get("name") == "Stage only the client documents")
        repo = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, repo, True)
        (repo / "oauth").mkdir()
        setup(repo)
        shim = repo / ".bin"
        shim.mkdir()
        (shim / "python3").symlink_to(sys.executable)
        r = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=60,
                           env={"PATH": f"{shim}:/usr/bin:/bin", "HOME": str(repo)})
        return r.returncode, repo

    def test_it_stages_exactly_the_one_file(self):
        code, repo = self.stage(lambda r: shutil.copy(CLIENT, r / "oauth" / "client.json"))
        self.assertEqual(code, 0)
        staged = [p.relative_to(repo / "_site") for p in (repo / "_site").rglob("*") if not p.is_dir()]
        self.assertEqual(staged, [pathlib.Path("oauth/client.json")])
        self.assertEqual((repo / "_site" / "oauth" / "client.json").read_bytes(), CLIENT.read_bytes())

    def test_it_refuses_a_link_or_a_document_that_isnt_right(self):
        def link(r):
            shutil.copy(CLIENT, r / "elsewhere.json")
            (r / "oauth" / "client.json").symlink_to(r / "elsewhere.json")

        def evil(r):
            d = json.loads(CLIENT.read_text())
            d["redirect_uris"].append("https://evil.example/cb")
            (r / "oauth" / "client.json").write_text(json.dumps(d))

        def moved(r):
            d = json.loads(CLIENT.read_text())
            d["client_id"] = "https://elsewhere.example/oauth/client.json"
            (r / "oauth" / "client.json").write_text(json.dumps(d))

        def secret(r):
            d = json.loads(CLIENT.read_text())
            d["client_secret"] = "x"
            (r / "oauth" / "client.json").write_text(json.dumps(d))

        def userinfo(r):   # what a parser would read as 127.0.0.1, and a browser as evil.example
            d = json.loads(CLIENT.read_text())
            d["redirect_uris"] = ["http://evil.example\\@127.0.0.1/callback"]
            (r / "oauth" / "client.json").write_text(json.dumps(d))

        def none(r):
            d = json.loads(CLIENT.read_text())
            d["redirect_uris"] = []
            (r / "oauth" / "client.json").write_text(json.dumps(d))

        def fragment(r):
            d = json.loads(CLIENT.read_text())
            d["redirect_uris"] = ["http://127.0.0.1:47615/callback#x"]
            (r / "oauth" / "client.json").write_text(json.dumps(d))

        def second_misnamed(r):   # client-2.json naming client.json's URL
            shutil.copy(CLIENT, r / "oauth" / "client.json")
            shutil.copy(CLIENT, r / "oauth" / "client-2.json")

        def missing(r):
            pass
        for setup in (link, evil, moved, secret, userinfo, none, fragment, second_misnamed, missing):
            with self.subTest(setup.__name__):
                self.assertNotEqual(self.stage(setup)[0], 0)

    def test_a_second_document_is_published_beside_the_first(self):
        def two(r):
            shutil.copy(CLIENT, r / "oauth" / "client.json")
            d = json.loads(CLIENT.read_text())
            d["client_id"] = d["client_id"].replace("client.json", "client-2.json")
            (r / "oauth" / "client-2.json").write_text(json.dumps(d))
            (r / "oauth" / "notes.txt").write_text("not published")
        code, repo = self.stage(two)
        self.assertEqual(code, 0)
        staged = sorted(str(p.relative_to(repo / "_site")) for p in (repo / "_site").rglob("*") if not p.is_dir())
        self.assertEqual(staged, ["oauth/client-2.json", "oauth/client.json"])

    def test_the_workflow_and_the_grant_read_loopback_the_same(self):
        pattern = re.search(r'LOOPBACK = re\.compile\(r"([^"]+)"\)', WORKFLOW.read_text()).group(1)
        self.assertEqual(pattern, credentials.LOOPBACK.pattern)

    def test_the_template_names_the_url_the_pin_does(self):
        self.assertIn(json.loads(CLIENT.read_text())["client_id"], WORKFLOW.read_text())


class Tripwire(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.srv = mcp_fixture.Server(self)
        self.oauth = self.tmp / "oauth"
        self.oauth.mkdir()
        self.pin = {**json.loads(CLIENT.read_text()), "client_id": self.srv.base + "/oauth/client.json"}
        (self.oauth / "client.json").write_text(json.dumps(self.pin))
        self.alerts = []
        for p in (mock.patch.object(credentials, "OAUTH", self.oauth), mock.patch.object(credentials, "REQUIRE_HTTPS", False),
                  mock.patch.object(credentials.hubconf, "STATE", self.tmp / "state"),
                  mock.patch("alert.once", lambda key, problems, ok, ids=None: self.alerts.append((key, list(problems))))):
            p.start()
            self.addCleanup(p.stop)

    def fetches(self) -> int:
        return len([e for e in self.srv.log if e["path"] == "/oauth/client.json"])

    def test_before_the_flag_ships_it_runs_only_when_asked_and_never_alerts(self):
        self.assertIsNone(credentials.tripwire()[0])
        self.assertEqual(self.fetches(), 0, "nothing is live to guard yet")
        ok, why = credentials.tripwire(force=True)   # the owner's check before the release that ships the flag
        self.assertFalse(ok)
        self.assertIn("404", why)
        self.assertEqual(self.alerts, [])

    def age(self, seconds):
        st = self.tmp / "state" / "credentials" / "shared-client.json"
        d = json.loads(st.read_text())
        d["at_s"] = time.time() - seconds
        st.write_text(json.dumps(d))

    def test_once_shipped_it_alerts_on_a_mismatch_and_checks_hourly_until_it_matches(self):
        (self.oauth / "published").write_text("")
        self.srv.metadata["/oauth/client.json"] = {**self.pin, "redirect_uris": ["https://evil.example/cb"]}
        ok, why = credentials.tripwire()
        self.assertFalse(ok)
        self.assertIn("redirect_uris", why)
        self.assertEqual(len(self.alerts[-1][1]), 1)
        self.assertFalse(credentials.shared_client_state()[0], "route (a) is off while it doesn't match")
        credentials.tripwire()
        self.assertEqual(self.fetches(), 1, "not again within the hour")
        self.age(credentials.TRIPWIRE_RETRY_S + 1)
        self.srv.metadata["/oauth/client.json"] = dict(self.pin)
        self.assertTrue(credentials.tripwire()[0], "hourly while it didn't match")
        self.assertEqual(self.alerts[-1], ("credential:shared-client", []), "the recovery")
        self.assertTrue(credentials.shared_client_state()[0])
        self.age(credentials.TRIPWIRE_RETRY_S + 1)
        credentials.tripwire()
        self.assertEqual(self.fetches(), 2, "daily once it matches")

    def test_unreachable_isnt_a_mismatch(self):
        (self.oauth / "published").write_text("")
        for answer in ("error", 503, 429):
            with self.subTest(answer):
                self.alerts.clear()
                if answer == "error":
                    patch = mock.patch.object(credentials, "http", side_effect=credentials.Transient("connection refused"))
                else:
                    patch = mock.patch.object(credentials, "http", lambda *a, **k: (answer, {}, b""))
                with patch:
                    ok, why = credentials.tripwire(force=True)
                self.assertIsNone(ok)
                st = json.loads((self.tmp / "state" / "credentials" / "shared-client.json").read_text())
                self.assertTrue(st["unreachable"])
                self.assertEqual(self.alerts, [], "no alert for a few days")
                ok_a, why_a = credentials.shared_client_state()
                self.assertFalse(ok_a, "route (a) stays off while unverified")
                self.assertIn("couldn't be checked", why_a)
        st_path = self.tmp / "state" / "credentials" / "shared-client.json"
        st = json.loads(st_path.read_text())
        st["unreachable_since"] = time.time() - credentials.UNREACHABLE_ALERT_S - 1
        st_path.write_text(json.dumps(st))
        with mock.patch.object(credentials, "http", side_effect=credentials.Transient("connection refused")):
            credentials.tripwire(force=True)
        self.assertEqual(len(self.alerts[-1][1]), 1, "after three days in a row, it alerts")

    def test_the_result_is_tied_to_the_pin(self):
        (self.oauth / "published").write_text("")
        self.srv.metadata["/oauth/client.json"] = dict(self.pin)
        self.assertTrue(credentials.tripwire()[0])
        self.assertTrue(credentials.shared_client_state()[0])
        (self.oauth / "client.json").write_text(json.dumps({**self.pin, "client_name": "other"}))   # a new release's pin
        self.assertFalse(credentials.shared_client_state()[0], "another pin: never checked")
        credentials.tripwire()
        self.assertEqual(self.fetches(), 2, "and checked again at once")

    def test_route_a_is_checked_again_right_before_the_grant_uses_it(self):
        (self.oauth / "published").write_text("")
        self.srv.metadata["/oauth/client.json"] = dict(self.pin)
        credentials.tripwire()
        meta = {"client_id_metadata_document_supported": True}
        client = credentials.choose_client(meta, {}, "http://127.0.0.1:47615/callback", "X")
        self.assertEqual(client["route"], "a")
        self.assertEqual(self.fetches(), 2, "the grant-time check")
        self.srv.metadata["/oauth/client.json"] = {**self.pin, "redirect_uris": ["https://evil.example/cb"]}
        with mock.patch.object(credentials, "ask", return_value=""):
            self.assertIsNone(credentials.choose_client(meta, {}, "http://127.0.0.1:47615/callback", "X"),
                              "it falls through to the owner's own app (here: no client ID given)")

    def test_a_stale_check_runs_again(self):
        (self.oauth / "published").write_text("")
        self.srv.metadata["/oauth/client.json"] = dict(self.pin)
        credentials.tripwire()
        st = self.tmp / "state" / "credentials" / "shared-client.json"
        d = json.loads(st.read_text())
        d["at_s"] = time.time() - credentials.TRIPWIRE_S - 1
        st.write_text(json.dumps(d))
        credentials.tripwire()
        self.assertEqual(self.fetches(), 2)

    def test_the_command(self):
        self.srv.metadata["/oauth/client.json"] = dict(self.pin)
        with mock.patch("sys.stdout") as out:
            self.assertEqual(credentials.main(["check-published"]), 0)
        self.assertIn("matches", "".join(str(c.args[0]) for c in out.write.call_args_list))


class Boundary(unittest.TestCase):
    def test_no_account_name_but_the_organizations(self):
        """The URL is read only from oauth/client.json: no code names where the site lives."""
        for f in [*(ROOT / "bin").iterdir()]:
            if f.is_file() and f.suffix in (".py", "") and f.name != "__pycache__":
                text = f.read_text(errors="ignore")
                self.assertIsNone(re.search(r"[a-z0-9-]+\.github\.io/oauth", text), f.name)


if __name__ == "__main__":
    unittest.main()
