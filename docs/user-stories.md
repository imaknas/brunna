# Who uses brunna, and how

Two things in brunna ask for different things of you. **Remembering** (notes beside each project, a mailbox between
projects, one list of what needs you) works with any agent that has MCP and needs no model. **Work that runs by itself**
(tidying the notes overnight, waking a session for a letter, handling a letter nobody took, scheduled agent jobs) needs a
model, and most of it needs Claude Code today.

## By agent

| You use | Notes and mail | Reminded when a session opens | Woken by a letter | A letter nobody took | Nightly notes |
|---|---|---|---|---|---|
| Claude Code | yes | yes | yes | yes | yes |
| Claude Desktop, its Code tab | yes | yes | yes | yes | yes |
| Codex CLI | yes | at the start | not yet | not yet | your choice |
| Codex app | as the CLI; local use isn't measured | as the CLI | not yet | not yet | your choice |
| Claude Code and Codex | yes, shared | both | Claude Code's sessions | by Claude Code | yes |
| Copilot, Cursor, another agent with MCP | yes, set up by hand; Copilot isn't tested | no | no | no | your choice |

"Your choice" is `brunna llm setup`: your Codex login, an API key, or a local Ollama. With none, notes stay as they are.

**Only Claude Code.** Connect your projects and work as usual. A session opens with the project's notes and any mail, writes
notes as it goes, and the rest runs by itself. A session can send a letter to another project, and that project's session is
woken to handle it.

**Only Claude Desktop.** The Code tab is Claude Code. You can install brunna without a terminal
([how](../README.md#set-up-through-your-agent)), except for making and saving your token. The chat tabs aren't in a project
folder, so they aren't connected.

**Only Codex.** Run `brunna user-settings --install --runtime codex` once, and letters and notes reminders appear when a session
opens; the mailbox and the notes are brunna's tools (MCP). Projects you sandbox aren't served in Codex yet, and Codex is never
woken: nothing yet says whether somebody is looking at the thread.

**Claude Code and Codex.** One mailbox and one set of notes per project, whichever you open. Start in Codex, note where you
stopped, carry on in Claude Code. What runs by itself is Claude Code's.

**Only Copilot.** Put the snippet `brunna mcp config` prints into its MCP settings, and it can read and send mail and write
notes. What runs by itself needs a model of your own. Nothing of this has been tried on Copilot.

## With or without a personal assistant

Projects remember, talk and run the same either way. What differs is where **what only you can settle** goes.

| | Without one (the default) | With one |
|---|---|---|
| Where it waits | The status page and `brunna owner` | Your assistant |
| How you answer | `brunna owner answer <id> "…"`, or in a session | In your assistant, from your phone |
| New items | A notice, if you set up `alerts` | Your assistant tells you |
| Talking to a project's session from your phone | No | Yes |
| Trying an idea in a project of its own | You start it | You ask your assistant |
| Setting it up | Nothing to do | The assistant, `brunna.json`'s `assistant` section and the `assistant` module |

**Without one.** In the morning you look at the status page: what needs you, across all projects. You answer with
`brunna owner`, or tell a session, and the project carries on. Turn on `alerts` and the `owner` module, and a notice comes every
hour there is something new; without `alerts` it only shows on the page.

**With one.** Your assistant gets what needs you through [Principal Handoff](https://doi.org/10.5281/zenodo.23852711), a
published protocol, and asks you where you already talk to it. Your answer goes back to the project. A to-do closed on either
side closes on the other; when a project says one is done, the assistant closes it or asks you first. It can also lend its conversation to a
project's session, so you can talk to that session, and answer its permission questions, from your phone.

The module that does this is written for one assistant, Muninn. Another one can join by implementing the protocol and the
contracts in [contracts/](../contracts); it isn't something to switch on.
