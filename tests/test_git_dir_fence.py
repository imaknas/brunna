"""The git fence (docs/design/git-broker.md, "The fence", r2 B2; step 1 of its order of work): for a project whose git goes
through the platform (the registry's enforce.git: platform, owner-only), no sandbox of it writes a git dir or makes one,
anywhere it may write. The neutral port (policy.git_fence), its Seatbelt rendering in both profile kinds (services and
command jobs, cap-run), Landlock's refusal, and the fence itself under the real Seatbelt profile with real git: every
planting route the design measured refused, each with an unfenced positive control, and git's reads still working.
Nothing changes for a project not switched on.

    bin/py -m unittest tests.test_git_dir_fence
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import isolation  # noqa: E402
import landlock  # noqa: E402
import names  # noqa: E402
import policy  # noqa: E402
import seatbelt  # noqa: E402

ON = {"name": "proj", "enforce": {"sandbox": True, "git": "platform"}}


def pol_for(ws: pathlib.Path, doc: dict, **kw) -> dict:
    return {"workspace": ws, "write": [], "other_projects": [], "deny_read": [], "credentials": [],
            "credential_files": [], "git_fence": policy.git_fence(doc, [ws]), **kw}


class Port(unittest.TestCase):
    def test_off_unless_switched_on_with_a_sandbox(self):
        for doc in ({}, {"enforce": {"sandbox": True}}, {"enforce": {"git": "platform"}},
                    {"enforce": {"sandbox": False, "git": "platform"}}, {"enforce": {"sandbox": True, "git": "x"}}):
            self.assertFalse(policy.git_platform(doc), doc)
            self.assertIsNone(policy.git_fence(doc, ["/w"]), doc)
        self.assertTrue(policy.git_platform(ON))

    def test_what_it_names(self):
        f = policy.git_fence(ON, ["/w", "/rt", None])
        self.assertEqual((f["names"], f["files"]), ([".git"], ["HEAD"]))
        dirs = [f"{r}/.git/{n}" for r in ("/w", "/rt") for n in names.all("repo_dir")]
        self.assertEqual(f["repo_dirs"], dirs)
        self.assertEqual(f["markers"], [{"root": f"{d}/cache/uv", "glob": "sdists-v*/.git"} for d in dirs])
        # the marker's root is where the project's uv cache is (policy.cache_env)
        ws = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        uv = policy.cache_env(ws)["UV_CACHE_DIR"]
        self.assertIn(uv, [m["root"] for m in policy.git_fence(ON, [ws])["markers"]])

    def test_the_policy_carries_it_only_when_switched_on(self):
        ws = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        for enforce, on in (({"sandbox": True}, False), ({"sandbox": True, "git": "platform"}, True)):
            doc = {"name": "proj", "paths": {"workspace": str(ws)}, "enforce": enforce}
            got = policy.policy("proj", doc, {"projects": {"proj": doc}})["git_fence"]
            self.assertEqual(got is not None, on, enforce)
            if on:
                self.assertIn(str(ws / ".git" / names.current("repo_dir")), got["repo_dirs"])


class Rendering(unittest.TestCase):
    def setUp(self):
        self.ws = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_off_renders_nothing_at_all(self):
        """No project switched on: every profile is byte-identical to one built without the key."""
        off = pol_for(self.ws, {"enforce": {"sandbox": True}})
        self.assertIsNone(off["git_fence"])
        bare = {k: v for k, v in off.items() if k != "git_fence"}
        self.assertEqual(seatbelt.service_profile(off, [self.ws], 18000), seatbelt.service_profile(bare, [self.ws], 18000))
        self.assertEqual(seatbelt.profile(off, "gpu"), seatbelt.profile(bare, "gpu"))
        self.assertEqual(seatbelt.git_fence_rules(off), [])

    def test_on_in_both_profile_kinds(self):
        on = pol_for(self.ws, ON)
        rules = seatbelt.git_fence_rules(on)
        self.assertTrue(rules)
        for text in (seatbelt.service_profile(on, [self.ws]), seatbelt.profile(on, "gpu")):
            for r in rules:
                self.assertIn(r, text)

    def test_the_platform_folder_is_kept_out_never_allowed_back(self):
        """An allow after the deny would reopen the control-file denies under it too: only denies here."""
        rules = [r for r in seatbelt.git_fence_rules(pol_for(self.ws, ON)) if not r.startswith(";")]
        self.assertTrue(all(r.startswith("(deny ") for r in rules), rules)
        self.assertIn(f'(require-not (subpath "{self.ws}/.git/{names.current("repo_dir")}"))', rules[0])

    def test_the_head_rule_spares_folders(self):
        rules = seatbelt.git_fence_rules(pol_for(self.ws, ON))
        self.assertIn('(deny file-write* (require-all (regex #"/(HEAD)$") (require-not (vnode-type DIRECTORY))))', rules)


class Adapters(unittest.TestCase):
    def test_seatbelt_declares_it_and_landlock_doesnt(self):
        self.assertIn("git-dirs-fenced", isolation.GUARANTEES)
        self.assertIn("git-dirs-fenced", seatbelt.guarantees())
        with mock.patch.object(landlock, "abi", lambda: 6):
            self.assertNotIn("git-dirs-fenced", landlock.guarantees())

    def test_a_fence_policy_is_refused_where_the_adapter_cant_fence(self):
        """Linux: never run unfenced; the command isn't run at all."""
        ex = mock.Mock()
        fake = type("A", (), {"can_sandbox": staticmethod(lambda: None), "exec_service": ex,
                              "guarantees": staticmethod(lambda: set(isolation.REQUIRED))})
        with mock.patch.object(isolation, "adapter", lambda: ("landlock", fake)):
            self.assertIsNone(isolation.unmet({"git_fence": None}))
            self.assertIn("git-dirs-fenced", isolation.unmet({"git_fence": {"names": [".git"]}}))
            with self.assertRaises(OSError):
                isolation.exec_service({"git_fence": {"names": [".git"]}}, [], None, ["true"])
            ex.assert_not_called()
            isolation.exec_service({"git_fence": None}, [], None, ["true"])
            ex.assert_called_once()


ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a",
       "GIT_COMMITTER_EMAIL": "a@example.com", "LANG": "C"}
