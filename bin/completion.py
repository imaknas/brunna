"""Completion for judgment points (answer only): which provider, which model, where the key is, and the same handling
every provider goes through. docs/design/llm-providers.md.

{settings}'s `llm` block (no such block = the current behaviour: judgment points go to runtime.default's agent interface
as before, and this module takes no part):

  "llm": {
    "providers": {
      "<name>": {"impl": "anthropic | openai | ollama | <a runtime with completion, e.g. claude>",
                 "base_url": "…", "key": {"secret": "<file name in secrets_dir>"} | {"env": "<environment variable>"},
                 "location": "cloud | local",
                 "models": {"fast": "<name>" | {"name", "context", "price": {"in", "out"}}, …},
                 "strict": false, "max_tokens_field": "max_tokens"}
    },
    "use": {"fast": "<provider>", "standard": "<provider>", "strong": "<provider>"},   point's model_group → provider
    "points": {"<judgment point>": {"provider": "<name>", "model": "<name>"}},           override for one point
                                       (or {"runtime": "<registered runtime>"}: an agent-shaped point on that runtime)
              "<project>/<point>": {"provider": "<name>", "model"?: "<name>"}            a project's declared model
                                       call (registry models:), pinned by the owner; the project names only a tier
    "budget": {"daily_usd": 2.0}
  }

Which points the providers serve (routed): a point pinned to a provider (llm.points.<point>.provider), and, when `use`
is set, every completion point by its group. Without `use`, every point not pinned stays exactly where it is without
llm.providers: on runtime.default's completion interface. So a provider added for a few points only (a local model
serving one project's reviewer) moves nothing else.

The handling is the same for every provider (an implementation only "sends it and brings back the raw text"):
  1. before sending, mask strings that look like credentials (one masking point for the whole platform)
  2. estimate the length: over the model's context → not sent (the provider isn't allowed to truncate silently)
  3. today's spending over budget.daily_usd → not sent
  4. 429/503/529: back off and retry (at most 3 times); a length truncation isn't retried (it would truncate the same
     way again)
  5. strip a reasoning model's <think>…</think>, parse the JSON (tolerating ``` fences), validate with minischema;
     if invalid, retry once with the errors attached
     a point declaring max_attempts makes at most that many requests, retries of both kinds counted (1: none)
  6. every call is recorded in .state/llm-usage.jsonl, as every agent run is (bin/usage_log.py: tokens, cost, billing =
     metered | subscription | local | unknown; and the project). A subscription call's cost_usd is the tool's own
     estimate, not a charge
The key is read only here (secrets_dir or an environment variable) and handed to the implementation in conf["_key"],
never put into any subprocess's environment.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import minischema  # noqa: E402
import usage_log  # noqa: E402

_ = i18n.domain(__file__)

USAGE = hubconf.STATE / "llm-usage.jsonl"
GROUPS = ("fast", "standard", "strong")
DEFAULT_GROUP = "standard"
MAX_RATE_RETRIES = 3

# Mask strings that look like credentials before sending to a model (originally in reflect_llm.py: in the first trial
# run the model quoted the first few characters of a Finnhub API key pasted in a conversation into the report).
# Tokens with known prefixes, and long enough mixed letter-digit strings, are all replaced by the mask text below.
PREFIXED = (r"sk-[A-Za-z0-9_\-]{10,}|sk-ant-[A-Za-z0-9_\-]{10,}|AIza[0-9A-Za-z_\-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
            r"|xox[abpr]-[A-Za-z0-9\-]{10,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-.]+")
# what is masked, as one pattern (the definition; reflect_llm imports it). Its last branch's lookaheads scan a run to its
# end at every position, so on a long run without a digit it is quadratic: redact() masks the same matches in linear time
SECRET = re.compile(r"(?:" + PREFIXED + r"|(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{20,})")
_PREFIXED = re.compile(r"(?:" + PREFIXED + r")")
_SCAN = re.compile(r"(?:" + PREFIXED + r")|(?P<run>[A-Za-z0-9_\-]{20,})")
_PREFIX_START = re.compile(r"sk-|AIza|gh[pousr]_|xox[abpr]-|eyJ")
_DIGIT, _LETTER = re.compile(r"\d"), re.compile(r"[A-Za-z]")


def redact(text: str) -> str:
    """SECRET.sub("[redacted]", text), in linear time. A run of 20+ token characters found where no prefix matches is
    masked whole when it holds a digit and a letter; when it doesn't, no later position in it can match that branch
    either (a suffix of the run lacks what the run lacks), so only a prefix starting inside it can, and only those
    starts are tried."""
    out, pos, n = [], 0, len(text)
    while pos <= n:
        m = _SCAN.search(text, pos)
        if not m:
            break
        if m.group("run") is not None and not (_DIGIT.search(m.group()) and _LETTER.search(m.group())):
            inner, p = None, m.start() + 1
            while (c := _PREFIX_START.search(text, p, m.end())):
                if (inner := _PREFIXED.match(text, c.start())):
                    break
                p = c.start() + 1
            if inner is None:
                out.append(text[pos:m.end()])
                pos = m.end()
                continue
            m = inner
        out.append(text[pos:m.start()] + "[redacted]")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


class Refused(Exception):
    """Not sent this time (wrong settings, no key, too long, budget used up): the reason is for a person to read."""


def settings() -> dict | None:
    s = hubconf.get("llm")
    return s if isinstance(s, dict) and s.get("providers") else None


def key_envs() -> set[str]:
    """Keys the settings give through environment variables: removed before handing over to any subprocess (agent job,
    mailbox session), or any agent with Bash could read them."""
    s = settings() or {}
    return {p["key"]["env"] for p in (s.get("providers") or {}).values()
            if isinstance(p, dict) and isinstance(p.get("key"), dict) and p["key"].get("env")}


def is_key(v: str) -> bool:
    return bool(re.fullmatch(r"[!-~]+", v.strip()))


def _one_key(v: str, where) -> str:
    if not is_key(v):   # never sent: an error about it could quote it
        raise Refused(_("not a key: {where} holds a space, a line break or other than plain text", where=where))
    return v.strip()


def read_key(conf: dict) -> str | None:
    """None = this provider needs no key (e.g. a local Ollama, Claude Code logging in by itself). Needed but not
    available → Refused."""
    k = conf.get("key")
    if not k:
        return None
    if k.get("secret"):
        f = hubconf.secrets_dir() / k["secret"]
        try:
            v = f.read_text().strip()
        except OSError:
            raise Refused(_("no key: {file} does not exist (`brunna llm set-key` writes it)", file=f))
        if not v:
            raise Refused(_("no key: {file} is empty", file=f))
        return _one_key(v, f)
    if k.get("env"):
        v = os.environ.get(k["env"])
        if not v:
            raise Refused(_("no key: environment variable {var} is not set (the scheduler runs under launchd and does not "
                            "see the shell's settings; a secret is more reliable)", var=k["env"]))
        return _one_key(v, k["env"])
    raise Refused(_("key must be {{\"secret\": {file}}} or {{\"env\": {var}}}", file=_("file name"),
                    var=_("variable name")))


def routed(point: str, spec: dict | None = None) -> bool:
    """Whether llm.providers serves this completion point (resolve) rather than runtime.default's completion interface:
    the point is pinned to a provider, or llm.use names its group (spec's model_group). Every other point, the groups
    use leaves out included, stays on the runtime as without llm.providers."""
    s = settings()
    if not s:
        return False
    pin = (s.get("points") or {}).get(point)
    if isinstance(pin, dict) and pin.get("provider"):
        return True
    use = s.get("use") or {}
    return bool(use.get((spec or {}).get("model_group") or DEFAULT_GROUP))


def nowhere(runtime: str, spec: dict | None = None) -> str:
    """Why a completion point the providers don't serve (routed) has nowhere to go: the runtime has no completion
    interface, and either there are no llm.providers or they don't take this point."""
    if settings():
        return _("runtime {name} has no completion interface, and llm.providers take only the points pinned to them and the "
                 "groups llm.use names (not {group}); set llm.use, or pin this point (brunna llm setup sets use)",
                 name=runtime, group=(spec or {}).get("model_group") or DEFAULT_GROUP)
    return _("runtime {name} has no completion interface, and {settings} has no llm section (set one up with brunna llm "
             "setup)", name=runtime)


def models_of(conf: dict) -> dict:
    """A provider's group → model: its own models, else, for a runtime, the adapter's (which follow the adapter's version)."""
    import runtime_ports
    impl = conf.get("impl")
    return conf.get("models") or (runtime_ports.models(impl) if impl in runtime_ports.RUNTIMES else {})


def resolve(point: str, spec: dict) -> dict:
    """Which provider and model this judgment point goes to: llm.points' choice > models[model_group] of the provider
    llm.use[model_group] names. A pinned model the provider lists in its models keeps that entry's context and price."""
    s = settings()
    if not s:
        raise Refused(_("{settings} has no llm section"))
    providers = s["providers"]
    group = spec.get("model_group") or DEFAULT_GROUP
    pin = (s.get("points") or {}).get(point) or {}
    name = pin.get("provider") or (s.get("use") or {}).get(group)
    if not name:
        raise Refused(_("llm.use does not say which provider serves the {group} group", group=group))
    conf = providers.get(name)
    if not isinstance(conf, dict):
        raise Refused(_("llm.providers has no {name}", name=name))
    models = models_of(conf)
    entry = pin.get("model") or models.get(group)
    if not entry:
        raise Refused(_("llm.providers.{name}.models has no {group}", name=name, group=group))
    if isinstance(entry, str):
        # a pinned name the provider lists keeps what is written there: the entry of the point's own group when it is
        # that model, else the first group (fast, standard, strong) listing it
        listed = [models.get(g) for g in (group, *GROUPS)]
        entry = next((m for m in listed if isinstance(m, dict) and m.get("name") == entry), entry)
    m = entry if isinstance(entry, dict) else {"name": entry}
    return {"provider": name, "impl": conf.get("impl"), "conf": conf, "group": group, "model": m.get("name"),
            "context": m.get("context"), "price": m.get("price"), "location": conf.get("location", "cloud")}


_DIGITS = re.compile(r"[0-9]")


def estimate_tokens(text: str) -> int:
    """Rather overestimate: UTF-8 bytes / 3 (a Chinese character is about one token, English about 4 characters a
    token, so both come out high), but an ASCII digit is a token of its own (Qwen's and Llama 3's tokenizers split
    numbers into digits: digit-heavy text came out about 3x under, measured 11-15)."""
    digits = len(_DIGITS.findall(text))
    return (len(text.encode()) - digits) // 3 + digits + 1


SCHEMA_PROMPT_CHARS = 300   # provider_http.schema_in_prompt's own words around the schema


def request_tokens(prompt: str, schema: dict | None, max_tokens: int, system: str = "") -> int:
    """What one request needs of the context: the prompt, the system prompt, the schema (sent as a grammar or, when the
    provider can't, written into the prompt), and the room for the answer."""
    extra = json.dumps(schema, ensure_ascii=False) + " " * SCHEMA_PROMPT_CHARS if schema is not None else ""
    return estimate_tokens(system + prompt + extra) + max_tokens


def today_cost() -> float:
    """Today's spending: metered rows only (a row from before billing was written counts). A subscription run's cost is
    what the tool says it would have cost, not a charge, so it never uses up the budget metered calls need
    (docs/design/project-agent-calls.md A5)."""
    usage_log.sweep(USAGE)
    return sum((usage_log.number(r.get("cost_usd")) for r in usage_log.rows(USAGE, datetime.date.today().isoformat())
                if r.get("billing", "metered") == "metered"), 0.0)


def metered(target: dict) -> bool:
    """Pay-per-use: a cloud provider reached with an API key, or a runtime whose own login is one (its adapter's billing). A subscription's login and local providers are not. The one rule the project opt-in
    (registry metered_models) is checked against."""
    if target.get("location", "cloud") != "cloud":
        return False
    return bool((target.get("conf") or {}).get("key")) or login_metered(target)


def login_metered(target: dict) -> bool:
    """A runtime whose own login is an API key: pay-per-use."""
    import runtime_ports
    impl = target.get("impl")
    if impl not in runtime_ports.RUNTIMES or "auth" not in runtime_ports.capabilities(impl):
        return False
    rt = runtime_ports.load(impl, ports=("auth",), platform_runs=False)
    return rt.billing((target.get("conf") or {}).get("auth") or runtime_ports.default_login(impl), {}, True) == "metered"   # {}: the login itself


def billing(target: dict) -> str:
    """How a call is paid for, written on every usage row: metered (pay-per-use, the cost is real), local (no money),
    subscription (a runtime's own login: cost_usd is what the tool reports the call would cost, not a charge)."""
    if metered(target):
        return "metered"
    return "local" if target.get("location") == "local" else "subscription"


_open: list[tuple[str, str, float | None]] = []   # (point, run, what is held for it): the requests this process has open


def on_budget(target: dict) -> bool:
    """A request through a runtime's own login that is billed per use and says what it cost: held like its agent runs (spending)."""
    import runtime_ports
    import spending
    impl = target.get("impl")
    return impl in runtime_ports.RUNTIMES and spending.held(impl, (target.get("conf") or {}).get("auth") or runtime_ports.default_login(impl))


def begin(point: str, target: dict, project: str | None = None, capped: float | None = None) -> str | None:
    """Open a completion's request in the usage log (usage_log.begin): settled by record. capped: what it may cost is held for
    it; when it doesn't fit today's budget, Refused."""
    if capped is not None:
        import spending
        rid, why = spending.reserve(point, target["provider"], target["model"], capped, project)
        if rid is None:
            raise Refused(why)
    else:
        rid = usage_log.begin(USAGE, point, target["provider"], target["model"], billing(target), project, gated=login_metered(target))
    if rid:
        _open.append((point, rid, capped))
    return rid


def record(point: str, target: dict, res, project: str | None = None, run: str | None = None) -> float | None:
    """The request's row, its open run settled (run, else the point's latest: a caller about to be killed, mailbox_route, has
    no other name for it). One that never started (res.started False) used nothing: no row."""
    entry = next((x for x in reversed(_open) if (x[1] == run if run else x[0] == point)), None)
    if entry:
        _open.remove(entry)
    rid = entry[1] if entry else run
    if not getattr(res, "started", True):
        usage_log.drop(USAGE, rid)
        return None
    cost = res.cost_usd
    price = target.get("price") or {}
    if cost is None and price and res.input_tokens is not None:
        cost = ((res.input_tokens or 0) * float(price.get("in", 0)) + (res.output_tokens or 0) * float(price.get("out", 0))) / 1e6
    row = {"ts": usage_log.now(), "point": point,
           "provider": target["provider"], "model": target["model"], "input_tokens": res.input_tokens,
           "output_tokens": res.output_tokens, "cost_usd": cost, "stop": res.stop, "billing": billing(target)}
    if cost is None and entry and entry[2] is not None:
        cost = entry[2]
        row |= {"cost_usd": cost, "cost_assumed": True}
    elif cost is None and login_metered(target):
        row["cost_unknown"] = True
    if project:
        row["project"] = project
    usage_log.settle(USAGE, rid, row)
    return cost


def record_run(point: str, runtime: str, model: str | None, how: str, cost: float | None, stop: str | None,
               project: str | None = None, input_tokens: int | None = None, output_tokens: int | None = None,
               run: str | None = None, gated: bool = True, reserved: float | None = None) -> None:
    """One row for an agent run (run: what usage_log.begin opened for it): the runtime is the provider, how the adapter says
    it is paid for. gated: one at a strength its project chose, whose cost must be told (unknown_cost_today)."""
    row = {"ts": usage_log.now(), "point": point,
           "provider": runtime, "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost,
           "stop": stop, "billing": how}
    if cost is None and reserved is not None:   # it gave none (its timeout, an unreadable result): what was held for it (spending)
        row |= {"cost_usd": reserved, "cost_assumed": True}
    elif how == "metered" and cost is None and gated:   # e.g. it ran to its timeout: what it spent can't be told (unknown_cost_today)
        row["cost_unknown"] = True
    if project:
        row["project"] = project
    usage_log.settle(USAGE, run, row)


def unknown_cost_today() -> bool:
    """Whether a pay-per-use agent run today ended without a cost (record_run's cost_unknown): the day's spending can't be
    told then, so llm.py runs no more pay-per-use runs at a strength a project chose until the next day."""
    usage_log.sweep(USAGE)
    return any(r.get("cost_unknown") is True for r in usage_log.rows(USAGE, datetime.date.today().isoformat()))


def strip_think(text: str) -> str:
    """A reasoning model's leading <think>…</think> block taken off (also one with only the closing tag, which some
    models print). Only a block before the answer: never anything inside it (a JSON string may say "</think>")."""
    t = text.lstrip()
    if t.startswith("<think>"):
        end = t.find("</think>")
        return t[end + len("</think>"):] if end != -1 else t
    end = t.find("</think>")
    if end != -1 and not t.startswith(("{", "[", "```")):   # no opening tag, and the text isn't an answer already
        return t[end + len("</think>"):]
    return text


def parse_json(text: str):
    t = strip_think(text).strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        return json.loads(t)
    except ValueError:
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j > i:
            return json.loads(t[i:j + 1])   # if that fails too, ValueError is raised
        raise


def runtime_target(name: str, model: str | None, auth: str) -> dict:
    """Without llm.providers: hand over to the runtime's (runtime.default) completion interface, with the model decided
    by llm.model_for."""
    return {"provider": name, "impl": name, "conf": {"auth": auth}, "group": None, "model": model,
            "context": None, "price": None, "location": "cloud"}


MIN_REQUEST_S = 5   # less time than this left of the call's timeout_s: no further request is started


def run(point: str, spec: dict, prompt: str, schema: dict | None, sleep=time.sleep, target: dict | None = None,
        project: str | None = None, clock=time.monotonic) -> dict:
    """Run one completion. Returns the same shape llm.py gives its callers; raises Refused(reason) on failure.
    target not given = decided by hub.json's llm; given (runtime_target) = handed to that runtime.
    spec's timeout_s (default 600) bounds the whole call, retries and backoff included: each request gets what is left,
    so a caller with a hard deadline (cap-runner's, for a project's call) sees this one's refusal first."""
    import runtime_ports
    total = int(spec.get("timeout_s") or 600)
    deadline = clock() + total
    target = target or resolve(point, spec)
    conf = dict(target["conf"])
    conf["_key"] = read_key(conf)
    conf["_context"] = target["context"]
    import spending
    capped = spending.cap(point, {"shape": "completion", **spec}, target["impl"], target.get("group"), target["model"]) if on_budget(target) else None
    budget = ((settings() or {}).get("budget") or {}).get("daily_usd")
    if capped is not None and (why := spending.stopped(point, project, capped)):
        raise Refused(why)
    if capped is None and budget is not None and today_cost() >= float(budget):
        raise Refused(_("today's model spending has reached the budget (llm.budget.daily_usd ${budget}); not sending this "
                        "one", budget=budget))
    if capped is None and budget is not None and not target.get("price") and login_metered(target):
        raise Refused(_("{provider}'s login is billed per use and what a call costs can't be told, so it can't be kept within "
                        "llm.budget.daily_usd; not sending this one", provider=target["provider"]))
    prompt = redact(prompt)
    max_tokens = int(spec.get("max_tokens") or 8192)
    try:
        impl = runtime_ports.load_completion(target["impl"])
    except runtime_ports.Unsupported as e:   # e.g. the command runtime, which has no completion interface: a refusal, said
        raise Refused(str(e)) from None
    context = target["context"]
    if not context and callable(getattr(impl, "model_context", None)):
        # the settings don't say: a provider that can ask the model (a local Ollama's /api/show) does, so the check
        # below still holds and the provider sizes its window from it
        context = conf["_context"] = impl.model_context(conf, target["model"])

    def fits(text: str) -> None:
        """Every request is measured before it is sent, the retry's appended errors included (with the schema, which may
        end up in the prompt): over the context → not sent."""
        need = request_tokens(text, schema, max_tokens)
        if context and need > int(context):
            raise Refused(_("prompt too long: about {tokens} tokens plus {max_tokens} of output exceed "
                            "{provider}/{model}'s {context}-token context", tokens=need - max_tokens,
                            max_tokens=max_tokens, provider=target["provider"], model=target["model"], context=context))

    def time_left() -> int:
        left_s = deadline - clock()
        if left_s < MIN_REQUEST_S:
            raise Refused(_("out of time: {point} may take {total} s in all, and too little is left for another request "
                            "to {provider}/{model}", point=point, total=total, provider=target["provider"],
                            model=target["model"]))
        return int(left_s)

    fits(prompt)
    req = runtime_ports.CompletionRequest(point=point, prompt=prompt, model=target["model"], schema=schema,
                                          max_tokens=max_tokens, timeout_s=total, max_cost_usd=capped)
    cost_total, last_errs = 0.0, None
    # max_attempts: the most requests one call may make, retries of both kinds included (a point anyone's text reaches, so a
    # crafted input can't make it cost more than one); unset = the retries above
    left = int(spec.get("max_attempts") or 0) or None
    for attempt in range(2):   # the second = ask again with the errors attached when the output was invalid
        rate_tries = 0
        while True:
            req = req._replace(timeout_s=time_left())
            rid = begin(point, target, project, capped)
            res = impl.complete(req, conf)
            cost_total += record(point, target, res, project, rid) or 0
            left = left - 1 if left is not None else None
            if capped is not None and (res.ok or res.refused):
                spending.login_told(target["impl"], res.refused or None)
            if res.stop != "rate_limited" or rate_tries >= MAX_RATE_RETRIES or left == 0:
                break
            rate_tries += 1
            sleep(min(res.retry_after_s or 5 * 2 ** rate_tries, 60, max(deadline - clock() - MIN_REQUEST_S, 0)))
        if not res.ok:
            if res.capped and capped is not None:
                spending.note_stop(point, project, capped)
                raise Refused(_("stopped at the most it may cost (${cap}): {settings} llm.budget.caps.{point} raises it", cap=f"{capped:.2f}",
                                point=point))
            if res.stop == "length":
                raise Refused(_("output cut off at the length limit ({provider}/{model}, max_tokens {max_tokens}); not "
                                "retried", provider=target["provider"], model=target["model"], max_tokens=max_tokens))
            raise Refused(f"{target['provider']}/{target['model']}:{res.stop}:{res.error}")
        if schema is None:
            return _out(point, target, res.text, None, cost_total, res)
        try:
            obj = parse_json(res.text)
            last_errs = minischema.errors(schema, obj)
        except ValueError:
            obj, last_errs = None, [_("not JSON")]
        if not last_errs:
            return _out(point, target, res.text, obj, cost_total, res)
        if left == 0:
            break
        again = (prompt + "\n\n---\nThe previous answer was not valid (" + "; ".join(last_errs[:5])
                 + "). This time reply with one valid JSON only, no other text.")
        fits(again)
        req = req._replace(prompt=again)
    if left == 0 and attempt == 0:
        raise Refused(_("output does not match the schema: {errors}", errors="; ".join(last_errs[:3])))
    raise Refused(_("output does not match the schema (after one retry): {errors}", errors="; ".join(last_errs[:3])))


def _out(point, target, text, obj, cost, res=None) -> dict:
    # the field names are the ones llm.py has always given its callers (librarian, consolidate… read these)
    out = {"type": "result", "is_error": False, "result": text, "structured_output": obj, "total_cost_usd": cost or None,
           "llm_point": point, "provider": target["provider"], "model": target["model"], "llm_model": target["model"]}
    if getattr(res, "runtime_version", None):   # a runtime's completion: the version of its checked copy that ran
        out["llm_runtime_version"] = res.runtime_version
    return out


def health_all() -> list[tuple[str, bool, str]]:
    """(provider, healthy or not, reason): for doctor. A key that can't be read also counts as not healthy."""
    import runtime_ports
    out = []
    for name, conf in ((settings() or {}).get("providers") or {}).items():
        try:
            c = dict(conf)
            c["_key"] = read_key(c)
            ok, why = runtime_ports.load_completion(c.get("impl")).health(c)
        except (Refused, KeyError, runtime_ports.Unsupported, runtime_ports.MissingFunctions) as e:
            ok, why = False, str(e)
        out.append((name, ok, why))
    return out


# ---------- settings checks (called by config-check) ----------
# A key's value may never be written in hub.json: anything that looks like a key is blocked. Model names often have
# digits and hyphens (e.g. <family>-<model>-4-5-20251001), so SECRET's "long enough, mixed letters and digits" rule
# isn't used: only known prefixes and 32 or more letters/digits with no separator.
KEYLIKE = re.compile(r"^(?:sk-|sk-ant-|AIza|gh[pousr]_|xox[abpr]-|eyJ)|^[A-Za-z0-9]{32,}$")
PRIVATE = re.compile(r"^(?:localhost|127\.\d+\.\d+\.\d+|\[?::1\]?|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
                     r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+|[\w.-]+\.ts\.net)$")


PROJECT_CALL = re.compile(r"^[^/\s]+/[a-z0-9][a-z0-9-]{0,47}$")   # <project>/<registry.MODEL_POINT>


def _strings(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(o, str):
        yield path, o


def config_errors(llm: dict, projects: dict | None = None, runtime: str | None = None) -> list[str]:
    """The llm block's problems. projects (the registry being checked, config-check passes it) and runtime
    (runtime.default) add one cross-check: a provider pinned on a project's agent call (registry agents:), which would be
    ignored (an agent call runs on the runtime)."""
    import urllib.parse
    import runtime_ports
    errs = []
    for where, v in _strings(llm):
        if KEYLIKE.search(v):
            errs.append(_("{where} looks like a key value: the value goes in the secrets directory ({{\"secret\": file "
                          "name}}) or an environment variable ({{\"env\": name}})", where=where))
    providers = llm.get("providers") or {}
    if not isinstance(providers, dict):
        return errs + [_("providers must be an object")]
    if not providers and llm.get("use"):
        errs.append(_("use is set but providers is not (to pin only some judgment points' models, write only points)"))
    # providers without use: only the points pinned to one move (routed); the rest stay on runtime.default
    for name, p in providers.items():
        if not isinstance(p, dict):
            errs.append(_("providers.{name} must be an object", name=name))
            continue
        impl = p.get("impl")
        if impl in runtime_ports.PROVIDERS:
            pass
        elif impl in runtime_ports.RUNTIMES:
            if "completion" not in runtime_ports.capabilities(impl):
                errs.append(_("providers.{name}: runtime {impl} has no completion interface", name=name, impl=impl))
        else:
            errs.append(_("providers.{name}.impl `{impl}` is not known (have: {have})", name=name, impl=impl,
                          have=", ".join([*runtime_ports.PROVIDERS, *runtime_ports.RUNTIMES])))
        k = p.get("key")
        if k is not None and (not isinstance(k, dict) or len([x for x in ("secret", "env") if k.get(x)]) != 1):
            errs.append(_("providers.{name}.key must be {{\"secret\": file name}} or {{\"env\": variable name}} (only "
                          "one of them)", name=name))
        elif k and k.get("secret") and ("/" in k["secret"] or k["secret"].startswith(".")):
            errs.append(_("providers.{name}.key.secret is a file name only (it lives in the platform's secrets directory)",
                          name=name))
        if p.get("location", "cloud") not in ("cloud", "local"):
            errs.append(_("providers.{name}.location can only be cloud / local", name=name))
        if p.get("location") == "local" and p.get("key"):
            # local is never metered (completion.metered): a provider reached with a key is someone's paid service
            errs.append(_("providers.{name}: location local can't have a key (a provider reached with a key is billed "
                          "per use, and local ones are counted as free)", name=name))
        if p.get("location") == "local":
            host = urllib.parse.urlparse(p.get("base_url") or "").hostname or ""
            if not (p.get("base_url") and PRIVATE.match(host)):
                errs.append(_("providers.{name}: location local only allows a base_url on this machine, a private "
                              "network or the tailnet (now {base_url!r})", name=name, base_url=p.get("base_url")))
        for g, m in (p.get("models") or {}).items():
            if g not in GROUPS:
                errs.append(_("providers.{name}.models.{group}: the group can only be {groups}", name=name, group=g,
                              groups=" / ".join(GROUPS)))
            if not (isinstance(m, str) and m) and not (isinstance(m, dict) and m.get("name")):
                errs.append(_("providers.{name}.models.{group} must be a model name, or {{name, context?, price?}}",
                              name=name, group=g))
    for g, name in (llm.get("use") or {}).items():
        if g not in GROUPS:
            errs.append(_("use.{group}: the group can only be {groups}", group=g, groups=" / ".join(GROUPS)))
        elif name not in providers:
            errs.append(_("use.{group}: no provider {name}", group=g, name=name))
        elif g not in models_of(providers[name] or {}):
            errs.append(_("use.{group} → {name}, but providers.{name}.models has no {group}", group=g, name=name))
    try:
        import yaml
        declared = yaml.safe_load((hubconf.ROOT / "contracts" / "llm-points.yaml").read_text())["points"]
        known = set(declared)
    except Exception:
        known, declared = None, {}
    import runtime_ports
    for pt, pin in (llm.get("points") or {}).items():
        # <project>/<name>: a project's declared call (registry models: or agents:). Whether the project still declares it
        # is not an error (a project's own manifest.update shouldn't break the owner's config check; doctor and
        # schedulectl models say so); a provider on one it declares as an agent call is: that pin would be ignored
        project_call = isinstance(pt, str) and "/" in pt
        proj, _sep, call = pt.partition("/") if project_call else ("", "", "")
        agent_call = project_call and projects is not None and \
            call in ((projects.get(proj) or {}).get("agents") or {}) and call not in ((projects.get(proj) or {}).get("models") or {})
        if project_call and not PROJECT_CALL.match(pt):
            errs.append(_("points.{point}: a project's call is pinned as <project>/<name>, the name as its registry "
                          "models: or agents: declares it", point=pt))
        elif not project_call and known is not None and pt not in known:
            errs.append(_("points.{point}: not a declared judgment point", point=pt))
        if project_call and isinstance(pin, dict) and pin.get("runtime") is not None:
            errs.append(_("points.{point}.runtime: a project's call runs on its judgment point's runtime; pin a model "
                          "(and, for a model call, a provider)", point=pt))
        elif agent_call and isinstance(pin, dict) and pin.get("provider") not in (None, runtime):
            errs.append(_("points.{point}.provider: {point} is an agent call (registry agents:), which runs on the "
                          "runtime {runtime}; a provider pin there would be ignored, so it is refused (pin only a model)",
                          point=pt, runtime=runtime))
        elif not isinstance(pin, dict):
            errs.append(_("points.{point} must be an object ({{model}} or {{provider, model?}})", point=pt))
        elif pin.get("provider") is not None and pin["provider"] not in providers:
            errs.append(_("points.{point}.provider must name one of the providers", point=pt))
        elif pin.get("runtime") is not None and pin["runtime"] not in runtime_ports.RUNTIMES:
            errs.append(_("points.{point}.runtime must name a registered runtime (have: {have})", point=pt,
                          have=", ".join(runtime_ports.RUNTIMES)))
        elif pin.get("runtime") is not None and ((declared.get(pt) or {}).get("stream")
                                                  or pt in runtime_ports.RUNTIME_ARG_POINTS):
            errs.append(_("points.{point}.runtime: {point} is resumed or streamed with the default runtime's own "
                          "arguments, so it cannot run on another one", point=pt))
        elif pin.get("runtime") is not None and (declared.get(pt) or {}).get("shape") == "completion":
            errs.append(_("points.{point}.runtime: a completion point goes to llm.providers, not to a runtime", point=pt))
        elif pin.get("runtime") is not None and runtime_ports.throwaway_only(pin["runtime"]) \
                and pt not in runtime_ports.THROWAWAY_POINTS:
            errs.append(_("points.{point}.runtime: {runtime} runs only in the platform's throwaway worktree, and {point} "
                          "does not run in one (those that do: {points})", point=pt, runtime=pin["runtime"],
                          points=", ".join(runtime_ports.THROWAWAY_POINTS)))
        elif pin.get("provider") is None and not pin.get("model") and not pin.get("runtime"):
            errs.append(_("points.{point}: without a provider, model is required (the model the runtime uses)", point=pt))
    return errs
