"""The git broker's allowlists (bin/git_allow.py; docs/design/git-broker.md, "The allowlists" and "Step 2 build list",
slice a), pure, on any OS: every subcommand in exactly one class, each refusal with its reason (unknown subcommands and
options, abbreviations, sticky shorts, -c keys, environment variables, an alias, the signing and submodule options, the
owner's other decisions), and a selecting environment running in place.

    bin/run-tests   (or brunna test)
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import git_allow as g  # noqa: E402


def kind(*argv, env=None):
    return g.classify(list(argv), env or {})


_en = None


def setUpModule():
    """The reasons are read in English here (their texts in each language: tests/test_i18n_golden_git_broker.py)."""
    global _en
    from unittest import mock
    import os
    _en = mock.patch.dict(os.environ, {"AGENT_HUB_LANG_UI": "en", "BRUNNA_LANG_UI": "en"})
    _en.start()


def tearDownModule():
    _en.stop()


class Classes(unittest.TestCase):
    def test_each_class(self):
        cases = {
            g.IN_PLACE: [("status",), ("log", "--oneline"), ("diff", "--ext-diff"), ("branch",), ("branch", "-a"),
                         ("branch", "--list", "w*"), ("tag",), ("tag", "-l", "v*"), ("stash", "list"),
                         ("worktree", "list"), ("remote", "-v"), ("reflog",), ("config", "--get", "user.name"),
                         ("config", "user.name"), ("hash-object", "f"), ("fsck",), ("format-patch", "--stdout", "HEAD~1"),
                         ("apply", "--stat", "p"), ("clean", "-n"), ("--version",), (), ("commit", "--help"),
                         ("notes", "list"), ("archive", "HEAD")],
            g.P1: [("add", "."), ("rm", "--cached", "f"), ("commit", "-m", "x"), ("hash-object", "-w", "f"),
                   ("apply", "--cached", "p"), ("notes", "add", "-m", "n"), ("notes", "--ref", "x", "append", "-m", "n"),
                   ("gc",)],
            g.COMPOSED: [("rm", "f"), ("mv", "a", "b"), ("switch", "x"), ("checkout", "x"), ("reset", "--hard"),
                         ("merge", "x"), ("rebase", "x"), ("stash",), ("stash", "pop"), ("worktree", "add", "x"),
                         ("branch", "x"), ("branch", "-d", "x"), ("tag", "v1"), ("init", "x"), ("clone", "u", "d"),
                         ("apply", "p"), ("apply", "--index", "p"), ("update-ref", "refs/heads/x", "HEAD"),
                         ("remote", "rename", "a", "b")],
            g.NETWORK: [("fetch",), ("pull",), ("push",), ("ls-remote", "origin"), ("remote", "update"),
                        ("remote", "show", "origin")],
            g.CONFIG: [("config", "user.name", "x"), ("config", "--unset", "user.name"), ("config", "set", "a.b", "c"),
                       ("remote", "add", "origin", "https://e.invalid/x"), ("remote", "set-url", "origin", "u"),
                       ("remote", "remove", "origin")],
        }
        seen = set()
        for want, argvs in cases.items():
            for argv in argvs:
                with self.subTest(argv):
                    c = kind(*argv)
                    self.assertEqual(c.kind, want, c.reason)
                    seen.add(argv)
        self.assertEqual(len(seen), sum(map(len, cases.values())), "each case in one class only")

    def test_the_ops_slice_a_runs(self):
        self.assertEqual({kind(*a).op for a in (("add", "."), ("rm", "--cached", "f"), ("commit", "-m", "x"),
                                                ("hash-object", "-w", "f"), ("apply", "--cached"),
                                                ("notes", "add", "-m", "n"), ("gc",))},
                         {"add", "rm-cached", "commit", "hash-object", "apply-cached", "notes-add", "gc"})

    def test_subcommand_classes_dont_overlap(self):
        groups = [g.READS, g.COMPOSED_SUBS, g.NETWORK_SUBS, set(g.REFUSED_SUBS)]
        for i, a in enumerate(groups):
            for b in groups[i + 1:]:
                self.assertFalse(a & b)


class Refusals(unittest.TestCase):
    def refused(self, *argv, env=None, says=""):
        c = kind(*argv, env=env)
        self.assertEqual(c.kind, g.REFUSED, (argv, c.kind))
        self.assertIn(says, c.reason)
        self.assertIn("nothing was run", g.message(c))
        return c

    def test_unknown_subcommands_and_aliases(self):
        self.refused("frob", says="git frob isn't one")
        self.refused("co", says="an alias is refused")

    def test_by_name(self):
        for sub in ("submodule", "filter-branch", "difftool", "mergetool", "hook", "bisect", "daemon", "send-email",
                    "credential", "credential-store", "remote-https", "fast-import", "bundle", "replace", "maintenance"):
            self.refused(sub)
        self.refused("archive", "--remote=u", "HEAD", says="--remote")
        self.refused("fsck", "--lost-found", says="lost-found")
        self.refused("hash-object", "-w", "--literally", "f", says="--literally")
        self.refused("format-patch", "-o", "d", "HEAD~1", says="--stdout")
        self.refused("reflog", "expire", says="expire")

    def test_unknown_options_and_abbreviations(self):
        self.refused("commit", "--exe", "x", says="unknown option --exe")
        self.refused("commit", "--mess=x", says="unknown option --mess")
        self.refused("add", "--chm=+x", "f", says="unknown option")
        self.refused("commit", "-mx", "-Z", says="unknown option -Z")
        self.refused("commit", "--author", says="needs a value")
        self.refused("add", "-n=1", says="unknown option -=")

    def test_sticky_shorts_and_clusters(self):
        c = kind("commit", "-qam", "msg")
        self.assertEqual(c.opts, [("-q", None), ("-a", None), ("-m", "msg")])
        c = kind("commit", "-mSave", "-a")
        self.assertEqual(c.opts, [("-m", "Save"), ("-a", None)])
        self.assertEqual(c.kind, g.P1, "a value holding S isn't signing")
        c = kind("commit", "-m", "-x", "--", "-p")
        self.assertEqual((c.kind, c.pos), (g.P1, ["-p"]), "after -- every argument is a path")
        c = kind("commit", "-m", "x", "--end-of-options", "--exe")
        self.assertEqual((c.kind, c.pos), (g.P1, ["--exe"]))

    def test_c_keys(self):
        self.refused("-c", "core.fsmonitor=x", "add", ".", says="core.fsmonitor")
        self.refused("-c", "core.hooksPath=/x", "commit", "-m", "x", says="core.hooksPath")
        self.refused("-c", "commit.gpgSign=true", "commit", "-m", "x", says="commit.gpgSign=false")
        self.refused("-c", "user.name", "add", ".", says="without a value")
        c = kind("-c", "user.name=a", "-c", "color.ui=always", "-c", "advice.x=false", "add", ".")
        self.assertEqual(c.kind, g.P1)
        self.assertEqual(c.gopts, ["-c", "user.name=a", "-c", "color.ui=always", "-c", "advice.x=false"])
        self.assertEqual(kind("-c", "core.fsmonitor=x", "status").kind, g.IN_PLACE, "in place runs as the caller")

    def test_environment(self):
        for var in ("GIT_EXTERNAL_DIFF", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR",
                    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_PROXY_COMMAND",
                    "GIT_SOMETHING_NEW"):
            with self.subTest(var):
                self.refused("add", ".", env={var: "x"}, says=var)
        self.refused("add", ".", env={"GIT_TRACE": "/tmp/t"}, says="names a file")
        c = kind("add", ".", env={"GIT_TRACE": "1", "GIT_AUTHOR_NAME": "a", "LC_ALL": "C", "TZ": "UTC",
                                  "GIT_PAGER": "less", "PATH": "/x", "HOME": "/h"})
        self.assertEqual(c.kind, g.P1)
        self.assertEqual(c.env, {"GIT_TRACE": "1", "GIT_AUTHOR_NAME": "a", "LC_ALL": "C", "TZ": "UTC"})

    def test_a_selecting_environment_goes_in_place(self):
        for var in g.ENV_SELECT:
            with self.subTest(var):
                self.assertEqual(kind("commit", "-m", "x", env={var: "/x"}).kind, g.IN_PLACE)
        self.assertEqual(kind("--git-dir=/x", "commit", "-m", "x").kind, g.IN_PLACE)
        self.assertEqual(kind("--work-tree", "/x", "commit", "-m", "x").kind, g.IN_PLACE)
        self.assertEqual(kind("commit", "-m", "x", env={"GIT_DIR": ""}).kind, g.P1, "an empty one selects nothing")

    def test_signing_and_submodules(self):
        for argv in (("commit", "-S", "-m", "x"), ("commit", "--gpg-sign=K", "-m", "x"), ("tag", "-s", "v1"),
                     ("tag", "-as", "v1"), ("tag", "-u", "K", "v1"), ("tag", "--sign", "v1"), ("push", "--signed"),
                     ("merge", "-S", "x"), ("rebase", "--gpg-sign", "x")):
            self.refused(*argv, says="signing")
        for argv in (("clone", "--recurse-submodules", "u"), ("pull", "--recurse-submodules=yes"),
                     ("checkout", "--recurse-submodules", "x"), ("submodule", "update")):
            self.refused(*argv, says="submodules")
        for argv in (("init", "--bare", "x"), ("clone", "--bare", "u"), ("clone", "--mirror", "u"),
                     ("init", "--separate-git-dir", "d", "x")):
            self.refused(*argv, says="bare or separate")

    def test_strategies(self):
        for argv in (("merge", "-s", "evil", "x"), ("merge", "-sevil", "x"), ("rebase", "--strategy=evil"),
                     ("pull", "--strategy", "evil"), ("cherry-pick", "--strategy=evil", "x"),
                     ("revert", "--strategy", "evil", "x")):
            self.refused(*argv, says="merge strategies")
        for s in g.STRATEGIES:
            self.assertEqual(kind("merge", "-s", s, "x").kind, g.COMPOSED)
        self.assertEqual(kind("cherry-pick", "-s", "x").kind, g.COMPOSED, "cherry-pick -s is --signoff")

    def test_interactive_and_editors(self):
        for argv in (("add", "-p"), ("add", "-ip"), ("add", "--patch"), ("add", "-e"), ("commit", "-p", "-m", "x"),
                     ("commit", "--interactive")):
            self.refused(*argv, says="apply --cached")
        for argv in (("commit", "-e", "-m", "x"), ("commit", "-c", "HEAD"), ("commit", "--squash=HEAD"),
                     ("commit", "--fixup=reword:HEAD"), ("notes", "add")):
            self.refused(*argv, says="editor")
        self.refused("commit", env={"EDITOR": "vim"}, says="-m or -F")
        self.assertEqual(kind("commit", env={"GIT_EDITOR": "true"}).kind, g.P1, "no editor of the caller's")
        self.assertEqual(kind("commit", "--amend", "--no-edit", env={"EDITOR": "vim"}).kind, g.P1)
        self.assertEqual(kind("commit", "-F", "-", env={"VISUAL": "code"}).kind, g.P1)

    def test_config_forms(self):
        self.refused("config", "--global", "user.name", "x", says="repo's own config")
        self.refused("config", "--add", "a.b", "c", says="--add")
        self.refused("config", "--unset-all", "a.b", says="--unset-all")
        self.refused("config", "--bool", "a.b", "true", says="plain values")
        self.refused("config", "--unset", "a.b", "c", says="one key")
        c = kind("config", "--local", "user.name", "x")
        self.assertEqual((c.kind, c.op, c.pos), (g.CONFIG, "config-set", ["user.name", "x"]))
        self.refused("remote", "add", "-f", "o", "u", says="unknown option -f")
        self.refused("remote", "set-url", "--push", "o", "u", says="--push")
        self.refused("remote", "add", "-bad", "u", says="")
        self.refused("remote", "add", "o/x", "u", says="plain remote name")
        self.refused("remote", "set-branches", "o", "x", says="set-branches")

    def test_notes_ref(self):
        self.refused("notes", "--ref", "refs/heads/main", "add", "-m", "x", says="notes refs")
        self.refused("notes", "--ref", "../x", "add", "-m", "x", says="notes refs")
        self.assertEqual(kind("notes", "--ref=refs/notes/x", "add", "-m", "x").kind, g.P1)

    def test_relevant_env_keeps_only_what_is_read(self):
        env = {"PATH": "/x", "HOME": "/h", "GIT_DIR": "d", "LANG": "C", "EDITOR": "vim", "SSH_AUTH_SOCK": "/s",
               "LC_CTYPE": "UTF-8", "AWS_SECRET": "k"}
        self.assertEqual(g.relevant_env(env), {"GIT_DIR": "d", "LANG": "C", "EDITOR": "vim", "LC_CTYPE": "UTF-8"})


if __name__ == "__main__":
    unittest.main()
