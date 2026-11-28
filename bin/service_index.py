"""Which services there are on this machine, and who may call each (docs/design/service-ecosystem.md, step 3).

  installed   every installed service (bin/installed_services.py): runs in its own sandbox
  project     a registered, unsandboxed project of the owner's whose entry provides {contract: service, v: <major>}
              and whose working tree holds a valid brunna-service.yaml: runs in its workspace with the owner's rights
              (a sandboxed one is step 3b)

A consumer may call a service when its entry consumes {contract: service, from: <service>, v: <major>} at the service's
current interface major, it is among the service's callers (not set: every registered project, never another
service), and it isn't sandboxed (step 3a). Standard library + PyYAML.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import shlex
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import installed_services as svc  # noqa: E402

_ = i18n.domain(__file__)

CONTRACT = "service"
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
# a client takes tool names of at most 64 characters, and adds its own prefix naming the server to each (a few
# characters plus the server's name): a served name is kept to what leaves room for that
CLIENT_MAX = 64
CLIENT_PREFIX = 7   # beside the server's name


@dataclasses.dataclass
class Service:
    name: str
    kind: str                  # installed | project
    interface: int
    what: str
    mcp: list[str]             # the MCP server's words
    cwd: pathlib.Path
    callers: list[str] | None  # None: every registered project


def _kind1(name: str, doc: dict) -> Service | None:
    import isolation
    import service_manifest
    if isolation.sandboxed({**doc, "name": name}) or "__" in name:
        return None   # a sandboxed one is step 3b; a name holding __ wouldn't split from its tools' names
    if not any(isinstance(p, dict) and p.get("contract") == CONTRACT for p in doc.get("provides") or []):
        return None
    ws = hubconf.path((doc.get("paths") or {}).get("workspace"))
    if not ws or not ws.is_dir():
        return None
    m, errs = service_manifest.check(ws)
    if errs or not isinstance(m, dict) or m.get("name") != name:
        return None
    try:
        words = shlex.split(m["mcp"]["run"])
    except ValueError:
        return None
    return Service(name, "project", m["interface"], m["what"], words, ws, doc.get("callers"))


def services(projects: dict | None = None) -> dict[str, Service]:
    projects = hubconf.projects() if projects is None else projects
    out: dict[str, Service] = {}
    for name, e in svc.entries().items():
        try:
            words = shlex.split(e["run"]["mcp"])
        except ValueError:
            continue
        out[name] = Service(name, "installed", e["interface"], e.get("what") or "", words,
                            svc.folders(name, e["commit"])["code"], e.get("callers"))
    for name, doc in projects.items():
        if name in out or name == hubconf.hub_project():
            continue
        s = _kind1(name, doc)
        if s:
            out[name] = s
    return out


def consumed(consumer: str, projects: dict | None = None) -> tuple[list[Service], list[str]]:
    """(the services this project may call, why each one it consumes can't be)."""
    import isolation
    projects = hubconf.projects() if projects is None else projects
    doc = projects.get(consumer) or {}
    wanted = [c for c in doc.get("consumes") or [] if isinstance(c, dict) and c.get("contract") == CONTRACT]
    if not wanted:
        return [], []
    if isolation.sandboxed({**doc, "name": consumer}):
        return [], [_("{project} is sandboxed: services are served only to unsandboxed projects for now (step 3b)",
                      project=consumer)]
    have = services(projects)
    ok, why = [], []
    for c in wanted:
        s = have.get(str(c.get("from")))
        if not s:
            why.append(_("{service}: no such service here", service=c.get("from")))
        elif str(c.get("v")) != str(s.interface):
            why.append(_("{service}: consumed at interface {want}, but it is at {have}", service=s.name,
                         want=c.get("v"), have=s.interface))
        elif s.callers is not None and consumer not in s.callers:
            why.append(_("{service}: {project} is not among its callers", service=s.name, project=consumer))
        else:
            ok.append(s)
    return ok, why


def tool_name(service: str, tool: str, server: str) -> str | None:
    """The name a service's tool is served under, or None when it can't be (its name, or the name a client makes of
    it, with its prefix and the server's name, over CLIENT_MAX)."""
    if not isinstance(tool, str) or not TOOL_NAME.match(tool):
        return None
    name = f"{service}__{tool}"
    return name if CLIENT_PREFIX + len(server) + len(name) <= CLIENT_MAX else None
