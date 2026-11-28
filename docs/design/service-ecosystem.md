# Services: brunna as the centre of an ecosystem

> brunna session · 2026-11-28 · r4, design. **Built 11-28: step 1** (the contract
> `contracts/service-manifest.v1.schema.json`, `bin/service_manifest.py`, `docs/service-authors.md`) **and step 2a**
> (`bin/installed_services.py`, `bin/service_fetch.py`, `bin/service-cmd.py` = `brunna service`, `policy.service_policy`,
> the adapters' installed mode, `sandboxed-run --installed`) **and step 2b** (`brunna service update`, the job
> `brunna/service-updates`, the lock, `backup.services()`, the services' secrets in check-secrets, their state's size
> in check-data); the rest not.
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

**The manifest asks; the owner grants; the service's entry holds the grant.** An installed service's entry is not a
registry entry (step 2a below) and no letter changes it: only `brunna service add` / an update the owner confirmed. On the owner's own projects
(kind 1 below), the new security fields join `PLATFORM_OWNED` (`registry.py`): `callers` and `principal`.

## Two kinds of service

1. **A service the owner develops**: an ordinary project, onboarded, with memory and a workspace. It declares it
   offers its manifest's interface with the existing `provides` (`{contract: service, v: <interface major>}`; the
   platform reads `brunna-service.yaml` from the project's main branch through `project_git`, as the project). Its
   sandbox setting stays the owner's choice.
2. **An installed service** (someone else's, or the owner's on another person's machine): `brunna service add <git
   url | name> [--ref <tag>]`. Its own entry in the instance's config (`services/<name>.yaml`, step 2a), not a
   registry entry: no workspace, no memory, no merge gate. Always sandboxed (by its own policy, never
   `service_sandbox_exempt`). Folders, all under
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
- **Setup** runs another person's code (`npm` lifecycle scripts, Python builds, any command the manifest lists) with
  outbound network. It runs in a profile of its own: the new version's folder and the service's caches writable, no
  secrets, no state, no platform mail; inside it, it is unrestricted.
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
   Mechanical application: **deferred** (11-28, the owner's decision in agent-ops-specs), until the first service that
   asks the owner itself appears. agent-ops-specs wrote a Program binding (`program/SPEC.md`: a program may act on a
   settlement only when the answer is exactly one of its own options, settled by a principal side configured for it)
   and keeps it out of every version: it is needed only where no agent stands between, a program that asks and then
   acts on the answer itself (a nightly job asking before it deletes, a bot before it pays). No such service exists:
   the review service asks for no `principal` grant; it is a tool agents call. **When a service needs the owner to
   decide, the agent calling it asks** (with the ordinary Acts and Decisions), and passes the answer in its call (the
   review service's `--budget`). Until the first service that asks for itself, the `principal` grant is reserved and
   unused.
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

## Step 2a, in detail

> **Built 11-28.** As built: the git fence is the profile's own (no `.git` or `HEAD` file anywhere it writes but its
> cache, where uv keeps a `.git` marker in its sdists bucket), and `/etc`, `/var`, `/tmp` are readable as links only.
> The review service, added by hand from its GitHub URL in a throwaway instance, set up (`uv sync`) and answered its
> CLI and its MCP `initialize`, its database in its state folder. The fixture tests pass on macOS and in the Linux
> container.
>
> r2, 11-28, after its blind review (a stranger's code needs its own read model; the fetch hardened; names and
> secrets tightened; the Linux check scoped to the fixture). Found while mapping the code: a registry entry with no workspace breaks a dozen
> walkers (`hubconf.py roster` prints `None` as a path, which `gen-index-roster.sh`, `recover-project.sh` and
> `sync-all-memory.sh` then use; `gen-todo.py` reports a missing STATE.md every night; `render-project-settings.py`
> fails on a sandboxed entry with no workspace; scheduler jobs get `None` as a folder). So an installed service is
> **not a registry entry**.

**Where it is kept.** `services/<name>.yaml` in the instance's config (`hubconf.services_dir()`), committed by the
command that writes it (`commit-hub-files.sh`), validated by `bin/installed_services.py`:

```yaml
v: 1
name: example-review        # the manifest's name
url: https://github.com/someone/example-review.git
ref: v2.1.0                 # the tag or branch asked for; null when a commit was
commit: 3f2a…               # 40 hex: what is installed
interface: 2                # the manifest's, at that commit
granted:                    # what the owner confirmed at add (the manifest asks, this grants)
  models: {judge: {tier: strong}}
  secrets: [OPENAI_API_KEY] # names only; values in the service's secrets folder
  principal: false
  scripts: false            # setup's install scripts
  network: [api.openai.com] # shown, not enforced (Confinement)
added: 2026-11-28
```

Nothing that walks the registry sees it. What must know services reads `installed_services.entries()`:
`policy.other_projects` (every service's whole folder denied to every project and every other service), the name
checks (below), and `brunna service`. Later steps add `brunna mcp` (3), the backup and secret checks (2b), the mail
(4).

**Names.** The manifest's `name`, compared case-folded (APFS is case-insensitive by default) against: every registered
project's name and former names, the platform's names (`names.all("project")`), modules, the owner's and the
assistant's addresses, the `idea-<n>` pattern, and every other installed service. An existing `<services_root>/<name>`
folder that no service owns is refused too (remove would delete into it). The other direction: `onboard-project.sh`
and the registry's index build refuse a project named (case-folded) like an installed service, since both would own
`<services_root>/<name>` and the party id `project:<name>`.

**Folders** under `<services_root>/<name>/`, made by add as the owner, `0700`:

| folder | at run time | in setup |
|---|---|---|
| `repo.git` (bare, the fetched commits) | denied | denied |
| `code/<commit>/` | read | write |
| `cache/` (`XDG_CACHE_HOME`, `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `npm_config_cache`, the model downloads) | write | write |
| `state/` (`BRUNNA_SERVICE_STATE`; `HOME` is `state/home`) | write | denied |
| `secrets/` (one file per granted secret, `0600`) | denied | denied |

