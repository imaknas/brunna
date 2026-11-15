#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""Idea sandboxes that have grown into projects -- a mechanical check at zero LLM cost (docs/design/idea-sandboxes.md,
"Growing up").

An idea sandbox (registry `type: idea`) is opened to explore; some of them turn into real work and nobody notices until
a session hits a wall (no remote, no gh). This lists every idea sandbox whose registry entry already declares what a
throwaway exploration doesn't need -- the signals:

  secrets        it keeps credentials of its own
  tests          it declares test tiers (the merge gate runs them)
  metered        the owner allowed it a provider billed per use (metered_models)
  services/jobs  something of it runs on its own, long-lived or on a schedule
  network        it reaches hosts beyond the package indexes (PACKAGE_HOSTS)

Only registry facts: commit counts and ages would be guesses. Listing is a question for the owner, never an upgrade:
upgrading is `bin/upgrade-idea.py` (a GitHub repo, the type), which only the owner starts. An owner who wants it to stay
an idea says so once with `--keep <project>`: the signals it has then are recorded, and it is listed again only when a
new one appears.

Usage: check-idea-upgrades.py [--project P] [--apply] | --keep P
Output: one markdown line per idea sandbox to ask about, nothing at all otherwise.
--apply (platform job brunna/idea-upgrades, nightly after the librarian and todo): the lines go into todo.md's fixed
  block (removed when there are none) and the idea-upgrades section of today's digest, and only a one-line summary is
  printed -- whatever the result. todo.md is regenerated as a whole by gen-todo.py, so this job runs after todo.
"""
import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import hublib  # noqa: E402
import i18n  # noqa: E402

_ = i18n.domain(__file__)

# hosts any exploration may need to install things; reaching anything else is a signal
PACKAGE_HOSTS = frozenset({
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "registry.yarnpkg.com", "crates.io", "static.crates.io",
    "index.crates.io", "proxy.golang.org", "sum.golang.org", "rubygems.org", "repo.maven.apache.org",
    "github.com", "objects.githubusercontent.com", "codeload.github.com", "huggingface.co", "cdn-lfs.huggingface.co",
})
KEPT = hubconf.STATE / "idea-upgrades-kept.json"


def signals(doc: dict) -> list[str]:
    """The signals this registry entry shows, as stable codes, in a fixed order."""
    out = []
    if doc.get("secrets"):
        out.append("secrets")
    if doc.get("tests"):
        out.append("tests")
    if doc.get("metered_models"):
        out.append("metered")
    if doc.get("services") or doc.get("jobs"):
        out.append("runs")
    hosts = [h for h in doc.get("network") or [] if isinstance(h, str)]
    if any(h.lower().lstrip("*.") not in PACKAGE_HOSTS for h in hosts):
        out.append("network")
    return out


def describe(code: str, doc: dict) -> str:
    """a signal, in the UI language"""
    names = lambda key: ", ".join(str(x.get("name")) for x in doc.get(key) or [] if isinstance(x, dict))  # noqa: E731
    return {
        "secrets": lambda: _("credentials of its own ({names})", names=names("secrets")),
        "tests": lambda: _("declared tests"),
        "metered": lambda: _("a provider billed per use"),
        "runs": lambda: _("services or jobs that run on their own"),
        "network": lambda: _("network access beyond package indexes ({hosts})", hosts=", ".join(
            h for h in doc.get("network") or [] if isinstance(h, str) and h.lower().lstrip("*.") not in PACKAGE_HOSTS)),
    }[code]()


def kept() -> dict:
    try:
        d = json.loads(KEPT.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def scan(only: str | None) -> list[str]:
    """one to-do line per idea sandbox to ask about"""
    held = kept()
    lines = []
    for name, doc in sorted(hubconf.projects().items()):
        if (only and name != only) or doc.get("type") != "idea":
            continue
        sig = signals(doc)
        if not sig or set(sig) <= set(held.get(name) or []):
            continue
        lines.append("- [ ] " + _(
            "**{project}**: an idea sandbox that already has {signals}. Upgrade it to a project? "
            "`bin/upgrade-idea.py {project} [--public]` (a dry run first; the sandbox stays on); "
            "to keep it an idea: `bin/check-idea-upgrades.py --keep {project}`",
            project=name, signals="; ".join(describe(s, doc) for s in sig)))
    return lines


def keep(name: str) -> int:
    doc = hubconf.projects().get(name)
    if not doc or doc.get("type") != "idea":
        print(_("{project} is not an idea sandbox", project=name), file=sys.stderr)
        return 1
    held = kept()
    held[name] = signals(doc)
    KEPT.parent.mkdir(parents=True, exist_ok=True)
    KEPT.write_text(json.dumps(held, ensure_ascii=False, indent=1) + "\n")
    print(_("{project} stays an idea; asked again only when a new signal appears", project=name))
    return 0


def heading() -> str:
    """the heading of the digest section and the todo.md block (looked up per call: the language is read at run time)"""
    return _("Idea sandboxes that look like projects")


def write_todo_block(todo: pathlib.Path, lines: list[str]) -> None:
    """todo.md's <!-- idea-upgrades:begin/end --> block: put at the end of the file when there is one to ask about,
    removed when there is none. Nothing is done when todo.md does not exist yet."""
    if not todo.exists():
        return
    s = re.sub(r"\n*<!-- idea-upgrades:begin -->.*?<!-- idea-upgrades:end -->\n?", "\n", todo.read_text(),
               flags=re.S).rstrip() + "\n"
    if lines:
        why = _("Their registry entries declare what an exploration doesn't need. Upgrading gives a project its GitHub "
                "repo and type; only the owner decides it.")
        s += ("\n<!-- idea-upgrades:begin -->\n## " + heading() + _(" (mechanical check)") + "\n\n" + why + "\n\n"
              + "\n".join(lines) + "\n<!-- idea-upgrades:end -->\n")
    todo.write_text(s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--keep", metavar="PROJECT")
    args = ap.parse_args()
    if args.keep:
        return keep(args.keep)
    lines = scan(args.project)
    if not args.apply:
        if lines:
            print("\n".join(lines))
        return 0
    write_todo_block(hubconf.data_file("todo.md"), lines)
    hublib.digest_append("idea-upgrades", f"## {heading()}\n\n" + "\n".join(lines) if lines else "")
    print(_("idea upgrade check: {n} {n:idea sandbox|idea sandboxes} to ask about", n=len(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
