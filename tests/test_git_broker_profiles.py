"""The git broker's profiles as text (docs/design/git-broker.md, "Step 2 build list", slice a), on any OS: P1 (the
git-dir writer) carries _isolation whole and every deny by name, no temp area and no fence, its denied names, pins and
the exec guard last, and is never built without the project's read rules; the init profile; P2 is the service profile
plus the exec guard; the socket rule renders only for a switched-on project; and with nothing switched on, _isolation's
and service_profile's output is step 1's (a golden built from the code before slice a, at fake paths), as is
sandboxed-run's environment.

    bin/run-tests   (or brunna test)
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
BIN = TESTS.parent / "bin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(TESTS))
import git_broker_fixture as fx  # noqa: E402
import names  # noqa: E402
import policy  # noqa: E402
import seatbelt  # noqa: E402

GOLDEN = TESTS / "fixtures" / "git-broker-profiles-off.json"
GIT = ["/fake/git/bin/git", "/fake/git/libexec/git-core/"]
ON = {"sandbox": True, "git": "platform"}
GD = pathlib.Path("/fake/workspace/proj/.git")


def pol_on(**kw):
    doc = {"name": "proj", "paths": {"workspace": "/fake/workspace/proj"}, "enforce": ON}
    return fx.fake_policy(git_fence=policy.git_fence(doc, ["/fake/workspace/proj"]),
                          git_broker=policy.git_broker(doc, "proj", pathlib.Path("/fake/hub/.state")), **kw)


def lines(text: str) -> list[str]:
    return [x for x in text.splitlines() if x and not x.startswith(";")]


class StepOneUnchanged(unittest.TestCase):
    def test_off_renders_as_step_one(self):
        self.assertEqual(fx.off_renderings(), json.loads(GOLDEN.read_text()))

    def test_the_socket_rule_only_when_switched_on(self):
        with fx.fixed_machine():
            off = seatbelt.service_profile(fx.fake_policy(), [pathlib.Path("/fake/workspace/proj")])
            on = seatbelt.service_profile(pol_on(), [pathlib.Path("/fake/workspace/proj")])
        self.assertNotIn("unix-socket", off)
        self.assertIn('(deny network-outbound (remote unix-socket (subpath "/fake/hub/.state/git-broker")))', on)
        self.assertIn('(allow network-outbound (remote unix-socket (path-literal '
                      '"/fake/hub/.state/git-broker/proj/service")))', on)
        self.assertNotIn("proj/session", on)

    def test_the_policy_carries_the_broker_only_when_switched_on(self):
        self.assertIsNone(policy.git_broker({"enforce": {"sandbox": True}}, "proj"))
        gb = policy.git_broker({"paths": {"workspace": "/fake/w"}, "enforce": ON}, "proj", pathlib.Path("/fake/s"))
        self.assertEqual(gb["service"], "/fake/s/git-broker/proj/service")
        self.assertEqual(gb["session"], "/fake/s/git-broker/proj/session")
        self.assertEqual(gb["nonce_dir"], "/fake/w/.hub-run/git")
        self.assertTrue(gb["wrapper_dir"].endswith("/bin/git-wrapper"))

    def test_sandboxed_run_puts_the_wrapper_first_only_when_switched_on(self):
        loader = importlib.machinery.SourceFileLoader("sandboxed_run", str(BIN / "sandboxed-run"))
        spec = importlib.util.spec_from_loader("sandboxed_run", loader)
        sr = importlib.util.module_from_spec(spec)
        loader.exec_module(sr)
        import hubconf
        import isolation
        seen = {}
        for gb in (None, {"wrapper_dir": "/fake/hub/bin/git-wrapper", "service": "/fake/s/git-broker/proj/service"}):
            pol = {**fx.fake_policy(), "git_broker": gb, "env": {"UV_CACHE_DIR": "/fake/c"}}
            env = {"PATH": "/usr/bin:/bin", "HOME": "/fake/home"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(hubconf, "projects", lambda: {"proj": {"paths": {}}}), \
                    mock.patch.object(policy, "policy", lambda *a: pol), \
                    mock.patch.object(isolation, "can_sandbox", lambda: None), \
                    mock.patch.object(isolation, "unmet", lambda p: None), \
                    mock.patch.object(os, "chdir", lambda d: None), \
                    mock.patch.object(isolation, "exec_service", lambda *a: seen.__setitem__(bool(gb), dict(os.environ))), \
                    contextlib.redirect_stderr(io.StringIO()):
                sr.main(["--project", "proj", "--cwd", "/fake/w", "--", "true"])
        self.assertEqual(seen[False], {"PATH": "/usr/bin:/bin", "HOME": "/fake/home", "UV_CACHE_DIR": "/fake/c"})
        self.assertEqual(seen[True]["PATH"], "/fake/hub/bin/git-wrapper:/usr/bin:/bin")
        self.assertEqual(names.env("GIT_SOCKET", environ=seen[True]), "/fake/s/git-broker/proj/service")


class WriterProfile(unittest.TestCase):
    def p1(self, pol=None):
        with fx.fixed_machine():
            pol = pol or pol_on()
            text = seatbelt.git_writer_profile(pol, [GD], [pathlib.Path("/fake/workspace/proj"),
                                                           pathlib.Path("/fake/hub/.shared")], GIT)
            iso = seatbelt._isolation({**pol, "git_fence": None}, [GD], (), [pathlib.Path("/fake/workspace/proj"),
                                                                           pathlib.Path("/fake/hub/.shared")], temp=False)
            fence = seatbelt.git_fence_rules(pol)
            mounts = seatbelt.mount_denies()
        return text, iso, fence, mounts

    def test_p1_carries_isolation_whole(self):
        """Every line of _isolation but its write allow is in P1, so a deny added there later reaches P1 too."""
        text, iso, _f, _m = self.p1()
        allow = [x for x in iso if x.startswith("(allow file-write*")]
        self.assertEqual(len(allow), 1)
        got = text.splitlines()
        for line in iso:
            self.assertIn(line, got)

    def test_each_deny_by_name(self):
        text, _iso, _f, mounts = self.p1()
        got = text.splitlines()
        for line in [*(m for m in mounts if not m.startswith(";")), f'(deny file-read* file-write* (regex #"{seatbelt.DISK_DEVICES}"))',
                     "(deny lsopen)", "(deny network-outbound (remote ip))", "(deny network-outbound (remote unix-socket))",
                     "(deny network-inbound (local ip))", "(deny network-bind)"]:
            self.assertIn(line, got)
        self.assertTrue(any(x.startswith("(deny mach-lookup") and "com.apple.SecurityServer" in x for x in got))

    def test_no_temp_area_and_no_fence(self):
        text, _iso, fence, _m = self.p1()
        allow = next(x for x in text.splitlines() if x.startswith("(allow file-write*"))
        self.assertEqual(allow, f'(allow file-write* (subpath "{GD}") '
                                + " ".join(f'(literal "{d}")' for d in seatbelt.DEVICES) + ")")
        self.assertTrue(fence)
        for rule in fence:
            if not rule.startswith(";"):
                self.assertNotIn(rule, text)

    def test_denied_names_pins_and_the_exec_guard_last(self):
        text, _iso, _f, _m = self.p1()
        got = lines(text)
        for name in ("[cC][oO][nN][fF][iI][gG]", "[hH][oO][oO][kK][sS](/|$)", "[iI][nN][fF][oO]/[aA][tT][tT][rR]",
                     "[sS][hH][aA][lL][lL][oO][wW]", "[iI][nN][fF][oO]/[gG][rR][aA][fF][tT][sS]",
                     "[oO][bB][jJ][eE][cC][tT][sS]/[iI][nN][fF][oO]/[aA][lL][tT]",
                     "[wW][oO][rR][kK][tT][rR][eE][eE][sS]/[^/]+/([cC][oO][mM][mM][oO][nN][dD][iI][rR]|"):
            self.assertIn(f'(regex #"^{re.escape(str(GD))}/{name}', text, name)
        pins = next(x for x in got if x.startswith("(deny file-write-unlink"))
        for p in ("[oO][bB][jJ][eE][cC][tT][sS]", "[rR][eE][fF][sS]", "[lL][oO][gG][sS]", "[iI][nN][fF][oO]",
                  "[wW][oO][rR][kK][tT][rR][eE][eE][sS]", "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c
                                                                  for c in names.current("repo_dir"))):
            self.assertIn(p, pins)
        self.assertEqual(got[-2:], ["(deny process-exec)",
                                    '(allow process-exec (literal "/fake/git/bin/git") (subpath '
                                    '"/fake/git/libexec/git-core"))'])
        i_guard, i_names = got.index("(deny process-exec)"), got.index(pins)
        self.assertGreater(i_guard, i_names)

    def test_never_without_the_projects_read_rules(self):
        for bad in ({"workspace": "/fake/w"}, {k: v for k, v in fx.fake_policy().items() if k != "deny_read"}):
            with self.assertRaises(ValueError):
                seatbelt.git_writer_profile(bad, [GD], [], GIT)
            with self.assertRaises(ValueError):
                seatbelt.git_init_profile(bad, GD, [], GIT)

    def test_the_exec_guard_needs_gits_programs(self):
        with self.assertRaises(ValueError):
            seatbelt.exec_guard([])


class InitAndMaterialiser(unittest.TestCase):
    def test_the_init_profile(self):
        with fx.fixed_machine():
            text = seatbelt.git_init_profile(pol_on(), GD, [pathlib.Path("/fake/workspace/proj")], GIT)
        got = lines(text)
        self.assertIn(f'(allow file-write* (subpath "{GD}") ' + " ".join(f'(literal "{d}")' for d in seatbelt.DEVICES)
                      + ")", got)
        self.assertNotIn("[cC][oO][nN][fF][iI][gG]", text, "init must write config")
        self.assertIn("(deny network-outbound (remote ip))", got)
        self.assertEqual(got[-2], "(deny process-exec)")

    def test_p2_is_the_service_profile_plus_the_guard(self):
        with fx.fixed_machine():
            pol = pol_on()
            dirs = [pathlib.Path("/fake/workspace/proj")]
            p2 = seatbelt.materialiser_profile(pol, dirs, GIT)
            svc = seatbelt.service_profile(pol, dirs)
            guard = seatbelt.exec_guard(GIT)
        self.assertEqual(p2, svc + "\n".join(guard) + "\n")
        self.assertIn(seatbelt.git_fence_rules(pol)[1], p2)


if __name__ == "__main__":
    unittest.main()
