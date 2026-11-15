"""The registry's enforce.capabilities and the security fields manifest.update cannot change
(docs/design/capabilities.md).

    bin/py -m unittest discover -s tests
"""
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

try:
    import registry  # noqa: E402  needs PyYAML
except ImportError:
    registry = None

CATALOG = {"gpu": {"risk": "low", "default_for": ["idea"]}}


def doc(**extra):
    d = {"v": 1, "name": "idea-7", "type": "idea", "purpose": "x", "paths": {"workspace": "/tmp"}}
    d.update(extra)
    return d


@unittest.skipIf(registry is None, "needs PyYAML")
class Capabilities(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(registry, "capabilities", return_value=CATALOG)
        p.start()
        self.addCleanup(p.stop)

    def errs(self, d):
        return registry.validate_enforce(d)

    def test_work_needs_sandbox_and_a_bool(self):
        self.assertEqual(self.errs(doc(type="project", enforce={"sandbox": True, "work": True})), [])
        self.assertEqual(self.errs(doc(enforce={"sandbox": False, "work": False})), [])
        self.assertTrue(self.errs(doc(enforce={"sandbox": False, "work": True})))
        self.assertTrue(self.errs(doc(enforce={"work": True})))
        self.assertTrue(self.errs(doc(enforce={"sandbox": True, "work": "yes"})))

    def test_work_is_platform_owned(self):
        self.assertIn("enforce", registry.PLATFORM_OWNED)

    def test_declared_capability(self):
        self.assertEqual(self.errs(doc(enforce={"sandbox": True, "capabilities": ["gpu"]})), [])

    def test_unknown_capability(self):
        self.assertTrue(self.errs(doc(enforce={"sandbox": True, "capabilities": ["camera"]})))

    def test_needs_sandbox(self):
        self.assertTrue(self.errs(doc(enforce={"sandbox": False, "capabilities": ["gpu"]})))
        self.assertTrue(self.errs(doc(enforce={"capabilities": ["gpu"]})))

    def test_projects_keep_capabilities_too(self):
        # 10-25 m10251626: after upgrading to a full project, capabilities the owner approved stay; the approval
        # record is the platform-owned enforce, type is not looked at
        self.assertEqual(self.errs(doc(type="project", enforce={"sandbox": True, "capabilities": ["gpu"]})), [])
        self.assertTrue(self.errs(doc(type="project", enforce={"sandbox": False, "capabilities": ["gpu"]})))

    def test_shape(self):
        self.assertTrue(self.errs(doc(enforce={"sandbox": True, "capabilities": "gpu"})))
        self.assertTrue(self.errs(doc(enforce={"sandbox": True, "other": 1})))
        self.assertEqual(self.errs(doc(enforce={"sandbox": True})), [])

    def test_git_through_the_platform_needs_a_sandbox_and_only_says_platform(self):
        """docs/design/git-broker.md, "Rollout per project": enforce.git: platform, owner-only, needs the sandbox it is
        rendered into"""
        self.assertEqual(self.errs(doc(enforce={"sandbox": True, "git": "platform"})), [])
        self.assertTrue(self.errs(doc(enforce={"git": "platform"})))
        self.assertTrue(self.errs(doc(enforce={"sandbox": False, "git": "platform"})))
        for bad in ("direct", True, None, ""):
            self.assertTrue(self.errs(doc(enforce={"sandbox": True, "git": bad})), bad)

    def test_git_through_the_platform_is_refused_for_an_exempt_project(self):
        """its services and jobs get no sandbox at all, so nothing would render the fence (step 1's second review)"""
        on = doc(enforce={"sandbox": True, "git": "platform"})
        with mock.patch.object(registry.hubconf, "get",
                               lambda k, d=None: [on["name"]] if k == "service_sandbox_exempt" else d):
            self.assertTrue(self.errs(on))
            self.assertEqual(self.errs(doc(enforce={"sandbox": True})), [])

    def test_catalog_needs_profile_file(self):
        mock.patch.stopall()
        with mock.patch.object(registry.hubconf, "get", return_value={"_comment": "x", "gpu": {}, "nofile": {}}):
            self.assertIn("gpu", registry.capabilities())
            self.assertNotIn("nofile", registry.capabilities())
            self.assertNotIn("_comment", registry.capabilities())


@unittest.skipIf(registry is None, "needs PyYAML")
class PlatformOwned(unittest.TestCase):
    cur = doc(enforce={"sandbox": True, "capabilities": ["gpu"]}, privacy={"reflection": "stats_only"})

    def test_unchanged_is_fine(self):
        self.assertEqual(registry.platform_owned_changes(dict(self.cur, mailbox={"escalate": True}), self.cur), [])

    def test_each_field_is_locked(self):
        for k, v in [("enforce", {"sandbox": False}), ("enforce", {"sandbox": True, "capabilities": ["gpu", "x"]}),
                     ("type", "project"), ("network", ["evil.example"]), ("allow_write", ["~/"]),
                     ("privacy", {"reflection": "include"}), ("paths", {"workspace": "~"})]:
            self.assertTrue(registry.platform_owned_changes(dict(self.cur, **{k: v}), self.cur), k)

    def test_a_letter_cant_switch_git_through_the_platform_on_or_off(self):
        on = dict(self.cur, enforce={**self.cur["enforce"], "git": "platform"})
        self.assertTrue(registry.platform_owned_changes(on, self.cur))
        self.assertTrue(registry.platform_owned_changes(self.cur, on))

    def test_removing_counts_as_change(self):
        new = {k: v for k, v in self.cur.items() if k != "privacy"}
        self.assertTrue(registry.platform_owned_changes(new, self.cur))


if __name__ == "__main__":
    unittest.main()
