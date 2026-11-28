#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""A service's manifest, `brunna-service.yaml` at its repository's root (docs/design/service-ecosystem.md; contract
contracts/service-manifest.v1.schema.json): the schema, plus what a schema can't say.

  - data paths are relative to the service's state folder: no leading /, no ~, no .. step, no glob, bracket, brace
    or control character, and no two entries where one is inside the other;
  - secret names are unique and none is an environment name the platform sets or a process depends on (BRUNNA_* and
    AGENTHUB_* are the platform's, BRUNNA_CALLER above all; PATH, HOME, LD_*, DYLD_* and the like would change how the
    service runs, not give it a key);
  - model names follow the registry's rule (bin/registry.py MODEL_POINT).

The manifest is read as YAML with duplicate keys refused (a later key silently winning would make what is shown at
install differ from what a reader of the file sees).

  service_manifest.py <file or folder>     check one; prints each problem, exit 1 if any
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import i18n  # noqa: E402
import minischema  # noqa: E402

_ = i18n.domain(__file__)

FILE = "brunna-service.yaml"
SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "contracts" / "service-manifest.v1.schema.json"
MODEL_POINT = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")   # registry.MODEL_POINT (registry imports too much to share)
PLAIN = re.compile(r"[^\x00-\x1f\x7f()\[\]{}*?]+\Z")
RESERVED_PREFIXES = ("BRUNNA_", "AGENTHUB_", "LD_", "DYLD_", "PYTHON", "NODE_", "UV_", "NPM_", "PIP_", "GIT_", "SSH_",
                     "LC_", "XDG_", "PERL5", "RUBY", "CARGO_", "RUSTUP_", "BUN_", "DENO_")
RESERVED = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "LANG", "TERM", "PWD", "IFS",
            "ENV", "BASH_ENV", "PROMPT_COMMAND", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "JAVA_TOOL_OPTIONS",
            "_JAVA_OPTIONS", "TZ"}
PLATFORM_DIR = ".brunna"   # in the state folder: the service's platform mail (outbox, inbox view), never its data


def _reserved(name: str) -> bool:
    up = name.upper()
    return up in RESERVED or up.startswith(RESERVED_PREFIXES)


def _loader():
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        seen = set()
        for k, _v in node.value:
            if k.tag == "tag:yaml.org,2002:merge":   # `<<: *anchor` would hide keys from a reader of the file
                raise yaml.constructor.ConstructorError(None, None, _("merge keys (<<) are not allowed"), k.start_mark)
            key = loader.construct_object(k, deep=deep)
            if not isinstance(key, (str, int, float, bool, type(None))):
                raise yaml.constructor.ConstructorError(None, None, _("a key must be a plain value"), k.start_mark)
            if key in seen:
                raise yaml.constructor.ConstructorError(None, None, _("duplicate key {key!r}", key=key), k.start_mark)
            seen.add(key)
        return loader.construct_mapping(node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return Strict


def parse(text: str) -> tuple[object, list[str]]:
    """The YAML document, or None with why it can't be read."""
    import yaml
    try:
        return yaml.load(text, Loader=_loader()), []   # noqa: S506 (a SafeLoader subclass)
    except yaml.YAMLError as e:
        return None, [_("not readable as YAML: {err}", err=str(e).splitlines()[0] if str(e) else type(e).__name__)]


def _fold(part: str) -> str:
    """A path step as the disk may compare it (case-insensitive, NFC): Results and results are one folder on APFS."""
    import unicodedata
    return unicodedata.normalize("NFC", part).casefold()


def _rel_problem(raw: str) -> str | None:
    if raw.startswith(("/", "~")) or "\\" in raw:
        return _("must be relative to the state folder (no leading / or ~)")
    if not PLAIN.match(raw):
        return _("no glob, bracket, brace or control character")
    parts = raw.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return _("no empty, . or .. step")
    if _fold(parts[0]) == PLATFORM_DIR:
        return _("{dir} in the state folder is the platform's", dir=PLATFORM_DIR)
    return None


def problems(doc) -> list[str]:
    """Every problem with a parsed manifest; empty = valid."""
    errs = minischema.errors(json.loads(SCHEMA.read_text()), doc)
    if not isinstance(doc, dict):
        return errs
    for name in (doc.get("models") or {}) if isinstance(doc.get("models"), dict) else ():
        if not isinstance(name, str) or not MODEL_POINT.match(name):
            errs.append(_("models: name {name!r} must be lowercase letters, digits and -", name=name))
    seen: set[str] = set()
    for i, s in enumerate(doc.get("secrets") or [] if isinstance(doc.get("secrets"), list) else []):
        name = s.get("name") if isinstance(s, dict) else None
        if not isinstance(name, str):
            continue
        if name in seen:
            errs.append(_("secrets[{i}]: {name} is named twice", i=i, name=name))
        seen.add(name)
        if _reserved(name):
            errs.append(_("secrets[{i}]: {name} is an environment name the platform sets or a process depends on",
                          i=i, name=name))
    paths: list[tuple[int, tuple[str, ...]]] = []
    for i, d in enumerate(doc.get("data") or [] if isinstance(doc.get("data"), list) else []):
        raw = d.get("path") if isinstance(d, dict) else None
        if not isinstance(raw, str) or not raw:
            continue
        why = _rel_problem(raw)
        if why:
            errs.append(_("data[{i}]: path {path}: {why}", i=i, path=raw, why=why))
            continue
        parts = tuple(_fold(p) for p in raw.split("/") if p)
        for j, other in paths:
            if parts[:len(other)] == other or other[:len(parts)] == parts:
                errs.append(_("data[{i}]: path {path} overlaps data[{j}]", i=i, path=raw, j=j))
        paths.append((i, parts))
    return errs


def check(path: pathlib.Path) -> tuple[object, list[str]]:
    """Read and check a manifest file (or a folder holding one)."""
    if path.is_dir():
        path = path / FILE
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        return None, [_("can't read {path}: {err}", path=path, err=e)]
    doc, errs = parse(text)
    if errs:
        return None, errs
    return doc, problems(doc)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(_("usage: service_manifest.py <file or folder>"), file=sys.stderr)
        return 2
    _doc, errs = check(pathlib.Path(argv[0]))
    for e in errs:
        print(e)
    if not errs:
        print(_("the manifest is valid"))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
