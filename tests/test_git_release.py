"""brunna release (bin/git_release.py; docs/design/platform-push.md, "11-16: releases and the plain owner form"): the owner
publishes a GitHub release of a sandboxed project with their gh, at a commit already on the remote, after a typed yes.

Throwaway everything: the registry (hubconf.projects patched) names one sandboxed project whose paths.origin is
git@github.com:o/proj.git, and the owner's temp git config rewrites that URL (url.<path>.insteadOf) to a bare repo in the
test's temp folder; the platform's state is there too. `gh` is a stand-in first on PATH that records every call (with
the notes file's content and mode) and, on `release create`, makes the tag in that bare repo; GH_CONFIG_DIR is in the
temp folder and no GH_TOKEN, so nothing can reach the real gh's sign-in, GitHub or a real repo. The visibility probe and
the project's letter are stubbed. Each refusal has a control that publishes.

    bin/run-tests <a folder holding this module>
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import git_release  # noqa: E402
import git_remote  # noqa: E402
import hubconf  # noqa: E402
import isolation  # noqa: E402
import push_queue  # noqa: E402
import runtime_ports  # noqa: E402

ORIGIN = "git@github.com:o/proj.git"
IDENT = {"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.org", "GIT_COMMITTER_NAME": "Dev",
         "GIT_COMMITTER_EMAIL": "dev@example.org", "GIT_AUTHOR_DATE": "2026-11-16T10:00:00+00:00",
         "GIT_COMMITTER_DATE": "2026-11-16T10:00:00+00:00"}

FAKE_GH = r'''
import json, os, pathlib, subprocess, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
a = sys.argv[1:]
def opt(name):
    return a[a.index(name) + 1] if name in a else None
rec = {"argv": a, "cwd": os.getcwd()}
if a[:2] == ["release", "create"] and opt("--notes-file"):
    f = pathlib.Path(opt("--notes-file"))
    rec["notes"] = f.read_text()
    rec["notes_mode"] = oct(f.stat().st_mode & 0o777)
with (d / "gh.jsonl").open("a") as fh:
    fh.write(json.dumps(rec) + "\n")
if a[:2] == ["api", "user"]:
    if (d / "no-login").exists():
        print("To get started with GitHub CLI, please run:  gh auth login", file=sys.stderr)
        sys.exit(4)
    print("octo")
    sys.exit(0)
if a[:2] == ["release", "view"]:
    if (d / "release-exists").exists():
        print(json.dumps({"url": "https://github.com/o/proj/releases/tag/" + a[2], "isDraft": True}))
        sys.exit(0)
    if (d / "gh-broken").exists():
        print("HTTP 401: Bad credentials (https://api.github.com/graphql)", file=sys.stderr)
        sys.exit(1)
    print("release not found", file=sys.stderr)
    sys.exit(1)
def git(*args):
    return subprocess.run(["git", "--git-dir", os.environ["FAKE_REMOTE"], *args], capture_output=True, text=True)
def tag_exists(t):
    return git("rev-parse", "--verify", "-q", f"refs/tags/{t}").returncode == 0
if a[:2] == ["api", "--hostname"] and "POST" in a and a[a.index("-X") + 2].endswith("/git/refs"):
    fields = dict(f.split("=", 1) for f in (a[i + 1] for i, x in enumerate(a) if x == "-f"))
    if (d / "ruleset").exists():   # a tag ruleset refuses the platform's tag
        print("gh: Repository rule violations found (HTTP 422)", file=sys.stderr)
        sys.exit(1)
    if (d / "silent-post").exists():   # gh says yes, the tag isn't there
        sys.exit(0)
    if (d / "race-tag").exists():   # someone else tags another commit just before the platform does
        git("update-ref", fields["ref"], (d / "race-tag").read_text().strip())
    if git("rev-parse", "--verify", "-q", fields["ref"]).returncode == 0:   # GitHub: an existing ref isn't made again
        print("gh: Reference already exists (HTTP 422)", file=sys.stderr)
        sys.exit(1)
    git("update-ref", fields["ref"], fields["sha"])
    print(json.dumps({"ref": fields["ref"], "object": {"sha": fields["sha"]}}))
    sys.exit(0)
if a[:2] == ["release", "create"] and (d / "create-fails").exists():
    print("HTTP 502: Bad Gateway", file=sys.stderr)
    sys.exit(1)
if a[:2] == ["release", "create"]:
    # as GitHub documents it: --target is used only when the tag is made here; an existing tag is kept as it is
    if (d / "move-tag").exists():
        git("update-ref", f"refs/tags/{a[2]}", (d / "move-tag").read_text().strip())
    if not tag_exists(a[2]):
        if "--verify-tag" in a:
            print(f"tag {a[2]} doesn't exist in the repo o/proj, aborting due to --verify-tag flag", file=sys.stderr)
            sys.exit(1)
        git("tag", a[2], opt("--target") or "HEAD")
    print("https://github.com/o/proj/releases/tag/" + a[2])
    sys.exit(0)
print("fake gh: " + " ".join(a), file=sys.stderr)
sys.exit(2)
'''


class Tty(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="release-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = self.tmp / "fake"
        fakebin = self.tmp / "fakebin"
        for d in (self.fake, fakebin, self.tmp / "home", self.tmp / "gh-config"):
            d.mkdir()
        (self.fake / "gh.py").write_text(FAKE_GH)
        (fakebin / "gh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.fake / "gh.py"}" "$@"\n')
        (fakebin / "gh").chmod(0o755)
        self.remote = self.tmp / "remote.git"
        self.gitconfig = self.tmp / "owner-gitconfig"
        self.gitconfig.write_text(f'[safe]\n\tbareRepository = explicit\n[url "{self.remote}"]\n\tinsteadOf = {ORIGIN}\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GH_", "GITHUB_", "SSH_"))}
        env.update(HOME=str(self.tmp / "home"), PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
                   GH_CONFIG_DIR=str(self.tmp / "gh-config"), FAKE_DIR=str(self.fake), FAKE_REMOTE=str(self.remote),
                   GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                   LC_ALL="C", AGENT_HUB_LANG_UI="en", AGENT_HUB_LANG_CONTENT="en", **IDENT)
        self.docs = {"proj": {"name": "proj", "paths": {"workspace": str(self.tmp / "ws" / "proj"), "origin": ORIGIN},
                              "enforce": {"sandbox": True}}}
        self.letters, self.vis = [], "public"
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(hubconf, "STATE", self.tmp / "state"),
                  mock.patch.object(hubconf, "projects", lambda: self.docs),
                  mock.patch.object(hubconf, "worktree_lookup", lambda: False),
                  mock.patch.object(isolation, "confined_now", lambda marked=False: False),
                  mock.patch.object(isolation, "reads_blocked", lambda: False),
                  mock.patch.object(runtime_ports, "current_session", lambda env=None: None),
                  mock.patch.object(git_remote, "visibility", lambda origin: self.vis),
                  mock.patch.object(push_queue, "project_letter",
                                    lambda project, text, key: self.letters.append((project, text, key)))):
            p.start()
            self.addCleanup(p.stop)
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.seed = self.tmp / "seed"
        self.git(self.tmp, "init", "-q", "-b", "main", str(self.seed))
        self.first = self.commit("README", "hello\n", "init")
        self.tip = self.commit("a.txt", "a\n", "add a")
        self.git(self.seed, "push", "-q", str(self.remote), "main")

    # ---------- helpers ----------
    def git(self, cwd, *args) -> str:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r.stdout

    def commit(self, path, text, msg) -> str:
        f = self.seed / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        self.git(self.seed, "add", "--", path)
        self.git(self.seed, "commit", "-q", "-m", msg)
        return self.git(self.seed, "rev-parse", "HEAD").strip()

    def push(self, ref="main"):
        self.git(self.seed, "push", "-q", str(self.remote), ref)

    def run_(self, *args, tty=True, answer="y", out_tty=None, on_answer=None):
        out, err = Tty(out_tty if out_tty is not None else tty), io.StringIO()
        prompts = []

        def reply(prompt=""):
            prompts.append(prompt)
            out.write(f"{prompt}{answer}\n")
            if on_answer:
                on_answer()
            return answer
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty)), \
                mock.patch("builtins.input", reply):
            code = git_release.main(list(args))
        self.prompts = prompts
        return code, out.getvalue(), err.getvalue()

    def gh_calls(self) -> list[dict]:
        f = self.fake / "gh.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def creates(self) -> list[dict]:
        return [c for c in self.gh_calls() if c["argv"][:2] == ["release", "create"]]

    def posts(self) -> list[str]:
        """The sha of each tag the platform asked GitHub to make (the refs API)."""
        return [next(x[4:] for x in c["argv"] if x.startswith("sha=")) for c in self.gh_calls()
                if c["argv"][:2] == ["api", "--hostname"]]

    def remote_tag(self, tag) -> str:
        r = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{}}"],
                           capture_output=True, text=True)
        return r.stdout.strip()

    def owned(self) -> pathlib.Path:
        return self.tmp / "state" / "git-broker" / "proj" / "remote.git"

    def releases(self) -> list[dict]:
        f = self.owned().parent / "releases.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def assert_nothing_created(self):
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.remote_tag("v1.0.0"), "")
        self.assertEqual(self.releases(), [])
        self.assertEqual(self.letters, [])


class Publishes(Base):
    def test_the_default_branch_tip_after_a_typed_yes(self):
        code, out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.OK, err)
        [c] = self.creates()
        notes = c["argv"][c["argv"].index("--notes-file") + 1]
        self.assertEqual(c["argv"], ["release", "create", "v1.0.0", "--repo", "github.com/o/proj", "--verify-tag",
                                     "--title", "v1.0.0", "--notes-file", notes])
        post = [x["argv"] for x in self.gh_calls() if x["argv"][:2] == ["api", "--hostname"]]
        self.assertEqual(post, [["api", "--hostname", "github.com", "-X", "POST", "repos/o/proj/git/refs",
                                 "-f", "ref=refs/tags/v1.0.0", "-f", f"sha={self.tip}"]], "the tag pinned first")
        self.assertIn(self.tip, out, "the sha given to gh is the one shown")
        self.assertEqual((c["notes"], c["notes_mode"]), ("", "0o600"), "no notes: an empty private file, gh never asks")
        self.assertFalse(os.path.exists(notes), "the notes file is removed")
        self.assertFalse(pathlib.Path(c["cwd"]).is_relative_to(self.tmp / "ws"))
        self.assertEqual(self.remote_tag("v1.0.0"), self.tip)
        # the order: the account named and the release looked up (read-only), then the yes, the release looked up again,
        # the tag made at the shown sha, and only then the release on it
        self.assertEqual([x["argv"][:2] for x in self.gh_calls()],
                         [["api", "user"], ["release", "view"], ["release", "view"], ["api", "--hostname"],
                          ["release", "create"]])
        self.assertIn("read-only: the account's name, and that github.com/o/proj has no release named v1.0.0 yet", out)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("will use that login on github.com/o/proj to create the tag v1.0.0", out)
        self.assertIn("https://github.com/o/proj/releases/tag/v1.0.0", out)
        [rec] = self.releases()
        self.assertEqual((rec["tag"], rec["sha"], rec["by"], rec["tag_seen"]), ("v1.0.0", self.tip, "owner", self.tip))
        [(project, text, key)] = self.letters
        self.assertEqual(project, "proj")
        for s in ("v1.0.0", self.tip[:12], "https://github.com/o/proj/releases/tag/v1.0.0"):
            self.assertIn(s, text)
        self.assertIn("If Zenodo (or another archive) is connected to this repo", out, "the DOI warning is always said")
        self.assertNotIn("which such archives read", out, "no archive file named")

    def test_notes_go_through_a_file_never_argv(self):
        f = self.tmp / "notes.md"
        long = "## What's new\n\n" + "\n".join(f"- change {i}" for i in range(40)) + "\n"
        f.write_text(long)
        code, out, err = self.run_("proj", "v1.0.0", "--title", "First release", "--notes-file", str(f))
        self.assertEqual(code, git_release.OK, err)
        [c] = self.creates()
        self.assertEqual(c["notes"], long)
        self.assertNotIn("change 1", " ".join(c["argv"]))
        self.assertNotIn(str(f), c["argv"], "the owner's file is copied, so what was shown is what is sent")
        self.assertIn("--title", c["argv"])
        self.assertEqual(c["argv"][c["argv"].index("--title") + 1], "First release")
        self.assertIn("> - change 0", out)
        self.assertIn("> - change 39", out, "every line is shown before the yes")
        code, _out, err = self.run_("proj", "v1.0.1", "--notes", "short and sweet")
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.creates()[-1]["notes"], "short and sweet")

    def test_a_target_branch_or_commit_on_the_remote(self):
        self.git(self.seed, "checkout", "-q", "-b", "feature")
        feat = self.commit("f.txt", "f\n", "a feature")
        self.push("feature")
        code, out, err = self.run_("proj", "v1.0.0", "--target", "feature")
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.posts()[-1], feat)
        self.assertEqual(self.remote_tag("v1.0.0"), feat)
        self.assertIn("on the remote's branch feature", out)
        code, _out, err = self.run_("proj", "v0.9.0", "--target", self.first[:9])
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.posts()[-1], self.first, "a short id is given to GitHub as the full sha")

    def test_commits_since_the_previous_tag(self):
        self.git(self.seed, "tag", "-a", "-m", "zero nine", "v0.9", self.first)
        self.git(self.seed, "push", "-q", str(self.remote), "v0.9")
        self.commit("b.txt", "b\n", "add b")
        self.commit("c.txt", "c\n", "add c")
        self.push()
        code, out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.OK, err)
        self.assertIn(f"commits  3 since v0.9 ({self.first[:12]})", out)
        self.assertIn("add c", out)

    def test_an_archive_file_brings_the_doi_warning(self):
        for name in (".zenodo.json", "CITATION.cff"):
            with self.subTest(name):
                self.commit(name, "{}\n", f"add {name}")
                self.push()
                code, out, err = self.run_("proj", f"v-{name.strip('.').lower()}", answer="n")
                self.assertEqual(code, git_release.REFUSED)
                self.assertIn(f"!! The target has {name}, which such archives read", out)
                self.assertIn("If Zenodo (or another archive) is connected to this repo, publishing this release makes "
                              "a permanent archived copy with a DOI that can't be withdrawn", out)
                self.git(self.seed, "rm", "-q", name)
                self.git(self.seed, "commit", "-q", "-m", f"drop {name}")


class Refusals(Base):
    def test_the_tag_is_on_the_remote_already(self):
        for kind in ((), ("-a", "-m", "annotated")):
            with self.subTest(kind=kind):
                self.git(self.seed, "tag", *kind, "v1.0.0", self.first)
                self.git(self.seed, "push", "-q", str(self.remote), "v1.0.0")
                code, _out, err = self.run_("proj", "v1.0.0")
                self.assertEqual(code, git_release.REFUSED)
                self.assertIn("the remote has the tag v1.0.0 already", err)
                self.assertEqual(self.gh_calls(), [], "gh was used")
                subprocess.run(["git", "--git-dir", str(self.remote), "tag", "-d", "v1.0.0"], capture_output=True)
                self.git(self.seed, "tag", "-d", "v1.0.0")
        code, _out, err = self.run_("proj", "v1.0.0")   # the control
        self.assertEqual(code, git_release.OK, err)

    def test_a_release_of_that_name_exists(self):
        (self.fake / "release-exists").touch()
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("has a release named v1.0.0 already", err)
        self.assertEqual(self.prompts, [], "asked to confirm a release that exists already")
        self.assert_nothing_created()

    def test_gh_that_cant_tell_fails_closed(self):
        (self.fake / "gh-broken").touch()
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.FAILED)
        self.assertIn("Bad credentials", err)
        self.assertEqual(self.prompts, [])
        self.assert_nothing_created()

    def test_gh_not_signed_in(self):
        (self.fake / "no-login").touch()
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.FAILED)
        self.assertIn("gh auth login", err)
        self.assertEqual(self.prompts, [], "asked without naming the account")
        self.assert_nothing_created()

    def test_an_unpushed_target(self):
        unpushed = self.commit("u.txt", "u\n", "not pushed")
        code, _out, err = self.run_("proj", "v1.0.0", "--target", unpushed)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("is not a commit on the remote", err)
        # the object in the owned repo (an earlier push that was refused) doesn't make it the remote's
        subprocess.run(["git", "--git-dir", str(self.owned()), "fetch", "-q", str(self.seed), "main:refs/stale/x"],
                       check=True, capture_output=True)
        self.assertEqual(subprocess.run(["git", "--git-dir", str(self.owned()), "cat-file", "-e", unpushed]).returncode, 0)
        code, _out, err = self.run_("proj", "v1.0.0", "--target", unpushed)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("is not a commit on the remote", err)
        code, _out, err = self.run_("proj", "v1.0.0", "--target", "nosuchbranch")
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("neither a branch the remote has nor a commit id", err)
        self.assertEqual(self.gh_calls(), [])
        self.assert_nothing_created()
        self.push()   # the control: pushed, it is released
        code, _out, err = self.run_("proj", "v1.0.0", "--target", unpushed)
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.remote_tag("v1.0.0"), unpushed)

    def test_bad_tag_names(self):
        for tag in ("-v1", "v 1", "v1..2", "v1~", "v1:2", "x\x1b[2J", "", "a" * 101, "v1.lock", "v1/"):
            with self.subTest(tag=tag):
                code, _out, err = self.run_("proj", tag)
                self.assertEqual(code, git_release.REFUSED, err)
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(self.owned().exists(), "the remote was contacted")

    def test_not_github_not_sandboxed_not_registered(self):
        cases = [({"origin": "git@gitlab.com:o/proj.git"}, "is not a GitHub repo"),
                 ({"origin": str(self.remote)}, "is not a GitHub repo"),
                 ({"origin": None}, "has no remote"),
                 ({"sandbox": False}, "is not sandboxed"),
                 ({"gone": True}, "is not in the registry")]
        for change, said in cases:
            with self.subTest(change=change):
                doc = json.loads(json.dumps(self.docs["proj"]))
                if "origin" in change:
                    doc["paths"]["origin"] = change["origin"]
                if "sandbox" in change:
                    doc["enforce"] = {}
                self.docs = {} if "gone" in change else {"proj": doc}
                code, _out, err = self.run_("proj", "v1.0.0")
                self.assertEqual(code, git_release.REFUSED)
                self.assertIn(said, err)
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(self.owned().exists())

    def test_notes_are_shown_whole_or_refused(self):
        f = self.tmp / "notes.md"
        for text, said in (("".join(f"line {i}\n" for i in range(201)), "the notes are 201 lines"),
                           ("x" * (33 << 10), "KB: every line is shown before you say yes")):
            with self.subTest(said=said):
                f.write_text(text)
                code, _out, err = self.run_("proj", "v1.0.0", "--notes-file", str(f))
                self.assertEqual(code, git_release.REFUSED)
                self.assertIn(said, err)
        self.assertEqual(self.gh_calls(), [])
        f.write_text("".join(f"line {i}\n" for i in range(200)))   # the control: 200 lines, all shown
        code, out, err = self.run_("proj", "v1.0.0", "--notes-file", str(f))
        self.assertEqual(code, git_release.OK, err)
        self.assertIn("> line 199", out)

    def test_a_target_named_like_a_commit_is_a_commit(self):
        # a branch on the remote whose name is the full id of the older commit, pointing at the newer one
        self.git(self.seed, "branch", self.first, self.tip)
        self.push(f"refs/heads/{self.first}")
        code, _out, err = self.run_("proj", "v1.0.0", "--target", self.first)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("is a commit id and also a branch on the remote", err)
        self.assertEqual(self.gh_calls(), [])
        code, _out, err = self.run_("proj", "v1.0.0", "--target", self.first[:10])   # the control: another length
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.remote_tag("v1.0.0"), self.first, "the commit, never the branch's tip")

    def test_nobody_at_a_terminal_and_yes_alone(self):
        code, _out, err = self.run_("proj", "v1.0.0", tty=False)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("nobody is at a terminal", err)
        code, _out, err = self.run_("proj", "v1.0.0", "--yes", tty=False)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("--yes isn't taken for a release", err)
        code, _out, err = self.run_("proj", "v1.0.0", out_tty=False)   # the plan can't be sent into a pipe
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("nobody is at a terminal", err)
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(self.owned().exists(), "the remote was contacted")

    def test_yes_with_a_terminal_still_asks(self):
        code, out, _err = self.run_("proj", "v1.0.0", "--yes", answer="n")
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("--yes isn't taken for a release: answer the question below", out)
        self.assertEqual(len(self.prompts), 1)
        self.assert_nothing_created()

    def test_only_y_or_yes_goes_on(self):
        for answer in ("n", "", "no", "Y please", "ok"):
            with self.subTest(answer=answer):
                code, _out, err = self.run_("proj", "v1.0.0", answer=answer)
                self.assertEqual(code, git_release.REFUSED)
                self.assertIn("You said no", err)
        self.assertEqual([c["argv"][:2] for c in self.gh_calls()], [["api", "user"], ["release", "view"]] * 5,
                         "only reads before the yes")
        self.assert_nothing_created()
        code, _out, err = self.run_("proj", "v1.0.0", answer="YES")
        self.assertEqual(code, git_release.OK, err)

    def test_only_in_the_owners_own_terminal(self):
        for target, value, said in ((runtime_ports, "current_session", "not in a coding agent's session"),
                                    (isolation, "reads_blocked", "never inside a project's sandbox"),
                                    (isolation, "confined_now", "never inside a project's sandbox"),
                                    (hubconf, "worktree_lookup", "is a linked worktree of the hub")):
            with self.subTest(value), mock.patch.object(target, value, lambda *a, **k: "claude:abc"):
                code, _out, err = self.run_("proj", "v1.0.0")
                self.assertEqual(code, git_release.REFUSED)
                self.assertIn(said, err)
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(self.owned().exists())

    def test_usage(self):
        for args in ((), ("proj",), ("proj", "v1", "extra"), ("proj", "v1", "--notes", "a", "--notes-file", "f"),
                     ("proj", "v1", "--bogus"), ("proj", "v1", "--title")):
            with self.subTest(args=args):
                code, _out, err = self.run_(*args)
                self.assertEqual(code, git_release.REFUSED)
                self.assertIn("usage: brunna release", err)
        self.assertEqual(self.gh_calls(), [])


class Races(Base):
    """What changes on GitHub while the plan is on screen, or between the platform's own steps."""

    def test_a_tag_made_while_the_owner_reads(self):
        # a workflow or another terminal tags the older commit while the plan is shown: gh release create would keep
        # that tag (--target applies only to a tag gh makes), so nothing may be created
        def tag_it():
            subprocess.run(["git", "--git-dir", str(self.remote), "tag", "v1.0.0", self.first], check=True)
        code, _out, err = self.run_("proj", "v1.0.0", on_answer=tag_it)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("appeared at", err)
        self.assertEqual((self.creates(), self.posts()), ([], []))
        self.assertEqual(self.remote_tag("v1.0.0"), self.first)
        self.assertEqual((self.releases(), self.letters), ([], []))

    def test_a_tag_made_just_before_the_platforms(self):
        (self.fake / "race-tag").write_text(self.first)
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("appeared at", err)
        self.assertEqual(self.creates(), [])
        self.assertEqual((self.releases(), self.letters), ([], []))

    def test_a_release_made_while_the_owner_reads(self):
        code, _out, err = self.run_("proj", "v1.0.0", on_answer=lambda: (self.fake / "release-exists").touch())
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("has a release named v1.0.0 already", err)
        self.assert_nothing_created()
        self.assertEqual(self.posts(), [])

    def test_a_release_that_went_out_elsewhere_is_said_and_not_recorded(self):
        (self.fake / "move-tag").write_text(self.first)
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.ELSEWHERE)
        self.assertIn("went out on a different commit", err)
        self.assertIn(self.first[:12], err)
        self.assertEqual((self.releases(), self.letters), ([], []), "recorded or told although it isn't the shown sha")

    def test_a_tag_left_at_the_target_goes_on(self):
        subprocess.run(["git", "--git-dir", str(self.remote), "tag", "v1.0.0", self.tip], check=True)
        code, out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.OK, err)
        self.assertIn("on the remote already, at the target", out)
        self.assertIn("publish a release of the tag v1.0.0 that is already at", out)
        self.assertEqual(self.posts(), [], "no second tag")
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.releases()[0]["tag_existing"], "lightweight")
        self.assertNotIn("v1.0.0 (", out.split("commits", 1)[1].split("\n", 1)[0], "counted as an earlier release")


