# Services: brunna as the centre of an ecosystem

> brunna session · 2026-11-28 · r4, design. **Built 11-28: step 1** (the contract
> `contracts/service-manifest.v1.schema.json`, `bin/service_manifest.py`, `docs/service-authors.md`); the rest not.
> History: r2 after two blind reviews of r1 (security; fit with the
> platform): the wiring moved into the platform's own MCP server, installed services got their own folders, outbox and
> answer path, the manifest can't widen a grant, and egress is said plainly not to be confined yet. r3 after a third
> review of r2: service tools only for a project known by its folder and never from a confined `brunna mcp`, a
> service's outbox limited to replies and owner records, the CLI runner off cap-runner's Seatbelt-only jobs. r4
> after a fourth review: service tools only on a positive probe (fails closed on Linux), services' folders in every
> read-deny, services excluded from the default callers, Transport 0.1 named.
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
     agents). The service's state folder is also taken by the platform's runners as its folder, so `brunna complete`
     (under the `models` grant) and the question check work from it. Those run through cap-runner today, which is
     Seatbelt-only and in the optional sandbox module; the runner that takes `use` requests (below) takes these for
     services too, on macOS and Linux, so a service needs no module;
   - `secrets/`: the values the owner put in at add.

   The whole `<services_root>/<name>/` (code, state, secrets) is in every other project's and service's read-deny
   (`policy.other_projects`, built from `registry.dirs`, which today gives a runtime folder only to an entry with
   `runtime:`; `dirs()` and `project_data._own_folders` learn `type: service`).

   Add refuses a name that is a registered project's former name or the old side of a rename (`hubconf` rename
   records): records for the old name would reach the service, and core §8 delivers to a project only what it may know.

   Add shows what the manifest asks (models and tiers, secrets, principal, the network it says it reaches, that
   outbound isn't confined) and the owner confirms; the confirmed set is the registry entry. A missing required secret
   stops the add with how to get one; an optional one is listed and the service starts without it.

   Updates: the registry keeps the commit each granted tag resolved to; a tag that moved is refused. A new release
   tag is fetched automatically (decided 11-28) and set up beside the running version, and replaces it only if setup
   passed; one whose manifest asks for more than was granted always waits for the owner (an owner to-do), the old version running.

   `brunna service remove <name>` (never `uninstall`, which removes brunna) removes code and the entry; state only
   with `--purge`.

Every service verb is under `brunna service` (add, remove, list, run, init, check).

## How a consumer calls

A consumer declares what it uses with the existing `consumes` (`{contract: service, from: <service>, v: <major>}`, by
manifest.update as any registry change). A service limits who may call with `callers: [<project>…]` (default: every
project of the owner's, not other services, which need `callers` to name them; set by the owner only).

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
- what keeps a confined process from being served as another project is that it can't start the service: every
  service's folder (code, secrets) is read-denied to every other project and service (`Confinement`, and the
  read-deny under "Two kinds of service"), so a nested `bin/sandboxed-run` has nothing to run, even under Landlock,
  which stacks. On top of that, as defence in depth, `brunna mcp` serves service tools only when a positive probe
  passes: it can read the platform's state and secrets folders, resolved without their environment overrides
  (`BRUNNA_STATE`, `BRUNNA_SECRETS` and the old spellings could otherwise point it at folders a sandbox owns), as
  `owner_terminal.refusal()` decides. A consumer's `brunna mcp` that its runtime starts outside the session's sandbox
  passes, as intended. Detecting a sandbox instead would fail open (on Linux `confined_now` sees only an environment
  mark). Tested on both adapters with an unmarked confined process and with a runtime-sandboxed Linux shell (whether
  Claude Code's Linux `denyRead` hides a folder or mounts an empty one isn't measured);
- Codex has no confine port: a Codex session in a sandboxed project's folder isn't confined by the platform, so
  `brunna mcp` serves it no service tools there (the project is sandboxed, the session isn't what the registry says);
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
   its outbox; `hubbus dispatch` takes it (as for a sandboxed project) and the records go to the principal side as a
   project's do: with an assistant, the bridge sends them; with the built-in owner side they stay on the bus for
   `bin/owner.py`. A question goes through the question check like a project's (run by the services' runner above). Without the grant its records are refused.
   The answer path is new. A requester needn't have a state document (core §1: the role is the deployment's to
   authenticate; Acts §5: a requester with no state document need not implement the Markdown binding). Delivering a
   settled or reopened record to the service is core §8's delivery to the project, the principal side's duty, which
   brunna carries as the transport into the service's inbox view; it is not the registrar's (the registrar applies the
   Markdown binding to state documents) and carries none of the registrar's claims.
   Mechanical application: agent-ops-specs wrote the **Program binding** (11-28, `program/SPEC.md`, with
   `conformance/apply.json` and a reference `ph_program.py`; a 0.7 proposal, unpublished, since a binding shares its
   extensions' version and 0.6 is frozen). Its result is a pure function of the records: apply (a value), open,
   withdrawn, dismissed, judgement, or fault. It applies only when the service wrote the item itself (its own `by`,
   `terms_rev` and digest matching), a principal side configured for the binding settled it, authority is principal
   or confirmed, and the answer is exactly an option (Decisions: `answer.option` among the options and `answer.text`
   the option's label, no note; Acts: done or dropped, no note). Judgement means asking again (a new decision with the
   same options, the old one withdrawn as replaced); fault is reported to the deployment once. What it asks of brunna
   as the deployment: which requesters are programs, which principal sides implement the binding, and a key prefix per
   service; of the service: options on every decision, text saying what each option, a dismissal and giving up do,
   intent written before acting, refetch from the start after losing its store; of the principal side: an answered
   option's label written into `answer.text` verbatim, anything else said in text (so the program asks again).
   Until Acts and Decisions 0.7 list it, mechanical application stays outside the published spec, but services can be
   built to it now.
   A call waiting on the owner returns to its consumer at once with the question's id; it never holds the call open.
   Owner to-dos and questions from services appear with projects' (`todo.md`, the principal side), the service named.
2. **The principal side is a role anyone can fill.** Muninn is one, `bin/owner.py` another; an assistant that
   implements Principal Handoff and passes its suites should plug in by configuration. Missing today: Principal Handoff
   defines records, not how they travel, on purpose (core §10: any transport with at-least-once delivery and
   authenticated senders; §15 leaves transport out of scope; Acts §9 lists transport bindings as open). What core
   asks of any transport: at-least-once with retries, the authenticated sender checked against `by` and its role,
   delivery to the project rather than a session, and a sent record changed only by a later record. brunna's link to a
   principal side today is the assistant's own API (the `brunna/hubbus-bridge` job, `hubbus bridge`, against
   `assistant.api`, formats vendored under `contracts/vendor/muninn/`), so another assistant needs brunna to learn its
   API.
   Since 11-28 agent-ops-specs has **Principal Handoff Transport 0.1** (experimental, `transport/SPEC.md`, its own
   version line outside 0.6's freeze; "a transport, not a binding in the sense of core §11"): send, fetch from an
   opaque cursor, ack, with an HTTP mapping (an MCP mapping to follow, same semantics); the principal side is the
   server. What it changes for brunna's bridge when it moves to it: fetch returns every origin's records, brunna's own
   included (`incoming_errors` refuses a non-principal origin today; it must skip its own instead); a cursor is
   compared only for equality, and an `unknown_cursor` after the log is restored means fetching from the start and
   resending its own records the log lacks; HTTPS, or a Unix socket on the same machine (plain HTTP on loopback does
   not conform); a relay credential may stand for several projects' parties. Muninn moves to it too. The Program
   binding (above) is the second. brunna keeps the current path until the
   bridge and Muninn both speak Transport; the bridge is the mailbox's code.

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

## Decided by the owner (11-28), and open

- Installed services follow release tags automatically (set up beside, replaced only if setup passed, a moved tag
  refused); one whose manifest asks for more than was granted waits for the owner.
- The first public service is the owner's public review service alone; the compute project stays the owner's own
  (kind 1) until the owner decides otherwise.
- Asked of agent-ops-specs (11-28, the owner's go): both written. Transport 0.1 (experimental, pushed) and the Program
  binding (a 0.7 proposal, not pushed yet). Open: when the bridge and Muninn move to Transport, and telling Muninn
  what both ask of a principal side.
