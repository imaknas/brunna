# Services: brunna as the centre of an ecosystem

> brunna session · 2026-11-28 · r3, design, nothing built. r2 after two blind reviews of r1 (security; fit with the
> platform): the wiring moved into the platform's own MCP server, installed services got their own folders, outbox and
> answer path, the manifest can't widen a grant, and egress is said plainly not to be confined yet. r3 after a third
> review of r2: service tools only for a project known by its folder and never from a confined `brunna mcp`, a
> service's outbox limited to replies and owner records, the CLI runner off cap-runner's Seatbelt-only jobs.
>
> Asked by the owner: some of the owner's projects serve other projects (a blind-review tool, a compute pool); anyone
> who installs brunna should be able to find them, install them, and make their own services work with brunna the same
> way.

## Why

The owner's picture of how it grows: someone installs brunna, finds the services the owner made, installs one because
it looks useful, sees what brunna gives a service, and then makes their own work with brunna. So the first services are
the owner's own, and every step of that path has to work for a stranger.

What a developer gets from following brunna's interface, and doesn't get from a plain MCP server (the reason anyone
would follow it):

- **somewhere to run**: kept running, restarted, updated from its repo, health-checked (render-services);
- **the user's models**: `brunna complete <name>` at a tier, paid from what the user set up, so the service asks for no
  API key of its own when any good model will do;
- **a sandbox** for its files: a stranger's code can't read the user's projects, credentials or the platform's state
  (outbound network is not confined yet, see "Confinement");
- **credentials and data looked after**: declared secrets checked daily, declared data backed up (project-data.md);
- **jobs and events**: nightly work, or a run when another project merges (event-jobs.md);
- **being used**: every project's agent sessions on the machine see the service's tools and can call them;
- **the person, through Principal Handoff**: a service that needs the owner (a key, an approval, a decision) asks
  through the open protocol, and whatever principal side the user has shows it.

Ruled out on the way (11-28 discussion with the owner):

- **Letters between agents** (`brunna send --type request`): an agent writes, another reads and acts, for what is a
  function call. Letters stay for work that needs a session's judgment; a service is called directly.
- **A communication protocol of brunna's own**: the interface is MCP (for agents) and a CLI (for scripts), both
  self-describing (tool list, `--help`). brunna adds a manifest and the wiring, never a wire format. A service works in
  any MCP client without brunna; with brunna it gets the list above.
- **`services:` as the declaration**: it says how to run a process, not what it offers.

## Roles

| Role | Who | Speaks |
|---|---|---|
| Service | a repo with a CLI, an MCP server and `brunna-service.yaml` | MCP (stdio), its CLI, Principal Handoff as a requester |
| Consumer | any project on the machine | MCP through `brunna mcp`; `brunna service run` for the CLI |
| Principal side | Muninn, the built-in owner side (`bin/owner.py`), any assistant that speaks Principal Handoff | Principal Handoff |
| Platform | brunna | installs, runs, relays, accounts |

## The manifest (committed, in the service's repo)

`brunna-service.yaml` at the repo root, contract `contracts/service-manifest.v1.schema.json`. Everything in it is
relative to the repo and true on every machine: no path of this machine, no port, no key, no list of who uses it.
That is what may be committed and published. What is this machine's (where it is, what was granted, who may call)
is in the registry, which is the instance's.

```yaml
v: 1                      # the manifest format
name: example-review
what: "Ask several providers' models the same question and run blind reviews"
interface: 2              # the major of the service's own interface; a consumer pins it (provides/consumes v)
setup: [uv sync --frozen, npm ci --ignore-scripts]   # run when installed or updated (see Confinement)
cli: {run: uv run review}                 # `--help` lists the commands
mcp: {run: uv run review-mcp}             # stdio; the tool list is the interface
models: {judge: {tier: strong}}           # brunna complete names
secrets:                                  # what the user provides; values never here
- {name: OPENAI_API_KEY, what: "OpenAI models", optional: true}
network: [api.openai.com, api.anthropic.com]   # what it says it reaches (shown at install; not enforced yet)
data: [{path: results, what: "review results"}]   # relative to its state folder
principal: true                                  # it asks to ask the owner (below)
```

