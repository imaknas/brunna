"""The provider for a local Ollama (completion interface; docs/design/llm-providers.md).

Uses the **native API** (/api/chat), not its OpenAI-compatible endpoint: the compatible endpoint cannot set num_ctx,
so a prompt longer than the default context is silently cut from the front.
The context window: the model's context is the one in {settings} ({name, context} in provider.models), else what
/api/show says (model_context, which completion.py asks before its length check). num_ctx is always sent, sized to
the request (completion.request_tokens: prompt, system and schema, digits a token each, plus max_tokens; rounded up to
a power of two, at least 8192) and never above that context: Ollama's own default is a few thousand tokens, and a
model's whole context (often 256k) would make it reserve memory for a window the call never uses.
truncate and shift are false: a prompt that still overflows is Ollama's error, not a silently shortened prompt.
Thinking: a model /api/show lists as thinking gets think: false (also when /api/show can't be read); one that isn't is
sent no think field. A leading <think>…</think> left in the answer is taken off (completion.strip_think). An empty
answer is an error, not an empty result.
Structured output: first `format: <schema>`; if the backend does not support it (MLX answers 501), the schema is
written into the prompt instead (setting `structured: false` on the provider skips the first attempt).
Failures come back as errors with Ollama's own reason (not running, model not pulled, over the context, timed out);
no other model is tried.
"""
from __future__ import annotations

import i18n
from provider_http import RATE_LIMITED, call, schema_in_prompt
from runtime_ports import CompletionResult

_ = i18n.domain(__file__)

DEFAULT_BASE = "http://127.0.0.1:11434"
MIN_CTX = 8192


def _base(conf) -> str:
    return (conf.get("base_url") or DEFAULT_BASE).rstrip("/")


_shown: dict[tuple[str, str], dict] = {}


def _show(base: str, model: str) -> dict:
    """/api/show's answer for the model ({} when it can't be read: Ollama not running, model not pulled; the request
    itself then says why). Only an answer is kept for the process; a failure is asked again next time."""
    if (base, model) not in _shown:
        status, data, _h, _e = call("POST", base + "/api/show", {"model": model}, {}, 10)
        if status != 200 or not isinstance(data, dict):
            return {}
        _shown[(base, model)] = data
    return _shown[(base, model)]


def model_context(conf, model) -> int | None:
    """The model's context length as Ollama reports it (model_info's <family>.context_length); None = unknown."""
    if not model:
        return None
    info = _show(_base(conf), model).get("model_info") or {}
    lengths = [v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int) and v > 0]
    return max(lengths) if lengths else None


def num_ctx(need: int, context: int | None) -> int:
    """The window for a request needing `need` tokens (completion.request_tokens): a power of two, at least MIN_CTX,
    never above the model's context."""
    size = MIN_CTX
    while size < need:
        size *= 2
    return min(size, int(context)) if context else size


def complete(req, conf) -> CompletionResult:
    import completion
    msgs = ([{"role": "system", "content": req.system}] if req.system else []) + [{"role": "user", "content": req.prompt}]
    base = _base(conf)
    options = {"num_predict": req.max_tokens,
               "num_ctx": num_ctx(completion.request_tokens(req.prompt, req.schema, req.max_tokens, req.system or ""),
                                  conf.get("_context"))}
    # truncate/shift false: a prompt over num_ctx is an error (HTTP 400, "exceeds the available context size"), never cut
    # from the front and answered (llama.cpp models did that by default, measured on 0.34.2 on 11-15; the MLX backend
    # doesn't cut at all)
    body = {"model": req.model, "messages": msgs, "stream": False, "options": options, "truncate": False,
            "shift": False}
    caps = _show(base, req.model).get("capabilities")
    if not isinstance(caps, list) or "thinking" in caps:
        body["think"] = False
    url = base + "/api/chat"
    if req.schema is not None and conf.get("structured", True):
        body["format"] = req.schema
    status, data, _h, err = call("POST", url, body, {}, req.timeout_s)
    if req.schema is not None and status == 501 and "format" in body:
        # this model's backend does not support structured output (MLX backend: "structured output is unavailable"):
        # write the schema into the prompt instead
        body.pop("format")
        body["messages"] = msgs[:-1] + [{"role": "user", "content": schema_in_prompt(req.prompt, req.schema)}]
        status, data, _h, err = call("POST", url, body, {}, req.timeout_s)
    if status in RATE_LIMITED:
        return CompletionResult(False, stop="rate_limited", error=err)
    if status is None and "timed out" in err:
        return CompletionResult(False, stop="error", error=_(
            "{model} at {base} had not finished after {seconds} s (slow generation, timed out)", model=req.model, base=base,
            seconds=req.timeout_s))
    if status is None:
        return CompletionResult(False, stop="error", error=_("Ollama at {base} could not be reached: {error}", base=base,
                                                            error=err))
    if status != 200 or not isinstance(data, dict):
        return CompletionResult(False, stop="error", error=err or f"HTTP {status}")
    stop = "length" if data.get("done_reason") == "length" else "end"
    text = completion.strip_think((data.get("message") or {}).get("content") or "").strip()
    usage = {"input_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
    if stop == "end" and not text:
        return CompletionResult(False, text="", stop="error", error=_("the model gave an empty answer"), **usage)
    return CompletionResult(stop == "end", text=text, stop=stop, error="" if stop == "end" else "done_reason=length",
                            **usage)


def health(conf) -> tuple:
    status, data, _h, err = call("GET", _base(conf) + "/api/tags", None, {}, 10)
    if status != 200:
        return False, err or f"HTTP {status}"
    have = {m.get("name") for m in (data or {}).get("models") or []}
    want = {m if isinstance(m, str) else m.get("name") for m in (conf.get("models") or {}).values()}
    missing = sorted(w for w in want if w and w not in have and f"{w}:latest" not in have)
    return (not missing), (_("models not on this machine: {models} (ollama pull <name>)", models=", ".join(missing))
                            if missing else "")


def list_models(conf) -> list[str]:
    """Models already pulled on this machine. Ollama not running -> empty."""
    status, data, _h, _e = call("GET", _base(conf) + "/api/tags", None, {}, 10)
    return sorted(m.get("name") for m in (data or {}).get("models") or [] if m.get("name")) if status == 200 else []
