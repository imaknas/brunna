---
name: new-or-onboard-project
description: Create a brand-new project or bring an existing folder into the agent-hub pipeline, end to end. Use whenever the user asks to "start/create a new project" or "onboard/connect this folder/project" — handles location, scaffolding, visibility, sandbox and push decisions, then delegates everything mechanical to one command.
---

# New or Onboard Project

Two commands do the mechanical work, so this skill covers only the decisions that need judgment. Don't redo by hand
what they already do (no `git init` first, no `gh repo create`):

- `agenthub connect <folder>`: existing folders. It works out the layout (one folder, one repo, a monorepo, or a folder
  holding several repos), says what connecting does, asks per layout, and calls `onboard-project.sh` for each project.
- `agenthub onboard <path>` (= `bin/onboard-project.sh`): one project, new or existing. It does the git init, an empty
  first commit and the GitHub repo. What is in the folder stays uncommitted.

## Decision 1: a brand-new project, or existing folders?

- **Brand-new project** (the user says "create a new project for me"): the directory usually doesn't exist yet or is
  empty. Go to decisions 2, 3, 4 and 5, then run `agenthub onboard`; it creates the directory itself.
- **Existing folders** (the user says "connect this" or points at a path that already has things in it): skip decision 3,
  look at decisions 4 and 5, then run `agenthub connect <folder>`. A repo with unsaved changes is skipped and the reason
  printed: **don't clean it up for the user and re-run**. Pass the message on unchanged and let them decide whether to
  commit or discard those changes. This protects their work that isn't ready yet. The same for a folder inside another
  repo, or a repo with nothing on its current branch: the user decides.

## Decision 2: which path does the project go in?

- **Default** for a new project: under the workspace root (`hub.json` `workspace_root`),
  `<workspace root>/<name>`, whatever the language and whether or not it is a code project.
- Never inside the memory copies or the services root (`hub.json` `memory_copies`, `services_root`): those are the
  platform's.
- If the user names a path themselves, what they say goes; don't override it. Existing folders stay where they are.

## Decision 3: does a brand-new project need language scaffolding?

If the user named a language or framework ("create a Rust project", "use Python"), first run the matching init command
in the directory (`cargo init`, `uv init`, `npm init` etc.), **then** onboard it. Onboarding commits nothing of it: the
project's own session commits the scaffolding afterwards, with a `.gitignore` in place (and pushes it, as the user decides).

If the user named no language and just wants a place to keep things (research notes and the like), no scaffolding.

## Decision 4: should the GitHub repo be public or private?

**Private by default**: that is also the command's default, no flag needed.

Add `--public` only when the user explicitly says this project is meant to be public (a portfolio, open source). If
unsure, ask; don't guess.

`--no-remote` (no GitHub repo) only when the user says the project doesn't need one for now; when the settings have no
`github.owner`, onboarding stays local by itself and says so (`--public` fails). Tell them the trade-off: without a remote there is no copy beyond this machine (the platform's
encrypted backup, if on, still covers it). `agenthub connect` uses `--no-remote` unless given `--remote`.

## Decision 5: sandbox it, and which branches may the platform push?

Ask the user both, in the conversation, with these words (docs/design/platform-push.md sections 1 and 2); never decide
for them:

1. **Sandbox it?** "Will its agent touch other people's data (colleagues' records, other people's documents or
   profiles, recordings of others), or run or read untrusted code or content (downloaded models, others' benchmarks,
   unattended processing of outside papers, pages or mail)?" Yes: `--sandbox`. A sandbox keeps a mistake, or an
   instruction planted in that data, inside the project. Maturity is no reason either way: a new project isn't
   sandboxed for being new. No, or the user doesn't say: no flag (not sandboxed, as before).
2. **Only for a sandboxed project with a remote: which branches may the platform push for its sessions?**
   - `none`: the user pushes themselves;
   - `others`: every branch but the default;
   - `all`: the default branch too.

   On a public remote, also say: "every branch you allow is published as soon as it is pushed, and a site or deploy
   that builds from branches (gh-pages, a hosting 'production branch', preview deploys) runs it. `others` keeps the
   default branch's CI and deploys yours, not the content private."

   `others` and `all` use the user's own GitHub login: before passing either, tell them in the conversation that it
   will use their `gh` login on github.com/<owner>/<repo> to add a deploy key with WRITE access that can write only
   that repo; the platform keeps the private key in its secrets folder (not in the cloud backup) and uses it only to
   push the branches this project's sessions ask for, after the automatic checks; undo with
   `brunna push setup <name> --revoke` (deletes it at GitHub and here). Pass `--push others|all` only after they say
   yes to that: the flag is taken as their yes (the same text is printed again, and nothing more is asked). Other
   forges get no platform push.

   `none`, or a question the user leaves open: pass `--push none`, or nothing. Nothing gives `none` too, for every
   visibility (11-16: no longer `all` for a private remote), plus a to-do to decide later with
   `brunna push setup <name>`, which says what it does and asks before using their gh login. `--push-email
   <address>` (repeatable) names the commit emails those pushes may carry; without it the platform proposes them (on
   a public remote GitHub's noreply address only).

## Run

```bash
agenthub connect <folder> [--remote] [--sandbox] [--push none|others|all]                 # existing folders
agenthub onboard [--public] [--no-remote] [--sandbox] [--push none|others|all] <path>     # one project
```

Both leave nothing half-done when something fails, and carry on when run again after an interruption. When it
finishes, give the user its output as is (including the GitHub URL, and whether `--public` was used); then tell them
to start a new session in the project's folder, since the project's setup loads when a session starts.
