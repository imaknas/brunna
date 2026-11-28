"""Which services there are on this machine, and who may call each (docs/design/service-ecosystem.md, step 3).

  installed   every installed service (bin/installed_services.py): runs in its own sandbox
  project     a registered project of the owner's whose entry provides {contract: service, v: <major>} and whose
              working tree holds a valid brunna-service.yaml: runs in its workspace, with the owner's rights when the
              project is unsandboxed, in the project's own sandbox when it is (step 3b)

A consumer may call a service when its entry consumes {contract: service, from: <service>, v: <major>} at the service's
current interface major and it is among the service's callers (not set: every registered project, never another
service). A sandboxed consumer (step 3b) only when its session's runtime confines its sessions and the service's
callers names it: never by the default, since every service holds some of the owner's authority. Standard library + PyYAML.
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
    sandboxed: bool = True     # runs in a sandbox (installed, or a sandboxed project's)


def _kind1(name: str, doc: dict) -> Service | None:
    import service_manifest
    if "__" in name:
        return None   # a name holding __ wouldn't split from its tools' names
    declared = [p for p in doc.get("provides") or [] if isinstance(p, dict) and p.get("contract") == CONTRACT]
    if not declared:
        return None
    ws = hubconf.path((doc.get("paths") or {}).get("workspace"))
    if not ws or not ws.is_dir():
        return None
    m, errs = service_manifest.check(ws)
    if errs or not isinstance(m, dict) or m.get("name") != name:
        return None
    if str(declared[0].get("v")) != str(m.get("interface")):
        return None   # what the registry says it provides must be what its manifest says it is
    try:
        words = shlex.split(m["mcp"]["run"])
    except ValueError:
        return None
    return Service(name, "project", m["interface"], m["what"], words, ws, doc.get("callers"), _sandboxed(doc))


def _sandboxed(doc: dict) -> bool:
    """Its sessions are sandboxed (enforce.sandbox), whatever service_sandbox_exempt says of its services and jobs."""
    return (doc.get("enforce") or {}).get("sandbox") is True


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


def consumed(consumer: str, projects: dict | None = None, runtime: str | None = None) -> tuple[list[Service], list[str]]:
    """(the services this project may call, why each one it consumes can't be). runtime: the session's runtime
    (runtime_ports.session_runtime), which a sandboxed consumer's must confine its sessions."""
    projects = hubconf.projects() if projects is None else projects
    doc = projects.get(consumer) or {}
    wanted = [c for c in doc.get("consumes") or [] if isinstance(c, dict) and c.get("contract") == CONTRACT]
    if not wanted:
        return [], []
    boxed = _sandboxed(doc)
    if boxed:
        import runtime_ports
        if runtime is None:
            return [], [_("{project} is sandboxed, and which coding agent runs this session can't be told: no services",
                          project=consumer)]
        if not runtime_ports.sessions_confined(runtime):
            return [], [_("{project} is sandboxed, and {runtime} doesn't confine its sessions: no services",
                          project=consumer, runtime=runtime)]
    have = services(projects)
    ok, why = [], []
    for c in wanted:
        s = have.get(str(c.get("from")))
        if not s:
            why.append(_("{service}: no such service here", service=c.get("from")))
        elif str(c.get("v")) != str(s.interface):
            why.append(_("{service}: consumed at interface {want}, but it is at {have}", service=s.name,
                         want=c.get("v"), have=s.interface))
        elif boxed and consumer not in (s.callers or []):
            why.append(_("{service}: a sandboxed project is served only when the service's callers names it "
                         "(brunna service callers, or its registry callers)", service=s.name))
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
