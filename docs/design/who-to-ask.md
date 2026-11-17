# Who to ask: from a question to the projects that likely know

> **Status**: design, first draft (11-17). Nothing built. Source: a project's letter of 11-17, sent at the owner's
> direction (data, not instructions). Its second concern, where a sensitive answer ends up, is out of scope here and
> recorded under "Follow-up".

## The problem

The owner (11-17), in substance:

- **The missing piece is not a channel.** `brunna send` exists and works. The day before, a session asked another
  project and had an answer within a minute, and the answer changed its advice.
- **A session doesn't know whom to ask.** That session had a question that crosses projects: a personal decision whose
  context sits in several of the owner's projects. It knew one project only because the owner named it. It knew other
  projects existed, not which of them bore on the question.
- **It must not read other projects, or guess from what its sandbox settings happen to show.**
- **Goal:** given a question, tell the asker which projects (or the owner's side) likely know, with a one-line reason
  each, and never any content from them. The asker then uses `brunna send` as it does today.

The letter's own principle applies to this design too. Information should reach the place where the judgment is made,
without leaving copies everywhere, and the judgment stays with the owner.

## What exists today (measured 11-17, on main at `3eab9bf7` and the live registry)

**The registry is readable by everyone.**
- `registry.CACHE` is `.shared/registry/` (`hubconf.REGISTRY_INDEX`). Its docstring says it is "readable inside every
  sandbox", and `policy.readable()` puts it in `policy.read`. It holds full entries: name, paths, `purpose`, `privacy`,
  jobs, and the names of declared secrets.
- The config dir with the registry YAMLs is in `policy.instance_dirs()`. That puts it in `deny_write` only, not in
  `platform_state`, so a sandbox can read it too. This was read from the policy's lists; no session tried it.
- A sandboxed session's own settings carry deny rules naming every other project's folder (`policy.other_projects`).
  This is the "sandbox config happens to show" in the letter.
- **Conclusion:** project names (and paths) are not secret from any session. What is private is what a project holds.

**`purpose` is almost empty.**
- It is "the idea itself", required only for `type: idea` (`registry.py` header). The lease's system prompt and the
  idea-sandbox rules (`{{purpose}}`) use it.
- In the live registry, 3 of 30 entries have one.

**`privacy.reflection`** (`include | stats_only | exclude`; one of the security fields only the owner changes)
- In the live registry: 23 include, 5 stats_only, 1 exclude, 1 unset (= include).
- Only two places read it, both through `hubconf.reflection_private()`, which lumps stats_only and exclude together:
  - `reflect_llm.EXCLUDED`: not even a summary of those projects goes to the weekly review's model;
  - `mailbox_route.text_allowed`: a candidate session's last reply reaches the routing model only for include projects.
- **The librarian doesn't read it.** Every project's notes go to the librarian's model nightly, and the digest and
  `index.md` carry content from all of them.

**`index.md`** (data dir):
- The roster block (`bin/gen-index-roster.sh`) is name + path from the registry.
- The block outside it is written by the `librarian-index` point. It holds cross-project relations stated as facts
  (dates, decisions, what one project's STATE.md says about another). That is content, not topics.
- `policy.platform_state` denies the data dir to every sandbox; unsandboxed sessions can read it.

**STATE.md and the memory copies.**
- A sandboxed session can't read another project's STATE.md: other projects' workspaces and memory copies are in
  `other_projects`, and `deny_read` covers the raw notes.
- An unsandboxed session (24 of the 30 projects) runs as the user and can read all of it. There the platform only
  advises (`tool-agnostic.md`: "a session the user opens themselves has only the tool's native protection").

**`brunna send` and routing.**
- The recipient is a project or `owner`. An unknown address is refused with "project names are in the project
  registry" (`hubbus send_letter`).
- A sandboxed project's commands write its outbox (`<workspace>/.git/brunna/outbox/`). `hubbus dispatch` (a core
  service, every minute) takes the outbox in as that project and writes the project's view back (`write_views`,
  `inbox.json`) (`bus-sender.md`).
- Routing picks a **session** within the recipient project (`mailbox_route.py`). Nothing picks a **project**.

**Synchronous session commands.**
- `brunna complete`, `agent` and `cap-run` write a request into `.hub-run/requests/`, and cap-runner answers into
  `.hub-run/jobs/<id>/` (`SESSION_COMMANDS` in `bin/agenthub`, `bin/hub-run`).
