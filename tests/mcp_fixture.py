"""A small remote MCP server and OAuth authorization server for tests (docs/design/platform-credentials.md): local HTTP
on a free port, nothing reaching out. The MCP side answers streamable HTTP (JSON, or SSE when told), logs every
request's method, path, headers and JSON-RPC body, and refuses requests without the expected bearer token. The OAuth side
serves RFC 9728 / RFC 8414 metadata and a token endpoint whose answers a test sets.

    srv = mcp_fixture.Server(self, token="tok")       started, stopped with the test
    srv.url                                          http://127.0.0.1:<port>/mcp
    srv.log                                          [{"method", "path", "headers", "body"}]
    srv.sse = True                                    answer requests as an SSE stream
    srv.server_request = True                         put a sampling request before each SSE answer
    srv.redirect_to = "http://…"                      answer every POST with 302 there
    srv.token_answers = [(status, body), …]           what the token endpoint answers, in turn
"""
import http.server
import json
import threading

TOOLS = ("upwork__get_messages", "upwork__list_offers", "upwork__send_message", "echo_auth")


class Server:
    def __init__(self, test, token: str = "tok"):
        self.token, self.log, self.sse, self.server_request, self.redirect_to = token, [], False, False, None
        self.token_answers, self.metadata = [], {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.log.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "body": None})
                if self.path in outer.metadata:
                    return self._send(200, json.dumps(outer.metadata[self.path]).encode())
                self._send(404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw.decode()
                outer.log.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body})
                if self.path == "/token":
                    code, ans = outer.token_answers.pop(0) if outer.token_answers else (400, {"error": "invalid_grant"})
                    return self._send(code, json.dumps(ans).encode())
                if self.path == "/register":
                    return self._send(*outer.register) if hasattr(outer, "register") else self._send(404)
                if self.path == "/revoke":
                    return self._send(getattr(outer, "revoke_status", 200))
                if outer.redirect_to:
                    return self._send(302, extra={"Location": outer.redirect_to})
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, b'{"error":"invalid_token"}', extra={
                        "WWW-Authenticate": f'Bearer resource_metadata="{outer.base}/.well-known/oauth-protected-resource/mcp"'})
                if not isinstance(body, dict) or "id" not in body:
                    return self._send(202)
                if "method" not in body:   # a reply to a server request
                    return self._send(202)
                meth = body["method"]
                if meth == "initialize":
                    res = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fixture", "version": "0"}}
                elif meth == "tools/list":
                    res = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in TOOLS]}
                elif meth == "tools/call":
                    name = (body.get("params") or {}).get("name")
                    text = self.headers.get("Authorization") if name == "echo_auth" else f"called {name}"
                    res = {"content": [{"type": "text", "text": text}]}
                else:
                    res = {}
                answer = {"jsonrpc": "2.0", "id": body["id"], "result": res}
                if outer.sse:
                    events = []
                    if outer.server_request:
                        events.append({"jsonrpc": "2.0", "id": "s1", "method": "sampling/createMessage", "params": {}})
                        events.append({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
                    events.append(answer)
                    data = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                    return self._send(200, data, "text/event-stream", {"Mcp-Session-Id": "s-1"})
                self._send(200, json.dumps(answer).encode(), extra={"Mcp-Session-Id": "s-1"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.url = self.base + "/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        test.addCleanup(self.httpd.server_close)
        test.addCleanup(self.httpd.shutdown)

    def oauth(self, **meta) -> None:
        """RFC 9728 and RFC 8414 metadata for this server, the authorization server being the same origin."""
        self.metadata["/.well-known/oauth-protected-resource/mcp"] = {"resource": self.url,
                                                                      "authorization_servers": [self.base]}
        self.metadata["/.well-known/oauth-authorization-server"] = {
            "issuer": self.base, "authorization_endpoint": self.base + "/authorize", "token_endpoint": self.base + "/token",
            "revocation_endpoint": self.base + "/revoke", "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token"], **meta}

    def posts(self, path: str = "/mcp") -> list:
        return [e for e in self.log if e["method"] == "POST" and e["path"] == path]
