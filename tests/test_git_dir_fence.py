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
PF2 = f".git/{names.other('repo_dir')}"    # its other spelling (both are made by the platform in the fixture)
UV = f"{PF}/cache/uv"
MARKER = f"mkdir -p {UV}/sdists-v9 && : > {UV}/sdists-v9/.git"
# renamex_np(a, b, RENAME_SWAP): a swap is checked with each end's existing type (step 1's second cold review)
SWAP = ('"$PY" -c \'import ctypes, sys; l = ctypes.CDLL("/usr/lib/libSystem.B.dylib"); '
        'sys.exit(0 if l.renamex_np(sys.argv[1].encode(), sys.argv[2].encode(), 2) == 0 else 1)\'')


def per_spelling(pf: str) -> dict:
    """The platform folder's rules, probed under one spelling."""
    uv = f"{pf}/cache/uv"
    marker = f"mkdir -p {uv}/sdists-v9 && : > {uv}/sdists-v9/.git"
    return {
        "hardlink-to-config-in-platform-folder": f"ln .git/config {pf}/hl",
        "symlink-written-through": f"ln -s ../config {pf}/s && echo y >> {pf}/s",
        "platform-folder-renamed": f"mv {pf} .git/elsewhere",
        "platform-folder-removed": f"rmdir {pf}",
        "git-below-platform-folder": f"mkdir -p {pf}/x/.git",
        "uv-bucket-moved-out": f"{marker} && mv {uv}/sdists-v9 moved",
        "uv-folder-moved-out": f"mkdir -p {uv} && mv {uv} moved",
        "uv-cache-moved-out": f"{marker} && mv {pf}/cache moved",
        "marker-elsewhere-in-cache": f"mkdir -p {uv}/other && : > {uv}/other/.git",
        "marker-hardlinked-as-git": f"{marker} && mkdir m && ln {uv}/sdists-v9/.git m/.git",
        "marker-as-a-folder": f"mkdir -p {uv}/sdists-v9/.git",
        "marker-as-a-link": f"mkdir -p {uv}/sdists-v9 && ln -s ../../../.. {uv}/sdists-v9/.git",
        "folder-moved-onto-marker": f'mkdir -p "$T/m/objects" {uv}/sdists-v9 && mv "$T/m" {uv}/sdists-v9/.git',
        "cache-made-as-a-link": f"ln -s .. {pf}/cache",
        "uv-made-as-a-link": f"mkdir -p {pf}/cache && ln -s .. {uv}",
        "bucket-made-as-a-link": f"mkdir -p {uv} && ln -s ../.. {uv}/sdists-v9",
    }


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
    "head-rename-swap": f"mkdir -p s/HEAD && echo 'ref: refs/heads/main' > s/g && {SWAP} s/g s/HEAD && test -f s/HEAD",
    "head-rename-swap-in-temp": f'mkdir -p "$T/s/HEAD" && echo x > "$T/s/g" && {SWAP} "$T/s/g" "$T/s/HEAD"'
                                f' && test -f "$T/s/HEAD"',
    "move-git-away": "mv .git g",
    "hardlink-to-config": "ln .git/config hl",
    "worktree-gitfile-rewritten": "echo 'gitdir: /tmp' > wt/.git",
    "commit": "git commit -q --allow-empty -m x",
    **{f"{k}{'' if pf == PF else ' (other spelling)'}": v for pf in (PF, PF2) for k, v in per_spelling(pf).items()},
}
# the chain the second cold review ran (a HEAD file by a swap, then a git-dir shape moved onto uv's marker name):
# without the fence it lands, and git run outside (the owner's, safe.bareRepository=explicit included) enters it
CHAIN = (f'mkdir -p "$T/m/objects" "$T/m/refs/heads" "$T/m/HEAD" && printf "[core]\\n\\tbare = false\\n" > "$T/m/config"'
         f' && echo "ref: refs/heads/main" > "$T/m/g" && {SWAP} "$T/m/g" "$T/m/HEAD" && rm -rf "$T/m/g"'
         f' && mkdir -p {UV}/sdists-v1/sub && mv "$T/m" {UV}/sdists-v1/.git')