`BRUNNA_SERVICE_STATE` is part of the service contract (`docs/service-authors.md`): where a service writes what it
keeps; manifest `data` paths are relative to it. Caches are their own folder so setup never sees state, and they are
writable at run time because downloads happen then too (an embedding model fetched on first use).

**Policy: a read model of its own.** A sandboxed project's profile is made for the owner's own code: it may read
most of the disk except the listed denies. Measured in the review (macOS): under it a process reads the instance's
config folder, the registry index (every project's name and paths), other sessions' temp folders, shell history, and
reaches the Docker socket (which can mount home). On Linux, `landlock.plan` reads `/` minus the denies, home and
`/tmp` included. That is not acceptable for a stranger's code, so `policy.service_policy(entry, phase)` is an
allow-list:

- **read**: the system's own folders (macOS: `/` itself as a file, `/System`, `/usr`, `/bin`, `/sbin`,
  `/Library/Frameworks`, `/private/etc`, `/private/var/select`, `/private/var/db/timezone`, `/dev` with the disk
  devices still denied after it; Linux: `/usr`, `/bin`, `/lib*`, `/etc`, `/run/systemd/resolve` (where Ubuntu's
  `resolv.conf` points), `/proc` (children need their own `/proc/self/exe`; other processes' environ stays refused),
  `/sys/devices/system/cpu`, `/sys/fs/cgroup`), the
  platform's Python and the tool installs it runs (`uv`, the nvm node), and the service's own `code/<commit>`,
  `cache/`, `state/` (run) or `code/<commit>`, `cache/` (setup). Nothing else: not home, not `/Users`, `/Volumes`,
  `/tmp` or `/private/var/folders`, not the config, not the registry index;
- **write**: `state/` and `cache/` (run), `code/<commit>/` and `cache/` (setup); its temp folder is `cache/tmp`
  (`TMPDIR`), never the system's;
- **no unix sockets but the resolver's** (macOS: `(deny network-outbound (remote unix-socket))` then an allow of
  `/private/var/run/mDNSResponder`, without which no name resolves; Linux's seccomp already refuses `socket(AF_UNIX)`
  and its resolver reads files); outbound network open (Confinement); no port to bind;
