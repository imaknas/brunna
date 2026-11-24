# Security

agent-hub runs coding agents unattended, gives them sandboxes, and holds credentials for the services it schedules.
A flaw in any of that matters, so please report one privately.

## Reporting

Use GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under the Security tab).
Please don't open a public issue for a security problem. Say what you found, how to reproduce it, and what it lets an
attacker do.

## Scope

In scope: anything that lets a project's agent, service or job act outside what its registry entry allows. That
includes escaping the sandbox, reading another project's files or memory, writing the platform's own files, sending
mail as another project, or reaching credentials. Bugs that leak an instance's private data into this repository's
code or into what the platform publishes also count. The sandbox's guarantees and their limits on each OS are in
[docs/design/capabilities.md](docs/design/capabilities.md).

**What a sandboxed project's own files can do to its sessions.** A coding agent reads settings files in the folder it is
opened in, and those a project commits can name programs (hooks, helpers, tool servers) that the agent runs as you, outside
its sandbox, before anything the platform puts beside them has a say. So the platform doesn't contain a project whose
committed files are hostile: open a sandboxed project's sessions only once you have looked at its committed settings, and
look again after merging or pulling a change to them.

What the platform does: its own value wins wherever the agent lets one win (for Claude Code: its hooks stay on, the
sandbox stays on, what may not be read stays so); every other entry of the kinds it knows that runs something, or lets
its sessions reach more, is told to you once, as it is, at the next render (every 15 minutes); its own hooks take nothing of
the session's environment that says what to run; and real repos inside the project's folder get the same settings.

What it doesn't guarantee: anything before that render; entries the agent adds up from every settings file (they are told,
not taken away); a folder in the project that something running there, its own sessions included, made into a repo of its
own since the last render, or whose `.git` isn't a real repo's, as a submodule's isn't (a session opened in it reads that
folder's files only, and has no sandbox of the platform's); and, where you installed the
user-level worktree hooks, the line of shell in your own settings that starts them, which the agent runs with the
session's environment. Details: "A project's own settings files" in [docs/design/capabilities.md](docs/design/capabilities.md).

Out of scope: a session the owner opens by hand. There the coding agent's own protections apply, and the platform
advises but does not enforce (see "Tool neutrality" in [AGENTS.md](AGENTS.md)).

## Supported versions

Fixes go into the latest release only.