PF = f".git/{names.current('repo_dir')}"   # the repo's platform folder
UV = f"{PF}/cache/uv"
MARKER = f"mkdir -p {UV}/sdists-v9 && : > {UV}/sdists-v9/.git"

# each: a shell snippet that exits 0 when the write it tries lands. Refused under the fence; each lands without it.
REFUSED = {
    "config": "echo x >> .git/config",
    "git-config": "git config x.y z",
    "hook": "echo x > .git/hooks/post-index-change",
    "info-attributes": "mkdir -p .git/info && echo '* filter=x' > .git/info/attributes",
    "nested-repo": "mkdir sub && mkdir sub/.git",
    "nested-repo-upper": "mkdir sub && mkdir sub/.GIT",
    "nested-repo-mixed": "mkdir sub && mkdir sub/.Git",
    "gitfile": "mkdir sub && echo 'gitdir: ../.git' > sub/.git",
    "init-workspace": "git init -q sub",
    "init-temp": 'git init -q "$T/r"',
    "init-bare-temp": 'git init -q --bare "$T/x.git"',       # no .git in the name: the HEAD rule refuses it
    "built-in-temp-moved-in": 'mkdir -p "$T/b/.git" && mv "$T/b" built',
    "head-file": "echo 'ref: refs/heads/main' > HEAD",
    "head-folded": "mkdir d && echo x > d/head",
    "head-temp": 'mkdir -p "$T/shape" && echo x > "$T/shape/HEAD"',
    "head-symlink": "mkdir y && ln -s ../f y/HEAD",
    "head-hardlink": "mkdir t && ln f t/HEAD",
    "head-renamed-onto": "mkdir r && echo x > r/h && mv r/h r/HEAD",
    "head-copied": "mkdir u && cp f u/HEAD",
    "move-git-away": "mv .git g",
    "hardlink-to-config": "ln .git/config hl",
    "hardlink-to-config-in-platform-folder": f"ln .git/config {PF}/hl",
    "symlink-written-through": f"ln -s ../config {PF}/s && echo y >> {PF}/s",
    "platform-folder-renamed": f"mv {PF} .git/elsewhere",
    "platform-folder-removed": f"rmdir {PF}",
    "git-below-platform-folder": f"mkdir -p {PF}/x/.git",
    "worktree-gitfile-rewritten": "echo 'gitdir: /tmp' > wt/.git",
    "commit": "git commit -q --allow-empty -m x",
    "uv-bucket-moved-out": f"{MARKER} && mv {UV}/sdists-v9 moved",
    "uv-cache-moved-out": f"{MARKER} && mv {PF}/cache moved",
    "marker-elsewhere-in-cache": f"mkdir -p {UV}/other && : > {UV}/other/.git",
    "marker-hardlinked-as-git": f"{MARKER} && mkdir m && ln {UV}/sdists-v9/.git m/.git",
}
# what still works under the fence
WORKS = {
    "status": "git status --porcelain >/dev/null",
    "log": "git log --oneline >/dev/null",
    "diff": "git diff --exit-code",
    "worktree-status": "git -C wt status --porcelain >/dev/null && git -C wt log -1 >/dev/null",
    "outbox": f"mkdir -p {PF}/outbox && echo y > {PF}/outbox/x",
    "platform-folder-mkdir-p": f"mkdir -p {PF}",
    "uv-marker": MARKER,
    "a-folder-named-head": "mkdir -p e/HEAD && echo x > e/HEAD/f",
    "names-that-only-start-with-git": "echo x > .gitignore && echo y > .gitattributes && mkdir -p .github && echo z > .github/w",
    # the test runner's way: the index and the object folder in temp, the repo's objects as an alternate
    "export-to-temp": 'GIT_INDEX_FILE="$T/idx" GIT_OBJECT_DIRECTORY="$T/obj" '
                      'GIT_ALTERNATE_OBJECT_DIRECTORIES="$PWD/.git/objects" '
                      'sh -c \'mkdir -p "$T/obj" && git read-tree HEAD && echo n > f2 && git add f2 && git write-tree\''
                      ' >/dev/null',
}