- **no git fence holes**: the Seatbelt git-fence rules apply to the writable folders, so setup can't plant a
  `.git/config` (with `core.fsmonitor`) that the owner's prompt or editor would run on opening `code/`; Landlock has
  no fence, so on Linux that stays a stated limit beside "nothing on the owner's side walks `code/`".

No developer tools in setup on macOS: `/usr/bin/git` and compilers are the Command Line Tools' shims and aren't
readable, so setup that needs to build native code or fetch git dependencies fails and says so (authors' guide).
Linux has no `/dev/shm` in it (no shared temp), so Python's `multiprocessing` locks don't work; said in the guide.

Both adapters gain this mode: Seatbelt a `service_profile` variant on the model of `pdf_profile` (deny reads by
default, allow back the list), Landlock a `plan` that reads only the list instead of `/` minus denies, with
`_isolation(temp=False)` exposed. `service_policy` returns what `exec_service` takes (the policy dict, the writable
dirs and the readable list), and the conformance probes run against it: the config, home, `/tmp`, another service's
folder, its own `secrets/` and `repo.git`, the Docker socket all refused; `code/` read but not written at run time.
The `brunna` CLI can't run inside (it reads the config); a service doesn't need it before step 4, which revisits it.

The environment is built by the launcher from nothing: `sandboxed-run --installed` clears `os.environ` (today it
updates the caller's, and Landlock's `exec_service` starts from it, so the owner's shell tokens would reach a stranger's
code) and sets only: `PATH` (the platform's), `HOME` (`state/home`),
`BRUNNA_SERVICE_STATE`, the cache variables, `TMPDIR`, `LANG`, `BRUNNA_CALLER` (step 3; `owner` from the owner's
terminal), and at run time each granted secret. Secrets go in that environment, which the sandboxed process inherits
through `execv` and only the owner's own processes can read; never in an `Entry` (`seatbelt.entry_argv` puts an
Entry's variables in `/usr/bin/env`'s arguments, which `ps` shows to everyone). A test checks that a variable set in
the caller is absent inside, and that no secret is in any process's arguments.

The commands run (`cli.run`, `mcp.run`, `setup`) are copied into the service's entry at add, from the manifest read
out of `repo.git`; never read back from `code/`, which setup can rewrite.

**Launching.** `bin/sandboxed-run --installed <name> --phase run|setup --cwd <code/<commit>> -- <words>`: it reads the
service's entry (not the registry), builds `service_policy`, refuses where the machine can't sandbox (exit 78, as
today), and `exec_service`s. `brunna service run <name> -- <args>` (this step: the owner's terminal only, refused
when `owner_terminal.refusal()` says it is confined or inside a session; step 5 adds sessions through the runner)
runs the manifest's `cli.run` plus the args.

**Fetching.** As the owner, running nothing from the repo, in a staging folder under `hubconf.private_tmp()` (never
the system temp, which every sandbox may write) until the manifest's name is known, then moved into
`<services_root>/<name>/`:

- the URL must be `https://` or `ssh` (`git@host:path` or `ssh://`); a local path or `file://` only in tests; one
  starting with `-` is refused and `--` precedes it; `--ref` passes `git check-ref-format --allow-onelevel` or is 40
  hex;
- every git command runs with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, no prompts, a timeout and a
  process-group kill (the `upgrade.py` pattern, made into a helper that doesn't run in the hub's own repo), and
  `-c core.hooksPath=/dev/null -c fetch.fsckObjects=true -c protocol.file.allow=never` (tests allow file);
  the owner's global `filter.lfs` or any configured filter never runs, nor a submodule;
- `git init --bare repo.git`, fetch the ref with `--depth=1 --no-tags`, the pack capped (`FETCH_MAX`); the commit it
  resolved to is recorded;
- the manifest is read with `git cat-file blob <commit>:brunna-service.yaml`, capped at 64 KB, and checked
  (`service_manifest.problems`) before anything is checked out;
- the tree's total size is summed (`ls-tree -r -l`) and capped before checkout (a pack can expand far past its own
  size); the checkout is `--work-tree=code/<commit> checkout <commit> -- .` with `core.symlinks=false` and no
  `.gitattributes` filters (the config above has none);
