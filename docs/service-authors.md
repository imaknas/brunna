# Writing a service for brunna

> **Draft.** The manifest format below is fixed (version 1) and can be checked today. Installing services
> (`brunna service add`), calling them from other projects, and asking the owner are designed but not built yet
> ([docs/design/service-ecosystem.md](design/service-ecosystem.md), steps 2 to 7). This page grows as they land.

A brunna service is an ordinary repository with three things:

1. **a CLI**, for scripts, whose `--help` lists its commands;
2. **an MCP server on stdio**, for agents, whose tool list is its interface;
3. **`brunna-service.yaml`** at the repository's root: what it offers and what it asks for.

The first two work without brunna, in any MCP client. The manifest is what lets brunna run the service, wire it into
the user's other projects, and give it what it asks for.

## What brunna will give a service

- somewhere to run: kept running, restarted, updated from your release tags;
- the user's models through `brunna complete <name>` at a tier, so you need no API key of your own when any good
  model will do;
- a sandbox for files: your code can't read the user's projects, credentials or the platform's state, which is why
  users can install it. Outbound network is not confined yet; the hosts you list are shown to the user;
- your declared credentials checked and your declared data backed up;
- every project on the machine that the user lets use your service sees its tools;
- through Principal Handoff, a way to ask the user a question or give them a task, shown in whatever assistant they
  use.

## The manifest

```yaml
v: 1                      # the manifest format
name: example-review      # lowercase letters, digits and -, 2 to 48 characters
what: "Ask several providers' models the same question and run blind reviews"
interface: 2              # your interface's major version: raise it when a change breaks callers
setup:                    # run in your folder after install and each update, in order
- uv sync --frozen
- npm ci --ignore-scripts
cli: {run: uv run review}
mcp: {run: uv run review-mcp}
models:                   # model calls through the platform, by name; the platform picks the model for the tier
  judge: {tier: strong}   # fast | standard | strong
secrets:                  # what the user provides, as environment variables; never the values
- name: OPENAI_API_KEY
  what: "OpenAI models"
  how: "make a key at the provider's API keys page"
  optional: true          # the service starts without it
network: [api.openai.com, api.anthropic.com]   # the hosts it reaches
data:                     # what it keeps, relative to its state folder; backed up unless backup: false
- {path: results, what: "review results"}
- {path: cache, what: "downloaded model lists", backup: false, why: "fetched again from the providers"}
principal: true           # it asks to put questions and tasks to the user
```

Everything in it must be true on every machine: no path of yours, no port, no key, no list of who uses it. What the
user decides on their machine (what is granted, who may call) lives in their own configuration, never in your repo.

Rules beyond the shape:

- `cli` and `mcp` are both required.
- `data` paths are relative to the state folder brunna gives the service: no leading `/` or `~`, no `..`, no glob
  characters, no entry inside another (compared case-insensitively), and nothing under `.brunna/`, which is the
  platform's.
- Secret names are environment variable names, each once, and none the platform or a process relies on: nothing
  starting with `BRUNNA_`, `AGENTHUB_`, `LD_`, `DYLD_`, `PYTHON`, `NODE_`, `UV_`, `NPM_`, `PIP_`, `GIT_`, `SSH_`, `LC_`,
  `XDG_`, `PERL5`, `RUBY`, `CARGO_`, `RUSTUP_`, `BUN_` or `DENO_`, and not `PATH`, `HOME`, `TMPDIR`, `LANG`, `TZ`, the
  Java options, the proxy or CA variables and the like.
- `backup: false` needs a `why`.
- Duplicate keys and merge keys (`<<`) in the YAML are refused.

The schema is [contracts/service-manifest.v1.schema.json](../contracts/service-manifest.v1.schema.json). To check a
manifest from a brunna checkout:

```bash
bin/py bin/service_manifest.py <your repo>
```

## Writing the service

- **Your state folder is the only place you write.** Keep your code's folder read-only at run time; caches and data go
  in the state folder (its path is given to you at run time).
- **Lockfiles.** Write setup to install from lockfiles (`uv sync --frozen`, `npm ci --ignore-scripts`). Once installing
  is built, setup will run in a profile of its own, and install scripts will run only if the user allows them.
- **Who is calling.** Each call carries `BRUNNA_CALLER`, the project that called, so you can attribute cost or limit
  use. Never trust a caller name sent in a tool's arguments.
- **Interface versions.** Callers pin your `interface` major. Adding a tool or an optional argument keeps it; removing
  or changing one raises it.
- **Long work.** A tool call should return quickly. For work that takes minutes (a review), return an id and offer a
  tool to fetch the result.
- **Asking the user.** With `principal: true` granted, a question or task goes to the user through Principal Handoff
  (how is added here with step 4). A call that waits on the user returns at once with the question's id.
