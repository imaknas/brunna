"""integrations/: clients for other apps, one folder per app (AGENTS.md, docs/design/herdr.md), each laid out as its app
requires and reading only the platform's neutral output. The Claude Code mod (integrations/claude-code/,
docs/design/agent-surface.md) is checked as display only: see ClaudeCodeMod. herdr shares a folder laid out like a herdr home: RECIPE.md and preview.png at its top, the
mod in its mods/<name>/ (a recipe inside mods/<name>/ is not found, and herdr's index skips the repo). The mod imports
only herdr's own modules, runs no program but brunna (and sh, for its fallback to ~/.local/bin), and its recipe has the
sections herdr's guide asks for.

    bin/py -m unittest tests.test_mods
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import names  # noqa: E402
SHARED = ROOT / "integrations" / "herdr"  # the folder herdr.dev lists
MOD = SHARED / "mods" / "brunna"
HERDR_MODULES = {"gpui-kit", "gpui-base", "herdr"}
RECIPE_SECTIONS = ["## What you see", "## How it behaves", "## Built from", "## Watch out", "## Done when"]


class HerdrMod(unittest.TestCase):
    def test_layout(self):
        self.assertTrue((MOD / "main.js").is_file())
        mod = json.loads((MOD / "mod.json").read_text())
        self.assertIn(mod.get("place"), {"right", "left", "top", "bottom", "none"})
        for name in ("RECIPE.md", "preview.png"):
            self.assertTrue((SHARED / name).is_file(), f"{name} goes at the shared folder's top, beside mods/")
            self.assertFalse((MOD / name).exists(), f"{name} inside mods/brunna/ is not where herdr's index looks")
        self.assertFalse((SHARED / "layouts").exists(), "a mod's shared folder has no layouts/: that would make it a layout")
        self.assertFalse((ROOT / "mods").exists(), "the mod lives in integrations/herdr/, not at the repo root")

    def test_reads_only_neutral_output(self):
        js = (MOD / "main.js").read_text()
        self.assertEqual(set(re.findall(r'from\s+"([^"]+)"', js)) - HERDR_MODULES, set(), "imports beyond herdr's own")
        programs = set(re.findall(r'\[\s*"([^"]+)"\s*,\s*\[', js)) | set(re.findall(r'exec\(\s*"([^"]+)"', js))
        self.assertLessEqual(programs, {"brunna", "sh"}, programs)
        self.assertIn('"glance", "--json"', js, "it reads brunna glance --json")
        for banned in ("setTimeout", "setInterval", "bus.db", "hub.json", "brunna.json", "fetch("):
            self.assertNotIn(banned, js)

    def test_recipe(self):
        text = (SHARED / "RECIPE.md").read_text()
        self.assertTrue(text.startswith("# brunna\n"))
        self.assertIn("![brunna](preview.png)", text)
        positions = [text.find(s) for s in RECIPE_SECTIONS]
        self.assertTrue(all(p > 0 for p in positions) and positions == sorted(positions), positions)

    @unittest.skipUnless(shutil.which("node"), "no node to check the syntax with")
    def test_main_js_parses(self):
        r = subprocess.run(["node", "--check", "--input-type=module"], input=(MOD / "main.js").read_text(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


CC = ROOT / "integrations" / "claude-code"   # the Claude Code mod (docs/design/agent-surface.md)
CC_MODULE = CC / "hooks" / "brunna.tsx"
CC_ALLOWED = {"inbox", "brunna"}


def _hook_bodies(src: str, event: str) -> list[tuple[str, str]]:
    """(matcher, body) of each `on('<event>', {…}, hook)` in the module, the body up to the call's closing paren."""
    out = []
    for m in re.finditer(r"on\('" + re.escape(event) + r"',\s*(\{[^}]*\})?", src):
        depth, i = 1, src.index("(", m.start()) + 1
        while depth and i < len(src):
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        out.append((m.group(1) or "", src[m.end():i - 1]))
    return out


