"""Long-running services follow the platform's code (docs/design/service-code.md).

D1: hubconf.code_rev() reads the commit HEAD names from git's files, in a main checkout, a linked worktree, with packed
refs and detached. D2: a process whose code the checkout has moved on from doesn't write the live registry index (the
11-24 reproduction: an old validation that rejects what the new code accepts, refreshing after the merge).

    bin/py -m unittest tests.test_service_code
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import gitdirs  # noqa: E402
import hubconf  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class CodeRev(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "hub"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a").write_text("1")
        git(self.repo, "add", "a")
        git(self.repo, "commit", "-qm", "one")

    def rev(self, root):
        with mock.patch.object(hubconf, "ROOT", pathlib.Path(root)):
            return hubconf.code_rev()

    def test_main_checkout_follows_its_branch(self):
        first = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.rev(self.repo), first)
        (self.repo / "a").write_text("2")
        git(self.repo, "commit", "-qam", "two")
        self.assertEqual(self.rev(self.repo), git(self.repo, "rev-parse", "HEAD"))
        self.assertNotEqual(self.rev(self.repo), first)

    def test_packed_refs(self):
        git(self.repo, "pack-refs", "--all")
        self.assertFalse((self.repo / ".git" / "refs" / "heads" / "main").exists())
        self.assertEqual(self.rev(self.repo), git(self.repo, "rev-parse", "HEAD"))

    def test_detached_head_as_upgrade_leaves_it(self):
        first = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "tag", "v1")
        (self.repo / "a").write_text("2")
        git(self.repo, "commit", "-qam", "two")
        git(self.repo, "checkout", "-q", "--detach", "v1")
        self.assertEqual(self.rev(self.repo), first)

    def test_linked_worktree_reads_its_own_head(self):
        wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(wt))
        (wt / "a").write_text("3")
        git(wt, "commit", "-qam", "side")
        self.assertEqual(self.rev(wt), git(wt, "rev-parse", "HEAD"))
        self.assertNotEqual(self.rev(wt), self.rev(self.repo))

    def test_not_a_checkout(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(self.rev(plain))
        self.assertIsNone(self.rev(self.repo / "sub-does-not-matter"), "a folder inside a repo isn't its checkout")

    def test_unborn_or_garbage_head(self):
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/nothing\n")
        self.assertIsNone(self.rev(self.repo))
        (self.repo / ".git" / "HEAD").write_text("not a sha\n")
        self.assertIsNone(self.rev(self.repo))
        (self.repo / ".git" / "HEAD").write_text("ref: refs/../../etc\n")
        self.assertIsNone(gitdirs.head(gitdirs.dirs(self.repo)))


# The 11-24 reproduction, in a process of its own with a scratch config and state: the index is first written by the
# current code; then the same process plays one started before a merge (LOADED_REV another commit than the checkout's)
# whose validation rejects the project (as the old code rejected modules using found_exit) and refreshes.
SCRIPT = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    import hubconf, registry
    stale = sys.argv[2] == "stale"
    registry.LOADED_REV = "a" * 40
    hubconf.code_rev = lambda: "a" * 40
    registry.refresh()                                   # the new code's index
    if stale:
        hubconf.code_rev = lambda: "b" * 40              # then the checkout moves on (the merge)
    old_validate = registry.validate
    registry.validate = lambda doc, stem: (["unknown field"] if stem == "proj" else old_validate(doc, stem))
    (registry.CACHE / "proj.json").unlink()              # no last valid copy to fall back on, as for a new module
    got = registry.refresh()
    print(json.dumps({"returned": sorted(got["projects"]), "errors": sorted(got["errors"])}))
""")


class OldCodeDoesNotWriteTheIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        cfg = self.tmp / "config"
        (cfg / "projects").mkdir(parents=True)
        (cfg / "hub.json").write_text(json.dumps({"v": 1, "registry_dir": "projects",
                                                  "owner": {"address": "owner", "tag": "owner"}}))
        ws = self.tmp / "ws" / "proj"
        ws.mkdir(parents=True)
        (cfg / "projects" / "proj.yaml").write_text(f"v: 1\nname: proj\npaths:\n  workspace: {ws}\n")
        self.env = {**{k: v for k, v in os.environ.items()
                       if not k.startswith(("AGENT_HUB_", "BRUNNA_")) or k in ("AGENT_HUB_PYTHON", "BRUNNA_PYTHON")},
                    "HOME": str(self.tmp / "home"), "AGENT_HUB_CONFIG": str(cfg), "AGENT_HUB_STATE": str(self.state),
                    "AGENT_HUB_LANG_UI": "en"}

    def run_as(self, which):
        r = subprocess.run([sys.executable, "-c", SCRIPT, str(BIN), which], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        index = json.loads((self.state / "shared" / "registry" / "index.json").read_text())
        return json.loads(r.stdout.splitlines()[-1]), index, r.stderr

    def test_reproduction_current_code_does_write(self):
        # the control: the same refresh by a process on the checkout's code writes what it validated
        got, index, _err = self.run_as("current")
        self.assertNotIn("proj", got["returned"])
        self.assertNotIn("proj", index["projects"])

    def test_superseded_code_leaves_the_index_alone(self):
        got, index, err = self.run_as("stale")
        self.assertNotIn("proj", got["returned"], "the old process gets its own view")
        self.assertIn("proj", index["projects"], "the index the new code wrote is left as it was")
        self.assertNotIn("proj", index["errors"])
        self.assertEqual(err.count("the live index is left to the new code"), 1, err)


if __name__ == "__main__":
    unittest.main()
