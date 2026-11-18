# brunna for Claude Code

A Claude Code mod that shows brunna inside the session you already have open (docs/design/agent-surface.md):

- **The band** above the prompt, only while something needs you: how many items wait for you, whether the platform has
  a problem, a letter this session has held too long without handling it, or letters that wait for another session
  because this session's tool version failed the platform's sandbox check. Its button opens the pane.
- **The "needs you" pane**: your open to-dos and questions across projects, newest first, and the platform's alerts. An
  item of this session's own project has a button that fills the prompt box with the platform's words for asking the
  agent about it; you edit and send it. An item only your own terminal can settle shows its `brunna` command, with a
  button that copies it. Items of other projects are read-only here, and the whole pane is read-only when your owner
  side is the assistant.
- **`/inbox`** fills the prompt box with the request to read and handle this project's letters; **`/brunna`** opens the
  pane.

It is display only. It runs one program, `brunna` (`brunna surface --json` once a minute, `brunna glance --json` when the
pane opens), from your home folder, never the project's. It puts nothing into the model's context, registers no tool,
and never submits, approves or runs anything for you. Uninstalled, disabled or broken, nothing brunna guarantees
changes: letters still reach your sessions through brunna's hooks, and `brunna glance` and the status page still show
what needs you.

## Install

Once, at the prompt of a Claude Code session in a terminal, with `<hub>` the folder brunna is installed in (the checkout
you ran `bin/setup` from; `~/.local/bin/brunna` names it):

```
/plugin install brunna --marketplace <hub>
```

Answer `y` to add the marketplace, then choose the user scope. From a shell instead:
`claude plugin install brunna --marketplace <hub>`. The mod is read from that folder, so it is always the release brunna
runs; after `brunna upgrade`, `/reload-plugins` in an open session (or its next start) picks up the new one.

Only you install it: brunna and its agents never add a marketplace or a plugin to your settings.

## Checking it

`claude plugin validate integrations/claude-code` (the mod) and `claude plugin validate .` at the repo root (the
marketplace). `tests/test_mods.py` checks what the mod may run and register.
