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
        script = next(s["run"] for s in job["steps"] if s.get("name") == "Stage only the client document")
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
        for setup in (link, evil, moved, secret):
            with self.subTest(setup.__name__):
                self.assertNotEqual(self.stage(setup)[0], 0)

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

    def test_once_shipped_it_checks_daily_and_alerts_on_a_mismatch(self):
        (self.oauth / "published").write_text("")
        self.srv.metadata["/oauth/client.json"] = {**self.pin, "redirect_uris": ["https://evil.example/cb"]}
        ok, why = credentials.tripwire()
        self.assertFalse(ok)
        self.assertIn("redirect_uris", why)
        self.assertEqual(self.alerts[-1], ("credential:shared-client", [self.alerts[-1][1][0]]))
        self.assertFalse(credentials.shared_client_state()[0], "route (a) is off while it doesn't match")
        credentials.tripwire()
        self.assertEqual(self.fetches(), 1, "at most once a day")
        self.srv.metadata["/oauth/client.json"] = {**self.pin, "logo_uri": "https://x/y.png"}   # other fields may differ
        self.assertTrue(credentials.tripwire(force=True)[0])
        self.assertEqual(self.alerts[-1], ("credential:shared-client", []), "the recovery")
        self.assertTrue(credentials.shared_client_state()[0])

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
