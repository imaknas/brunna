# The agent surface: what brunna shows and offers inside a coding agent

Status: design 11-18, not built; **r1 (11-18), after a blind review** that found nothing blocking. Its points are folded
in below: new G rows and a recount, a session surface keyed by the session's record, the band trimmed to what needs the
owner, a fixed command allowlist, install from the hub's own checkout. Three of its points are being fixed in code
separately and are only recorded here: S1 (the platform renders a sandboxed worktree's settings itself), S6 (a
conformance canary for plugins in platform runs) and S7 (the foreign-control check reports plugin manifests and plugin
settings keys). None of them is part of the mod's plan.

It answers the owner's question of 11-18: could brunna's Claude Code-specific hooks and features become a Claude Code mod
(a plugin of function hooks, installed at user scope from a marketplace file), and which platform features should be
mods? The owner's constraint: whatever goes into the mod is first extracted behind a neutral interface, so Codex or any
other agent can plug in the same way. The inventory below is measured from the code at `d8302ffc` (r1 at `8b9c5ab6`);
the plugin facts come from the plugin-authoring reference and type declarations shipped with Claude Code 2.1.325 (the
skill's `SKILL.md` was not at the path given; `reference.md`, the types and the examples were).

## The short answer

- **A mod, yes, for what the owner sees and the commands the owner is offered. Not for what the hooks do today.** Of
  the 45 integration points counted below, 5 are reminders or notices (U), 25 keep a platform flow correct (C) and 15
  are guarantees (G). Every U item is text written for the session's model, not for the owner, and none of the C or G
  items may depend on an optional plugin the owner can remove. So the first mod moves no hook. What it adds is new: what
  needs the owner and the platform's health, drawn in the session the owner already has open, plus two commands.
- **The neutral layer comes first.** Lifecycle events already have a neutral interface (`bin/hub-event`,
  `runtime_ports.EVENTS`, `HubEvent`, each adapter's `parse_event`/`inject`/`deny`). This design adds a second, smaller
  layer for display and commands: data out and commands offered. The existing `brunna glance --json` (glance.v1) gives
  the owner's view across projects; a new session-scoped `brunna surface --json` gives the session's. The Claude Code mod is one
  adapter of that layer, like the herdr mod is of glance. Codex's adapter is what Codex is measured to support today,
  which for display is nothing. With no adapter at all, everything still works through the rules text, the CLI and the
  MCP server.
- **Two hooks could move later**, each only once the platform can check that the mod took part and falls back when it
  didn't: the idle-session waiter (the `Stop` hook) and the user-level worktree hooks for projects that aren't
  sandboxed. Neither moves in the first slice. A sandboxed project's worktree branch never moves.

## Part 1: inventory

One row is one (runtime, native event or place, handler), because one `hub-event` command fans out to handlers of
different classes (`HANDLERS` in `bin/hub-event`). The classes:

- **G**: a guarantee the platform relies on (security, enforcement). It stays in the platform's own mechanism or in the
  tool settings the platform renders and drift-checks, and fails closed where the code makes it fail closed.
- **C**: correctness of a platform flow (letters reaching a session, presence for dispatch, memory sync).
- **U**: a reminder, notice or display: reinforcement only.

"Fails" says what the code does when the handler or its entry point breaks. "Path" marks a command or file that holds
this machine's absolute paths (the hub's location from `hubconf.hub_bin`/`sh_py`/`sh_path`, the platform's Python),
written per project, per worktree or per user.

### A. Claude Code project hooks

Written into each project's `.claude/settings.local.json` (gitignored and in `.git/info/exclude`): for an unsandboxed
project by `sync-local-setup.sh` step 2 → `hub-event install` → `runtime_claude.install_hooks` (the platform's hooks
replaced as a set, the user's own kept); for a sandboxed project by `render-project-settings.py` →
`runtime_claude.platform_hooks()`, drift written back every 15 minutes. Every command is
`[ -x <hub>/bin/hub-event ] && <hub's python> <hub>/bin/hub-event --runtime claude <event> || true`: **Path, in every
project**, and it always exits 0 (the `hub-event` header's invariant), so every row here fails open unless it says
otherwise.

| # | Native event | Handler (`bin/hub-event`) | What it does | Class |
|---|---|---|---|---|
| A1 | SessionStart | `hub_pull` | the hub's own main checkout: `git pull` before work | C |
| A2 | SessionStart | `memory_pull` | `git pull` in `.agent-memory` (as the project when sandboxed: `AS_PROJECT`) | C |
| A3 | SessionStart | `local_setup` → `sync-local-setup.sh` step 1 | the rules file's managed block regenerated from `templates/` | C |
| A4 | SessionStart | `local_setup`, step 2 | the hooks re-installed (unsandboxed only) | C |
| A5 | SessionStart | `local_setup`, steps 0b, 2b, 3 | memory remote renamed, `AGENT.md` committed, local files in `info/exclude` | C |
| A6 | SessionStart | `local_setup` step 0 / `moved_notice` | "this folder moved to the hub machine, don't work here", into the model's context | U |
| A7 | SessionStart | `local_setup` step 4 | "committed files hold this machine's paths", into the model's context | U |
| A8 | SessionStart, PostToolUse(Bash) | `nudge` → `memory-nudge.py` | "notes are behind the commits", into the model's context | U |
| A9 | SessionStart, UserPromptSubmit, PostToolUse(Bash) | `session_record` | the session's process on record, so dispatch counts it live | C |
| A10 | SessionStart, UserPromptSubmit, PostToolUse(Bash) | `mail` → `hubbus.mail_text` | presence, the letters this session should get (told, held), into the model's context | C |
| A11 | SessionStart | `wake_address` | the session's wake address, mode 600, in the platform's state | C |
| A12 | every event | the per-version gate (`gate`, `GATED_OFF`) | a sandboxed project's session on a version that failed a safety check gets no mail, wake address or reminder; told once | G |
| A13 | SessionEnd | `mail_end`, `end_mark`, `wake_address_drop` | what the session held is freed; its records go | C |
| A14 | SessionEnd, PreCompact | `memory_sync` → `git-sync-memory.sh` | notes and `STATE.md` committed and pushed (the bundle path when sandboxed) | C |
| A15 | PreToolUse(Desktop's start-session tool) | `guard` → `guard.py` | refuses opening a session in another project ("letters only"). Allows when it breaks (its header); the boundary itself is the sandbox | G |
| A16 | Stop (in the background, `asyncRewake`, 14-day timeout) | `hub-event wait` → `runtime_claude.wait` | the waiter: exits with the code that wakes the idle session when a wake request comes; heartbeat read by dispatch | C |
| A17 | WorktreeCreate, WorktreeRemove (sandboxed projects only) | `worktree-make`/`worktree-drop` → `session_worktree.py` | the tool's worktree made or removed as the project, never as the user. **Fails closed** (exit 1, no `|| true`) | G |

### B. The hook dispatcher itself

What `hub-event` decides before any handler runs, for every runtime's hooks and the worktree entry point alike.

| # | Where | What | Class |
|---|---|---|---|
| H1 | `hub-event` `handle` (the `cage`, `holds`, `scope_of` checks) with `AS_PROJECT`, `NOT_IN_FOLDER`, `MEMORY_ONLY` | the folder rule: a handler that runs git or reads what the project can plant runs as the project (inside its sandbox) or where `project_git.scope_of` says the user may; only handlers that touch no folder run as the user; a folder swapped for a link elsewhere gets no work | G |
| H2 | `hub-event` `handle` and `worktree` (`CAGED_NEEDS <= capabilities(runtime)`) | a sandboxed project's session under a runtime the platform can't confine gets nothing: its hooks do nothing, logged as refused (fail closed) | G |

### C. Settings the platform renders (not hooks)

| # | Where | What | Class |
|---|---|---|---|
| B1 | a sandboxed project's `.claude/settings.local.json`, by `render-project-settings.py` (`policy.py` → `runtime_claude.session_settings`) | `sandbox` (failIfUnavailable, no unsandboxed commands, allowWrite, denyRead, network), the deny/allow rules, `env` (`NO_FOLDER_GIT`, tool caches); drift written back and reported. Absolute paths by nature | G |

### D. Rules text

| # | Where | What | Class |
|---|---|---|---|
| R1 | the managed block in `CLAUDE.local.md` (`hub-runtime instructions-files`, `prompts.agent_rules`) | the platform's rules for every session: names only the `brunna` command, no paths | C |
| R2 | Codex: SessionStart context (`rules` handler, `RULES_AT_START`, `rules_note()`) | the same text, since Codex reads no untracked rules file | C |
| R3 | the MCP server's `initialize` instructions (`templates/mcp-instructions.md`) | the rules and the open-letter count for an agent that has only MCP | C |

### E. Claude Code worktree hooks, user level

In `~/.claude/settings.json`, installed only by the owner (`merge-hook-settings.py --worktree`), kept current by
`brunna upgrade` → `brunna user-settings --update` → `runtime_claude.user_refresh`, removed by uninstall
(`user_cleanup`). Every command is `[ -x <hub>/bin/worktree-memory.sh ] && <hub>/bin/worktree-memory.sh <what> || true`:
**Path, in user settings**. In a main checkout the script does nothing. In a linked worktree it calls `hub-event
worktree <what>` first (outside any sandbox), then the rest.

| # | Native event | What | Class |
|---|---|---|---|
| W1 | SessionStart (`start`) | `hub-event worktree start`: the session's record and its mail | C |
| W2 | SessionStart (`start`) | links `.agent-memory` and the rules file, writes the worktree's own settings (`hub-runtime worktree-settings` → `runtime_claude.install_worktree`: the platform's project hooks, `install_hooks`, plus the memory folder as a working directory with Edit denied on `AGENT.md`/`RULES`/`.git` there), pulls memory, prints the rules file the first time | C |
| W3 | SessionStart (`start`) | `memory-nudge.py --session-start`; "the nightly cleanup removed dependencies here" | U |
| W4 | SessionEnd, PreCompact (`end`) | memory sync (`project_git.py sync-memory` when sandboxed); at SessionEnd the session's letters freed | C |
| W5 | PostToolUse(Bash) (`nudge`) | mail | C |
| W6 | UserPromptSubmit (`prompt`) | mail | C |
| W7 | Stop (`wait`, in the background) | the waiter, as A16 | C |
| W8 | SessionStart, a sandboxed project's worktree | `render-project-settings.py --worktree`, then the rest run as the project (`project_git.py run-as`). Today the only thing that renders a sandboxed worktree's settings at once (otherwise the 15-minute job), though it is an owner opt-in and fails open (`|| true`). **Being fixed (S1)**: `worktree-make` renders them when it makes the worktree | G |
| W9 | SessionStart, PostToolUse(Bash) (`start`, `nudge`) | the after-commit reminder in `hub-event worktree`'s output (`_worktree_nudge`) | U |

### F. Codex

| # | Where | What | Class |
|---|---|---|---|
| X1 | `$CODEX_HOME/hooks.json` plus `config.toml` `[hooks.state."<real path of hooks.json>:<event>:<group>:<handler>"] trusted_hash` (`runtime_codex.merge_hooks`, `trust_entries`), installed only by the owner's `brunna user-settings --install --runtime codex`, refreshed by upgrade, removed by uninstall | SessionStart, UserPromptSubmit, PostToolUse(Bash), PreCompact, SessionEnd (3 s budget, memory sync detached) → `<hub>/bin/py <hub>/bin/hub-event --runtime codex <event>`. **Path**: the commands, the trust keys (the file's real path) and the hashes (over the commands) | C |

Its handlers are A1–A14 (no guard, no waiter), with three declared differences: a letter reaches the model only at
SessionStart (`TELLS_ON`), as a notice of ids, never a body (`LETTERS_AS_NOTICE`), and no Codex session is woken (D4,
until M11). Sessions in sandboxed projects aren't served (`_served_folder`, and H2; Step 4). These rows are not counted
twice.

### G. MCP servers, platform runs, legacy entry points, skills

| # | Where | What | Class |
|---|---|---|---|
| M1 | `brunna mcp` (`bin/mcp_server.py`), registered by the owner from the snippet `brunna mcp config` prints (an absolute command: **Path**, in the tool's settings) | the mailbox and notes for any MCP agent; presence as `mcp:<pid>` | C |
| M2 | `mcp_server.py --run <file>`, attached by `llm.py` to a confined run | the run's own mail tools, held to the confined checks | G |
| M3 | `bin/lease-relay.py` (lease sessions' `--permission-prompt-tool`) | every permission question goes to the owner, the rest refused | G |
| M4 | `mcp_server.py` under `HUB_LLM_POINT` without `--run` | any other agent the platform starts is refused every tool; its letters go through its output and `propose_apply` | G |
| P1 | Claude Code platform runs (`runtime_claude._flags`, `platform_settings`) | `-p --restricted`, the platform's settings file only, `--strict-mcp-config`, named tools, `NO_FOLDER_GIT` | G |
| P2 | Codex platform runs (`runtime_codex.run_home`, `HOME_CONFIG`) | a run home of its own (`features` plugins, apps, hooks, multi-agent off; a permission profile). The adapter still declares `PLATFORM_RUNS_FIRE_HOOKS` (the user's hooks can't be skipped, M5 on 0.165), so `hub-event` does nothing under `HUB_LLM_POINT` | G |
| P3 | lease sessions (`llm.py exec lease-session`, `runtime_claude.lease_settings`) | read-only tools, the deny list plus every credential folder, every platform command asks the owner through the relay (M3) | G |
| L1 | `hubbus hook <event>` (`cmd_hook`) | the entry point hooks installed before `hub-event` still call | C |
| L2 | `guard.py pre-tool` | the old guard entry point | G |
| O1 | `brunna skills sync` → each adapter's `SKILL_DIRS` (`.claude/skills`, `.agents/skills`) | skills the owner reviewed, installed where the agent loads them | C |
| O2 | `brunna user-settings --update` (run by upgrade) | the owner's earlier user-level installs (E, X1) kept at this version | C |
| O3 | `skills_sync.refuse_here` (`owner_terminal.refusal`) and the refused names (control names; each adapter's `SKILL_REFUSED`, which since `8b9c5ab6` includes a plugin's manifest) | the install runs only at the owner's own terminal, never in a sandbox or an agent's session; nothing a tool would load as code passes the review | G |

Out of scope: the git hooks the platform writes (`pre-commit`, `commit-msg`, `pre-push`: runtime-neutral) and the
session readers (`sessions_claude.py`, `sessions_codex.py`: they only read).

### Counts

| Class | Count | Rows |
|---|---|---|
| G | 15 | A12, A15, A17, H1, H2, B1, W8, M2, M3, M4, P1, P2, P3, L2, O3 |
| C | 25 | A1–A5, A9–A11, A13, A14, A16, R1–R3, W1, W2, W4–W7, X1, M1, L1, O1, O2 |
| U | 5 | A6, A7, A8, W3, W9 |

Two facts the counts hide. First, every U item is text for the session's model (the agent is the one who must write
the notes, leave the old folder, fix the leaked path); none is shown to the owner. Second, all project-, worktree- and
user-level hook commands, Codex's trust entries and the MCP registration hold the hub's absolute path, so a hub that
moves rewrites all of them (install, `user_refresh`, `render-project-settings`). The rules text (R1) holds none.

## Part 2: design

### Two layers

1. **Lifecycle (exists, unchanged).** The platform reacts to session events through `hub-event` and the adapters'
   native hooks. It carries every C and G item above. A new runtime plugs in by implementing the `lifecycle` port.
2. **Surface (new).** What the platform wants the owner to see in a session, and the commands it offers there. It is
   runtime-neutral data and commands. An adapter turns it into whatever the agent can draw. It carries no C or G item;
   its loss is a display that isn't there.

The surface layer has three parts:

- **Show, owner scope**: `brunna glance --json` (glance.v1), as the herdr mod reads it: `needs_you` (the owner's to-dos
  and questions; skills waiting for review (`skills_sync`) and push causes a project can't fix (`push_queue.raise_todo`)
  already arrive here as owner to-dos, a release waiting is not checked), open mail per project, health, the
  scheduler. Two optional fields per `needs_you` item, both written by the platform where the item is raised (adding a
  field keeps v1):
  - `prompt`: the platform's words for asking an agent about the item, in the content language;
  - `command`: an argv whose `argv[0]` is `brunna`, for an item only the owner's terminal can settle.
- **Show, session scope (new)**: `brunna surface --json --runtime <name> --session <native id> --cwd <folder>`, listed
  under `help all` and never in `brunna --help` (`tests/test_owner_surface.py`). Its contract is
  `contracts/session-surface.v1.schema.json`, in the platform's `provides` with a validated example. Draft:

  ```json
  {"v": 1, "set_up": true,
   "served": true, "why_not": null,
   "project": "example-app",
   "lines": ["brunna · 2 need you"],
   "owner": {"needs_you": 2, "side": "builtin", "health": "ok"},
   "mail": {"held": [{"id": "m1118…", "from": "other-app", "type": "request", "since": "…"}],
            "waiting": 1},
   "commands": [{"name": "inbox", "summary": "Read and handle this project's letters",
                 "prompt": "Run `brunna inbox` and handle the letters as your rules say."},
                {"name": "brunna", "summary": "What needs you"}]}
  ```

  - **Whose session.** The command qualifies the native id itself (`runtime_ports.qualify`) and decides everything from
    the session's own record (`runtime_ports.record_session`, written by the hooks) and the registry, never from the
    environment the command runs in. A session with no record is not served. The platform's own runs and headless
    sessions record none (`platform_run`, the adapter's `person_session`). The per-version gate is read from the
    version on the record.
  - **`served: false`**: `why_not` is one of `not_a_project`, `not_recorded`, `gated`, `not_served` (a Codex session in
    a sandboxed project) or `no_bus_access`. Except for `gated`, the answer then carries nothing else.
  - **`mail`**, from `hubbus.letter_state`, read-only. `held` lists letters told to this session that are still open,
    with this session as holder. `waiting` counts the letters `hook_shows` would tell this session next. Ids, senders
    and types only; a letter's text is read through `/inbox`, by the agent.
  - **`owner`** folds in what the band needs from glance (the count, the owner side and the health state), so the
    minute's poll is this one command.
  - **`lines`**: the display text, written by the platform in the interface language. Adapters draw these lines and
    glance's `prompt` texts, and never compose wording of their own.
- **Offer**: `commands` in the same output. The platform fixes the list; an adapter registers only the names it knows
  (`inbox`, `brunna`) and ignores any other. A command's `prompt` is filled into the person's prompt box for them to
  send. The adapter never runs a platform command itself. The agent runs it, inside its own sandbox, as the project,
  exactly as when the person types the same request.

What the command does, so the mod needs nothing more:

- It reads the bus read-only (as `glance.mail()` does: no schema run, no presence, no delivery) and decides the project
  by path (`hubbus.project_of`). It follows the rule `hub-event` keeps for handlers in `NOT_IN_FOLDER`: no git and no
  script of the folder. The only files it reads in the folder are the `.git` pointer files, by path, as `project_of`
  does. The gate, the record and whether the project is sandboxed are decided here, never in an adapter.
- No lock, exit 0 whenever it could answer, problems in the answer (the glance rule).

Later (slice 2, if the owner wants the reminders shown too): `hub-event` writes, after its handlers, one small file per
session in the platform's state holding the notices it produced (A6, A7, A8, A12's notice, W3, W9), and `brunna surface`
adds them as `notices: [{kind, level, text}]`. The hooks still put the same text into the model's context; the file only
lets the owner see it. This needs no git in the folder, because the hook already ran it, as the project where it must.

### Rules every surface adapter keeps

1. **Optional.** With it uninstalled, disabled or broken, nothing the platform guarantees changes. No C or G item moves
   to it, and doctor lists it as reinforcement, never as a requirement.
2. **Nothing into the model's context.** `mail_text` records presence, tells and holds. A second transport for letters
   would deliver twice unless the platform decided which one wins, so the mod adds no `prompt.submit` context, rewrites
   no tool result and registers no model-callable tool (`$.tool.register`) or agent type. Its `command.run` hooks return
   no text: a plugin's command output reaches the model too. Its commands and presses only fill the prompt box
   (`$.prompt.fill`), never submit.
3. **One read-only command, run neutrally.** It runs only `brunna surface --json` and `brunna glance --json` (argv, never
   a shell), found at the install's own place first (`~/.local/bin/brunna`, `install-cli`'s default, as the herdr mod's
   fallback) and only then on `PATH`. Its `cwd` is a neutral folder (the user's home), never the project's. The session's
   folder goes in as `--cwd`. The platform's root and Python variables (`AGENT_HUB_*`, `BRUNNA_*`) are passed blank. It
   reads no file in the folder itself.
4. **Shown only where brunna serves the session.** The mod draws only where the session draws somewhere
   (`$.session.surfaces()` not empty: a `-p` run draws nowhere) and the surface answers. It doesn't gate on
   `isInteractive`: the types give false for an SDK host, and the desktop app runs its sessions as one while still
   drawing (measure).
5. **Data is data.** Letter senders, to-do and question texts are drawn as plain text, never as Markdown with links to
   press, and never become a command or a prompt by themselves. A fill carries only the item's kind, project and id and
   the platform's own words (glance's `prompt`), never the item's text.
6. **Approvals stay the owner's own.** A button never confirms anything. For an item only the owner's terminal can
   settle (`brunna skills sync`, `brunna push`, `brunna release`), the pane shows glance's `command` and copies it, and
   shows the text only where the field is absent. `owner_terminal.refusal()` refuses those inside an agent's session
   anyway, and a press inside a session is not a typed yes (the 11-16 rule).

### Adapter: a Claude Code mod in `integrations/claude-code/`

The layout follows `integrations/herdr/`: one folder per app, laid out as the app requires, reading only the platform's
neutral output and running only `brunna`.

```
integrations/claude-code/
  .claude-plugin/plugin.json      name "brunna", version = the brunna release, engines["claude-code"]
  hooks/hooks.json                {"modules": ["./brunna.ts"]}
  hooks/brunna.ts                 register(on): the hooks below
.claude-plugin/marketplace.json   at the repo root: {"name": "brunna", "owner": {...},
                                  "plugins": [{"name": "brunna", "source": "./integrations/claude-code"}]}
```

The marketplace file sits at the root of the hub's own checkout. The owner installs from that folder, so the mod is the
release the platform runs (see "Install, consent, upgrade, versions"). A `source` pointing into a subfolder is not shown
in the reference, which shows only `"./"`. Check it with `claude plugin validate` while building; if it isn't accepted,
the marketplace file goes into `integrations/claude-code/` with `"./"` and the owner installs from that folder.

What it does in slice 1:

- `session.start` (where rule 4 holds): one `brunna surface --json`, then the same every 60 seconds (`$.clock.every`),
  kept in `$.state` so drawings update without a module variable. It registers its two commands (`$.command.register`).
- **The band** (`ui.render` on `AbovePrompt`): one row, only while something needs the owner. It shows the surface's
  `lines`: the needs-you count and any health problem, with a `[needs you]` button that opens the pane. Letters appear
  in the band only when the platform can't deliver them here (`served: false` with `gated`: "letters for this project
  wait for another session") or when a letter has been held by this session longer than a threshold. Otherwise the hooks
  already put them before the agent.
- **Toasts** (`$.ui.toast`): once per new `needs_you` item, and once per held letter past the threshold. Ids already
  shown are kept in `$.store`. Two sessions can write that list at once, so a toast may repeat or be missed now and then;
  that is acceptable for a display.
- **The pane "needs you"** (`$.ui.open`, a `Pane` hook). It runs `brunna glance --json` only when the pane opens or is
  refreshed, and lists glance's `needs_you` and health, newest first.
  - A press on an item of the session's own project fills the prompt (`$.prompt.fill`) with the item's kind, project and
    id and glance's `prompt`, which the owner edits and sends. The owner talks to the agent, and the agent does the work.
  - Items of other projects are read-only here.
  - The whole pane is read-only when the owner side is the assistant (`owner.side`): settling belongs to that side.
  - An item with a `command` shows it and copies it (`$.ui.copy`).
- **Commands**: `/inbox` fills the prompt box with the surface's `inbox` prompt, and the person sends it. `/brunna`
  opens the pane. The exact names Claude Code gives a plugin's commands are checked while building.

When a hook throws or a tree doesn't validate, Claude Code skips that hook alone and draws its own (the reference,
"Drawing"). The band or pane is missing and nothing else changes, which rule 1 already assumes.

This is the 11-16 owner-surface principle in practice: the owner "glances at the state" inside the session they already
have open, answers through the agent, and runs no new command. It doesn't fix the leaks that principle lists. Pushes and
releases of a sandboxed project still need the owner's terminal. The pane only makes them visible, with the command to
type.

### Adapter: Codex

What the repo has measured (`runtime-codex.md`, `runtime_codex.py`, `conformance_codex.py`):

- hooks: user-level `hooks.json` with a trust hash per handler, five events (SessionStart, UserPromptSubmit,
  PostToolUse, PreCompact, SessionEnd); context reaches the model only at SessionStart, and is developer-role, so
  letters are a notice of ids;
- no wake (D4 until M11); `codex queue --thread` exists (marked stable) and is not used;
- the MCP server works, and the rules point a Codex session at it where its sandbox can't write the mailbox;
- `.agents/skills/<name>/SKILL.md` loads as a skill (measured 11-14);
- `features.plugins` exists, and with it on, a run in a fresh home cloned a plugin marketplace's git repository
  (measured); what a Codex plugin can carry is not measured.

Unmeasured, so treated as missing: panes, a band or status line a plugin can draw, toasts, slash commands a plugin
registers, tools a plugin registers, and any hook that could display rather than inject. Until measured, Codex's surface
adapter is:

- **Show**: nothing inside Codex. The owner uses `brunna glance`, the status page, or a terminal client such as the
  herdr mod.
- **Offer**: the surface's `inbox` command as one user-level skill (`~/.agents/skills/brunna/`), generated from the
  platform's text. That is a user-level change, so it waits for the owner's `brunna user-settings --install --runtime
  codex`, the same yes that installs the hooks. Measure first that a user-level skill is offered as a command and not
  only read as instructions.

When Codex grows a display API, its adapter reads the same two JSON outputs. Nothing on the platform side changes.

### Adapter: none

Every agent without an adapter, Claude Code without the mod included, keeps what it has today: the rules text (R1–R3)
tells the agent to run `brunna inbox`, to send and close letters, and to write notes; the hooks (where the runtime has
them) deliver letters and reminders; the MCP server serves agents with only MCP. The owner reads `brunna glance`,
`brunna owner list`, the status page and the platform's notifications. Nothing in this design is needed for any of it.

### What moves, item by item

| Rows | Decision |
|---|---|
| All G (A12, A15, A17, H1, H2, B1, W8, M2, M3, M4, P1, P2, P3, L2, O3) | **Stay.** A mod is optional, can be skipped by another plugin above it, and its API is early access (the reference: "moves between releases"). A15 could be doubled by a `tool.call` hook, but the settings hook stays the one the platform renders |
| A1–A5, A9–A11, A13, A14, R1–R3, X1, M1, L1, O1, O2 | **Stay.** The mod adds display only |
| A6, A7, A8, W3, W9 (U) | **Stay as hooks**, since they are written for the model. Slice 2 may also show them in the band (the per-session notices file) |
| A16, W7 (the waiter) | **Later candidate.** The mod could hold the wait as a timer and wake the idle session with `$.prompt.submit`, which the reference says starts a turn once the session is idle. It would be the one place this design submits rather than fills, so it needs the owner's own decision. Safe to move because dispatch already counts a waiter only while its heartbeat is fresh (`WAITER_FRESH_S`), and each wake request is taken once (`_take` renames it first, atomically). With the mod gone, the session is simply not a wake candidate, and dispatch forks or triages as for any session without a waiter. Gain: no 14-day background hook process per session. Needs: the mod writing the same heartbeat through a `brunna` subcommand, a measurement that a timer's `$.prompt.submit` wakes an idle session in the terminal and the desktop app, and the hook removed only after that |
| W1, W2, W4–W6, W9 (worktree hooks, projects that aren't sandboxed) | **Later candidate.** The same hooks shipped by the plugin instead of written into `~/.claude/settings.json` would end the settings surgery (`merge_worktree_hooks`, `user_refresh`, `user_cleanup`). They are C, but they are already an owner opt-in that fails open (`|| true`). Doctor says when they are behind, not when they are missing (`runtime_claude` has no `user_missing`). Conditions: S1's fix has landed, so no sandboxed worktree depends on these hooks for its settings; the sandboxed branch (W8 and the run-as path) never moves to the mod. Open: whether a plugin of function hooks can also carry command hooks, and where its commands find the hub (a `userConfig` field is per user and not committed) |

### First slice

1. `brunna surface --json` and `contracts/session-surface.v1.schema.json` (example validated by `tests/test_contracts.py`),
   under `help all`; glance's optional `needs_you[].prompt` and `needs_you[].command`, written where the platform raises
   the item.
2. The mod in `integrations/claude-code/` and the marketplace file at the repo root, with what "Adapter: a Claude Code
   mod" lists: band, toasts, the "needs you" pane, `/inbox` and `/brunna`. Not slice 1: no context injection, no
   registered tools, no native notifications.
3. `tests/test_mods.py` grows a Claude Code class, as the herdr one:
   - the layout and the marketplace entry;
   - the module runs no program but `brunna`, registers no tool or agent, registers only the allowed command names, and
     its `command.run` hooks return no text;
   - `claude plugin validate` when `claude` is on PATH (skipped otherwise).

   `tests/test_runtime_boundary.py` needs no change: `integrations/` is an adapter, not a neutral file.
4. A doctor row through the Claude adapter (`runtime_claude`): the mod installed and at which version, or "not installed
   (optional)" with the install line. Reading the install state is the adapter's business; nothing neutral names Claude
   Code's plugin folder.
5. One install paragraph in `docs/` (the README stays lean), and one optional line at the end of `brunna init`.

No hook is removed in slice 1, and `hub-event` doesn't change. Recorded, not in this plan: S1, S6 and S7, which are
being fixed in code separately (see the top).

### Install, consent, upgrade, versions

- **Install is the owner's own act.** A user-level install gets nothing without the owner's direct yes in that session
  (AGENTS.md).
  - The owner adds the hub's own checkout as a folder marketplace and installs from it at user scope:
    `/plugin install brunna --marketplace <the hub's folder>`, or `claude plugin install …` from a shell. Doctor's row
    and `brunna init` print the exact line, with this machine's folder filled in.
  - An agent may offer the line; it never runs it. brunna never writes `enabledPlugins` or a marketplace into any
    settings file.
  - This is the one line the owner types, once. It is the tool's own command, not a new brunna command, so the 11-16
    rule holds.
- **Why the hub's checkout.** The mod is always the release the platform runs, so the mod and the JSON it reads can't
  drift apart. No network is needed, and a fork's own mod comes with the fork. After `brunna upgrade` moves the
  checkout, a session picks up the new mod with `/reload-plugins` (or its next start). Measure whether an install from
  a folder marketplace reads the folder or a copy. If it is a copy, the Claude adapter's `user_refresh`, which upgrade
  already runs, runs the tool's own plugin update where the mod is installed and adds it nowhere. A GitHub marketplace
  (`--marketplace <owner>/brunna`) also works, but follows its own update cadence, not the release the user runs.
- **Versions.** The mod's `plugin.json` version is the brunna release it ships in. The mod also checks the contract
  versions it knows (`v` of glance and session-surface). On an unknown major it draws one line, "brunna and its Claude
  Code mod don't match", and nothing else. The platform keeps the old major beside a new one until readers have moved
  (the `contracts/` rule), and adding a field keeps the version.

### Sandboxed projects, and whether a project can influence the mod

A user-scope mod loads in every session the owner opens, sandboxed projects included. Its module has no Node and no
DOM; whatever it runs goes through `$.process.run`, as the user. Whether the session's sandbox applies to those children
is **unmeasured**. The design doesn't depend on it: the one command the mod runs writes nothing, runs from a neutral
folder, and reads only the folder's `.git` pointers (rule 3). A sandboxed project's letters are read from the bus, as
the owner, never from the project's view file under its `.git`. If the sandbox does apply to the command, the read of
the bus is denied there, and the surface answers `served: false` (`why_not: no_bus_access`) rather than fall back to the
view.

Can a project influence the user-level mod? It must not. What was found:

1. **Plugins a project can bring.** A plugin auto-loads from the project's `.claude/skills/<name>` folder (the
   reference). A sandboxed project's own processes can't write `.claude/skills`, a control path in
   `runtime_claude.CONTROL`. `brunna skills sync` installs reviewed skills there, and since `8b9c5ab6` it refuses a
   plugin's manifest and `hooks.json` inside a skill (`SKILL_REFUSED`, O3). Such a plugin is not only the session's
   concern: per the reference (lines 120–121), a skills-folder plugin is also watched in a headless session, `claude -p`
   included. The adapter's own note (`runtime_claude._env`) records a user plugin's hook firing in a platform run. **S6,
   being fixed**: a conformance canary checks that no plugin runs in a platform run (P1), and if one does, the run turns
   hooks modules off (the reference names `--safe-mode`; measure which applies to `-p`). For an unsandboxed project
   this is no new boundary: it runs as the user already.
2. **Project settings and committed controls.** A project's `.claude/settings.json` can name `enabledPlugins`,
   `extraKnownMarketplaces` and `env`. For a sandboxed project the file is a control path its processes can't write.
   But the foreign-control check (`foreign_controls`) didn't report those keys, nor a plugin manifest or `hooks.json`
   that arrives committed. **S7, being fixed**: it reports all of them.
3. **Other plugins in the same chain** can hook `process.run` (rewrite or deny the mod's call), `state.set` on the mod's
   keys, `ui.press` with `{plugin: '*'}`, and `AbovePromptList` (hide the band). At worst they hide or falsify the
   display, or fill a prompt the person sees before sending. Rules 2, 5 and 6 keep it at that. A plugin that can load
   at all already runs as the user, which is item 1's problem, not the mod's.
4. **Environment.** `$.process.run` children get "the host process's own environment" plus what the call sets. Whether
   a project's settings `env` (a `PATH`) reaches the host process is unmeasured. Hence rule 3: a fixed place first, a
   neutral folder, and the platform's own variables blanked.

### Open questions for the owner

1. **Build slice 1?** The session surface command and contract, glance's two optional fields, and a display-only Claude
   Code mod (band, toasts, "needs you" pane, `/inbox`, `/brunna`), with no hook removed. Recommended: yes.
2. **Approvals in the pane: show only, or act?** Recommended: show only, with the platform's command to copy. A press
   inside an agent's session isn't your typed yes, and `skills sync` and `release` refuse there anyway.
3. **Where is the mod installed from?** Recommended: from the hub's own checkout, as a folder marketplace. The mod is
   then always the release you run, `brunna upgrade` needs only a plugin reload, and it needs no network. The
   alternative, a GitHub marketplace, updates on its own schedule.
4. **The waiter and the worktree hooks into the mod later?** Recommended: decide after slice 1 has run for a while and
   S1's fix has landed. The waiter would be the mod's one prompt submit. A sandboxed project's worktree branch stays
   with the platform either way.

### To measure or check while building

- A marketplace `source` pointing into a subfolder (`claude plugin validate`).
- Whether an install from a folder marketplace reads the folder live or a copy, and so whether upgrade needs
  `/reload-plugins` only or the tool's plugin update.
- What `session.start`'s `isInteractive` and `$.session.surfaces()` answer in the desktop app's local sessions (rule 4).
- The names Claude Code gives a plugin's slash commands.
- Whether the session's sandbox applies to `$.process.run` children.
- Whether a project's settings `env` reaches them, and whether `init.env` can blank `AGENT_HUB_*`/`BRUNNA_*`, or the
  values must be set to the user's own (rule 3).
- How long a held letter waits before the band shows it (the threshold), against the hooks' own telling.
- For the later waiter: a timer's `$.prompt.submit` waking an idle session, in the terminal and the desktop app.
- For Codex: what a Codex plugin can carry, and whether a user-level skill is offered as a command.

## The owner's decisions (11-18)

The owner took all four recommendations:
1. Build slice 1: `brunna surface` and its contract, glance's two optional fields, and the display-only Claude Code mod
   (band, the "needs you" pane, `/inbox`, `/brunna`). No hook is removed.
2. The pane shows approvals and the platform's command to copy; it acts on nothing.
3. The mod is installed from the hub's own checkout, as a folder marketplace.
4. Moving the idle-session waiter and the worktree hooks into the mod is decided after slice 1 has run a while and S1's
   fix has landed; a sandboxed project's worktree handling stays with the platform either way.
Installing the mod is a user-level change: done only on the owner's own yes in a session, never by an agent alone.