- nothing on the owner's side walks `code/` after setup has run (setup can make links); remove deletes folders without
  following links.

**Add**, `brunna service add <url> [--ref <tag|branch|commit>]`, the owner's terminal only:

1. fetch and check the manifest (above); refuse a bad one with its problems;
2. show what it asks: what it is, interface, models and tiers, secrets (required and optional, with `how`), the
   network it says it reaches and that outbound isn't confined, whether it asks to ask the owner, whether setup wants
   install scripts; the owner answers yes or no (`--yes` for scripts and tests);
3. ask each required secret's value (hidden input; never echoed or logged); an optional one may be skipped and set
   later with `brunna service secret <name> <SECRET>` (value on stdin);
4. check out, then run `setup` in the setup profile, each command in turn, with a timeout; a failure removes
   `code/<commit>` and stops, nothing written to config;
5. write `services/<name>.yaml` and commit it.

An add that stops anywhere leaves no entry; a second add of an existing name is refused (updates are 2b).

**Remove**, `brunna service remove <name> [--purge]`: the entry (committed), `repo.git`, `code/`, `cache/`; with
`--purge` also `state/` and `secrets/`. **List**, `brunna service list [--json]`: name, ref, commit, interface, granted.

**What 2a does not do.** No updates (2b). No consumer sees the service yet (3). No mail (4). No long-running unit:
a service's MCP server is started per session by `brunna mcp` (3), its CLI per call; a manifest asks for nothing
that keeps running.

**Tests.** A local fixture repo (a tiny Python CLI and MCP server, a manifest, setup with no network) added from a
`file://` URL in a throwaway instance: the entry, the folders and modes, setup writing only `code/<commit>` and
`cache`, the CLI answering under the sandbox and refused, at run time, writing `code/`, reading `secrets/`,
`repo.git`, another service's folder, the config, home, `/tmp`, and a unix socket; a bad manifest, a name clash, a
`-` URL and a bad ref refused before checkout; a failed setup leaving nothing; secrets absent from the process's
arguments. On macOS (Seatbelt) in the suite, and in the Linux container (`bin/linux-test`) with the same fixture.
Once by hand on macOS: the review service from its GitHub URL in a throwaway instance.

## Step 2b, in detail