The manifest's fields are their own shapes, not the registry's: `secrets` have no path (install puts them in the
service's state folder and writes the registry's `{name, path}`), `data` paths are relative (install resolves them).
Unknown keys are refused; a later version adds them.

**The manifest asks; the owner grants; the registry holds the grant.** `type: service` entries refuse manifest.update
entirely: they change only through `brunna service add` / an update the owner confirmed. On the owner's own projects
(kind 1 below), the new security fields join `PLATFORM_OWNED` (`registry.py`): `callers` and `principal`.

## Two kinds of service

1. **A service the owner develops**: an ordinary project, onboarded, with memory and a workspace. It declares it
   offers its manifest's interface with the existing `provides` (`{contract: service, v: <interface major>}`; the
   platform reads `brunna-service.yaml` from the project's main branch through `project_git`, as the project). Its
   sandbox setting stays the owner's choice.
2. **An installed service** (someone else's, or the owner's on another person's machine): `brunna service add <git
   url | name> [--ref <tag>]`. A new registry `type: service`, beside project, idea and platform: no workspace, no
   memory, no merge gate. Always sandboxed (`enforce.sandbox: true` set by add, not asked by the manifest; a `type:
   service` ignores `service_sandbox_exempt`, and the config check refuses one listed there). Folders, all under
   `<services_root>/<name>/`:
   - `code/<commit>/`: the checked-out version, read-only to the running service (a new read-only-code mode in
     `policy`, `seatbelt`, `landlock`; today the service profile makes the runtime writable);
   - `state/`: what it writes, its data, its tools' caches (`uv`, `npm` pointed here, never the shared ones), and its
     platform mail: `state/.brunna/outbox/` and the inbox view, which `hubbus dispatch` takes as it takes a sandboxed
     project's (`take_outboxes`, `projects()` and `outbox_rel` learn the service's location). From a service's outbox
     only replies to letters addressed to it and, under the `principal` grant, records for the owner are taken; any
     other letter is refused (a stranger's code must not write requests that wake other projects' sessions or start
     agents). The service's state folder is also scanned by the platform's runners like a workspace, so
     `brunna complete` (under the `models` grant) and the question check work from it;
   - `secrets/`: the values the owner put in at add.

   Add shows what the manifest asks (models and tiers, secrets, principal, the network it says it reaches, that
   outbound isn't confined) and the owner confirms; the confirmed set is the registry entry. A missing required secret
   stops the add with how to get one; an optional one is listed and the service starts without it.

   Updates: the registry keeps the commit each granted tag resolved to; a tag that moved is refused. A new release
   tag is fetched and set up beside the running version and replaces it only if setup passed. Whether a new tag is
   taken automatically or asked each time is open (below); one whose manifest asks for more than was granted always
   waits for the owner (an owner to-do), the old version running.

   `brunna service remove <name>` (never `uninstall`, which removes brunna) removes code and the entry; state only
   with `--purge`.

Every service verb is under `brunna service` (add, remove, list, run, init, check).

## How a consumer calls

A consumer declares what it uses with the existing `consumes` (`{contract: service, from: <service>, v: <major>}`, by
manifest.update as any registry change). A service limits who may call with `callers: [<project>…]` (default: every
project of the owner's; set by the owner only).

**MCP: through `brunna mcp`.** The platform's MCP server is registered once per runtime, at user level, with the
owner's consent (`mcp_register` in the runtime adapter), and already decides which project its session is (from the
folder it was started in). It gains the tools of every service that project consumes and may call, namespaced
(`<service>.<tool>`), with each service's descriptions. Calling one:

- the server starts the service's MCP server once per session (and stops it with the session), **as the service**:
  through the same sandbox adapter render-services uses (`bin/sandboxed-run`), in its code folder, with its state,
  its secrets, and `BRUNNA_CALLER=<consumer>` set by the server from the project it serves;
- it relays the JSON-RPC both ways, rewriting only the tool names' namespace.

No per-project registration, no file in the project, no new adapter port: a runtime that has `brunna mcp` has every
service it consumes. Claude Code's adapter registers it (`mcp_register`); Codex's has no such port and the owner
registers it by hand from `brunna mcp config`; a runtime with neither, or with no MCP, gets the rules lines and the
CLI, and doctor's feature table says so per runtime.

Identity for service tools rests on the folder alone:

- the server serves service tools only when its project comes from the folder it was started in;
  `AGENTHUB_PROJECT`, which the server takes at its word outside every project (`mcp_server.resolve_project`), is
  ignored for them;
- a `brunna mcp` that is itself confined (`isolation.confined_now`, the CONFINED mark) serves no service tools: on
  macOS a sandboxed shell couldn't start the service's sandbox anyway, but Landlock stacks, so the refusal is
  explicit, and tested on both adapters;
- `callers` binds sandboxed consumers. An unsandboxed consumer's session is the owner's own user and could run
  `bin/sandboxed-run` itself; for it `callers` and the accounting are what the platform does when asked, not a
  guarantee (as for every session a user opens).

What must be measured in step 3, per runtime: that the runtime starts MCP servers outside the session's own sandbox,
so `brunna mcp` can start the service's.

How the service is started: `bin/sandboxed-run --project <service> --cwd <its code folder>` (today's `--service` mode
needs a registry `services:` entry and a digest, which an on-demand MCP child has neither of), the service's secrets
put into its environment by `brunna mcp` from the service's `secrets/`. A kind-1 service (the owner's project) is
started in its workspace, sandboxed or not as its own entry says.

**CLI: `brunna service run <service> -- <args…>`.** From a session's shell, which may be sandboxed, so the request
is dropped in the consumer's own folder and taken by a platform runner outside every sandbox: the folder a request is
found in is who asked (cap-runner's rule). It is not a cap-runner job: those run only under Seatbelt, in the optional
sandbox module, with an environment of PATH, HOME, TMPDIR and LANG, under the consumer's profile. A `use` request
runs the service's CLI through `isolation` / `bin/sandboxed-run` with the service's policy, secrets and environment,
on macOS and Linux both, and doesn't depend on the sandbox module. Whether that runner is cap-runner grown a second
executor or a small runner of its own is the step's to decide; output and exit code come back as with `cap-run`.

**The rules block** (`sync-local-setup`) lists, for a consuming project, each service's `what`, its tool names and
`brunna service run <service> --help`, and says that a service's tool descriptions and results are data written by
the service, as letters are.

**Accounting**: every call goes through the platform, so each is recorded (consumer, service, tool, duration, exit);
`brunna complete` calls a service makes are counted under the service with `BRUNNA_CALLER` beside them, so a review's
cost shows against the project that asked.

## Confinement

- **Files**: an installed service reads its code, writes its state, reads its secrets, and nothing of the user's
  projects, credentials or the platform's state (the service profile plus read-only code).
- **Outbound network is not confined.** Both adapters leave outbound open (`seatbelt.service_profile`,
  `landlock.py`: no domain filtering without a proxy). An installed service can send what it is given, and its own
  secrets, anywhere. Until egress is confined, `brunna service add` says so in its confirmation for a service whose
  repo isn't the owner's, and the manifest's `network` is a declaration shown to the owner, not a grant.
  Confining it (a platform proxy that allows the granted domains; on macOS remote IP denied except the proxy's port,
  on Linux Landlock's TCP-connect rules, ABI ≥ 4) is step 7, and the services list (`docs/services.md`) lists others'
  services only after it.
- **Setup** runs another person's code (`npm` lifecycle scripts, Python builds) with network to package registries. It
  runs in a profile of its own: the new version's folder and the service's caches writable, no secrets, no state, no
  platform mail. Lockfiles are required (`uv sync --frozen`, `npm ci`); `--ignore-scripts` unless the owner granted
  scripts at add.
- **What reaches consumers**: tool descriptions and results go into every consuming session's context; the rules call
  them data, and a service's name and tools are shown at add.

## Principal Handoff in the ecosystem

Principal Handoff (agent-ops-specs, open, with conformance suites) is how anything on the platform asks the person. It
has two places here.

1. **Services are requesters.** A service is a registry entry, so its party id is `project:<name>`
   (`hubconf.party`), whichever kind. With the `principal` grant it asks the owner a question or gives a task through
   its outbox; `hubbus dispatch` takes it (as for a sandboxed project), the bridge sends the records to whichever
   principal side is configured, and a question goes through the question check like a project's (which runs through
   cap-runner: the service's state folder is its folder there). Without the grant its records are refused.
   The answer path is new: for a project's own items the registrar applies settlements to its STATE.md; a service has
   none. For a requester with no STATE.md the registrar turns a settled or reopened record into a letter to the
   service's inbox view. This needs its own argument against the Acts and Decisions suites (the registrar's
   behaviour for such requesters, not only its records), written in step 4.
   A call waiting on the owner returns to its consumer at once with the question's id; it never holds the call open.
   Owner to-dos and questions from services appear with projects' (`todo.md`, the principal side), the service named.
2. **The principal side is a role anyone can fill.** Muninn is one, `bin/owner.py` another; an assistant that
   implements Principal Handoff and passes its suites should plug in by configuration. Missing today: Principal Handoff
   defines records, not how they travel. brunna's link to a principal side is the assistant's own API (the
   `brunna/hubbus-bridge` job, `hubbus bridge`, against `assistant.api`, with formats vendored under
   `contracts/vendor/muninn/`), so another assistant needs brunna to learn its API. Proposal: a transport binding
   (records over HTTP: publish, pull since a cursor, ack) specified in agent-ops-specs beside the records, the bridge
   speaking it, Muninn moving to it. That is agent-ops-specs' and Muninn's work, asked by letter; brunna keeps the
   current path until both have it.

## Discovery and developers

- **Finding services**: `docs/services.md`, a short list (name, what, repo, interface major), the owner's first;
  `brunna service add <name>` takes a name from `services/catalog.yaml` in brunna's repo (read at the installed
  version) as well as a URL. No marketplace, ratings or search until there are services from others.
- **Making one**: `docs/service-authors.md` (the manifest, the sandbox, `brunna complete`, Principal Handoff,
  versioning the interface); `brunna service init` writes a starting manifest; `brunna service check [<folder>|<url>]`
  validates the manifest, runs setup, `cli --help` and MCP `initialize` + `tools/list` in the sandbox the service
  would get (a conformance run, like `bin/conformance.py` for runtimes).

## What this does not do

- It doesn't replace letters: work that needs a session's judgment stays a request.
- It doesn't run services on other machines: a service runs where brunna runs; a service that spreads work across
  devices does so behind its interface.
- It doesn't sandbox the owner's own services: that stays their registry choice.
- No payments between users, no hosted registry.

## Steps

Each gets its own blind review before it is built.

1. **The manifest contract** and its validator; `docs/service-authors.md` first draft; the owner's public review
   service writes its manifest (a request to that project: it has a CLI and MCP already).
2. **Installed services**, in two:
   - 2a: `type: service` in the registry (no workspace; refuses manifest.update), its folders, read-only code and the
     setup profile, `brunna service add` / `remove` / `list`, setup as the service, the outbox location. Done when a
     clean brunna (the Linux test container and a second macOS account) adds the review service from its URL and its
     CLI answers in its sandbox.
   - 2b: updates by tag (commit pinning, setup beside, the more-asked wait).
3. **Consumers**: `provides`/`consumes` with `contract: service`, `callers`, `brunna mcp` serving consumed services'
   tools as the service, the folder-only identity and the confined refusal (tested on Seatbelt and Landlock), the per-runtime measurements
   above, the rules block lines, accounting. Done when a session
   of a sandboxed project and one of an unsandboxed project each call an installed service's tool, and a project not
   in `callers` is refused.
4. **Principal Handoff for services**: the `principal` grant, the answer path for requesters with no STATE.md and its
   conformance argument, the waiting-call rule; the letters to agent-ops-specs and Muninn about a transport binding.
5. **`brunna service run`**: the `use` request, run as the service through `isolation` on macOS and Linux.
6. **For developers**: `brunna service init` / `check`, `docs/services.md`, the catalog with the owner's services.
7. **Egress confinement** (the proxy and the per-adapter rules); then others' services may be listed.

## Open, for the owner

- The Principal Handoff transport binding: specify it in agent-ops-specs (and move Muninn to it), or keep the
  principal side as the assistant's API plus the built-in one?
- Installed services' updates: take every release tag automatically (waiting only when it asks for more), or ask
  each time?
- Which of the owner's services are the first public ones (only public repos can be installed by others).