class ClaudeCodeMod(unittest.TestCase):
    """The mod is display only (agent-surface.md, "Rules every surface adapter keeps"): it runs no program but brunna's two
    read-only answers, puts nothing into the model's context (no prompt hook, no tool or agent, its commands return no
    text), registers only the commands the platform may offer, and never submits, notifies or toasts."""

    def setUp(self):
        # the code without its comments, which name what the module does not do
        self.src = re.sub(r"^\s*//.*$|\s//\s.*$", "", CC_MODULE.read_text(), flags=re.M)

    def test_layout_and_marketplace(self):
        manifest = json.loads((CC / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "brunna")
        self.assertEqual(json.loads((CC / "hooks" / "hooks.json").read_text()), {"modules": ["./brunna.tsx"]})
        self.assertTrue((CC / manifest["types"]).is_file())
        self.assertTrue((CC / "README.md").is_file())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual([(p["name"], p["source"]) for p in market["plugins"]], [("brunna", "./integrations/claude-code")],
                         "the hub's own checkout is the folder marketplace: the mod is the release the platform runs")
        self.assertNotIn("version", manifest, "the mod is read from the checkout; a version here would drift from the tag")

    def test_runs_only_brunnas_two_read_only_answers(self):
        self.assertEqual(self.src.count("$.process.run("), 1)
        self.assertIn("$.process.run([exe, ...args], init)", self.src)
        places = re.search(r"for \(const exe of \[(.*?)\]\)", self.src).group(1)
        self.assertEqual([x for x in re.findall(r"[`'](?:\$\{home\})?([^`']*)[`']", places) if x],
                         ["/.local/bin/brunna", "brunna"])
        calls = re.findall(r"brunna\(\$, \[([^\]]*)\]\)", self.src)
        self.assertEqual({re.match(r"\s*'([a-z]+)'", c).group(1) for c in calls}, {"surface", "glance"})
        self.assertTrue(all("'--json'" in c for c in calls))
        self.assertIn("cwd: home", self.src, "a neutral folder, never the session's")
        self.assertIn("env: BLANK", self.src, "every variable tests/test_surface.py checks, blank")

    def test_nothing_into_the_models_context(self):
        events = set(re.findall(r"\bon\('([a-z.]+)'", self.src))
        self.assertEqual(events, {"session.start", "command.run", "ui.render"})
        for banned in ("$.tool.", "$.agent.", "prompt.submit", "$.prompt.submit", "$.ui.toast", "$.ui.notify",
                       "$.ui.notice", "$.command.run", "context:", "session.append", "$.model", "$.http", "$.fs",
                       "Markdown", "Link", "import(", "$.audio", "$.settings", "$.config", "$.env.set"):
            self.assertFalse(banned in self.src, banned)

    def test_commands_allowlist_and_no_text(self):
        registered = set(re.findall(r"\$\.command\.register\(\{\s*name:\s*'([^']+)'", self.src))
        self.assertEqual(registered, CC_ALLOWED)
        self.assertIn("const ALLOWED = ['inbox', 'brunna'] as const", self.src)
        hooks = _hook_bodies(self.src, "command.run")
        self.assertEqual({re.search(r"command:\s*'([^']+)'", m).group(1) for m, _b in hooks}, CC_ALLOWED)
        for matcher, body in hooks:
            with self.subTest(matcher):
                self.assertEqual(re.findall(r"\breturn\b[^\n]*", body), ["return {}"], "a command's output reaches the model")
                self.assertNotIn("next", body)

    def test_presses_fill_and_never_submit(self):
        fills = re.findall(r"\$\.prompt\.fill\(\{([^}]*)\}\)", self.src)
        self.assertEqual(len(fills), 2)
        for f in fills:
            self.assertRegex(f.strip(), r"^text: (prompt|item\.prompt \?\? ''(, mode: 'append')?)$",
                             "only the platform's words, never the text")
        self.assertIn("item.project === s.project", self.src, "a press only for the session's own project")
        self.assertIn("readOnly = s?.owner.side === 'assistant'", self.src)
        self.assertIn("command[0] === 'brunna'", self.src)

    def test_a_newer_release_is_shown_and_copied_only(self):
        """The command is a constant, drawn and copied; what is run stays brunna's two answers (the tests above)."""
        self.assertIn("const UPGRADE = 'brunna upgrade'", self.src)
        self.assertEqual(len(re.findall(r"\bUPGRADE\b", self.src)), 3)
        self.assertIn("$.ui.copy({ text: UPGRADE, ", self.src)

    def test_contract_majors(self):
        self.assertIn("const SURFACE_V = 1", self.src)
        self.assertIn("const GLANCE_V = 1", self.src)
        for name, v in (("session-surface", 1), ("glance", 1)):
            schema = json.loads((ROOT / "contracts" / f"{name}.v{v}.schema.json").read_text())
            self.assertEqual(schema["properties"]["v"], {"const": v})

    @unittest.skipIf(names.env("TEST_RUNNER") or names.env("TEST_GUARD") or not shutil.which("claude"),
                     "a test run may not start the real coding agent: run `claude plugin validate integrations/claude-code`"
                     " (and on the repo root, for the marketplace) by hand")
    def test_claude_plugin_validate(self):
        with tempfile.TemporaryDirectory() as d:
            for target in (CC, ROOT):
                r = subprocess.run(["claude", "plugin", "validate", str(target)], capture_output=True, text=True,
                                   timeout=120, env={**os.environ, "CLAUDE_CONFIG_DIR": d})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("answers its own command: command.run{command=inbox}", r.stdout)


if __name__ == "__main__":
    unittest.main()