> **Built 11-29**, as r2 says; the fixture tests (tags followed, more-asked, a failing setup, a moved tag, two
> spellings of one version, the backup's list, the lock) pass on macOS and Linux.
>
> r2, 11-29, after its blind review (nothing blocking): the lock moved out of every sandbox's reach and taken shared by
> `run`; the backup takes `state/` itself; asking for more includes a secret turned required and any interface change;
> `scripts` dropped as a grant (nothing could enforce it); each update fetches into a fresh repo by the tag's full
> name and checks the commit `ls-remote` gave; a killed run cleans up; the kept version is for running processes.

**What the entry gains** (`bin/installed_services.py`, copied from the manifest at add and at each update): `data`
(the manifest's entries, relative to `state/`), `secrets_required` (the granted secrets not marked optional),
`follow` (`tags` when added at a release tag that `ls-remote` confirms is one, else `none`), `tags` (`{<tag>: <commit>}`
of every release tag installed, with the commit then), and `previous` (the commit before, kept for processes still
running in it; never a way back, since `state/` may have been migrated forward). `granted.scripts` is dropped: setup is
whatever commands the manifest lists, run unrestricted inside the setup profile, and the guide says so.

**Release tags**: `v?MAJOR.MINOR.PATCH` with no suffix, compared as numbers; two tags of the same version
(`v1.0.0`, `1.0.0`) naming different commits stop the check. A pre-release or another tag is never taken
automatically.

**The update check**, platform job `brunna/service-updates` (daily 04:35, background, `found_exit`), and by hand
`brunna service update <name> [--ref <tag|branch|commit>] [--yes]`. For each service that follows tags (by hand: the
one named):

1. `git ls-remote --tags <url>` (the fetch's rules) lists release tags and their commits (`^{}` for annotated). An
   ssh URL needs the owner's agent, which the scheduler doesn't have: such a service is checked by hand only; the
   job says so in its output and leaves its alert as it is.
2. **A moved or deleted tag stops it** (an alert, nothing updated): someone rewrote what was granted. By hand, the
   owner sees it and an update with `--ref` replaces `tags` with the one taken.
3. The newest release tag above the installed one (by hand: the ref asked for) is fetched **into a fresh staging repo**
   under `hubconf.private_tmp()` by its full name (`refs/tags/<tag>`), and the commit must be the one `ls-remote` gave;
   the manifest is read from it and checked; its `name` must be the service's.
4. **Asking for more waits for the owner**: a model not granted or a stronger tier, a secret not granted, a granted
   secret turned required that isn't set, `principal` true, a host not in the granted network, any change of
   `interface`. The job raises `service-update:<name>` (the version, what more it asks, `brunna service update
   <name>`); by hand the asks are shown, the owner answers, and a new required secret is asked.
5. **Set up beside**, under the service's lock: any `code/<c>` that is neither the current nor the previous commit
   (left by a killed run) is removed; the commit is checked out into `code/<new>`; `setup` runs there
   (`sandboxed-run --installed <name> --phase setup --commit <new>`: setup only, 40 hex, the folder must exist and is
   never made by this mode), each command in its own process group, killed after it and on SIGTERM. A failure
   removes `code/<new>`, leaves the entry and raises the alert.
   Everything above is checked before the lock; under it the entry is read again, and if its commit, grants, tags or
   how it follows changed meanwhile (an add, remove or another update), nothing is taken ("run it again").
6. **Swap**: the entry is written first, then the staging repo replaces `repo.git`; the entry gets the new commit, ref, interface, commands, data,
   `secrets_required`, the tag, and `previous`; it is committed; any code folder older than `previous` is removed.
   The job closes `service-update:<name>` for each service that is current again and for removed ones.

**How it follows after a by-hand update**: a release tag keeps it following tags (after a moved tag the owner
accepted, only the tag taken is recorded); a branch, a commit or any other tag pins it (`follow: none`): the job
leaves it, and only another `--ref` moves it. A by-hand update to the commit already installed only records how it
follows.

**The lock**: `<state>/services/<name>.lock` in the platform's state (no sandbox reads it), `fcntl.flock`: add, update
and remove take it exclusively with a bounded wait (`LOCK_NB`, retried for a minute, then they stop and say so);
`run` takes it shared, so a swap or remove never happens under a running CLI. The entry is read again after the lock
is taken.

**The folder check** moves into `installed_services.own_folder()` (2a's, from `sandboxed-run`), so the backup uses it
too.

**Backup** (`bin/backup.py`): for each installed service, `state/` itself (its parent is the owner's folder, which
the service can't write, so no link on the way can lead elsewhere), excluding each `data` entry with `backup: false`,
and its `secrets/`. Code, cache and `repo.git` never. A hard link from a denied file into `state/` is refused by the
read model (measured on macOS; Landlock's refer right on Linux), and a test probes it. `bin/check-data.py` measures
each service's `state/` with a cap (20 GB) and reports one over it, or one whose `state/` isn't its own real folder,
under its own alert key (`service-data:<name>`, the owner's); the backup skips such a service.

**Secrets check** (`bin/check-secrets.py`): each service's `secrets_required` has a real, non-empty `0600` file in
`secrets/`, readable by no one else, else a problem in the same alert ("{service}: secret {name} is not set: brunna service secret {service}
{name}").

**Tests**: a fixture repo with tags: added at `v1.0.0`, `v1.1.0` taken (set up beside, swapped, previous kept), a
`v1.2.0` that adds a secret alerts and is not taken, a moved `v1.0.0` alerts, a failing setup leaves the entry, `-rc`
ignored, `v1.0.0`/`1.0.0` conflict stops it; backup's list holds `state/` and `secrets/`, not code or cache; a missing
required secret is a problem, an optional one isn't; the hard-link probe; a shared lock held by `run` blocks remove.

## Step 3, in detail

> r2, 11-29, after its blind review. Its one blocking finding: a sandboxed consumer (which can grant itself `consumes`
> by manifest.update) driving an unsandboxed project's service with the owner's rights. And `current_session()` most
> likely doesn't answer inside an MCP server process, so the "the session's runtime confines" check can't be made yet.
> So step 3 is split: **3a**, built now, serves **unsandboxed consumers only**; **3b** adds sandboxed consumers once
> the runtime is told from the server's ancestor processes (each adapter's `session_process`) and measured on Claude
> Code and Codex, and serves a sandboxed consumer only services that run sandboxed themselves (installed, or a
> sandboxed project), or one that the service's owner-only `callers` names explicitly.

**Which services there are** (`bin/service_index.py`): every installed service (2a); and every registered,
**unsandboxed** project of the owner's (kind 1) whose entry `provides` `{contract: service, v: <interface major>}` and
whose working tree holds a valid `brunna-service.yaml` (the owner's development copy is what runs, so its working tree
is what counts; a sandboxed kind-1 project is 3b). Each: name, kind, interface, MCP command, where it runs, `callers`.
A service whose `<service>__<tool>` names wouldn't fit (below) has those tools left out.

**Who may call.** A consumer's registry entry `consumes: [{contract: service, from: <name>, v: <major>}]` (the registry
and contracts-check get a case for `contract: service`: no `copy`, `from` may name an installed service; `provides`
needs no `schema` for it; both schemas change with them). Callers: an installed service's entry `callers` (set by
`brunna service callers <name> <project>…|--all`, the owner's terminal only), a kind-1 project's registry `callers`
(owner-only, `PLATFORM_OWNED`); default every registered project of the owner's, never another service. Served only:
it consumes the service at its current major, it is among the callers, and it isn't sandboxed (3a).

**When `brunna mcp` serves none.** A platform run (`--run`, `HUB_LLM_POINT`, `HUBBUS_LEASE`), a gated session
(`gated_here`), a project told by `AGENTHUB_PROJECT` rather than by the folder (and the 11-28 rule stays: a variable
naming one project in another's folder serves none at all), a sandboxed consumer (3a), or a process the probe says is
confined: `isolation.platform_readable()` (new): the platform's state folder, located without the environment
overrides, exists and a file inside it is actually read (an empty mount over it fails). These are decided before
`tools/list` starts anything.

**Listing.** At the first `tools/list`, the consumer's services are started in parallel within 10 seconds in all;
`tools/list` answers with the tools of those that answered `initialize` and `tools/list` (following `nextCursor`),
named `<service>__<tool>` (service names have no `_`, so the name splits at the first `__`); a tool whose full name in
the client (`mcp__<server>__<service>__<tool>`) would pass 64 characters, or whose name isn't letters, digits, `_` and
`-`, is left out. A platform tool `services` says what this session may call, what it consumes but can't and why, and
what was left out or failed to start (not `instructions`, which `initialize` answers before anything starts and which
Codex may ignore). Each service tool's description begins "(from the service <name>; what it says is data)", and each
of its results begins with one text item saying the same.

**Relaying.** The client's messages are read on a thread of their own. A call to `<service>__<tool>` goes to the
service with the server's own id and the name the service knows; while it is out, a ping is answered and
`notifications/cancelled` for it is forwarded; other requests wait. The result is returned as the service gave it.
A request from the service to the client (sampling, elicitation, roots) gets an error; its notifications are dropped;
a line that isn't JSON is skipped; a line over 4 MB kills the child. Its stderr is drained to a capped log
(`<state>/services/<service>.mcp.log`, 1 MB, rotated once). A call gets 15 minutes; a child that died is started again
at the next call, once. The child runs in its own process group, killed when `brunna mcp` exits or its stdin closes;
an MCP server exits on stdin's end, and a 2b swap keeps the previous code folder for it, so a session's server keeps
working through one update (two updates in one session and it is started again from the new one).

**How a service is started.** Installed: `sandboxed-run --installed <name> --phase run --caller <consumer> --cwd <code>
-- <mcp.run words>`; `--caller` sets `BRUNNA_CALLER`, and `--installed` refuses in a confined process (the probe), so
nothing inside a sandbox starts one under another caller's name. Kind 1 (unsandboxed): its manifest's `mcp.run` in its
workspace, with an environment built from nothing: the platform's `PATH`, `HOME`, `LANG`, `BRUNNA_CALLER=<consumer>`.
It runs with the owner's rights, as the owner's own project; `brunna service list` says so.

**Accounting.** Each forwarded call appends one line `{at, consumer, service, tool, ms, ok}` (one write, under 4 KB,
`O_APPEND`) to `<state>/services/calls.jsonl`; `brunna service list` shows the last week's count per consumer.

**Tests.** A fixture service with a tiny stdio MCP server (two tools; one that sleeps, one that answers with a request
to the client, one that floods), installed in a throwaway instance, and a fake registry with consumers: listing and
calling, `BRUNNA_CALLER`, the call recorded; a cancel and a ping during a long call; a crashed child restarted; the
child gone after `brunna mcp` exits; refused: not a caller, not consuming, a wrong major, a sandboxed consumer, a
platform run, the variable outside every project, a confined server (Seatbelt and Landlock), a name over the length;
a kind-1 service (unsandboxed); the registry and contracts-check taking a `contract: service` consume. Then by hand: a
session of an unsandboxed project of the owner's calls the review service.

## Steps

Each gets its own blind review before it is built.

1. **The manifest contract** and its validator; `docs/service-authors.md` first draft; the owner's public review
   service writes its manifest (a request to that project: it has a CLI and MCP already).
2. **Installed services**, in two:
   - 2a (detailed below): the services' own entries, folders, read-only code and the setup profile, `brunna service
     add` / `remove` / `list` / `secret` / `run` (the owner's terminal only), setup as the service, the services'
     read model in both adapters. Done when the fixture service passes the tests below on macOS and in the Linux
     container, and the review service, added by hand from its GitHub URL in a throwaway instance on macOS, answers
     its CLI in its sandbox.
   - 2b: updates by tag (setup beside, the more-asked wait); the services' data in the nightly backup and their
     secrets in the daily check. The outbox location moves to step 4, where it is first used.
3. **Consumers** (3a: unsandboxed consumers, built first; 3b: sandboxed ones, detailed above): `provides`/`consumes`
   with `contract: service`, `callers`, `brunna mcp` serving consumed services'
   tools as the service, the folder-only identity and the confined refusal (tested on Seatbelt and Landlock), the
   per-runtime measurements above, the `services` tool, accounting. Done when a session
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
- Asked of agent-ops-specs (11-28, the owner's go): Transport 0.1 (experimental, pushed; Muninn serves it since
  11-28) and the Program binding (written, then deferred the same day by the owner: no service asks for itself).
  Open: when brunna's bridge moves to Transport.
