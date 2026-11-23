"""The roster gen-index-roster.sh generates: it lists every project even with no idea sandboxes (on 2026-10-24 it
once went entirely empty).

    bin/py -m unittest discover -s tests
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def project(name: str, extra: str = "") -> str:
    return (f"v: 1\nname: {name}\npaths:\n  workspace: ~/workspace/{name}\n  memory_bare: ~/git-origin/{name}-memory.git\n"
            f"  origin: (native)\n{extra}")


class IndexRoster(unittest.TestCase):
    def render(self, projects: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            hub = pathlib.Path(d)
            shutil.copy(ROOT / "hub.example.json", hub / "hub.json")
            (hub / "bin").symlink_to(ROOT / "bin")
            (hub / "projects").mkdir()
            for name, text in projects.items():
                (hub / "projects" / f"{name}.yaml").write_text(text)
            (hub / "index.md").write_text("# index\n\n## Other\n")
            subprocess.run([str(ROOT / "bin" / "gen-index-roster.sh")], env={"AGENT_HUB": str(hub), "PATH": "/usr/bin:/bin",
                                **{k: os.environ[k] for k in ("HOME", "AGENT_HUB_PYTHON", "BRUNNA_PYTHON") if k in os.environ}},
                           check=True, capture_output=True)
            text = (hub / "index.md").read_text()
        block = text.split("<!-- roster:begin")[1].split("<!-- roster:end")[0]
        return [l for l in block.splitlines() if l.startswith("| `")]

    def test_no_ideas_lists_every_project(self):
        rows = self.render({"alpha": project("alpha"), "beta": project("beta")})
        self.assertEqual(len(rows), 2)

    def test_ideas_listed_separately(self):
        rows = self.render({"alpha": project("alpha"), "idea-3": project("idea-3", "type: idea\npurpose: give it a try\n")})
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("| `alpha`"))
        self.assertTrue(rows[1].startswith("| `idea-3`"))


class FromALinkedWorktree(unittest.TestCase):
    """Run from a linked worktree of the hub with no root given, the roster is the machine's: the instance the main
    checkout's .hub-home names (on 11-23 it came back empty there and onboarding stopped before committing)."""

    def test_the_roster_is_read_through_the_main_checkout(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        main, wt, inst = d / "main", d / "wt", d / "inst"
        shutil.copytree(ROOT / "bin", main / "bin", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "locales", main / "locales")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(d), "AGENT_HUB_PYTHON": os.path.realpath(sys.executable),
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "c"], ["worktree", "add", "-q", str(wt)]):
            subprocess.run([*git, "-C", str(main), *args], env=env, check=True, capture_output=True)
        (main / ".hub-home").write_text(f"{inst}\n")
        (inst / "config" / "projects").mkdir(parents=True)
        (inst / "data").mkdir()
        shutil.copy(ROOT / "hub.example.json", inst / "config" / "hub.json")
        (inst / "config" / "projects" / "alpha.yaml").write_text(project("alpha"))
        r = subprocess.run([str(wt / "bin" / "gen-index-roster.sh")], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| `alpha`", (inst / "data" / "index.md").read_text())
        self.assertFalse((wt / "index.md").exists())


if __name__ == "__main__":
    unittest.main()