- cap-runner is the `sandbox` module's service, and `hub.example.json` ships with `modules.sandbox: false`.
- It serves only projects that declare `models:`, `agents:` or capabilities.
- Its `log.jsonl` records the point name, not the prompt, and job folders are kept 7 days.

**A session-only request folder** (platform push, `platform-push.md` section 3).
- `brunna push` writes `<push_dir>/<project>/<id>.request.json`, outside every workspace. The policy key
  `session_write` renders it only into the project's session sandbox, so services and jobs can't request.
- `hubbus dispatch` takes it in, a detached worker runs it, and the client waits up to 90 s for `<id>.result.json`
  (under the agents' own 120 s command timeout), then exits 75 with "no answer yet".

**The owner side.**
- `owner` goes to the assistant when the optional assistant module is on, through `hubbus-bridge`.
- Otherwise it goes to the built-in `bin/owner.py`: the owner gets an alert and answers with `brunna owner answer`.
- Either way a question to `owner` is a bus row (append-only, backed up nightly) and, without the assistant, work for
  the owner.

**The rules template.**
- `templates/agent-instructions.md`, "Cross-project changes, the project mailbox", says how to send, never whom to send
  to. Nothing tells a session where to find out.

**Precedent for untrusted input to a model.**
- `mailbox-route` (`contracts/llm-points.yaml`) judges a letter any project can send:
  - it runs through `llm.answer`, which prints and publishes nothing, so no `job.failed` quotes the model;
  - the model sees candidates by label, never by id;
  - its schema has no enum or pattern, so an error can't echo a value;
  - it takes one attempt and runs in the background, its result read on the next tick.

## Requirements

1. **No content crosses.**
   - Whatever the index holds for a project must be showable to any asker. The surest way is that the project wrote
     it to be shown.
   - The question is untrusted input: if the model could word the reason from anything less showable, a crafted
     question would pull it out.
   - So the model returns labels only, and the platform prints the stored text verbatim.
   - Enumeration (many questions in a row) then reveals nothing that wasn't meant to be shown.
2. **Works from a sandboxed session, through a channel that already crosses the boundary.**
   - The request is a file in a folder only that project writes, so the folder names the asker (as with the outbox,
     cap-runner and platform push).
   - The answer comes back as a file the platform writes there.
   - No bus row: the question shouldn't sit in the append-only log.
3. **Respects the privacy label without inventing enforcement.**
   - It reuses `privacy.reflection`. Direction's 10-29 row (no privacy routing until the evaluators can measure it)
     stands: this decides only what the index may hold.
4. **Tool-neutral.**
   - Judgment goes through `llm.py` points declared in `contracts/llm-points.yaml`, `shape: completion` (any
     provider).
   - The session command is a `brunna` subcommand and an MCP tool, never one runtime's syntax.
   - No model configured: the feature is off and says why (`runtime_ports.Unsupported`, doctor's feature table).
5. **Nothing new for the owner** (11-16, "the owner's surface stays small").
   - The owner writes no cards, approves no index, and gets no new command.
   - "Ask the owner" is offered only as a last resort.
6. **Works for a new user**: with two or three projects, no assistant module, and the `sandbox` module off as shipped.

## Designs

All three need an answer to the same question: **what does the index hold for a project, and who wrote it?** A line
about a project shown to an asker always says something about what the project holds. It satisfies "never any
content from them" only if it was written to be shown.

### (a) Platform-held cards and a matching point

**What is stored, and where.**
- One **card** per project: a line of at most 120 characters and up to 8 topic labels of at most 40 characters each,
  topics not facts ("thesis direction", not what was decided).
- Held in `.state/who/cards.json` with each card's date and source. `.state` is denied to every sandbox
  (`policy.platform_state`). The 24 unsandboxed projects can read it, as they can read every STATE.md.

**Where a card comes from: two sources, the owner's pick (question 2).**

- **(a1) Declared: the project's own agent writes it.**
  - `brunna who --card "<line>" --topics a,b,c` (and the MCP tool). The project is the request folder's owner, never a
    name in the file.
  - Getting started:
    - Once, and at each onboarding, the platform sends every project a `request` letter, "declare what you can be
      asked about", with the format.
    - A session or the headless triage agent answers it, with the project's own notes in front of it and its own
      judgment of what is showable.
    - The triage agent has no shell, so it replies with the card in a fixed form, which the platform parses. A reply
      that doesn't parse is asked once more.
  - Keeping it current: the nightly job sends a "check your card" request when the card is over 30 days old and STATE.md
    has changed since, at most once a month per project.
  - It is showable by construction: the project chose every word. It works the same for every privacy label (exclude
    aside, question 2), and no model reads anyone's notes for it.
  - Cost: one letter per project to start (about 30 here), then a few a month, all handled by agents.
  - Coverage grows as projects answer. A project that never answers is never a candidate.
- **(a2) Derived: the platform writes it from the notes.**
  - A nightly job `brunna/who-cards`, `after: librarian`, only for projects whose STATE.md or `purpose` changed.
  - A new point `who-card` (completion, fast): input = `purpose` + STATE.md as data, output = `{line, topics[]}` under
    the size limits.
  - For include projects only. stats_only and exclude never get a derived card.
  - It covers every include project from the first night, with no letters.
  - **But it is new material crossing.** A sandboxed asker can't read another project's STATE.md or the data dir.
    A card a model wrote from that STATE.md is a summary of content the asker couldn't see, and "topics, not facts" is
    an instruction to a model, not a guarantee. The librarian's model reading the same notes only shows that the
    provider sees nothing new; it doesn't make the card showable.
- A declared card always replaces a derived one.

**How a question gets to the platform and the answer comes back.**
- `brunna who "<question>"` (also the MCP tool `who`) writes `<workspace>/.git/brunna/who/<id>.json`. That is the
  platform's folder in the repo, the outbox's sibling, which only the project writes. It stays writable under the git
  fence (`policy.git_fence`'s `repo_dirs`).
- `hubbus dispatch` (core, every minute) takes it in as that project and starts the `who-to-ask` point in the background
  through `llm.answer`, as mailbox-route does. On a later tick it writes `<id>.answer.json` with `nofollow`, as
  `write_view` does, and deletes the request.
- The client prints the id first and waits up to 90 s (platform push's figure, under the agents' 120 s command
  timeout). Without an answer by then it exits 75; `brunna who --wait <id>` picks the answer up.
- Expected: one to two dispatch ticks. Not measured. Mailbox-route allows one model call per tick per project
  (`hubbus pick`), and `who` needs its own allowance so it doesn't queue behind routing.
- **Why not platform push's session-only folder:** that folder exists so services and jobs can't push. Asking who
  knows needs no such limit, and the repo folder works for unsandboxed projects with no new root or policy key.
- **Why not cap-runner** (seconds, like `brunna complete`):
  - it is the `sandbox` module's service, off in `hub.example.json`;
  - it serves only projects that declare something;
  - a new user would have no `who`.

**The `who-to-ask` point** (completion, fast, one attempt, output schema without enums or patterns):
- Input:
  - the question, cut at 2,000 characters, `<` escaped as in `mailbox_route.prompt`;
  - every eligible card under labels c1…cn, never the asker's own card or an exclude project's.
- Output: `{candidates: [{card: "c3", topics: [1, 4]}], owner_decision: bool}`.
- The platform drops any label or index that isn't real and keeps at most three. For each it prints
  `<project> — <stored line> (matched: <stored topic labels>)`. The model writes no text the asker sees.
- Fewer than two eligible cards: no model call. The card, if any, is printed with the owner line.

**The owner as a candidate.**
- The platform adds a fixed last line, `owner — …`, only in these cases:
  - nothing matched;
  - the model set `owner_decision` (the question asks what the owner wants or should decide);
  - the asker is the only project.
- With the assistant module on, `owner` is the assistant, which may know more. Without it, it is the owner in person,
  hence last resort.

**What the asker sees.**
- Only the matched cards and the owner line, never the whole set.
- Cards are showable, so this is about copies, not secrecy. A full list printed into every asking session would land
  in its transcript and possibly its notes, the spreading the letter warns about.
- A printed card is another project's words in the asker's context. The rules call it data, as they do letters.

**The question's own sensitivity.**
- The platform's model reads it, as it reads a `brunna complete` prompt. The owner can pin `who-to-ask` to a local
  model (`llm.points.who-to-ask`).
- **The asker's privacy label.** Asking is the session's explicit act, like `brunna complete`, which no label gates.
  - Proposed: a stats_only or exclude asker's question goes only where `mailbox_route.text_allowed` would send a reply's
    text: the provider of the runtime its sessions already use, with no pin on the point, or a local pin.
  - Anywhere else, it is refused with the reason.
- It stays out of:
  - the bus;
  - `job.failed` (`llm.answer`);
  - usage rows (point, provider and cost only);
  - eval recordings (this point's eval set is synthetic).
- The request and the answer live in the asker's own git dir, where its context already is. The request goes when it
  is taken in, the answer when it is read or after a day.
- A daily count per project in `.state/who/` holds the cap and records no text.

**Cost.**
- One fast call per question, with about 30 cards × 200 characters plus the question.
- Capped per project per day: 20 suggested, a `who` setting.
- (a2) adds one fast call per changed project per night.

**Failure modes.**
| Case | What happens |
|---|---|
| Nothing matches | "No project's card matches this question" + the owner line. Exit 0. |
| Wrong match | The asked project replies "not mine", at the price of one letter. The printed line lets the asker judge first, and the project can correct its card. |
| A card worded to match everything | It draws letters it can't answer. The size limits and "pick the most specific" in the prompt reduce this but don't prevent it. Each such letter costs one "not mine" reply. Watching how often a card is picked against how often the pick is followed is later work. |
| Stale card | Its date is printed. (a1): the monthly "check your card". (a2): rebuilt the night after STATE.md changes. |
| No card yet | The project is not a candidate. The owner line covers it. |
| No answer from the model | In fixed words: "no answer this time", + the owner line. Exit 1. |
| No answer within the wait | Exit 75, then `--wait <id>`. |
| Over the day's cap | Refused with the numbers. |
| No model configured | Off, said by the command and in doctor's feature table. Declaring a card still works. |

**Variant (a′): print every card and let the session match.**
- Same cards; `brunna who` takes no question and prints them all.
- For it:
  - the platform never reads the question;
  - no model call per question;
  - it works with `runtime: none`;
  - the asker's own, stronger model matches with the full context.
- Against it:
  - every asking session copies every card into its transcript;
  - the platform can't tell when to add the owner line.
- Owner question 1.

### (b) Projects declare what they can answer, in the registry

- **What is stored, and where:** a new registry field, e.g. `answers: {line, topics}`, set by the project's agent with
  `manifest.update` (project-owned, not a security field).
- **Matching:** the `who-to-ask` point, or the session reading the registry itself.
- **For it:**
  - the project writes its own card (as in a1);
  - it needs no new storage.
- **Against it:**
  - The registry is readable by every sandbox (above), so this is a directory every session can browse. It turns (a′)
    on for good, with no choice.
  - 3 of 30 entries have even a `purpose`, so nearly every project starts with nothing.
  - Each card is a `manifest.update` and a registry commit.
- **Kept as:** a1, the same project-written card, held with the platform instead of in the registry.

### (c) Route through the owner's side

- **How:** `brunna send --to owner --type question "whom should I ask about …"`. The assistant, when present, answers
  from what it knows of the owner's projects.
- **For it:** the assistant may know more than any card, including things outside brunna.
- **Against it:**
  - The assistant is optional. Without it the built-in owner side is the owner in person, so every question becomes
    the owner's work (against 11-16).
  - The question becomes a bus row (append-only, in the nightly backup) and enters the assistant's memory.
  - It is asynchronous, on the owner's or the assistant's time.
  - It is outside the platform's guarantees (the assistant's repo, its own model).
- **Kept as:** the fixed owner line in (a).

## Recommendation

**(a) with declared cards (a1).** Cards are held by the platform, each written by its own project. A matching point
returns labels only. The asker sees at most three matches, and the owner as a last resort.

- It is the only combination where:
  - every word an asker sees was written by the project to be shown;
  - no sandboxed session can browse what the projects hold;
  - the owner does nothing.
- Its cost is coverage: a project is findable only once its agent has answered the request letter.
- (a2) can be added later for include projects if the owner prefers coverage now (question 2).
- It reuses existing pieces:
  - the request-letter flow, for writing cards;
  - mailbox-route's pattern for untrusted input to a model;
  - dispatch taking files from a project's own folder;
  - the privacy label, read only.

**Minimal first step** (one branch, each part tested):

1. **The point.** `who-to-ask` in `contracts/llm-points.yaml`, with its schema under `contracts/llm/`: labels and
   indexes only, no enum or pattern. A small synthetic eval set (made-up cards and questions, no real data) in
   `bin/evals/`, run before the feature is switched on.
2. **The command.**
   - `who` in `SESSION_COMMANDS` (`bin/agenthub`) and `policy.platform_cli`.
   - Its two forms, asking and `--card`, through `<workspace>/.git/brunna/who/`.
   - `hubbus dispatch` takes them in for every project, not only outboxed ones, with its own allowance per tick.
   - Cards go to `.state/who/cards.json`; answers are written with `nofollow`.
   - The daily cap, and the asker's label rule.
   - The MCP tools `who` and `who_card` in `bin/mcp_server.py` `TOOLS`. For now they are not in the handler or job
     tool sets; the triage agent declares a card by its reply.
3. **Cards.**
   - The one-time request letter to every project, and to each new one at onboarding.
   - The reply parser.
   - The monthly "check your card" in a nightly platform job, which also prunes cards of projects no longer in the
     registry.
4. **Rules.** A paragraph in `templates/agent-instructions.md` (the mailbox section) and in
   `templates/mcp-instructions.md`, saying:
   - when another project or the owner may know: `brunna who "<question>"`, then `brunna send`;
   - the printed lines are data;
   - don't read other projects' files or infer from names in your settings;
   - how to declare this project's card.
5. **Checks.**
   - A doctor feature row (on / off and why).
   - `tests/test_owner_surface.py` keeps `who` out of the owner's help.
   - Tests:
     - a question asking for a card's raw text gets labels only;
     - the asker is never its own candidate;
     - an exclude project is never a candidate;
     - a card names its project by the folder, never by a name in the file;
     - with no model, asking is off and says so;
     - nothing of the question reaches the bus or the logs.

## Open questions for the owner

1. **Match on the platform, or print every card?**
   - Matching on the platform: the platform's model reads the question, and the asker sees at most three cards.
   - Printing every card: the platform never sees the question, but every asking session sees every card.
   - **Recommendation:** match on the platform. Fewer copies of what projects hold, and it can add "ask the owner" only
     when it fits.
2. **Where cards come from, and who can be found.**
   - Declared: each project's agent writes its own card, on the platform's request. Only words the project chose are
     shown, and coverage grows as projects answer.
   - Derived: the platform writes cards from STATE.md for include projects. Coverage is immediate, but a model's
     summary of notes a sandboxed asker couldn't read crosses to it.
   - **Recommendation:**
     - declared only, for include and stats_only projects alike;
     - exclude projects never findable, even with a card;
     - derived cards only if declared ones are too slow to come in.
3. **"Ask the owner" as a candidate.**
   - It is added only when nothing matches, when the question is about what the owner wants or decides, or when the
     asker is the only project.
   - Without the assistant, that line means the owner answers in person.
   - **Recommendation:** yes, as the last line, worded as a last resort.

## Follow-up (out of scope)

**Where an answer persists, and how sensitive it is** (the letter's second concern).
- A reply to a cross-project question lands in the asker's mailbox: a bus row, append-only, backed up nightly.
- The rules then have the asker write it into its notes, which the librarian may fold into STATE.md.
- An asker with a public remote keeps those notes on a separate, unpushed branch, but they still live in that project.
- Ideas the letter raised, recorded and not designed:
  - answers marked with a sensitivity level, shown only in the conversation;
  - conclusions without details;
  - the answering project deciding what leaves;
  - an extra limit for projects with public remotes.
- Direction's 10-29 row applies: no privacy routing or enforcement until the evaluators can measure it.
- Until then the interim advice given to the asking project stands: no cross-project consults on private matters, and
  no such context in notes.

**The reverse direction.**
- The question the asker sends with `brunna send` carries the asker's context into the answering project's mailbox
  and notes.
- `brunna who` itself sends nothing to the candidates. The follow-up letter does.

**Related, noticed while measuring (not this design's to fix):**
- `privacy.reflection` doesn't reach the librarian, so the digest and `index.md` carry content from stats_only and
  exclude projects. They are in the data dir, readable by unsandboxed sessions.
- `brunna complete` and `brunna agent` need cap-runner, which is declared only in `modules/sandbox.yaml` and ships
  off (`modules.sandbox: false`). A new user's project model calls should then fail with `hub-run`'s 125 (the run
  service isn't running) until the module is on. Read from the code, not run.
