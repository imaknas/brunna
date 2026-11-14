#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The platform's bridge to a remote MCP server, for one unattended run (docs/design/platform-credentials.md C5).

llm.py attaches it as a tool server of a credentials job's run; the coding agent starts it (stdio, newline-delimited
JSON-RPC) and it forwards to the server's streamable-HTTP endpoint with the platform's credential, which only it reads:
the token never enters the agent's environment, arguments, config or the run's folder.

    credential_bridge.py --run <run folder>/credential-<id>.json

The run file (written by llm.py, 600) names the project, the job and the credential. It is read once, without following
a link, and only from the platform's run folders (as bin/mcp_server.py reads its own); the job's credentials and the
tools it may call are then taken again from the registry (the runtime's credential_tools port), and anything that
doesn't match refuses every call.

What passes, and how:
- initialize (the client's capabilities stripped: no roots, sampling or elicitation offered upstream), ping,
  notifications/initialized and notifications/cancelled, tools/list (only the job's tools listed) and tools/call of one
  of them. Anything else, batches included, gets a JSON-RPC error; a request the server sends to the client is answered
  upstream with an error and never passed on.
- Requests go to the exact server URL only: no redirect is followed (a 3xx is an error), no proxy or CA from the
  environment, no other process started but the one fixed refresh below.
- What goes to the client is parsed and re-encoded JSON, every value the credential holds redacted from it.
- A 401/403 starts one detached `credentials.py refresh … --failed <hash>` (Claude kills its own children at the end
  of a run) and waits up to 20 s for it, then retries once; still refused, the call gets an error and the status says so.

It writes <run folder>/credential-<id>.status.json (initialized, tools listed, auth failures, whether each recovered,
the last error) and credential-<id>.log (method, tool, status, duration; no value): llm.py fails the run when the
bridge never initialized or ended on an auth failure, whatever the model said.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import credentials  # noqa: E402
import hubconf  # noqa: E402
import i18n  # noqa: E402
import names  # noqa: E402

_ = i18n.domain(__file__)

PROTOCOL = "2025-11-25"
METHODS = ("initialize", "ping", "tools/list", "tools/call")
NOTIFICATIONS = ("notifications/initialized", "notifications/cancelled")
RUN_FILE_MAX = 1 << 16
REFRESH_WAIT_S = 20
LOCATIONS = ("ROOT", "HOME", "CONFIG", "STATE", "DATA", "SECRETS", "EVALS")


class AuthFailed(Exception):
    pass


class Refused(Exception):
    pass


def header_for(sec: dict, cred: dict) -> dict:
    """The header the server takes the credential in: Authorization: Bearer <token> (an OAuth grant, a static value by
    default), or the static entry's own header with its value as is."""
    if cred.get("kind") == "static":
        name = (sec.get("server") or {}).get("header") or cred.get("header") or "Authorization"
        return {name: f"Bearer {cred['value']}" if name.lower() == "authorization" else cred["value"]}
    return {"Authorization": f"Bearer {cred['access_token']}"}


def secrets_of(cred: dict | None) -> list[str]:
    if not cred:
        return []
    vals = [cred.get("access_token"), cred.get("refresh_token"), cred.get("value"),
            (cred.get("client") or {}).get("client_secret")]
    return [v for v in vals if isinstance(v, str) and len(v) >= 6]


class Upstream:
    """One MCP session over streamable HTTP to the exact URL, with the given auth header."""

    def __init__(self, url: str, header: dict, op=None):
        self.url, self.header, self.session, self.proto = url, header, None, None
        self.op = op or credentials.opener()

    def send(self, msg: dict, on_message) -> int:
        """POST one message; each message the answer carries (JSON or SSE events) goes to on_message as it arrives.
        → the HTTP status. AuthFailed on 401/403, Refused on a redirect or another error."""
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "User-Agent": f"{names.current('cli')}-bridge", **self.header}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.proto:
            headers["MCP-Protocol-Version"] = self.proto
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), method="POST", headers=headers)
        try:
            r = self.op.open(req, timeout=credentials.HTTP_TIMEOUT_S * 4)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthFailed(e.code)
            if 300 <= e.code < 400:
                raise Refused(_("the server answered with a redirect ({status}), which the bridge never follows",
                                status=e.code))
            raise Refused(_("the server answered HTTP {status}", status=e.code))
        except (urllib.error.URLError, OSError) as e:
            raise Refused(_("the server couldn't be reached ({why})", why=getattr(e, "reason", e)))
        with r:
            if r.headers.get("Mcp-Session-Id"):
                self.session = r.headers["Mcp-Session-Id"]
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if r.status in (202, 204):
                return r.status
            if ctype == "text/event-stream":
                data = []
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif line == "" and data:
                        self._deliver("\n".join(data), on_message)
                        data = []
                if data:
                    self._deliver("\n".join(data), on_message)
            else:
                body = r.read()
                if body:
                    self._deliver(body.decode("utf-8", "replace"), on_message)
            return r.status

    def _deliver(self, text: str, on_message) -> None:
        try:
            m = json.loads(text)
        except ValueError:
            return
        for one in (m if isinstance(m, list) else [m]):
            if isinstance(one, dict):
                on_message(one)

    def call(self, msg: dict) -> dict | None:
        """A request and its response (server requests on the way answered with an error)."""
        out = {}

        def take(m):
            if m.get("id") == msg.get("id") and "method" not in m:
                out["r"] = m
            elif "method" in m and "id" in m:
                self.decline(m)
        self.send(msg, take)
        return out.get("r")

    def decline(self, m: dict) -> None:
        """A request the server sends to the client: never passed on, answered with an error."""
        try:
            self.send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601,
                                                                  "message": "not offered by this client"}}, lambda _m: None)
        except (AuthFailed, Refused):
            pass

    def list_tools(self) -> list[str]:
        """initialize + tools/list (the grant's check): the server's tool names."""
        init = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": names.current("cli"), "version": "0"}}})
        if not init or "result" not in init:
            raise Refused(_("initialize was refused: {error}", error=(init or {}).get("error")))
        self.proto = (init["result"] or {}).get("protocolVersion") or PROTOCOL
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, lambda _m: None)
        r = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = ((r or {}).get("result") or {}).get("tools") or []
        return [t.get("name") for t in tools if isinstance(t, dict)]


