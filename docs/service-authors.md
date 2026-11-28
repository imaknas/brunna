# Writing a service for brunna

> **Draft.** The manifest format below is fixed (version 1) and can be checked today. A user can install a service,
> run its CLI, get its new releases, and call its tools from the sessions of their other projects (unsandboxed ones
> so far). Calls from sandboxed projects and from scripts are designed but not built yet
> ([docs/design/service-ecosystem.md](design/service-ecosystem.md), steps 3b to 7). This page grows as they land.

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
principal: false          # reserved: asking the user itself (not used yet)
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

## Where it runs

`brunna service add <url> [--ref <tag|branch|commit>]` fetches your repository (nothing from it runs as the user),
checks the manifest, shows the user what it asks for, asks the user for each required secret, checks out the code and
runs `setup` in a sandbox. Then `brunna service run <name> -- <args>` runs `cli.run` with those arguments.

Your code runs in a sandbox made for a stranger's code: it reads only the system's own folders, `uv` and `node`, and
its own folders; it writes only its own folders; it can't use unix sockets (but the system's name resolver) or listen
on a port. Its environment is built from nothing:

| variable | what |
|---|---|
| `BRUNNA_SERVICE_STATE` | your state folder: the only place you write what you keep; `data` paths are relative to it |
| `HOME` | `<state>/home` at run time, `<cache>/home` in setup |
| `XDG_CACHE_HOME`, `UV_CACHE_DIR`, `npm_config_cache`, … | your cache folder (not backed up); downloads go here (Hugging Face follows `XDG_CACHE_HOME`) |
| `TMPDIR` | a temp folder of your own |
| `BRUNNA_CALLER` | who called: `owner` from the user's terminal; the calling project once step 3 lands |
| each granted secret | by its name, if the user set it |
| `PATH`, `LANG` | `uv`, `node`, `/usr/bin`, `/bin` |

- Your code folder is read-only at run time (and nothing setup started in the background outlives it): no `.env`
  beside the code, no writing a config file there. Your folders themselves can't be removed or replaced.
- In setup, the code folder and the cache are writable and the state folder isn't; no secrets are given.
- On macOS, setup has no `git` or compilers (the Command Line Tools aren't readable inside): install from wheels
  (`uv sync --frozen --no-build`, or a pure-Python project), never from a git dependency.
- On Linux there is no `/dev/shm`, so Python's `multiprocessing` locks don't work; Linux also can't stop your code
  from writing a `.git` folder in its own folders, which macOS refuses.
- Outbound network is open (the user is told so); list the hosts you reach in `network`.

## How projects call it

A project whose registry entry has `consumes: [{contract: service, from: <your service>, v: <interface major>}]` sees
your MCP tools in its agent sessions, through the platform's own MCP server, as `<service>__<tool>`. Keep tool names
short: the whole name the client sees (`mcp__brunna__<service>__<tool>`) must fit in 64 characters, of letters,
digits, `_` and `-`. Your server is started once per session (start fast: it has 20 seconds to answer `initialize`
and `tools/list`), as your service, with `BRUNNA_CALLER` the calling project. Requests your server makes to the
client (sampling, elicitation) aren't supported; a call has 15 minutes. The user decides which projects may call it
(`brunna service callers`); a project the user keeps in a sandbox is served only when named there. What your tools
return reaches the caller as text and images only (resources and links to them are dropped), and your tools'
`annotations` aren't passed on.

## Releases

A service added at a release tag (`v1.2.3` or `1.2.3`, no suffix) follows your releases: once a day the user's
platform looks for a newer one, sets it up beside the running version and switches only if setup passed. It waits for
the user instead when the new version asks for more than was granted (a model call, a stronger tier, a secret, a
secret turned required, a host, asking the user) or changes `interface`. Never move or delete a published tag: a
moved tag stops all updates until the user looks. Pre-release tags (`-rc1`) are never taken automatically.

The user's nightly backup takes your state folder (minus `data` entries with `backup: false`) and your secrets, never
your code or cache. A state folder over 20 GB is left out, and the user is told.

## Writing the service

- **Your state folder is the only place you write** (`BRUNNA_SERVICE_STATE`); caches go in `XDG_CACHE_HOME`.
- **Lockfiles.** Write setup to install from lockfiles (`uv sync --frozen`, `npm ci --ignore-scripts`). Once installing
  is built, setup will run in a profile of its own, and install scripts will run only if the user allows them.
- **Who is calling.** Each call carries `BRUNNA_CALLER`, so you can attribute cost or limit use. Never trust a
  caller name sent in a tool's arguments.
- **Interface versions.** Callers pin your `interface` major. Adding a tool or an optional argument keeps it; removing
  or changing one raises it.
- **Long work.** A tool call should return quickly. For work that takes minutes (a review), return an id and offer a
  tool to fetch the result.
- **Asking the user.** When your service needs the user to decide (a budget, a choice), the agent calling it asks
  the user and passes the answer in the call (an argument such as `--budget`). Leave `principal` false: a service
  that asks the user itself and acts on the answer is reserved for later (docs/design/service-ecosystem.md).