@unittest.skipUnless(seatbelt.can_sandbox() is None, "needs Seatbelt (sandbox-exec)")
class Seatbelt(unittest.TestCase):
    """Under the platform's real service profile, as a sandboxed project's service or job runs (step 1 renders the fence
    there and in cap-run only)."""

    def fixture(self, platform_folder=True):
        tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ws, t, home = tmp / "ws", tmp / "t", tmp / "home"
        for d in (t, home):
            d.mkdir()
        env = {**ENV, "HOME": str(home)}

        def git(*a):
            subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True, env=env)
        subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True, capture_output=True, env=env)
        (ws / "f").write_text("x\n")
        (ws / ".gitignore").write_text("wt/\n")
        git("add", "f", ".gitignore")
        git("commit", "-q", "-m", "init")
        git("worktree", "add", "-q", "-b", "w", str(ws / "wt"))   # a linked worktree, made outside the sandbox
        if platform_folder:   # the platform makes it (the rebuild, step 2): the sandbox can't remove or rename it
            (ws / PF).mkdir()
        # made outside any fence (the owner, another project, an unfenced session): see Limits
        (t / "pre" / ".git").mkdir(parents=True)
        (t / "pre" / ".git" / "config").write_text("[core]\n")
        (t / "preshape").mkdir()
        (t / "preshape" / "HEAD").write_text("ref: refs/heads/main\n")
        return tmp, ws, t, {**env, "T": str(t)}

    def run_in(self, snippet: str, fenced: bool, platform_folder=True):
        tmp, ws, t, env = self.fixture(platform_folder)
        with mock.patch.object(seatbelt, "TMP", [str(t)]):
            prof = seatbelt.service_profile(pol_for(ws, ON if fenced else {"enforce": {"sandbox": True}}), [ws])
        r = subprocess.run(["/usr/bin/sandbox-exec", "-p", prof, "/bin/sh", "-c", snippet], cwd=ws, env=env,
                           capture_output=True, text=True, timeout=60)
        return r, ws, t

    def test_every_planting_route_is_refused_and_lands_without_the_fence(self):
        for name, snippet in REFUSED.items():
            with self.subTest(name):
                r, _ws, _t = self.run_in(snippet, fenced=False)
                self.assertEqual(r.returncode, 0, f"positive control: {r.stderr}")
                r, _ws, _t = self.run_in(snippet, fenced=True)
                self.assertNotEqual(r.returncode, 0, f"landed under the fence: {r.stdout} {r.stderr}")

    def test_nothing_was_planted(self):
        """The refusals left no git dir, gitfile or HEAD file behind."""
        r, ws, t = self.run_in("; ".join(REFUSED.values()), fenced=True)
        planted = [p for root in (ws, t) for p in root.rglob("*")
                   if (p.name.lower() == ".git" or (p.name.lower() == "head" and not p.is_dir()))
                   and not p.is_relative_to(ws / ".git") and p not in (ws / "wt" / ".git", t / "pre" / ".git",
                                                                       t / "preshape" / "HEAD")]
        self.assertEqual(planted, [], r.stderr)
        self.assertEqual((ws / ".git" / "config").read_text().count("x.y"), 0)
        self.assertTrue((ws / PF).is_dir())

    def test_git_reads_and_the_platform_folder_still_work(self):
        for name, snippet in WORKS.items():
            with self.subTest(name):
                r, ws, _t = self.run_in(snippet, fenced=True)
                self.assertEqual(r.returncode, 0, r.stderr)
        r, ws, _t = self.run_in("git status --porcelain", fenced=True)
        self.assertFalse((ws / ".git" / "index.lock").exists(), "status's opportunistic refresh left a lock")

    def test_a_missing_platform_folder(self):
        """The sandbox may make it (a folder, as today's outbox does), but a link there gains nothing: what is written
        through it is matched where it lands, in the git dir."""
        r, ws, _t = self.run_in(f"mkdir {PF} && echo y > {PF}/x", fenced=True, platform_folder=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        for target in ("hooks", "."):   # the git dir's hooks, the git dir itself
            with self.subTest(target):
                snippet = f"ln -s {target} {PF} && echo x > {PF}/pre-commit"
                r, ws, _t = self.run_in(snippet, fenced=False, platform_folder=False)
                self.assertEqual(r.returncode, 0, f"positive control: {r.stderr}")
                r, ws, _t = self.run_in(snippet, fenced=True, platform_folder=False)
                self.assertNotEqual(r.returncode, 0, r.stderr)
                self.assertFalse((ws / ".git" / "hooks" / "pre-commit").exists())
                self.assertFalse((ws / ".git" / "pre-commit").exists())

    def test_limits(self):
        """A rename is checked only at its two ends: a folder already holding a .git or a HEAD file, made outside every
        fence (the owner, another project, a session of this one until each runtime renders the fence: step 4), is
        moved in whole. The fence keeps a fenced writer from building one; it can't stop moving one that exists. The
        HEAD shape is what safe.bareRepository=explicit is for (r2 B2, doctor's precondition row); a nested .git is
        what fencing every writer is for. If this ever fails, something refuses it, and the design doc should say so."""
        r, ws, _t = self.run_in('mv "$T/pre" landed && mv "$T/preshape" shape', fenced=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((ws / "landed" / ".git" / "config").is_file())
        self.assertTrue((ws / "shape" / "HEAD").is_file())


if __name__ == "__main__":
    unittest.main()