class AfterThePin(Base):
    def test_an_annotated_tag_at_the_target_is_someone_elses(self):
        subprocess.run(["git", "--git-dir", str(self.remote), "-c", "user.name=W", "-c", "user.email=w@x", "tag", "-a",
                        "-m", "from a workflow", "v1.0.0", self.tip], check=True)
        code, out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.OK, err)
        self.assertIn("an annotated tag already at the target, made by someone else", out)
        self.assertNotIn("left by a run that stopped", out)
        self.assertEqual(self.releases()[0]["tag_existing"], "annotated")

    def test_a_ruleset_that_refuses_the_tag_fails_closed(self):
        (self.fake / "ruleset").touch()
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.FAILED)
        self.assertIn("Repository rule violations", err)
        self.assert_nothing_created()

    def test_gh_says_yes_but_no_tag_is_there(self):
        (self.fake / "silent-post").touch()
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.FAILED)
        self.assertIn("GitHub answered, but the tag isn't there when read back", err)
        self.assertNotIn(": 0", err)
        self.assert_nothing_created()

    def test_a_failure_after_the_pin_leaves_the_tag_and_a_rerun_goes_on(self):
        (self.fake / "create-fails").touch()
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.FAILED)
        self.assertIn(f"The tag v1.0.0 is left at {self.tip[:12]}: running brunna release again continues from it", err)
        self.assertEqual(self.remote_tag("v1.0.0"), self.tip)
        (self.fake / "create-fails").unlink()
        code, out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.OK, err)
        self.assertIn("left by a run that stopped", out)
        self.assertEqual(len(self.posts()), 1, "the tag made once")

    def test_four_to_six_hex_are_a_commit_first(self):
        code, _out, err = self.run_("proj", "v0.1", "--target", self.first[:6])
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.posts()[-1], self.first)
        short = self.first[:5]
        self.git(self.seed, "branch", short, self.tip)
        self.push(f"refs/heads/{short}")
        code, _out, err = self.run_("proj", "v0.2", "--target", short)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("is a commit id and also a branch on the remote", err)

    def test_the_owners_own_text_keeps_tabs_and_wide_spaces_but_no_controls(self):
        f = self.tmp / "n.md"
        f.write_text("tab\there\nfull\u3000width\nok \x1b[2Jline\nrtl \u202egnp.exe\nzw a\u200bb\n")
        code, out, err = self.run_("proj", "v1.0.0", "--title", "First\u3000release", "--notes-file", str(f), answer="n")
        self.assertEqual(code, git_release.REFUSED, err)
        self.assertIn("> tab\there", out)
        self.assertIn("> full\u3000width", out)
        self.assertIn("title    First\u3000release", out)
        for bad in ("\x1b", "\u202e", "\u200b"):
            self.assertNotIn(bad, out)
        code, _out, err = self.run_("proj", "v1.0.0", "--title", "a\u202eb")
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("one printable line", err)


class Cli(unittest.TestCase):
    def test_brunna_release_is_the_owners_and_no_session_command(self):
        import policy
        src = (BIN / "agenthub").read_text()
        self.assertIn('"release": passthrough("git_release.py")', src)
        self.assertNotIn("release", policy.SESSION_SUBCOMMANDS, "a session's permissions must not let it through")


if __name__ == "__main__":
    unittest.main()
