"""The HTTP shared by LLM providers (standard library). Keys go only into this request's headers, never into the
environment or a log."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def call(method: str, url: str, body: dict | None, headers: dict, timeout: float) -> tuple[int | None, object, dict, str]:
    """(HTTP status, parsed JSON or None, response headers, error description). Status None = could not connect."""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), dict(r.headers), ""
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return e.code, parsed, dict(e.headers or {}), _without(headers, f"HTTP {e.code}:{raw[:1000].decode(errors='replace')}")[:310]
    except (urllib.error.URLError, OSError, ValueError) as e:
        # a header that isn't plain text is refused before anything is sent, with an error that quotes it: never passed on
        if any(not (isinstance(v, str) and v.isascii() and v.isprintable()) for v in headers.values()):
            return None, None, {}, f"{type(e).__name__}: the request could not be made (a header's value isn't a plain line of text)"
        return None, None, {}, _without(headers, str(e))[:300]


def _without(headers: dict, text: str) -> str:
    """An error's text without what was sent in a header (a service can quote the key back)."""
    for v in headers.values():
        for part in (str(v), str(v).removeprefix("Bearer ")):
            if len(part) >= 8:
                text = text.replace(part, "[redacted]")
    return text


RATE_LIMITED = {429, 503, 529}   # too many requests, temporarily overloaded: back off and retry


def retry_after(headers: dict) -> float | None:
    v = {k.lower(): x for k, x in headers.items()}.get("retry-after")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def schema_in_prompt(prompt: str, schema: dict) -> str:
    """The provider does not support structured output (e.g. Ollama's MLX backend answers 501, some compatible endpoints
    do not accept response_format): write the schema into the prompt. What comes back is still parsed by
    completion.py, validated with minischema and retried once if invalid -- the format does not rely on the
    provider."""
    return (prompt + "\n\n---\nReply with one JSON object only: no other text, not wrapped in ```. It must match this "
            "JSON schema:\n"
            + json.dumps(schema, ensure_ascii=False))