class Run:
    """The run file and what the registry says now. refusal: why every call is refused (None: it serves)."""

    def __init__(self, path: str):
        self.refusal, self.project, self.job, self.name, self.sec, self.tools = None, None, None, None, None, ()
        self.dir = pathlib.Path(path).parent
        # the server id from the file's name (credential-<id>.json), so a refused run's status is where llm.py looks
        name = pathlib.Path(path).name
        self.id = name[len("credential-"):-len(".json")] if name.startswith("credential-") and name.endswith(".json") \
            and len(name) > len("credential-.json") else "x"
        try:
            self._load(pathlib.Path(path))
        except Exception as e:
            self.refusal = _("this run's server is refused: {why}", why=e)

    def _load(self, path: pathlib.Path) -> None:
        base = pathlib.Path(os.path.realpath(hubconf.STATE / "agent-jobs" / ".settings"))
        folder = pathlib.Path(os.path.realpath(path.parent))
        if not (base in folder.parents):
            raise ValueError(_("its file {file} is not in the platform's run folders", file=path))
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise ValueError(_("its file {file} is not a file of this user that only it can read", file=path))
            raw = os.read(fd, RUN_FILE_MAX + 1)
        finally:
            os.close(fd)
        if len(raw) > RUN_FILE_MAX:
            raise ValueError(_("its file {file} is too large", file=path))
        d = json.loads(raw)
        if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("project", "job", "credential")):
            raise ValueError(_("its file {file} doesn't name a project, a job and a credential", file=path))
        self.project, self.job, self.name = d["project"], d["job"], d["credential"]
        # the registry's index as it is (never refreshed here: that starts git, in a process holding the agent's env)
        idx = {"projects": hubconf.projects()}
        doc, sec = credentials.entry(self.project, self.name, idx)
        self.sec, self.id = sec, sec["server"]["id"]
        job = next((j for j in doc.get("jobs") or [] if isinstance(j, dict) and j.get("name") == self.job), None)
        if job is None or self.name not in (job.get("credentials") or []):
            raise ValueError(_("{project}'s job {job} doesn't list the credential {name}", project=self.project,
                               job=self.job, name=self.name))
        import runtime_ports
        rt = runtime_ports.load(None, ports=("credential_tools",))
        tools = tuple(rt.rules_tools_named(job.get("allow") or [], self.id))
        if set(tools) != set(d.get("tools") or []):
            raise ValueError(_("its tools are not the ones the job's rules name"))
        self.tools = tools


