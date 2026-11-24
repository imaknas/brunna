# brunna for Claude Code

A Claude Code mod that shows brunna inside the session you already have open (docs/design/agent-surface.md):

- **The band** above the prompt, only while something needs you: how many items wait for you, whether the platform has
  a problem, a letter this session has held too long without handling it, or letters that wait for another session
  because this session's tool version failed the platform's sandbox check. Its button opens the pane. A newer brunna
  release gets a quiet line of its own when nothing else is there; one with a security fix stands beside the count.
- **The "needs you" pane**: your open to-dos and questions across projects, newest first, and the platform's alerts. An
  item of this session's own project has a button that fills the prompt box with the platform's words for asking the
  agent about it; you edit and send it. A newer brunna release is a line with the `brunna upgrade` command to copy. An item only your own terminal can settle shows its `brunna` command, with a
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

`tests/test_mods.py` runs in brunna's own tests and checks what the mod may run, pass and register. The rest starts
the real `claude`, which no test tier may do, so it is run by hand, with a scratch config folder so nothing reaches
your own Claude Code settings:

```
CLAUDE_CONFIG_DIR="$(mktemp -d)" claude plugin validate integrations/claude-code   # the mod
CLAUDE_CONFIG_DIR="$(mktemp -d)" claude plugin validate .                          # the marketplace, at the repo root
CLAUDE_CONFIG_DIR="$(mktemp -d)" claude plugin test integrations/claude-code       # tests/brunna.test.tsx
```

`tsc -p` type-checks it once the engine has laid its types in `.claude-plugin/types/` (a `--plugin-dir` session it
hot-reloads), or with the `tsconfig.json` in the header of the plugin-authoring skill's declaration file.

The mod passes blank every variable the platform reads, and Python's and the loader's own: a project's settings `env`
reaches the commands it runs (measured on Claude Code 2.1.327), and an empty value is no value to brunna.
