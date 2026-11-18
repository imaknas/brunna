# The agent surface: what brunna shows and offers inside a coding agent

Status: design 11-18, not built. It answers the owner's question of 11-18: could brunna's Claude Code-specific hooks and
features become a Claude Code mod (a plugin of function hooks, installed at user scope from a marketplace file), and which
platform features should be mods? The owner's constraint: whatever goes into the mod is first extracted behind a neutral
interface, so Codex or any other agent can plug in the same way. The inventory below is measured from the code at
`d8302ffc`; the plugin facts come from the plugin-authoring reference and type declarations shipped with Claude Code
2.1.325 (the skill's `SKILL.md` was not at the path given; `reference.md`, the types and the examples were).

## The short answer

- **A mod, yes, for what the owner sees and the commands the owner is offered. Not for what the hooks do today.** Of
  the 40 integration points counted below, 4 are reminders or notices (U), 25 keep a platform flow correct (C) and 11
  are guarantees (G). Every U item is text written for the session's model, not for the owner, and none of the C or G
  items may depend on an optional plugin the owner can remove. So the first mod moves no hook. What it adds is new: the
  owner's open items, the session's letters and the platform's health, drawn in the session the owner already has open,
  plus a few commands.
- **The neutral layer comes first.** Lifecycle events already have a neutral interface (`bin/hub-event`,
  `runtime_ports.EVENTS`, `HubEvent`, each adapter's `parse_event`/`inject`/`deny`). This design adds a second, smaller
  layer for display and commands. It is data out and commands offered: the existing `brunna glance --json` (glance.v1)
  for the owner's view across projects, and a new session-scoped `brunna surface --json`. The Claude Code mod is one
  adapter of that layer, like the herdr mod is of glance. Codex's adapter is what Codex is measured to support today,
  which for display is nothing. With no adapter at all, everything still works through the rules text, the CLI and the MCP server.
- **Two hooks could move later**, each only once the platform can check that the mod took part and falls back when it
  didn't: the idle-session waiter (the `Stop` hook) and the user-level worktree hooks. Neither moves in the first slice.

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

### B. Settings the platform renders (not hooks)

| # | Where | What | Class |
|---|---|---|---|
| B1 | a sandboxed project's `.claude/settings.local.json`, by `render-project-settings.py` (`policy.py` → `runtime_claude.session_settings`) | `sandbox` (failIfUnavailable, no unsandboxed commands, allowWrite, denyRead, network), the deny/allow rules, `env` (`NO_FOLDER_GIT`, tool caches); drift written back and reported. Absolute paths by nature | G |

### C. Rules text

| # | Where | What | Class |
|---|---|---|---|
| R1 | the managed block in `CLAUDE.local.md` (`hub-runtime instructions-files`, `prompts.agent_rules`) | the platform's rules for every session: names only the `brunna` command, no paths | C |
| R2 | Codex: SessionStart context (`rules` handler, `RULES_AT_START`, `rules_note()`) | the same text, since Codex reads no untracked rules file | C |
| R3 | the MCP server's `initialize` instructions (`templates/mcp-instructions.md`) | the rules and the open-letter count for an agent that has only MCP | C |

### D. Claude Code worktree hooks, user level

In `~/.claude/settings.json`, installed only by the owner (`merge-hook-settings.py --worktree`), kept current by
`brunna upgrade` → `brunna user-settings --update` → `runtime_claude.user_refresh`, removed by uninstall
(`user_cleanup`). Every command is `[ -x <hub>/bin/worktree-memory.sh ] && <hub>/bin/worktree-memory.sh <what> || true`:
**Path, in user settings**. In a main checkout the script does nothing; in a linked worktree it calls `hub-event
worktree <what>` first (outside any sandbox), then the rest.

| # | Native event | What | Class |
|---|---|---|---|
| W1 | SessionStart (`start`) | `hub-event worktree start`: record, mail, reminder in one output | C |
| W2 | SessionStart (`start`) | links `.agent-memory` and the rules file, writes the worktree's own settings (`hub-runtime worktree-settings`: the memory folder as a working directory, Edit denied on `AGENT.md`/`RULES`/`.git` there), pulls memory, prints the rules file the first time | C |
| W3 | SessionStart (`start`) | `memory-nudge.py --session-start`; "the nightly cleanup removed dependencies here" | U |
| W4 | SessionEnd, PreCompact (`end`) | memory sync (`project_git.py sync-memory` when sandboxed); at SessionEnd the session's letters freed | C |
| W5 | PostToolUse(Bash) (`nudge`) | mail and the reminder | C |
| W6 | UserPromptSubmit (`prompt`) | mail | C |
| W7 | Stop (`wait`, in the background) | the waiter, as A16 | C |
| W8 | SessionStart, a sandboxed project's worktree | `render-project-settings.py --worktree` (again: a worktree the platform made has its settings from the make, `session_worktree.make`, 11-18), then the rest run as the project (`project_git.py run-as`) | G |

### E. Codex

| # | Where | What | Class |
|---|---|---|---|
| X1 | `$CODEX_HOME/hooks.json` plus `config.toml` `[hooks.state."<real path of hooks.json>:<event>:<group>:<handler>"] trusted_hash` (`runtime_codex.merge_hooks`, `trust_entries`), installed only by the owner's `brunna user-settings --install --runtime codex`, refreshed by upgrade, removed by uninstall | SessionStart, UserPromptSubmit, PostToolUse(Bash), PreCompact, SessionEnd (3 s budget, memory sync detached) → `<hub>/bin/py <hub>/bin/hub-event --runtime codex <event>`. **Path**: the commands, the trust keys (the file's real path) and the hashes (over the commands) | C |

Its handlers are A1–A14 (no guard, no waiter), with three declared differences: a letter reaches the model only at
SessionStart (`TELLS_ON`), as a notice of ids, never a body (`LETTERS_AS_NOTICE`), and no Codex session is woken (D4,
until M11). Sessions in sandboxed projects aren't served (`_served_folder`; Step 4). These rows are not counted twice.

### F. MCP servers, platform runs, legacy entry points, skills

| # | Where | What | Class |
|---|---|---|---|
| M1 | `brunna mcp` (`bin/mcp_server.py`), registered by the owner from the snippet `brunna mcp config` prints (an absolute command: **Path**, in the tool's settings) | the mailbox and notes for any MCP agent; presence as `mcp:<pid>` | C |
| M2 | `mcp_server.py --run <file>`, attached by `llm.py` to a confined run | the run's own mail tools, held to the confined checks | G |
| M3 | `bin/lease-relay.py` (lease sessions' `--permission-prompt-tool`) | every permission question goes to the owner, the rest refused | G |
| P1 | Claude Code platform runs (`runtime_claude._flags`, `platform_settings`) | `-p --restricted`, the platform's settings file only, `--strict-mcp-config`, named tools, `NO_FOLDER_GIT` | G |
| P2 | Codex platform runs (`runtime_codex.run_home`, `HOME_CONFIG`) | a run home of its own (`features` plugins, apps, hooks, multi-agent off; a permission profile). The adapter still declares `PLATFORM_RUNS_FIRE_HOOKS` (the user's hooks can't be skipped, M5 on 0.165), so `hub-event` does nothing under `HUB_LLM_POINT` | G |
| P3 | lease sessions (`llm.py exec lease-session`, `runtime_claude.lease_settings`) | read-only tools, the deny list plus every credential folder, every platform command asks the owner through the relay (M3) | G |
| L1 | `hubbus hook <event>` (`cmd_hook`) | the entry point hooks installed before `hub-event` still call | C |
| L2 | `guard.py pre-tool` | the old guard entry point | G |
| O1 | `brunna skills sync` → each adapter's `SKILL_DIRS` (`.claude/skills`, `.agents/skills`) | skills the owner reviewed, installed where the agent loads them | C |
| O2 | `brunna user-settings --update` (run by upgrade) | the owner's earlier user-level installs (D, X1) kept at this version | C |

Out of scope: the git hooks the platform writes (`pre-commit`, `commit-msg`, `pre-push`: runtime-neutral) and the
session readers (`sessions_claude.py`, `sessions_codex.py`: they only read).

### Counts

| Class | Count | Rows |
|---|---|---|
| G | 11 | A12, A15, A17, B1, W8, M2, M3, P1, P2, P3, L2 |
| C | 25 | A1–A5, A9–A11, A13, A14, A16, R1–R3, W1, W2, W4–W7, X1, M1, L1, O1, O2 |
| U | 4 | A6, A7, A8, W3 |

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
  already arrive here as owner to-dos, a release waiting is not checked), open mail per
  project, health, the scheduler.
- **Show, session scope (new)**: `brunna surface --json --session <id> --cwd <folder>` (listed under `help all`, never in
  `brunna --help`: `tests/test_owner_surface.py`), contract `contracts/session-surface.v1.schema.json`, in the
  platform's `provides` with a validated example. Draft:

  ```json
  {"v": 1, "set_up": true,
   "served": true, "why_not": null,
   "project": "example-app",
   "mail": {"open": 3, "for_this_session": [{"id": "m1118…", "from": "other-app", "type": "request", "since": "…"}],
            "held_elsewhere": 1},
   "owner": {"needs_you": 2},
   "commands": [{"name": "inbox", "summary": "Read and handle this project's letters",
                 "prompt": "Run `brunna inbox` and handle the letters as your rules say."}]}
  ```

  `served: false` with `why_not` (`not_a_project`, `platform_run`, `headless`, `gated`, `not_served`, `no_bus_access`)
  and nothing else when the
  session isn't one the platform serves: a folder that isn't a project, a run the platform started (`HUB_LLM_POINT`,
  `HUBBUS_LEASE`), a session the per-version gate holds back, or a Codex session in a sandboxed project. Ids, senders and
  types only; a letter's text is read through `/inbox`, by the agent.
- **Offer**: `commands` in the same output, each a name, a summary and a `prompt`: the words the adapter hands to the
  session's agent as the person's own prompt. The adapter never runs a platform command itself. The agent runs it,
  inside its own sandbox, as the project, exactly as when the person types the same request.

What the command does, so the mod needs nothing more:

- It reads the bus read-only (as `glance.mail()` does: no schema run, no presence, no delivery) and decides the project
  by path (`hubbus.project_of`), with the same rule `hub-event` keeps for handlers in `NOT_IN_FOLDER`: no git and no script
  of the folder, nothing read from the folder's files. The gate, `platform_run` and whether the project is sandboxed
  are decided here, never in an adapter.
- For "letters for this session" it uses the judgment the hooks use (`inbox(c, project, session)` and `hook_shows`),
  without writing.
- No lock, exit 0 whenever it could answer, problems in the answer (the glance rule).

Later (slice 2, if the owner wants the reminders shown too): `hub-event` writes, after its handlers, one small file per
session in the platform's state holding the notices it produced (A6, A7, A8, A12's notice, W3), and `brunna surface`
adds them as `notices: [{kind, level, text}]`. The hooks still put the same text into the model's context; the file only
lets the owner see it. This needs no git in the folder, because the hook already ran it, as the project where it must.

### Rules every surface adapter keeps

1. **Optional.** With it uninstalled, disabled or broken, nothing the platform guarantees changes. No C or G item moves
   to it, and doctor lists it as reinforcement, never as a requirement.
2. **Nothing into the model's context, slice 1.** `mail_text` records presence, tells and holds. A second transport for
   letters would deliver twice unless the platform decided which one wins, so the mod adds no `prompt.submit` context,
   rewrites no tool result and registers no model-callable tool (`$.tool.register`) or agent type. Its commands submit a
   prompt the person sees.
3. **One read-only command.** It runs only `brunna surface --json` and `brunna glance --json` (argv, never a shell),
   found at the install's own place first (`~/.local/bin/brunna`, `install-cli`'s default, as the herdr mod's fallback)
   and only then on `PATH`. It passes the session id and folder and reads no file in the folder itself.
4. **Served sessions only, decided by brunna.** The mod draws only where the session draws somewhere
   (`$.session.surfaces()` not empty: a `-p` run draws nowhere) and `brunna surface` answers `served: true`. It does not
   gate on `isInteractive`: the types give false for an SDK host, and the desktop app runs its sessions as one while
   still drawing (measure). Whether a session is a platform run (`HUB_LLM_POINT`, `HUBBUS_LEASE`, inherited by the
   command) or a headless one (the Claude adapter's `person_session`, `HEADLESS_ENTRYPOINTS`) is the command's call.
5. **Data is data.** Letter senders, to-do and question texts are drawn as plain text, never as Markdown with links to
   press, and never become a command or a prompt by themselves.
6. **Approvals stay the owner's own.** A button never confirms anything. For an item that needs the owner at a
   terminal (`brunna skills sync`, `brunna push`, `brunna release`), slice 1 shows the item's text only: glance's
   `needs_you` items carry no command, and rule 5 forbids taking one from the text. A later optional field
   (`needs_you[].command`, an argv the item's writer sets; adding a field keeps glance at v1) would let the pane show
   and copy it.
   `owner_terminal.refusal()` refuses those inside an agent's session anyway, and a press inside a session is not a
   typed yes (the 11-16 rule).

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

The marketplace file sits at the repo root, because `/plugin install brunna --marketplace <owner>/brunna` reads it from
the repository's root. A `source` pointing into a subfolder is not shown in the reference (it shows only `"./"`): check
it with `claude plugin validate` while building. If it isn't accepted, the mod moves to its own small repository.

What it does in slice 1:

- `session.start` (where rule 4 holds): one `brunna surface --json` and one `brunna glance --json`, then the same every
  60 seconds (`$.clock.every`), kept in `$.state` so drawings update without a module variable. It registers the
  commands the surface offers (`$.command.register`).
- **The band** (`ui.render` on `AbovePrompt`): one row, only while something needs attention: "brunna · 2 letters for
  this session · 3 need you", with an `[inbox]` button (the inbox command) and a `[needs you]` button (opens the pane).
  When `served` is false, no row.
- **Toasts** (`$.ui.toast`): once per letter id newly for this session, and once per new `needs_you` item; ids seen are
  kept in `$.store`, so a restart doesn't repeat them. No native notification (`$.ui.notify`) in slice 1: the owner's
  phone and desktop already get the platform's own (alerts, the owner side).
- **The pane "needs you"** (`$.ui.open`, a `Pane` hook): glance's `needs_you` and health, newest first. A to-do or
  question row's press fills the prompt (`$.prompt.fill`, not submit) with a draft naming the item, which the owner edits
  and sends: the owner talks to the agent, and the agent does the work. A row that needs the owner's terminal shows its
  text (with the later `command` field: the command, and copies it with `$.ui.copy`).
- **Commands**: `/inbox` (fills the prompt box with the surface's `inbox` prompt, `$.prompt.fill`; the person sends it)
  and `/brunna` (opens the pane). The exact names Claude Code gives a plugin's commands are checked while building.

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
- **Offer**: the surface's `commands` as one user-level skill (`~/.agents/skills/brunna/`), generated from the same
  list. That is a user-level change, so it waits for the owner's `brunna user-settings --install --runtime codex`, the
  same yes that installs the hooks. Measure first that a user-level skill is offered as a command and not only read as
  instructions.

When Codex grows a display API, its adapter reads the same two JSON outputs. Nothing on the platform side changes.

### Adapter: none

Every agent without an adapter, Claude Code without the mod included, keeps what it has today: the rules text (R1–R3)
tells the agent to run `brunna inbox`, to send and close letters, and to write notes; the hooks (where the runtime has
them) deliver letters and reminders; the MCP server serves agents with only MCP. The owner reads `brunna glance`,
`brunna owner list`, the status page and the platform's notifications. Nothing in this design is needed for any of it.

### What moves, item by item

| Rows | Decision |
|---|---|
| All G (A12, A15, A17, B1, W8, M2, M3, P1, P2, P3, L2) | **Stay.** A mod is optional, can be skipped by another plugin above it, and its API is early access (the reference: "moves between releases"). A15 could be doubled by a `tool.call` hook, but the settings hook stays the one the platform renders |
| A1–A5, A9–A11, A13, A14, R1–R3, X1, M1, L1, O1, O2 | **Stay.** The mod adds display only (the letters' count, from the surface) |
| A6, A7, A8, W3 (U) | **Stay as hooks**, since they are written for the model. Slice 2 may also show them in the band (the per-session notices file) |
| A16, W7 (the waiter) | **Later candidate.** The mod could hold the wait as a timer and wake the idle session with `$.prompt.submit`, which the reference says starts a turn once the session is idle. It is safe to move because dispatch already counts a waiter only while its heartbeat is fresh (`WAITER_FRESH_S`) and each wake request is taken once (`_take` renames it first, atomically): with the mod gone, the session is simply not a wake candidate and dispatch forks or triages, as for any session without a waiter. Gain: no 14-day background hook process per session. Needs: the mod writing the same heartbeat through a `brunna` subcommand, a measurement that a timer's `$.prompt.submit` wakes an idle session in the terminal and the desktop app, and the hook removed only after that |
| W1, W2, W4–W6 (worktree hooks) | **Later candidate.** The same hooks shipped by the plugin instead of written into `~/.claude/settings.json` would end the settings surgery (`merge_worktree_hooks`, `user_refresh`, `user_cleanup`). They are C, but they are already an owner opt-in that fails open (`|| true`), and doctor already says when they are behind (not when they are missing: `runtime_claude` has no `user_missing`). Open: whether a plugin of function hooks can also carry command hooks, and where its commands find the hub without a path in the public plugin (a `userConfig` field is per user and not committed) |

### First slice

1. `brunna surface --json` and `contracts/session-surface.v1.schema.json` (example validated by `tests/test_contracts.py`),
   under `help all`.
2. The mod in `integrations/claude-code/` and the marketplace file at the repo root, with what "Adapter: a Claude Code
   mod" lists: band, toasts, the "needs you" pane, `/inbox` and `/brunna`. Not slice 1: no context injection, no
   registered tools, no native notifications.
3. `tests/test_mods.py` grows a Claude Code class, as the herdr one: the layout, the marketplace entry, the module runs
   no program but `brunna` and registers no tool, and `claude plugin validate` when `claude` is on PATH (skipped
   otherwise). `tests/test_runtime_boundary.py` needs no change: `integrations/` is an adapter, not a neutral file.
4. A doctor row through the Claude adapter (`runtime_claude`): the mod installed and at which version, or "not installed
   (optional)". Reading the install state is the adapter's business; nothing neutral names Claude Code's plugin folder.
5. One install paragraph in `docs/` (the README stays lean), and one optional line at the end of `brunna init`.

No hook is removed in slice 1, and `hub-event` doesn't change.

### Install, consent, upgrade, versions

- **Install is the owner's own act.** A user-level install gets nothing without the owner's direct yes in that session
  (AGENTS.md). The owner types `/plugin install brunna --marketplace <owner>/brunna`, answers `y` to adding the
  marketplace and picks the user scope; in a shell, `claude plugin install brunna --marketplace <owner>/brunna`; in the
  desktop app, Plugins → Add marketplace. An agent may offer the line (doctor's row, the init message); it never runs it,
  and brunna never writes `enabledPlugins` or a marketplace into any settings file. This is the one line the owner
  types, once; it is the tool's own command, not a new brunna command, so the 11-16 rule holds.
- **New users** get the same single line in the install docs.
- **Upgrade.** `brunna upgrade` already keeps current what the owner put into the user's tool settings for the platform
  (the `user_refresh` exception). The Claude adapter's `user_refresh` would run the tool's own plugin update for
  `brunna` only where it is installed, and add it nowhere. Without that, Claude Code's own marketplace update decides
  when the mod changes, on a cadence unrelated to brunna's releases.
- **Versions.** The mod's `plugin.json` version is the brunna release it shipped with. The marketplace entry should name
  that release's tag, so a user on release N gets mod N. Whether an entry can pin a ref is checked while building; if it
  can't, the marketplace follows main and the next point carries the load. The mod reads the contract versions it knows
  (`v` of glance and session-surface). On an unknown major it draws one line, "brunna and its Claude Code mod don't
  match: brunna upgrade", and nothing else. The platform keeps the old major beside a new one until readers have moved
  (the `contracts/` rule), and adding a field keeps the version.

### Sandboxed projects, and whether a project can influence the mod

A user-scope mod loads in every session the owner opens, sandboxed projects included. Its module has no Node and no
DOM; whatever it runs goes through `$.process.run`, as the user. Whether the session's sandbox applies to those children
is **unmeasured**. The design doesn't depend on it, because the one command the mod runs writes nothing and reads
nothing from the folder (rule 3). A sandboxed project's letters are read from the bus, as the owner, never from the
project's view file under its `.git` (a file in the folder). If the sandbox does apply to the command, the read of the
bus is denied there and the surface answers `served: false` (`why_not: no_bus_access`) rather than fall back to the view.

Can a project influence the user-level mod? It must not. What was found:

1. **Plugins a project can bring.** The reference says a plugin is auto-loaded from the project's
   `.claude/skills/<name>` folder, and a plugin is a folder with a `.claude-plugin/plugin.json`. A sandboxed project's
   own processes can't write `.claude/skills` (a control path in `runtime_claude.CONTROL`). But `brunna skills sync`
   installs reviewed skills there, and its refusal of "a control name inside a skill" (`skills_sync._control_tops`)
   covers only the names in the adapters' control lists, and `.claude-plugin` is not one. A reviewed skill folder holding
   `.claude-plugin/plugin.json` and a hooks module would then load as a plugin that runs as the user. **Fixed 11-18**:
   skills sync refuses `.claude-plugin` and `hooks.json` at any depth (`runtime_claude.SKILL_REFUSED`), and the
   foreign-control check reports either under the skills folders, an installed skill not excused. Measured the same day
   on 2.1.327: a folder in the project's `.claude/skills` holding both loads, its hooks list and its hooks module alike,
   in a session of a trusted folder (`claude -p` included); which of the two names is enough wasn't separated, so both
   stay refused. For an unsandboxed project this is no new boundary: it runs as the user already.
2. **Project settings.** A project's `.claude/settings.json` can name `enabledPlugins` and `extraKnownMarketplaces`.
   For a sandboxed project the file is a control path its processes can't write, and the foreign-control check reports
   one that appears. Since 11-18 that report names `enabledPlugins`, `extraKnownMarketplaces` and `env` when set
   (`runtime_claude.COMMITTED_KEYS`), as it names hooks.
3. **Other plugins in the same chain** can hook `process.run` (rewrite or deny the mod's call), `state.set` on the mod's
   keys, `ui.press` with `{plugin: '*'}`, and `AbovePromptList` (hide the band). At worst they hide or falsify the
   display, or fill a prompt the person sees before sending. Rules 2, 5 and 6 keep it at that. A plugin that can load
   at all already runs as the user, which is item 1's problem, not the mod's.
4. **Environment.** `$.process.run` children get "the host process's own environment" plus what the call sets. Whether a
   project's settings `env` (a `PATH`) reaches the host process is unmeasured, hence rule 3's fixed place first.

**Platform runs.** Measured 11-18 on 2.1.327, and checked for every new version since by the conformance check
`platform-run-plugins` (`conformance_claude.py`): a canary plugin (a hooks list's command and a hooks module, each
writing a marker) in the project's `.claude/skills/<x>`, in the config dir's `skills/<x>`, and installed from a folder
marketplace at user scope loads in none of the platform's `--restricted` runs, of either kind (a completion on the
platform's config dir; a restricted agent point on the user's `~/.claude`, the world's home in the check). The control,
the same run without `--restricted`, loads all six (the project's only once its folder is trusted, which a person's
folders are). `--plugin-dir` loads under `--restricted` too; the platform never passes it. With `CLAUDE_CONFIG_DIR`
naming a folder, `~/.claude/skills` isn't read. So P1 holds without anything turned off; the mod does nothing there
either (rule 4).

**Closed (11-18): runs without `--restricted` don't start beside a plugin.** A lease's runs (`lease-session`,
`lease-work`, `lease-handover`: `--settings` and `--strict-mcp-config`, no `--restricted`) start in the project's
workspace on the user's `~/.claude`. One run of the lease's shape was measured (its flags by hand, a throwaway config
dir, plain `-p`): a skills-folder plugin of a trusted project ran its hooks list and its hooks module, as the user,
outside the project's sandbox. P3's guarantee (read-only tools) doesn't cover a plugin's hooks. No switch that turns
plugins off and keeps the project's rules file was found (from `claude --help` and the binary's environment names, not
run): `--restricted`, `--safe-mode` and `--bare` (which skips settings' and installed plugins' hooks) all drop CLAUDE.md
too, and no environment variable turns plugins off.

The owner decided (11-18): such a run doesn't start while a skills folder it would read holds a plugin. Built in the
Claude adapter (`runtime_claude.unrestricted_refusal` → `folder_refusal`): before `run()` and `exec_stream()` start the
tool, a run whose final argv lacks `--restricted` looks through the skills folders (`SKILL_DIRS`) of
`plugin_folders(cwd)` for `SKILL_REFUSED` (`.claude-plugin`, `hooks.json`) at any depth, in any case, a link of the name
counted and no link followed (S7's `plugin_markers`), and refuses on a find, and on a skills folder it can't look through
whole (a link to a folder, unreadable, over the entry cap): fail closed. The reason names the folder and the marker;
`llm.py` reports it on the point's failure path (`job.failed`, `llm_error`). The folders are the run's folder, its
repository's top (a run in a subfolder) and, in a linked worktree, the main checkout (read from the `.git` pointers as
files, `gitdirs`, no git run). Which of the last two Claude Code reads for a worktree or a subfolder, and whether it
reads skills folders of the folders above the repository or nested below the run's folder, is unmeasured: the first two
are looked at, the rest not (the folders above are the user's). `claude mcp list`, which an `agent-job` with connectors
runs in the job's folder before its run (no `--restricted`, and it starts a plugin's tool servers), is refused the same
way. The lease runner asks the runtime's optional `folder_refusal` before it opens a lease, resumes one for a message or
switches its mode, and answers `409 folder_refused` with the reason (lease-runner.v2: a new error code), so the assistant
says why it didn't open; a lease's handover resume is refused by the adapter, and the runner's minimal handover follows.
A `--restricted` run isn't looked at: it loads no plugin (`platform-run-plugins`).

The points without `--restricted`, from `runtime_claude._flags` (neither `settings: platform` nor `restricted: true`;
completions always carry it): `librarian-index`, `agent-job`, `login-check`, `lease-session`, `lease-work`,
`lease-handover` (`tests/test_unrestricted_plugins.py` pins the list, so a new one is looked at). Out of scope: the
user's own plugins (their skills folder, marketplace installs), which the owner installed and which load in these runs
too (the control runs above; the vercel plugin's SessionEnd hook in a platform run, `runtime_claude._env`); the runs the
platform starts outside a project's folder (`claude agents --json`, `claude auth status`); and a session the user opens.

### Open questions for the owner

1. **Build slice 1?** The session surface command and contract, plus a display-only Claude Code mod (band, toasts,
   "needs you" pane, `/inbox`, `/brunna`), with no hook removed. Recommended: yes.
2. **Approvals in the pane: show only, or act?** Recommended: show the command and copy it. A press inside an agent's
   session isn't your typed yes, and `skills sync`/`release` refuse there anyway.
3. **Who updates the mod?** Recommended: `brunna upgrade`, where you installed it, pinned to the release you run.
   Otherwise Claude Code's own marketplace update moves it on its own schedule.
4. **The waiter and the worktree hooks into the mod later?** Recommended: decide after slice 1 has run for a while. Each
   needs the measurements listed under "What moves".

### To measure or check while building

- A marketplace `source` pointing into a subfolder, and whether an entry can pin a git ref (`claude plugin validate`).
- The command that updates an installed plugin from a script (question 3 depends on it), and that it adds nothing where
  the plugin isn't installed.
- What `session.start`'s `isInteractive` and `$.session.surfaces()` answer in the desktop app's local sessions (rule 4).
- The names Claude Code gives a plugin's slash commands.
- Whether the session's sandbox applies to `$.process.run` children, and whether a project's settings `env` reaches them.
- Whether user-scope plugins load in `claude -p --restricted` (the conformance canary above).
- That a skills-folder plugin needs `.claude-plugin/plugin.json` (finding 1), before adding the control name.
- For the later waiter: a timer's `$.prompt.submit` waking an idle session, in the terminal and the desktop app.
- For Codex: what a Codex plugin can carry, and whether a user-level skill is offered as a command.