# what still works under the fence
WORKS = {
    "status": "git status --porcelain >/dev/null",
    "log": "git log --oneline >/dev/null",
    "diff": "git diff --exit-code",
    "worktree-status": "git -C wt status --porcelain >/dev/null && git -C wt log -1 >/dev/null",
    "outbox": f"mkdir -p {PF}/outbox && echo y > {PF}/outbox/x",
    "platform-folder-mkdir-p": f"mkdir -p {PF} {PF2}",
    "uv-marker": MARKER,
    "uv-marker-other-spelling": f"mkdir -p {PF2}/cache/uv/sdists-v9 && : > {PF2}/cache/uv/sdists-v9/.git",
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
    """Under the platform's real profiles: the service profile, as a sandboxed project's service or job runs, and the
    cap-run profile (step 1 renders the fence in those two only)."""

    def fixture(self, folders=(PF, PF2)):
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
        for pf in folders:   # the platform makes them (the rebuild, step 2): never made, removed or renamed from inside
            (ws / pf).mkdir()
        # made outside any fence (the owner, another project, an unfenced session): see Limits
        (t / "pre" / ".git").mkdir(parents=True)
        (t / "pre" / ".git" / "config").write_text("[core]\n")
        (t / "preshape").mkdir()
        (t / "preshape" / "HEAD").write_text("ref: refs/heads/main\n")
        return tmp, ws, t, {**env, "T": str(t), "PY": sys.executable}

    def profile(self, ws, t, fenced: bool, kind: str = "service"):
        pol = pol_for(ws, ON if fenced else {"enforce": {"sandbox": True}})
        with mock.patch.object(seatbelt, "TMP", [str(t)]):
            return seatbelt.service_profile(pol, [ws]) if kind == "service" else seatbelt.profile(pol, "gpu")

    def run_in(self, snippet: str, fenced: bool, folders=(PF, PF2), kind="service"):
        tmp, ws, t, env = self.fixture(folders)
        r = subprocess.run(["/usr/bin/sandbox-exec", "-p", self.profile(ws, t, fenced, kind), "/bin/sh", "-c", snippet],
                           cwd=ws, env=env, capture_output=True, text=True, timeout=60)
        return r, ws, t

    def refused(self, kind):
        for name, snippet in REFUSED.items():
            with self.subTest(name):
                r, _ws, _t = self.run_in(snippet, fenced=False, kind=kind)
                self.assertEqual(r.returncode, 0, f"positive control: {r.stderr}")
                r, _ws, _t = self.run_in(snippet, fenced=True, kind=kind)
                self.assertNotEqual(r.returncode, 0, f"landed under the fence: {r.stdout} {r.stderr}")

    def works(self, kind):
        for name, snippet in WORKS.items():
            with self.subTest(name):
                r, ws, _t = self.run_in(snippet, fenced=True, kind=kind)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_planting_route_is_refused_and_lands_without_the_fence(self):
        self.refused("service")

    def test_every_planting_route_is_refused_under_the_cap_run_profile_too(self):
        self.refused("cap")

    def test_nothing_was_planted(self):
        """The refusals left no git dir, gitfile or HEAD file behind."""
        r, ws, t = self.run_in("; ".join(REFUSED.values()), fenced=True)
        planted = [p for root in (ws, t) for p in root.rglob("*")
                   if (p.name.lower() == ".git" or (p.name.lower() == "head" and not p.is_dir()))
                   and not p.is_relative_to(ws / ".git") and p not in (ws / "wt" / ".git", t / "pre" / ".git",
                                                                       t / "preshape" / "HEAD")]
        self.assertEqual(planted, [], r.stderr)
        self.assertFalse([p for p in (ws / ".git").rglob(".git") if not p.is_file()], "a .git that isn't uv's marker")
        self.assertEqual((ws / ".git" / "config").read_text().count("x.y"), 0)
        self.assertTrue((ws / PF).is_dir() and (ws / PF2).is_dir())

    def test_the_swap_chain_into_uvs_marker(self):
        """A HEAD file made by swapping it with a folder named HEAD, the git-dir shape then moved onto the marker's
        name: lands without the fence, and git run outside enters it from a folder below; refused under it."""
        def outside_git(ws):
            cfg = ws.parent / "owner-gitconfig"
            cfg.write_text("[safe]\n\tbareRepository = explicit\n")
            return subprocess.run(["git", "-C", str(ws / UV / "sdists-v1" / "sub"), "rev-parse", "--absolute-git-dir"],
                                  capture_output=True, text=True, env={**ENV, "GIT_CONFIG_GLOBAL": str(cfg),
                                                                       "HOME": str(ws.parent / "home")})
        r, ws, _t = self.run_in(CHAIN, fenced=False)
        self.assertEqual(r.returncode, 0, f"positive control: {r.stderr}")
        got = outside_git(ws)
        self.assertEqual(got.stdout.strip(), str(ws / UV / "sdists-v1" / ".git"), got.stderr)
        r, ws, _t = self.run_in(CHAIN, fenced=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((ws / UV / "sdists-v1" / ".git").exists())
        self.assertNotEqual(outside_git(ws).stdout.strip(), str(ws / UV / "sdists-v1" / ".git"))

    def test_git_reads_and_the_platform_folder_still_work(self):
        self.works("service")
        r, ws, _t = self.run_in("git status --porcelain", fenced=True)
        self.assertFalse((ws / ".git" / "index.lock").exists(), "status's opportunistic refresh left a lock")

    def test_they_still_work_under_the_cap_run_profile(self):
        self.works("cap")

    def test_the_cost_a_folder_named_head_stays(self):
        """What the swap rule costs: a folder named HEAD can be made, not removed or renamed, from inside."""
        r, _ws, _t = self.run_in("mkdir -p e/HEAD && rmdir e/HEAD", fenced=True)
        self.assertNotEqual(r.returncode, 0)
        r, _ws, _t = self.run_in("mkdir -p e/HEAD && rmdir e/HEAD", fenced=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_missing_platform_folder(self):
        """The platform makes it; nothing inside may, neither as a folder nor as a link (which would lead writes
        into the git dir)."""
        for snippet in (f"mkdir {PF}", f"mkdir {PF2}", f"ln -s hooks {PF} && echo x > {PF}/pre-commit",
                        f"ln -s . {PF2} && echo x > {PF2}/pre-commit"):
            with self.subTest(snippet):
                r, ws, _t = self.run_in(snippet, fenced=False, folders=())
                self.assertEqual(r.returncode, 0, f"positive control: {r.stderr}")
                r, ws, _t = self.run_in(snippet, fenced=True, folders=())
                self.assertNotEqual(r.returncode, 0, r.stderr)
                self.assertFalse(any((ws / p).exists() for p in (PF, PF2, ".git/hooks/pre-commit", ".git/pre-commit")))

    def test_a_link_made_before_never_moves_the_exception(self):
        """The profile is computed on every start. A link at the platform folder's unused spelling or at its cache/,
        made where nothing fenced it (a session of the project until step 4, or before the switch), leaves the rules
        as they were: below the .git they are written as the policy names them, never resolved, so a write through
        the link is matched in the git dir where it lands (step 1's cold review)."""
        # (the link, where it points: relative to its folder), each to the git dir or its hooks; the marker's place
        # as reached through the link
        links = [(PF2, ".", f"{PF2}/cache/uv"), (PF2, "hooks", f"{PF2}/cache/uv"), (f"{PF}/cache", "..", UV)]
        for link, target, uv in links:
            setup = f"ln -s {target} {link}"
            attacks = {"config": "echo x >> .git/config", "hook": "echo x > .git/hooks/pre-commit",
                       "through-the-link": f"echo x >> {link}/config",
                       "marker-through-the-link": f"mkdir -p {uv}/sdists-v9 && : > {uv}/sdists-v9/.git"}
            for name, attack in attacks.items():
                with self.subTest(setup=setup, attack=name):
                    tmp, ws, t, env = self.fixture(folders=(PF,))
                    before = self.profile(ws, t, True)
                    subprocess.run(["/bin/sh", "-c", setup], cwd=ws, env=env, check=True)   # outside any fence
                    prof = self.profile(ws, t, True)
                    self.assertEqual(prof, before, "the link changed the rules")
                    config = (ws / ".git" / "config").read_text()
                    r = subprocess.run(["/usr/bin/sandbox-exec", "-p", prof, "/bin/sh", "-c", attack], cwd=ws,
                                       env=env, capture_output=True, text=True, timeout=60)
                    self.assertNotEqual(r.returncode, 0, r.stderr)
                    self.assertEqual((ws / ".git" / "config").read_text(), config)
                    self.assertFalse((ws / ".git" / "hooks" / "pre-commit").exists())
                    self.assertFalse(any(x.name == ".git" for x in (ws / ".git").rglob("*")))

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
