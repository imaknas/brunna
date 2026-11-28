"""A service's manifest (bin/service_manifest.py, contracts/service-manifest.v1.schema.json;
docs/design/service-ecosystem.md)."""
from __future__ import annotations

import contextlib
import copy
import io
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import service_manifest as sm  # noqa: E402

EXAMPLE = ROOT / "contracts" / "fixtures" / "service-manifest.v1.example.yaml"


def example() -> dict:
    doc, errs = sm.parse(EXAMPLE.read_text())
    assert not errs, errs
    return doc


class Manifest(unittest.TestCase):
    def bad(self, doc) -> list[str]:
        errs = sm.problems(doc)
        self.assertTrue(errs, doc)
        return errs

    def test_example_is_valid(self):
        self.assertEqual(sm.problems(example()), [])

    def test_smallest(self):
        doc = {"v": 1, "name": "ab", "what": "x", "interface": 1, "cli": {"run": "a"}, "mcp": {"run": "b"}}
        self.assertEqual(sm.problems(doc), [])

    def test_required_and_unknown(self):
        for key in ("v", "name", "what", "interface", "cli", "mcp"):
            with self.subTest(key=key):
                doc = example()
                del doc[key]
                self.bad(doc)
        doc = example()
        doc["port"] = 8080   # nothing of a machine: unknown keys refused
        self.bad(doc)

    def test_field_shapes(self):
        cases = [("name", "Has-Caps"), ("name", "a"), ("interface", 0), ("interface", "2"), ("v", 2),
                 ("principal", "yes"), ("cli", {"run": "a", "port": 1}), ("mcp", {"run": "a\nrm -rf ~"}),
                 ("setup", ["ok", ""]), ("network", ["127.0.0.1"]), ("network", ["https://api.example.com"]),
                 ("models", {"judge": {"tier": "huge"}}), ("models", {"Judge": {"tier": "fast"}}),
                 ("secrets", [{"name": "lower", "what": "x"}]), ("secrets", [{"name": "A_KEY"}])]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                doc = example()
                doc[key] = value
                self.bad(doc)

    def test_backup_false_needs_why(self):
        doc = example()
        doc["data"] = [{"path": "cache", "what": "x", "backup": False}]
        self.bad(doc)
        doc["data"][0]["why"] = "downloaded again"
        self.assertEqual(sm.problems(doc), [])

    def test_data_paths(self):
        for raw in ("/abs", "~/x", "../up", "a/../b", "a//b", "./a", "a/*", "a[1]", "a\\b", "a\x01"):
            with self.subTest(path=raw):
                doc = example()
                doc["data"] = [{"path": raw, "what": "x"}]
                self.bad(doc)
        doc = example()
        doc["data"] = [{"path": "results", "what": "x"}, {"path": "results/old", "what": "y"}]
        self.bad(doc)
        doc["data"] = [{"path": "results/", "what": "x"}, {"path": "results-old", "what": "y"}]
        self.assertEqual(sm.problems(doc), [])

    def test_secret_names(self):
        for name in ("PATH", "HOME", "BRUNNA_CALLER", "AGENTHUB_PROJECT", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
                     "PYTHONPATH", "NODE_OPTIONS", "HTTPS_PROXY"):
            with self.subTest(name=name):
                doc = example()
                doc["secrets"] = [{"name": name, "what": "x"}]
                self.bad(doc)
        doc = example()
        doc["secrets"] = [{"name": "A_KEY", "what": "x"}, {"name": "A_KEY", "what": "y"}]
        self.bad(doc)

    def test_trailing_newline_and_json_types(self):
        for key, value in (("name", "ab\n"), ("cli", {"run": "a\n"}), ("network", ["api.example.com\n"]),
                           ("secrets", [{"name": "A_KEY\n", "what": "x"}]), ("v", True), ("v", 1.0)):
            with self.subTest(key=key, value=value):
                doc = example()
                doc[key] = value
                self.bad(doc)

    def test_more_paths_and_names(self):
        for data in ([{"path": "Results", "what": "x"}, {"path": "results/a", "what": "y"}],
                     [{"path": ".brunna/outbox", "what": "x"}], [{"path": ".Brunna", "what": "x"}]):
            with self.subTest(data=data):
                doc = example()
                doc["data"] = data
                self.bad(doc)
        for name in ("PIP_INDEX_URL", "XDG_CONFIG_HOME", "PERL5OPT", "RUBYOPT", "JAVA_TOOL_OPTIONS", "TZ", "CARGO_HOME"):
            with self.subTest(name=name):
                doc = example()
                doc["secrets"] = [{"name": name, "what": "x"}]
                self.bad(doc)

    def test_odd_yaml_keys(self):
        for text in ("? [a]\n: 1\n", "? {a: 1}\n: 1\n", "a: &b {x: 1}\nc:\n  <<: *b\n", "a:\n  b: 1\n  b: 2\n"):
            with self.subTest(text=text):
                doc, errs = sm.parse(text)
                self.assertIsNone(doc)
                self.assertTrue(errs)

    def test_not_a_mapping(self):
        for doc in (None, [], "text"):
            with self.subTest(doc=doc):
                self.bad(doc)

    def test_duplicate_yaml_key(self):
        doc, errs = sm.parse("v: 1\nname: ab\nname: cd\n")
        self.assertIsNone(doc)
        self.assertTrue(errs)

    def test_example_untouched_by_checks(self):
        doc = example()
        before = copy.deepcopy(doc)
        sm.problems(doc)
        self.assertEqual(doc, before)


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for redirect in (contextlib.redirect_stdout, contextlib.redirect_stderr):
            ctx = redirect(io.StringIO())
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)

    def test_folder_and_file(self):
        shutil.copy(EXAMPLE, self.tmp / sm.FILE)
        self.assertEqual(sm.main([str(self.tmp)]), 0)
        self.assertEqual(sm.main([str(self.tmp / sm.FILE)]), 0)

    def test_invalid_and_missing(self):
        (self.tmp / sm.FILE).write_text("v: 1\n")
        self.assertEqual(sm.main([str(self.tmp)]), 1)
        self.assertEqual(sm.main([str(self.tmp / "nope")]), 1)
        self.assertEqual(sm.main([]), 2)


if __name__ == "__main__":
    unittest.main()
