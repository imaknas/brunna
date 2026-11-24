"""E1 golden (docs/design/i18n.md) for what a session's surface adapter draws (bin/surface.py: the band's lines, the labels
around the data, the commands and their prompt) and the prompt the status page's tick writes on each needs_you item
(bin/gen-status-page.py), on zh-Hant (and on en in the second pass). Built in-process from the functions that make them;
the assistant's name is pinned.

    bin/run-tests tests   (or brunna test)
    I18N_GOLDEN_UPDATE=1 … rewrites it (only on purpose)
"""
import datetime
import importlib.util
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import golden_lang  # noqa: E402

GOLDEN = golden_lang.GOLDEN
UPDATE = os.environ.get("I18N_GOLDEN_UPDATE") == "1"
LANGS = {"AGENT_HUB_LANG_UI": golden_lang.LANG, "AGENT_HUB_LANG_CONTENT": golden_lang.LANG}
NOW = datetime.datetime(2026, 11, 18, 10, 0).astimezone()


def setUpModule():
    global _unpin
    _unpin = golden_lang.pin_names()


def tearDownModule():
    _unpin()


class Golden(unittest.TestCase):
    maxDiff = None

    def compare(self, name: str, text: str):
        f = GOLDEN / name
        if UPDATE or not f.exists():
            f.write_text(text)
            if not UPDATE:
                self.fail(f"new golden written: {f.relative_to(ROOT)} (review it, then rerun)")
            return
        self.assertEqual(text, f.read_text())

    def test_surface(self):
        import surface
        spec = importlib.util.spec_from_file_location("gsp_surface_golden", ROOT / "bin" / "gen-status-page.py")
        gsp = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, LANGS):
            spec.loader.exec_module(gsp)
            old = (NOW - datetime.timedelta(minutes=45)).isoformat()
            held = [{"id": "m1", "from": "x", "type": "request", "since": old}] * 2
            cases = {
                "one need": surface.lines({"needs_you": 1, "health": "ok"}, [], 30, NOW),
                "everything": surface.lines({"needs_you": 3, "health": "problem"}, held, 30, NOW),
                "one held": surface.lines({"needs_you": 0, "health": "ok"}, held[:1], 30, NOW),
                "gated": surface.lines({"needs_you": 0, "health": "ok"}, [], 30, NOW, gated_open=2),
                "gated, one": surface.lines({"needs_you": 0, "health": "ok"}, [], 30, NOW, gated_open=1),
            }
            up = {"current": "v0.22.0", "latest": "v0.23.0", "security": False, "since": ""}
            cases |= {
                "a release": surface.lines({"needs_you": 0, "health": "ok"}, [], 30, NOW, update=up),
                "a security release": surface.lines({"needs_you": 0, "health": "ok"}, [], 30, NOW, update={**up, "security": True}),
                "a security release, and needs": surface.lines({"needs_you": 2, "health": "ok"}, [], 30, NOW,
                                                               update={**up, "security": True}),
            }
            out = {"lines": cases, "labels": surface.labels(),
                   "label, a release": surface.labels(up)["update"],
                   "label, a security release": surface.labels({**up, "security": True})["update"], "commands": surface.commands(inbox=True),
                   "item prompts": [gsp.item_prompt("todo", "garden-app", "task-garden-app-1"),
                                    gsp.item_prompt("question", "garden-app", "m11180900-ab12")],
                   "usage": surface._("usage: brunna surface --runtime <name> --session <id> --cwd <folder> [--json]"),
                   "not served": surface._("not served here ({why})", why="not_recorded")}
        self.compare("surface.json", json.dumps(out, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    unittest.main()
