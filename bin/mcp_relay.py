"""A service's MCP server as a child of `brunna mcp` (docs/design/service-ecosystem.md, step 3, "Relaying").

The child runs in a process group of its own (killed with stop(), and when `brunna mcp` exits: its stdin closes and
an MCP server ends on that); its stdout is read on a thread (a line that isn't JSON is skipped, one over MAX_LINE kills
it); its stderr is drained to a capped log. Requests to it carry this server's own ids. A request it makes to the
client (sampling, elicitation, roots) is answered with an error; its notifications are dropped.
Standard library only.
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import signal
import subprocess
import threading
import time

MAX_LINE = 4 << 20
LOG_MAX = 1 << 20
PROTOCOL = "2025-06-18"


class ChildError(Exception):
    pass


class Child:
    def __init__(self, name: str, argv: list[str], cwd: pathlib.Path, env: dict[str, str], log: pathlib.Path):
        self.name, self.argv, self.cwd, self.env, self.log = name, argv, cwd, env, log
        self.p: subprocess.Popen | None = None
        self.out: queue.Queue = queue.Queue()
        self.next_id = 0
        self.tools: list[dict] = []

    # ---------- process ----------
    def start(self) -> None:
        self.p = subprocess.Popen(self.argv, cwd=str(self.cwd), env=self.env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def stop(self) -> None:
        if self.p is None:
            return
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _read_out(self) -> None:
        stream = self.p.stdout
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                self.out.put(None)
                return
            if len(line) > MAX_LINE:
                self.stop()
                self.out.put(None)
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.out.put(msg)

    def _drain_err(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        stream = self.p.stderr
        while (chunk := stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)):
            try:
                if self.log.exists() and self.log.stat().st_size > LOG_MAX:
                    os.replace(self.log, self.log.with_suffix(self.log.suffix + ".1"))
                with open(self.log, "ab") as fh:
                    fh.write(chunk)
            except OSError:
                pass

    # ---------- messages ----------
    def _send(self, msg: dict) -> None:
        try:
            self.p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
            self.p.stdin.flush()
        except (OSError, ValueError) as e:
            raise ChildError(f"{self.name}: its server has stopped ({e})") from None

    def request(self, method: str, params: dict, timeout: float, while_waiting=None) -> dict:
        """Send a request; its result, or ChildError. while_waiting(child, our_id) is called about every 0.2 s (the
        caller answers the client's pings and forwards a cancel there)."""
        self.next_id += 1
        rid = f"brunna-{self.next_id}"
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise ChildError(f"{self.name}: no answer to {method} in {int(timeout)} s")
            try:
                msg = self.out.get(timeout=min(0.2, left))
            except queue.Empty:
                if while_waiting:
                    while_waiting(self, rid)
                continue
            if msg is None:
                raise ChildError(f"{self.name}: its server has stopped")
            if "method" in msg:   # its own request to the client, or a notification
                if msg.get("id") is not None:
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "not supported through brunna"}})
                continue
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {}
                raise ChildError(f"{self.name}: {err.get('message') or 'error'}")
            return msg.get("result") if isinstance(msg.get("result"), dict) else {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def cancel(self, rid: str, reason: str = "") -> None:
        try:
            self.notify("notifications/cancelled", {"requestId": rid, "reason": reason})
        except ChildError:
            pass

    # ---------- MCP ----------
    def handshake(self, timeout: float) -> None:
        """initialize, then every page of tools/list, within timeout."""
        end = time.monotonic() + timeout
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "brunna", "version": "1"}}, timeout)
        self.notify("notifications/initialized", {})
        tools, cursor = [], None
        for _page in range(50):
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}, max(0.1, end - time.monotonic()))
            tools += [t for t in res.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
