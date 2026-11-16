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
if a[:2] == ["release", "create"]:
    subprocess.run(["git", "--git-dir", os.environ["FAKE_REMOTE"], "tag", a[2], opt("--target")], check=True)
    print("https://github.com/o/proj/releases/tag/" + a[2])
    sys.exit(0)
print("fake gh: " + " ".join(a), file=sys.stderr)
sys.exit(2)
'''


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

    def run_(self, *args, tty=True, answer="y"):
        out, err = io.StringIO(), io.StringIO()
        prompts = []
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty)), \
                mock.patch("builtins.input", lambda prompt="": (prompts.append(prompt), out.write(f"{prompt}{answer}\n"),
                                                                answer)[2]):
            code = git_release.main(list(args))
        self.prompts = prompts
        return code, out.getvalue(), err.getvalue()

    def gh_calls(self) -> list[dict]:
        f = self.fake / "gh.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def creates(self) -> list[dict]:
        return [c for c in self.gh_calls() if c["argv"][:2] == ["release", "create"]]

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
        self.assertEqual(c["argv"], ["release", "create", "v1.0.0", "--repo", "github.com/o/proj", "--target", self.tip,
                                     "--title", "v1.0.0", "--notes-file", notes])
        self.assertIn(self.tip, out, "the sha given to gh is the one shown")
        self.assertEqual((c["notes"], c["notes_mode"]), ("", "0o600"), "no notes: an empty private file, gh never asks")
        self.assertFalse(os.path.exists(notes), "the notes file is removed")
        self.assertFalse(pathlib.Path(c["cwd"]).is_relative_to(self.tmp / "ws"))
        self.assertEqual(self.remote_tag("v1.0.0"), self.tip)
        # the order: the account named, then the yes, then the release looked up and made
        self.assertEqual([x["argv"][:2] for x in self.gh_calls()],
                         [["api", "user"], ["release", "view"], ["release", "create"]])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("your gh login (octo) on github.com/o/proj", out)
        self.assertIn("https://github.com/o/proj/releases/tag/v1.0.0", out)
        [rec] = self.releases()
        self.assertEqual((rec["tag"], rec["sha"], rec["by"], rec["tag_seen"]), ("v1.0.0", self.tip, "owner", self.tip))
        [(project, text, key)] = self.letters
        self.assertEqual(project, "proj")
        for s in ("v1.0.0", self.tip[:12], "https://github.com/o/proj/releases/tag/v1.0.0"):
            self.assertIn(s, text)
        self.assertNotIn("!!", out, "no archive file: no DOI warning")

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
        self.assertIn("and 22 more lines", out)
        code, _out, err = self.run_("proj", "v1.0.1", "--notes", "short and sweet")
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.creates()[-1]["notes"], "short and sweet")

    def test_a_target_branch_or_commit_on_the_remote(self):
        self.git(self.seed, "checkout", "-q", "-b", "feature")
        feat = self.commit("f.txt", "f\n", "a feature")
        self.push("feature")
        code, out, err = self.run_("proj", "v1.0.0", "--target", "feature")
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.creates()[-1]["argv"][6], feat)
        self.assertIn("on the remote's branch feature", out)
        code, _out, err = self.run_("proj", "v0.9.0", "--target", self.first[:9])
        self.assertEqual(code, git_release.OK, err)
        self.assertEqual(self.creates()[-1]["argv"][6], self.first, "a short id is given to gh as the full sha")

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
                self.assertIn(f"!! The target has {name}", out)
                self.assertIn("If Zenodo (or another archive) is connected to this repo, publishing this release makes "
                              "a permanent archived copy with a DOI that can't be withdrawn.", out)
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
        self.assert_nothing_created()

    def test_gh_that_cant_tell_fails_closed(self):
        (self.fake / "gh-broken").touch()
        code, _out, err = self.run_("proj", "v1.0.0")
        self.assertEqual(code, git_release.FAILED)
        self.assertIn("Bad credentials", err)
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

    def test_nobody_at_a_terminal_and_yes_alone(self):
        code, _out, err = self.run_("proj", "v1.0.0", tty=False)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("nobody is at a terminal", err)
        code, _out, err = self.run_("proj", "v1.0.0", "--yes", tty=False)
        self.assertEqual(code, git_release.REFUSED)
        self.assertIn("--yes isn't taken for a release", err)
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
        self.assertEqual([c["argv"][:2] for c in self.gh_calls()], [["api", "user"]] * 5,
                         "only the account was read before the yes")
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


class Cli(unittest.TestCase):
    def test_brunna_release_is_the_owners_and_no_session_command(self):
        import policy
        src = (BIN / "agenthub").read_text()
        self.assertIn('"release": passthrough("git_release.py")', src)
        self.assertNotIn("release", policy.SESSION_SUBCOMMANDS, "a session's permissions must not let it through")


if __name__ == "__main__":
    unittest.main()