class Bridge:
    def __init__(self, run: Run, out=None, op=None):
        self.run, self.out = run, out or sys.stdout
        self.status = {"initialized": False, "tools_listed": None, "auth_failures": 0, "unrecovered": False,
                       "last_error": run.refusal}
        self.cred = None
        self.up = None
        if run.refusal is None:
            try:
                self.cred = credentials.load(run.project, run.name)
                if self.cred is None:
                    raise ValueError(_("{project}/{name} is not granted", project=run.project, name=run.name))
                self.up = Upstream(run.sec["server"]["url"], header_for(run.sec, self.cred), op=op)
            except Exception as e:
                run.refusal = _("this run's server is refused: {why}", why=e)
                self.status["last_error"] = run.refusal
        self.save()

    # --- files the run's folder holds ---
    def save(self) -> None:
        p = self.run.dir / f"credential-{self.run.id}.status.json"
        try:
            tmp = p.with_name(f".{p.name}.{os.getpid()}")
            tmp.write_text(json.dumps({**self.status, "at": time.time()}))
            os.replace(tmp, p)
        except OSError:
            pass

    def log(self, method, tool, status, started) -> None:
        try:
            with open(self.run.dir / f"credential-{self.run.id}.log", "a") as f:
                f.write(json.dumps({"method": method, "tool": tool, "status": status,
                                    "ms": int((time.time() - started) * 1000)}) + "\n")
        except OSError:
            pass

    # --- to the client ---
    def write(self, msg: dict) -> None:
        text = json.dumps(msg)
        for s in secrets_of(self.cred):
            text = text.replace(s, "[redacted]")
        self.out.write(text + "\n")
        self.out.flush()

    def error(self, mid, code: int, message: str) -> None:
        if mid is not None:
            self.write({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

    # --- one message from the client ---
    def handle(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return self.error(None, -32700, "parse error")
        if isinstance(msg, list):
            return self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batches are not taken"}})
        if not isinstance(msg, dict):
            return
        method, mid = msg.get("method"), msg.get("id")
        if method is None:
            return   # a reply to a server request, which the bridge never passes on
        if self.run.refusal:
            return self.error(mid, -32001, self.run.refusal)
        if isinstance(method, str) and method.startswith("notifications/"):
            if method in NOTIFICATIONS:
                self.forward(msg, None)
            return
        if method not in METHODS:
            return self.error(mid, -32601, _("{method} is not offered by this server", method=method))
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        tool = params.get("name") if method == "tools/call" else None
        if method == "tools/call" and tool not in self.run.tools:
            return self.error(mid, -32602, _("the tool {tool} is not one this run may call", tool=tool))
        if method == "initialize":
            msg = {**msg, "params": {**params, "capabilities": {}}}
        self.forward(msg, tool)

    def forward(self, msg: dict, tool) -> None:
        started, mid, method = time.time(), msg.get("id"), msg.get("method")

        def take(m):
            if "method" in m and "id" in m:
                self.up.decline(m)          # sampling, elicitation, roots…: never to the client
                return
            if "method" in m:
                return                      # the server's notifications stay with the bridge
            if m.get("id") != mid:
                return
            if method == "tools/list" and isinstance(m.get("result"), dict):
                listed = [t for t in m["result"].get("tools") or [] if isinstance(t, dict)]
                m["result"]["tools"] = [t for t in listed if t.get("name") in self.run.tools]
                self.status["tools_listed"] = len(m["result"]["tools"])
            if method == "initialize" and isinstance(m.get("result"), dict):
                self.up.proto = m["result"].get("protocolVersion") or PROTOCOL
                self.status["initialized"] = True
            self.write(m)
        status = None
        for attempt in (1, 2):
            try:
                status = self.up.send(msg, take)
                self.status["unrecovered"] = False
                break
            except AuthFailed as e:
                status = e.args[0]
                self.status["auth_failures"] += 1
                if attempt == 1 and self.refresh():
                    continue
                self.status.update(unrecovered=True, last_error=_("the server refused the credential ({status})",
                                                                   status=status))
                self.error(mid, -32001, _("the server refused the platform's credential; the owner has been told"))
                break
            except Refused as e:
                self.status["last_error"] = str(e)
                self.error(mid, -32002, str(e))
                break
        self.save()
        self.log(method, tool, status, started)

    def refresh(self) -> bool:
        """One detached refresh for the token just refused; → whether a new one is there to retry with."""
        # a static value is refused for good: the refresh then marks it as needing the owner (alert, to-do)
        failed = hashlib.sha256((self.cred.get("access_token") or self.cred.get("value") or "").encode()).hexdigest()
        env = {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin", "LANG": os.environ.get("LANG", "C")}
        for k in LOCATIONS:
            if (v := names.env(k)):
                names.env_set(env, k, v)
        try:
            p = subprocess.Popen([sys.executable, "-I", str(BIN / "credentials.py"), "refresh", self.run.project,
                                  self.run.name, "--failed", failed], env=env, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait(timeout=REFRESH_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            return False
        try:
            cred = credentials.load(self.run.project, self.run.name)
        except (OSError, ValueError):
            return False
        if not cred or cred.get("kind") != "oauth" or cred.get("access_token") == self.cred.get("access_token"):
            return False
        self.cred = cred
        self.up.header = header_for(self.run.sec, cred)
        return True


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "--run":
        print(_("usage: credential_bridge.py --run <run file>"), file=sys.stderr)
        return 2
    credentials.scrub_env()
    bridge = Bridge(Run(argv[1]))
    for line in sys.stdin:
        if line.strip():
            bridge.handle(line)
    bridge.save()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
