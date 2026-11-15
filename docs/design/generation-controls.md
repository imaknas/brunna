# Generation controls for a project's model calls

> **Status**: design, first draft (11-15). Not built. Not cold-reviewed yet. Asked for by the owner on 11-15, after a
> project's request.

Source: a research project reproduces a public benchmark in which an "editor" model works for up to 64 turns with four
tools, under fixed conditions: at most 4096 output tokens per response, temperature 0, reasoning off, nothing in its
context beyond the benchmark's own system prompt and those four tools. It ran the editor through in-session subagents
and could control none of that: response length, reasoning, and a tool the session attached on its own (an "advisor").
It asks that a project's declared model calls can carry generation conditions, so its results are comparable and can be
audited afterwards:

1. `max_output_tokens` per response;
2. reasoning off, or a level;
3. temperature;
4. `max_turns`, ending with "stopped at the limit";
5. a guarantee that nothing else is loaded: no advisor, tool servers or skill lists;
6. a system prompt the project chooses, or none, and nothing of the project's own rules (its rules file) added;
7. a record afterwards of every turn: input and output tokens, and which tools were called.

Its tool boundary is not the issue: today's `allow:` covers it (the agent writes request files into one folder of the
project and reads answers from another; the project's own driver answers them). It also named the other way: if `brunna
complete` took `max_tokens`, temperature and reasoning off, its own driver could run the turns itself.

Related: `project-agent-calls.md` (`agents:`, `brunna agent`), `llm-providers.md` ("Projects' calls on a provider (11-15)":
the owner pins a project's call to a provider), the direction.md backlog item "a project model call's own time limit".

## The answer in one paragraph

Comparable conditions come only from a backend that sends exactly what was asked and says what it sent, and the coding
agent runtime is not that backend: Claude Code has no temperature control at all, keeps its own system prompt and tool
definitions, continues a response cut at the output limit with a message of its own, and can attach an advisor from
the account's flags. So v1 puts the benchmark on the **single-shot route**: `brunna complete` grows a **messages
interface** (a conversation and tool definitions in, one assistant message out), the point's **declared** controls are
sent to a provider that can honour them (the Anthropic API, an OpenAI-compatible endpoint, a local Ollama), and the
project's own driver runs the 64 turns, executing the four tools in its own sandbox. Every control is checked three
ways (can the backend express it, did the backend accept it, does the response show it was kept), and a control that
fails any of them refuses the call; none is ever dropped. Agent calls (`agents:`) get `max_turns`, a per-turn record,
and the advisor turned off in every platform run; the other controls are refused on them in v1, said plainly. And the
consequence the owner has to see first: **on current Claude models the exact conditions may not exist** (temperature
is no longer accepted, and some models can't turn reasoning off), so which model the benchmark's editor is decides
whether a faithful reproduction is possible at all (question O1).

## Measurements (11-15)

No model call was made. Sources: the platform's checked copy of Claude Code (2.1.325; its `--help` and the strings in
its binary), this repo's adapters, and the Anthropic API reference bundled with the coding agent (labelled "bundled
reference, not run" below).

### Claude Code (`claude -p`), as the platform runs it

| Condition | What exists | Usable for comparable conditions? |
|---|---|---|
| Temperature | No flag, setting or environment variable. The main loop sends `temperature: 1` when thinking is off and the model accepts one, and none when thinking is on; only an internal override for its own side requests changes it | **No** |
| Output tokens per response | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` (caps, never raises, the model's own limit). But a response cut at the limit is **continued**: the tool sends a message of its own ("Output token limit hit. Resume directly…") and the model goes on | **No**: the benchmark ends that response there; Claude Code turns one cut into more model calls with text the benchmark never had |
| Reasoning | Hidden `--thinking disabled|adaptive`, `--max-thinking-tokens`, `CLAUDE_CODE_DISABLE_THINKING`, `MAX_THINKING_TOKENS=0`, `--effort low…max` | Expressible; what the model then accepts is per model (below) |
| Turn limit | Hidden `--max-turns N` (and `CLAUDE_CODE_MAX_TURNS`); the result's `subtype` says the limit ended it. lease-runner already passes it | **Yes**, behind a conformance check (hidden flags can change) |
| System prompt | `--system-prompt` replaces the default, `--append-system-prompt` adds to it (the platform's completion uses the latter). Tool definitions, and the reminders the tool attaches to messages, stay | **No** for "the benchmark's prompt and nothing else" |
| The project's rules file | `--restricted` (every confined run and every completion uses it) loads no user, project or local settings. A real run recorded in `mailbox-session-first.md` shows a **completion** in its own folder, with a rules file above it, not reading it; a `settings: platform` **agent run in a project's folder** (where the rules file is in the folder itself) is not recorded | Completions: yes. Agent runs: **not verified**; step 1 probes it |
| Tool servers | `--strict-mcp-config` (`tool_servers: none`) | Yes, already |
| Skills and commands | `--disable-slash-commands` exists; confined runs don't pass it today | Expressible |
| Advisor | A server-side tool. Off when `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` is set. Otherwise on when the provider is Anthropic's own, an account feature flag (or `CLAUDE_CODE_ENABLE_EXPERIMENTAL_ADVISOR_TOOL`) enables it, and either the `advisorModel` setting names a model or a "standby" path picks one with **no setting at all**. It adds its own section to the system prompt | See below |
| Built-in tools | `--tools ""` or an explicit list; the init event of stream-json lists them | Yes for Claude Code's own tools; the benchmark's four tools are not Claude Code tools and can only be reached through files (what the project does) or a tool server (refused for `project-agent`) |

**The advisor in a platform run.** The project saw it in its own session's subagents: the user-level settings there
name an advisor model, and a session loads them. A platform run (`project-agent`, every confined point, every
completion) passes `--restricted`, which ignores the user's settings file, so that setting doesn't reach it. The standby
path doesn't need the setting, and nothing read here shows `--tools`, `--strict-mcp-config` or `--restricted` turning
it off. **Not verified live**: whether a platform run gets an advisor today. It would not show in the init event's
tools (a server-side tool); it would show as advisor iterations in the usage (the binary counts iterations of type
`advisor_message`) and as the advisor's model in the result's `modelUsage`. A second model there alone proves nothing:
`evaluators.md`'s sealing table reports a single model only as a result of full sealing, so an unsealed run may list a
small side model the tool uses for itself. Step 1 turns the advisor off and probes it.

### What else Claude Code adds (why "empty harness" is out of reach there)

Its default system prompt (or the replacement plus its own sections: environment, the advisor's, the tool list), the
definitions of its built-in tools, reminders attached to user messages, the continuation message after a cut, and
retries of its own. None of them is a setting the platform can rely on to be absent, and the tool-neutrality rule says
what has to hold may not rest on a tool's features. So the Claude runtime **refuses** every generation control except
`max_turns`, on both its completion and its agent runs, and says why (below).

Found on the way: the runtime's `complete()` ignores the request's `max_tokens` (it builds no flag from it), so a
project's call on the Claude runtime is not held to the 8192 tokens `completion.run` believes it asks for. Under this
design that becomes a visible refusal when a point declares `max_output_tokens`; the silent ignore of the implicit
default is fixed in step 1 (said, not enforced: the runtime can't enforce it).

### The API providers (this repo's adapters, plus the bundled reference)

Cells marked † are neither in this repo's adapters nor in the bundled reference: from general knowledge of those APIs,
**not measured**. Steps 3 and 4 check each against the live backend (a fixture recorded from a real reply) before
relying on it.

| Control | Anthropic Messages API (`provider_anthropic`) | OpenAI-compatible (`provider_openai`) | Ollama native (`provider_ollama`) |
|---|---|---|---|
| Output tokens | `max_tokens` (always sent today); `stop_reason: max_tokens` | `max_tokens` or `max_completion_tokens` (`max_tokens_field`); `finish_reason: length` | `options.num_predict` (sent today); `done_reason: length` |
| Temperature | `temperature`. Bundled reference, not run: current models refuse a non-default value (400); some older models accept it, and from API version 2026-10-21 no model does. The adapter sends `anthropic-version: 2023-06-01` | `temperature`; reasoning models commonly refuse anything but the default †; a generic endpoint may accept and ignore it | `options.temperature` (greedy at 0) |
| Reasoning | `thinking` / `output_config.effort`. Bundled reference, not run: whether "off" exists is per model (Opus 5.5 can't, Sonnet 5.5 only as `between_tools`, Haiku 5.5 and Opus 4.8 can, Fable can't) | `reasoning_effort` on reasoning models †; what "off" means differs by vendor † | `think: false` (already sent for thinking models), `think: "low"…` only for some models †; `message.thinking` comes back separately † |
| System prompt | `system` (sent only when given) | a `system` message | a `system` message |
| Tools | `tools`; `tool_use` / `tool_result` blocks | `tools`; `tool_calls`, role `tool` † | `tools` (models listing the `tools` capability in `/api/show`); `message.tool_calls`, role `tool` † |
| Extra tools | none unless sent (the advisor is a tool the request must declare) | none unless sent | none unless sent |
| Per-response tokens | `usage.input_tokens/output_tokens` (+ cache fields) | `usage.prompt_tokens/completion_tokens` (+ `completion_tokens_details.reasoning_tokens` on some †) | `prompt_eval_count` / `eval_count` |

Also found: `provider_anthropic` gets structured output by forcing a tool (`tool_choice: {type: tool}`). Bundled
reference, not run: several current models refuse a forced `tool_choice` with a 400. That is a separate fix
(structured output through `output_config.format`), noted for direction.md, not part of this design.

### What the platform itself changes in the content

`completion.run` sends every prompt through `redact()`, which masks known key prefixes and **any run of 20 or more
letters, digits, `_` or `-` that holds both a letter and a digit**. In a benchmark that is hashes, long identifiers,
base64 and file names in tool results, rewritten as `[redacted]` on every turn, silently. And `strip_think` removes a
leading `<think>` block from an answer. Both are right for the platform's own points; both are content changes a
comparable run must at least see (C6, C7).

## Design

### C1. Where the controls live: on the declared point, never in the request

```yaml
models:
  editor:
    tier: standard
    interface: messages             # prompt (default, today's) | messages (C4)
    timeout: 20m                    # the backlog's per-point time limit: 1 s .. {settings} cap_runner.max_timeout_s (1800)
    generation:                     # optional; every key optional
      max_output_tokens: 4096       # 1 .. {settings} llm.project_max_output_tokens (default 32768)
      temperature: 0                # 0 .. 2
      reasoning: off                # off | low | medium | high
      system: none                  # none | from_request (C4); a request's system text is refused unless from_request
      content: verbatim             # verbatim | redacted (default redacted, today's; C6)
      context: 32768                # Ollama only: a fixed window, at most the model's (C10)
agents:
  research:
    tier: strong
    allow: [...]
    max_turns: 64                   # 1 .. 500
```

- **Declared, not per request.** `llm.py project` reads them from the registry, as it reads the tier; nothing in the
  request (which cap-runner treats as the project's untrusted text) chooses them. The registry entry is the audit
  record of what was asked; the per-call record (C5) says what was sent and kept. An experiment that varies a condition
  declares one point per setting (manifest.update, no approval).
- **Project-settable** (manifest.update). They narrow or shape one call; they don't choose a provider, a model or who
  pays. The two that can raise cost are capped by the owner's settings: `max_output_tokens` by
  `llm.project_max_output_tokens`, and `reasoning` has no level above `high`. `content: verbatim` sends the project's
  own text unmasked to the backend the owner chose for it; it changes nothing about where it goes (O3).
- **Unset = today's behaviour**, recorded as "the backend's default" (C5). A point with no `generation:` is exactly as
  now.
- The backend still comes only from the owner: `llm.points.<project>/<point>` pins a provider and model
  (`llm-providers.md`, 11-15). A project names a tier and conditions, never a model; when the model matters (it does
  here) the project asks the owner, as today.

### C2. Each backend declares what it can honour, per model where it can tell

A new optional function on the completion interface (neutral, in `runtime_ports`; each adapter fills it):

```
generation_support(conf, model) -> {control: "yes" | "no: <reason>" | "unknown"}
```

- **Static.** The Claude runtime answers `no` for every control but `max_turns` (its agent runs), each with its reason
  from the measurements. A runtime without the function answers `no` for all.
- **Per model where the backend says.** Anthropic's Models API lists capabilities; Ollama's `/api/show` lists
  `thinking` and `tools`. Where a model's support can't be read, the answer is `unknown`, and the run decides (C3).
- **Never silently.** Three cases, and each refuses the call when it fails:
  1. **Can't be expressed** (`no`): refused before anything is sent.
  2. **Expressed and rejected** (a 400 naming the parameter): the call fails with the backend's reason. Never retried
     without the control.
  3. **Expressed, accepted and ignored**: the dangerous case (a generic compatible endpoint, a model that thinks
     anyway). Checked on the response where it can be (C3); where it can't, the record says so.

### C3. Checked on every response

| Control | Verified from the response | When it fails |
|---|---|---|
| `max_output_tokens` | output tokens ≤ the cap; a stop at the limit reported as `length` | over the cap: the result is refused (`ignored: max_output_tokens`) |
| `reasoning: off` | no reasoning content (Anthropic `thinking` blocks, Ollama `message.thinking`, a leading `<think>` block) and no reasoning tokens where the usage reports them | refused; `strip_think` never hides it on such a point |
| `reasoning: <level>` | the backend accepted the field; depth can't be checked | recorded as `unverified` |
| `temperature` | **can't be checked** from a response | recorded as `sent, unverified`; for backends where acceptance-and-ignore is known (reasoning models on compatible endpoints) `generation_support` answers `no` |
| tools (messages) | every tool call names a declared tool | refused |

A refused result is still a usage row (it cost something) and a `job.failed`, as every other refusal.

### C4. The messages interface: the driver runs the turns

`brunna complete <point> --messages F` (a point declaring `interface: messages`; the prompt interface stays as it is):

```json
{"system": "…",
 "tools": [{"name": "…", "description": "…", "input_schema": {…}}],
 "messages": [
   {"role": "user", "content": [{"type": "text", "text": "…"}]},
   {"role": "assistant", "content": [{"type": "text", "text": "…"},
                                     {"type": "tool_call", "id": "c1", "name": "…", "arguments": {…}},
                                     {"type": "opaque", "backend": "anthropic", "data": {…}}]},
   {"role": "user", "content": [{"type": "tool_result", "id": "c1", "content": "…", "is_error": false}]}
 ]}
```

Out: `{"message": {"role": "assistant", "content": [...]}, "stop": "end | tool_calls | length | refused", "record": {…}}`.

- **One call = one model response = one turn.** The turn is defined neutrally: one response from the model, whatever it
  contains. The driver appends the answer and its tool results and calls again; `max_turns` on this route is the
  driver's, and the records show how many there were.
- **The neutral format is a contract** (`contracts/messages.v1.schema.json`). Each adapter translates it both ways:
  Anthropic `tool_use`/`tool_result` blocks, OpenAI `tool_calls` and role `tool`, Ollama `tool_calls` and role `tool`.
  A backend's own blocks that must go back unchanged (Anthropic's thinking blocks when reasoning is on) come out as
  `opaque` with the backend's name and go back verbatim; an `opaque` block from another backend is refused, never
  dropped.
- **A cut at the limit is a result here, not a refusal.** This departs on purpose from `completion.run`'s rule ("no
  silent truncation": a length stop is refused). Under a declared `max_output_tokens` the cut is one of the conditions:
  the answer comes back with `stop: length`, said in the record, and a tool call cut in the middle comes back marked
  `incomplete`, unparsed. The prompt interface keeps refusing a cut.
- **No schema, no retries** on this route: the answer is the model's turn as it is. Rate limits are still backed off
  within the call's time.
- **The tools run in the project's sandbox**, by its own driver. The platform only makes the model call; the model
  reaches nothing. No new reach, which is why `tools` needs no owner approval.
- `system: from_request` lets the request carry the system text; with `none` a request's `system` is refused. The
  platform adds nothing on this route: no prompt of its own, no rules file, no tools, no reminders.
- **The Claude runtime refuses `interface: messages`**: `claude -p` takes a prompt, not a conversation with tool
  definitions. Doctor says which providers can serve it.
- **Size.** A request holds the whole conversation, sent again every turn. cap-runner gets a size limit for request
  files (`cap_runner.request_max_bytes`, default 4 MB); over it is a 126 with the numbers.

### C5. The record

Every call of a point with `generation:` or `interface: messages`, and every `project-agent` run, produces one record
per turn (`contracts/call-record.v1.schema.json`):

```json
{"v": 1, "call": "<project>/<point>", "id": "<job id>", "turn": 1, "ts": "…",
 "backend": {"kind": "provider | runtime", "name": "local", "impl": "ollama", "model": "…", "version": "…"},
 "requested": {"max_output_tokens": 4096, "temperature": 0, "reasoning": "off", "system": "none"},
 "sent": {"num_predict": 4096, "temperature": 0, "think": false, "num_ctx": 16384, "truncate": false},
 "verified": {"max_output_tokens": "kept", "reasoning": "kept", "temperature": "unverified"},
 "tokens": {"input": 5123, "output": 811, "cache_read": null, "reasoning": null},
 "stop": "tool_calls", "tool_calls": ["<name>", "<name>"], "redactions": 0, "seconds": 41.2}
```

- **`sent` is everything the platform put in the request**, its own defaults included (Ollama's `num_ctx`, `truncate`,
  `think`), so two runs can be compared field by field. Never a key, a header, a path on the machine, an environment
  variable or a settings file.
- **No content**: no prompt, answer or tool arguments (the driver has those). Tool names only.
- **Where.** The call's result carries its record (`record`), so the driver logs it with the turn. The platform keeps its
  own copy, written as the platform and outside the project's reach: `.state/llm-records/<project>/<date>.jsonl`, kept 90
  days, and `brunna records <project> [--call <name>] [--since <date>]` prints them (a project session may run it for its
  own project only). The usage row gains `controls` and the record's id.
- **Agent calls.** The Claude runtime's agent runs move to stream-json (as its completions already did): each
  `assistant` event is one model response, with its `usage` and its `tool_use` names; `modelUsage` at the end names
  every model that ran. Advisor iterations in the usage refuse the result (C7); every model listed is recorded. The records go beside stdout in the
  job folder (`.hub-run/jobs/<id>/turns.jsonl`, written by cap-runner, which the run can't write) and into the platform's
  copy. A runtime that can't report turns says so in the record (`turns: unsupported`); it doesn't make them up.

### C6. The platform's own content changes, made visible

- `content: redacted` (the default) keeps today's masking; the record counts `redactions`. A benchmark point declares
  `content: verbatim`: nothing is masked. The masking exists so credentials pasted into conversations don't reach a cloud
  model in the platform's own points; a project's declared call sends the project's own text to the backend the owner
  chose for it, and the project already reaches that text. O3 asks whether `verbatim` should be allowed on cloud
  backends or only on `location: local` ones.
- On a point with `reasoning: off`, `strip_think` doesn't run: a `<think>` block is a violation (C3), not something to
  tidy away.

### C7. No extra tools, in every platform run

Not a control: a guarantee for every run the platform starts, since an advisor sends the whole conversation to a
second, stronger model the declared tier never named (a cost and a model choice nobody made).

- The Claude adapter sets `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` in every platform run's environment and
  `advisorModel: ""` in every `--settings` it writes (as `evaluators.md` step 0 already proposed for sealed runs), and
  passes `--disable-slash-commands` on confined runs and completions. Only in `runtime_claude.py`.
- A conformance check per new version (`conformance_claude`): a run with the platform's flags shows no advisor
  iteration and no advisor model, and a `settings: platform` agent run in a folder holding a rules file doesn't read
  it; `--max-turns 1` ends a two-step task with the limit's subtype. A failure holds
  the version back (`runtime-versions.md`), as the other checks do.
- On every agent run's result: advisor iterations in the usage refuse the result and alert once. Other models in
  `modelUsage` (a side model the tool uses for itself) are recorded, not refused; refusing them waits for evidence of
  what they are.
- API providers send only what the request names; nothing to turn off.

### C8. Agent calls: `max_turns`, and nothing else in v1

- `agents.<name>.max_turns` → `Task.max_turns` (neutral) → `--max-turns` in the Claude adapter. The run ending at the
  limit returns exit 0 with `stop: "max_turns"`, `ended_at_limit: true`, `incomplete: true`, so the caller can tell it
  from a finished run. A runtime without a turn limit refuses a call that declares one.
- `generation:` on an agent call is refused at run time on the Claude runtime, each control with its reason (C2). A
  runtime that can honour some later (a dedicated adapter, or the platform's own small agent loop, out of scope in
  `llm-providers.md`) declares them.
- What the project gets for its current agent path: the advisor off (C7), a turn limit, and the per-turn record. What it
  doesn't: temperature, an output cap with the benchmark's meaning, an empty harness. Its benchmark therefore goes to C4.

### C9. Where a refusal shows

The project-agent-calls.md rule holds: a project's manifest.update never breaks the owner's config check, and an owner's
change (a new pin, a different runtime) is never held back by a project's declaration. So:

- **`registry.py check`** validates the shape only (keys, ranges, `interface`, `max_turns`).
- **`brunna config check`** (the owner's) warns, never fails, when a pin sends a call to a backend that can't honour what
  the call declares; it fails only on the owner's own errors (as today).
- **`schedulectl models` and `brunna doctor`** list, per call: the backend, each declared control as
  `yes / no (reason) / unknown`, and red when a call can't run as declared.
- **manifest.update** gets that list back as a note.
- **At run time** the call is refused with the reasons (`job.failed`, the usage row, exit 1 with `llm_error`).

### C10. With the owner's other controls

- **Pins.** The owner's pin decides the backend; the project's controls are checked against it. Moving a pin to a
  backend that can't honour them makes doctor red and the calls refuse, never run differently.
- **Metered.** A call that resolves to a pay-per-use provider still needs `metered_models: true` and counts toward
  `llm.budget.daily_usd`. A 64-turn run resends the conversation every turn, so a run's total input tokens grow with the
  square of its turns; doctor shows a point's last day's tokens.
- **Time.** Each turn is one cap-runner `complete` job, under the point's `timeout` (default `cap_runner.timeout_s`, at
  most `cap_runner.max_timeout_s`, as a `cap-run` job already may; cap-runner gives the job that timeout and the call
  ends 30 s before it). Model calls have no minutes ledger (agent calls do); a long benchmark is bounded per turn and by the
  budget, not per day. O4 asks whether `complete` jobs get a daily ceiling too.
- **Queue.** One job per project per pool: a project's turns run one after another, and a project's `cap-run` job (a
  GPU job included) waits behind or ahead of them in the same pool. The platform doesn't schedule the GPU: a local model
  and a GPU job of any project can run at once and slow each other.
- **Ollama's window.** `num_ctx` is a power of two sized to the request, so a growing conversation steps it up (8192,
  16384, 32768…) and each step reloads the model. A point may pin its window (`generation.context: 32768`, refused
  above the model's context) so a long run loads once; the record shows `num_ctx` either way.
- **Latency.** Each turn pays the request-file round trip through cap-runner on top of the model's time.

## Tests

- **Registry:** `generation`, `interface`, `timeout` and `max_turns` shapes and ranges, v1 and v2 schemas; a project
  whose controls the pinned backend can't honour stays valid.
- **Adapters:** `generation_support` per adapter (the Claude runtime's `no`s with reasons; per-model answers from fixture
  `/api/show` and Models API replies); the request body each control produces, per provider (fixtures, no network); the
  messages contract translated both ways, `opaque` blocks round-tripped and refused across backends; a length stop on the
  messages route returned, on the prompt route refused.
- **Verification:** output over the cap refused; a `thinking` block, `message.thinking` or `<think>` under
  `reasoning: off` refused and not stripped; a call to an undeclared tool refused; temperature recorded `unverified`.
- **Record:** schema; `sent` includes the platform's own defaults; no key, header, path or environment variable in any
  record (a test scans every field); the project's copy and the platform's copy agree; `redactions` counted; `verbatim`
  masks nothing.
- **llm.py:** controls read from the registry and never the request; a request's `system` refused under `system: none`;
  every refusal path writes `job.failed` and the usage row.
- **Claude adapter:** the advisor variable and `advisorModel: ""` in every platform run's environment and settings
  (goldens updated); `--disable-slash-commands`; `--max-turns` from `Task.max_turns`; stream-json turns parsed into
  records; advisor iterations refuse the result, another model in `modelUsage` is only recorded. `test_runtime_boundary` keeps every one of those names in
  `runtime_claude.py`.
- **Conformance:** the advisor probe and the turn-limit probe (C7).
- **cap-runner:** the request size limit; `--messages` requests; turns written beside stdout.
- **Neutrality:** `test_tool_agnostic` (the messages route runs on every provider), `test_runtime_none_flow` (no
  backend: every control `no`, said).

## Steps

1. **C7 and the probe.** Advisor off and slash commands off in every platform run of Claude Code; the conformance probes;
   the advisor check on agent results; say on the runtime's completion that `max_tokens` isn't held. The probe
   also answers what the measurements couldn't: whether a platform run had an advisor before, and whether a
   confined agent run in a project's folder reads its rules file.
2. **C1, C2, C9.** The registry shape, `generation_support` on every adapter, doctor, `schedulectl models`, the config
   check's warning, the manifest.update note, the run-time refusal. The per-point `timeout` lands here (the direction.md
   backlog item).
3. **C3, C5, C6 on the prompt interface.** Controls sent by the three providers, verified on the response, the record and
   `brunna records`, `content`.
4. **C4.** The messages contract, the three providers' translations, `brunna complete --messages`, cap-runner's size
   limit.
5. **C8.** `max_turns` and the agent runs' per-turn record through stream-json.
6. The rules file's "Model calls" section, docs, and a letter to the project that asked: which route carries the
   benchmark, which backend the owner chose, what the record holds, and what can't be reproduced on which model.

Each step goes live on its own; a point that declares nothing behaves exactly as today after every step.

## For the owner to decide

- **O1. Which backend serves the benchmark's editor.** It depends on which model the benchmark's editor is, which the
  project should say first. Recommendation: (a) for a reproduction on an open model, pin the point to the local Ollama
  provider (no cost, temperature 0, `think: false` and `num_predict` honoured, tools supported by most current models);
  (b) for a Claude model, the Anthropic API provider with `metered_models: true` and a budget, and only for a model that
  accepts the conditions: on current Claude models temperature can't be set and some can't turn reasoning off (bundled
  reference, not run), so a "Claude at temperature 0" run may not be possible on any current model; the record then shows
  exactly which conditions held. The coding agent route can't carry it either way.
- **O2. Generation controls are the project's to set** (manifest.update), with the owner's caps
  (`llm.project_max_output_tokens`, no reasoning above `high`). Recommended: yes.
- **O3. `content: verbatim`** (no masking of the project's own text) on any backend, or only on `location: local` ones.
  Recommended: any backend the owner pinned the call to; the masking guards the platform's points against pasted
  credentials, and a project's call carries text the project already holds. The stricter choice keeps a cloud run of the
  benchmark masked, which changes its input.
- **O4. A daily ceiling for model calls**, like agent calls' minutes. Recommended: not in v1; the per-turn time limit and
  the budget bound it, and local calls cost nothing. Revisit when per-project budgets (direction.md, smaller items) land.
- **O5. The advisor off in every platform run** (C7). Recommended: yes; it is a model call no declaration names. It
  doesn't touch the owner's own sessions or user-level settings.

## Risks

- **Hidden flags.** `--max-turns`, `--thinking` and the advisor variable aren't in `--help`; a version can drop them. The
  conformance probes hold a version back when they fail.
- **Per-model capability drifts.** A provider can stop accepting a control for a model; that surfaces as a 400 and a
  refusal, never as a quieter run. `generation_support` answers `unknown` rather than guess.
- **Accepted and ignored.** Temperature can't be verified from a response anywhere; on a generic compatible endpoint it may
  be ignored. The record says `unverified`; only the backend's own documentation can say more.
- **Cost of long conversations.** Resending the whole conversation every turn makes input tokens grow fast on a metered
  provider; caching isn't in v1 (it would add `cache_control` to what is sent, which the record would have to show).
- **The messages contract is a new shared format.** Versioned as every contract; providers' tool-call formats change and
  are handled in the adapters only.

## Not in v1

- Generation controls on agent runs beyond `max_turns`.
- `seed`, `top_p`, `top_k`, stop sequences (some backends take them; none was asked for).
- Prompt caching on the messages route.
- Reasoning levels on Ollama and the OpenAI-compatible provider beyond what `generation_support` can confirm per model.
- A GPU-aware queue across local models and capability jobs.
