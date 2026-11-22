# Extra capabilities for idea sandboxes + declarations independent of the runtime

> **Status**: on 10-25 the owner confirmed A–F as proposed; implemented and verified the same day (see "10-25
> implementation and verification" at the end).

Source: the assistant's request `m10251437-1f92` (10-25 14:37). The direction the owner agreed: the platform maintains
a fixed set of "capabilities", projects declare which they use, and sessions in a sandbox run things under the
corresponding limits through one platform command. The first capability is `gpu`.
The owner also specified on 10-25: **capabilities in the registry are declared in runtime-independent vocabulary, with
the Claude-specific translation gathered in one place**.

The draft and live tests were done by another agent-hub session ("testing"), handed over on 10-25 to the session of
this design, which from then on owns the design, implementation and commits. The profile prototype and the two escape
tests are copied in `.state/cap-prototype/` (not in version control).

## Why

`idea-1` (rebuilding a room in 3D from photos) needs Blender to render. On the Mac, Blender only supports Metal, and
Claude Code's sandbox blocks it from detecting the GPU, so it crashes at start; today the owner has to run it himself
in a terminal. Claude Code's sandbox has no setting to open GPU/IOKit/Metal, and no custom Seatbelt profile.

What "no hands-on work from the owner" means (confirmed by the owner in the "testing" session on 10-25): **he does not
have to operate Blender himself**. Confirmation on the phone stays as it is: in work mode `hub-run …` is a Bash
command, sent to the phone every time for him to allow. That human check does not change.

## 10-25 live tests (the "testing" session; all in temporary directories or test sandboxes, deleted afterwards)

1. **The platform's own macOS sandbox rules (Seatbelt) let Blender use the GPU**: the same render script, unrestricted
   and under the `gpu` rules, produced two preview images, a `.blend` and a `.glb` in about 1 second both times.
2. **The same rules block 16 escapes**: writing outside the folder, reading `~/.ssh`, reading claude credentials,
   reading another project, reading a service's `.env`, reading platform secrets, **the Keychain**, network
   connections (including 127.0.0.1), `open` launching an app, `osascript` sending Apple Events, `launchctl`,
   `crontab`; writing/reading its own folder and the temporary area work. The Keychain is something Claude Code's
   sandbox cannot block (known-issues.md); the platform's rules can (blocking `securityd`'s mach-lookup directly).
   `open` and `osascript` were allowed at first (Calculator really was launched outside the sandbox); only adding
   `(deny lsopen)`, `(deny appleevent-send)` and a process-exec deny blocked them.
3. **Sandboxes cannot be stacked**: running `sandbox-exec` inside Claude Code's sandbox always gives
   `sandbox_apply: Operation not permitted`. So the step that applies the platform's rules cannot happen inside the
   session's sandbox.
4. **Verified in the real location**: the test folder was put in `~/workspace/<probe>` (the rules first block all of
   `~/workspace`, then allow its own folder, and the two really overlap there). In a temporary directory this overlap
   cannot be tested.
5. **Claude Code's `excludedCommands` works, but only for "standalone" commands**: running the excluded wrapper
   command alone leaves Claude Code's sandbox; chained with other commands (`… && …`, `…; …`), the whole chain stays in
   the sandbox, and escapes chained after it are blocked. The result is the same with or without the lease's
   `--settings`.

## Three layers: declaration, translation, enforcement

```
project registry      ── bin/policy.py ──┬─ bin/runtime_claude.py → .claude/settings.local.json, lease --settings
(neutral vocabulary)                     │   (how Claude Code does it; the only place in the platform
  enforce.sandbox                        │    that knows Claude's settings format)
  enforce.capabilities                   │
  network, allow_write                   └─ bin/seatbelt.py → the platform's Seatbelt profile, for cap-runner
                                             (how macOS blocks it; goes through no agent)
```

- **`bin/policy.py` (new)**: computes a project's policy from the registry, in neutral words only:
  `{workspace, write: [paths], deny_write: [paths], deny_read: [paths], credentials: [paths], network: [domains],
  platform_commands: [paths], capabilities: [names]}`. The content is what `render-project-settings.desired()` computes
  today, with Claude's syntax (`Read(...)`, `Edit(...)`, `Bash(... *)`) removed. `credentials` (`~/.ssh`,
  `~/.claude*`, `~/.aws`, `~/.config/gh`) is today written only in `lease-runner.deny_settings()`; it moves here into
  the same policy, with the locations in `hub.json`'s `credentials` (no runtime paths in the code). The Keychain is not
  a file; `seatbelt.py` handles it itself. Paths all come from `hubconf` (the `~/agent-hub` hard-coded in `desired()`
  today is fixed along the way).
- **`bin/runtime_claude.py` (extended)**: its header already says "switching tools changes only this file", but so far
  only mailbox routing follows that. These two move in:
  - `settings(policy)` → the `sandbox` block + the platform's deny/allow rules (the Claude half of today's
    `desired()`), `settings_path(workspace)`, `sandbox_rendered(workspace)` (`lease-runner.work_ready` reads
    `settings.local.json` itself today)
  - `lease_settings(policy)` → the lease's `--settings` file (today's `lease-runner.deny_settings()`, including the
    rules blocking `security`)
  `render-project-settings.py` keeps only the mechanism "write the file, record what was written last, detect drift";
  what to write it asks `runtime_claude`.
- **`bin/seatbelt.py` (new)**: `profile(policy, cap)` → SBPL text. The **read** lists (`deny_read`, `credentials`) use
  the same `policy` as Claude's, not written by hand separately (the prototype's read list was hand-written; that
  changes now). **Writes** deliberately do not follow `policy.write`: only the project folder + the temporary area. Work
  run on a project's behalf has no reason to write the bus directly (that would bypass `hubbus`'s checks) or the shared
  package caches.

**Not moved this time, left to m10251446 (see the last section)**: the lease's `claude` flags, stream-json parsing,
tool names, rate limit, `--resume`, the Claude tool rules in `lease-relay.py`, the tool lists in
`contracts/llm-points.yaml`. Once that is done, knowledge of Claude Code is only in `runtime_claude.py` (translation),
`llm.py` (the only place that runs `claude -p`), plus the transport layer of `lease-relay.py`: it is itself Claude's
`--permission-prompt-tool` (MCP, answering `behavior`/`updatedInput`); its rules will decide by kind, but the process
and the return format stay Claude-specific.

**Boundary test** (`tests/test_runtime_boundary.py`, new): the neutral files touched this time (`policy.py`,
`seatbelt.py`, `cap-runner.py`, `hub-run`, the capability section of `registry.py`) may not contain Claude's words
(`.claude`, `Read(`/`Edit(`/`Bash(`, `claude`). `lease-runner.py` is added once m10251446 is done.

## Registry

```yaml
type: idea
enforce:
  sandbox: true
  capabilities: [gpu]     # new; only capability names the platform has are accepted
network: [...]            # existing, already neutral
allow_write: [...]        # existing
```

- `registry.py` validates: every name in `capabilities` must be in the platform's capability list;
  `enforce.sandbox: true` is required; only `type: idea` is accepted this time.
- **Capability list**: `hub.json`'s `capabilities: {gpu: {risk: low, default_for: [idea], description}}` +
  `capabilities/<name>.sb` (that capability's profile fragment). A new capability later = one more list entry + one
  fragment file, with no change to the entry point or the projects.
- **`gpu` is given to idea sandboxes by default**: `create-idea-sandbox.py` **writes it explicitly** into the registry
  at creation per `default_for` (the registry shows what a project has, rather than it being implied by `type`);
  `idea-1` gets one line added. Capabilities with `risk: high` are never added automatically; the owner agrees and the
  registry is changed in the agent-hub session.

**The registry's security fields can only be changed by the platform** (fixing an existing hole): today
`registry.py apply-updates` only checks that "the sending project changes its own registry". So a session in a
sandbox could send a `manifest.update` that turns `enforce.sandbox` off, or widens `allow_write`/`network`; with
`capabilities` added it could grant itself capabilities; changing `type` to `idea` would get it work mode and
capabilities; removing `stats_only` from `privacy` would put an idea's content into the weekly report's cloud call.
Changed to: if `type`, `enforce`, `allow_write`, `network` or `privacy` differ from the current registry, the whole
update is rejected, with an explanation that the agent-hub session must make the change (the owner confirming). No
project has ever changed these fields through manifest.update (git log shows only one update, from the assistant, not
touching them), so existing flows are unaffected.

## The entry point: `hub-run` (the platform runs it on the project's behalf)

**Two approaches**

- **A. Exclude + wrap (what the assistant originally proposed)**: put `hub-run` into the project's
  `excludedCommands`; once outside Claude Code's sandbox it applies the platform's rules itself with `sandbox-exec`.
  Tested and works (point 5 above). Drawbacks: security relies on Claude Code judging chained commands correctly, and
  if its judgment ever has a gap, the commands chained after it leave the sandbox wholesale; it has to be written into
  Claude's settings, so switching tools means finding the equivalent mechanism; and the escaped `hub-run` cannot
  reliably know which project it belongs to (`cd` into another project and run it, and it can impersonate).
- **B. The platform runs it (proposed)**: `hub-run` stays in the session's sandbox and only writes the request into
  **its own project folder**'s `.hub-run/requests/`; a resident platform service outside the sandbox picks the request
  up, checks it, runs it under the platform's rules only then, and writes the output and exit code back.
  The only requirement on the runtime is "can write its own folder", which any agent can do; not one line has to be
  added to Claude's settings. A is kept as a fallback.

**How a request proves which project sent it**: the sandbox only lets each project write its own folder; other
projects cannot write into it. The service only scans `<workspace>/.hub-run/requests/` of projects that declare
capabilities in the registry, and **the folder a request is found in is the project it belongs to**; the project
folder used for running comes from the registry, not from anything in the request.
(Not in the shared bus directory: every project can write there, so project A could impersonate B.) `.hub-run/` is
written into `.git/info/exclude`.

**Request format**: `{"v": 1, "id", "cap": "gpu", "argv": [...], "cwd": "<relative to the project folder>",
"timeout_s"}`. `argv` is a JSON list; the service directly execs `sandbox-exec -f <profile> -D WS=… -D HOME=… <argv>`,
with no shell string in between.

**Checks when the service receives a request** (any failure writes back `rejected` with the reason, nothing runs):
- `.hub-run/`, `requests/` and the request file itself must all be inside the project folder after resolving to real
  paths, and be regular files/directories: if `.hub-run` is replaced by a symlink to another project, the result files
  would be written there, so this level must be blocked too (the prototype draft only checked `cwd`). Results are
  written back opening files without following symlinks.
- `id` may only be a plain file name; `cwd`, resolved to a real path, must be inside the project folder (a symlink
  inside the folder pointing out is blocked too).
- `cap` must be in this project's `enforce.capabilities` in the registry; the request file is at most 64 KB; requests
  that are too old or already answered are ignored.
- **The profile is not kept in the project folder**: computed from the registry on every run and written to
  `.state/cap-runner/<project>/<id>/profile.sb` (the session cannot write there). The prototype draft put it in
  `<workspace>/.hub-run/profiles/`: a place the session itself can change, where changing one line amounts to no
  sandbox.

**Service**: `local.agent-hub.cap-runner` (`bin/cap-runner.py`, resident, `KeepAlive`, scanning once a second), written
in agent-hub's registry `services:` (like lease-runner, its plist is generated by `render-services.py`, a Discord alert
is sent when it is not running, and `recover-service.sh` can rebuild it), also registered in `services.tsv`. Not put
into lease-runner: restarting the runner must not kill running work.
- Only `PATH`, `HOME`, `TMPDIR`, `LANG` are passed as environment variables, everything else is cleared (no tokens, no
  `HUBBUS_*`).
- Timeout 10 minutes by default, 30 minutes at most (`hub.json`'s `cap_runner`); on timeout the whole process group is
  killed; at most 2 running at once, 1 per project.
- `stdout`, `stderr`, `exit` are written next to the request; each job writes one line to
  `.state/cap-runner/log.jsonl` (project, capability, argv, exit code, time).
- The service not running: if `hub-run` sees no service pick the request up within 30 seconds, it withdraws the request
  (the service restarts itself when updated, and launchd waits 10 seconds), prints that the platform's run service is
  not running, and exits with 125.
- Any start failure answers `rejected` + the reason (a client is never left waiting); unfinished jobs are marked `lost`
  when the service restarts.

**`hub-run` (the client)**: a small standard-library script, `hub-run --cap gpu [--timeout seconds] -- <command…>`. It
prints the job id first, prints output while waiting, and exits with the same exit code. A session's Bash call has its
own timeout (2 minutes by default, 10 at most); what gets killed is `hub-run`, not the job, so `hub-run --wait <id>` can
reattach. Without `--cap` nothing is accepted (a capability is an authorisation; which one must be stated). The request
file is written to a temporary file first and then renamed (the service never reads half-written JSON); if nobody
picks it up in 30 seconds the request is deleted before exiting, so a late-starting service never runs a job nobody is
waiting for.

## What the `gpu` profile really is (stated clearly)

- The rules **allow everything first, then block the dangerous parts** (a deny list): writes only to its own folder and
  the temporary area; no reading `~/workspace` or `~/services` (except its own folder), memory copies, platform
  secrets, credentials, the Keychain; no network at all (**including 127.0.0.1**, so a job cannot reach lease-runner
  or the assistant); the Keychain's system service blocked; "asking another system service to start a program for it"
  blocked (opening apps, Apple Events, `launchctl`, `crontab`, `at`). **Everything not listed is allowed, Metal
  included**.
- So this time the `gpu` fragment is empty: Metal was never on the deny list. Here the capability's role is
  **authorisation** (who may use the platform runner), not opening another hole. If this later becomes "block
  everything by default, open item by item" (finding out one by one what Blender needs; not done this time), the `gpu`
  fragment becomes the Metal/IOKit rules.
- Compared with Claude Code's sandbox: reads are stricter (all of `~/workspace` and `~/services` blocked), network is
  stricter (fully closed), and it additionally blocks the Keychain and launching programs; writes lose the bus
  directory and package caches, but gain **the whole system temporary area** (`/private/tmp`, `/private/var/folders`,
  where other programs' temporary files are too). Accepted for the trial; if a capability needs tightening, each job
  gets its own `.hub-run/tmp` as `TMPDIR` and the shared temporary area is removed.
- Blocking "looking at other processes' information, sending signals to other processes" will be added too, tested
  live first.
- **The real check is still the phone confirmation**: the rules are a reinforcement; tested to block what is listed
  above; nothing beyond that is guaranteed.

## Leases and the assistant

- **The lease contract does not change**: the command shape is the one the assistant proposed
  (`hub-run --cap gpu -- <command>`), and in work mode it is just a Bash command that needs confirming.
- When the project declares capabilities, work mode's system prompt gets one line: "run programs that need the GPU
  with `<hub>/bin/hub-run --cap gpu -- …` (reattach long jobs with `--wait <id>`)", with the path built from `hubconf`
  (the lease's PATH does not include agent-hub's bin).
- Interactive sessions opened over SSH can use it too (their sandbox also lets them write their own folder).
- The phone confirmation is not loosened. The platform profile does block the Keychain, so "`hub-run` without
  confirmation" can be discussed again later, but it is the owner's decision, not part of this.

## Decisions for you to make

**A. Have the platform run it (B), not excludedCommands (A)** (proposed): reasons above; A tested and works, kept as a
fallback.

**B. `gpu` written explicitly into the registry at creation** (proposed): not implied by `type: idea`; `idea-1` gets
one line `capabilities: [gpu]`.

**C. `type`, `enforce`, `allow_write`, `network`, `privacy` can only be changed by the platform** (proposed): a
`manifest.update` touching them is rejected. This hole exists today, not only for capabilities.

**D. Where the translation layer sits and its scope** (proposed): the neutral `policy.py`; Claude's translation in
`runtime_claude.py`; the platform profile in `seatbelt.py`. This time only "how the settings are written" moves; the
lease's flags, events and tool names are left to m10251446.

**E. The `gpu` profile uses "allow first, then block" during the trial** (proposed): including the known looseness of
the shared temporary area, listed clearly above.

**F. m10251446 handled separately** (proposed): see the next section.

## Order (one commit per step, adding only its own files)

1. `policy.py` + `runtime_claude.settings()`/`lease_settings()`; `render-project-settings.py` and `lease-runner.py`
   switch to them. **Behaviour unchanged**: before and after, the settings computed for every project are byte for
   byte the same as `.state/rendered-settings/`, and the lease's `--settings` too. Add the boundary test.
2. Registry: `enforce.capabilities`, the capability list, `apply-updates` blocking security fields;
   `contracts/project.v1.schema.json` gains the fields (adding fields needs no version bump).
3. `seatbelt.py` + `capabilities/gpu.sb` + `tests/test_cap_profile.py` (the prototype's 16 items; the test folder is
   created under `workspace_root` so the overlap is tested, skipped when not on the hub machine; if the `open` item is
   not blocked it really launches Calculator, so teardown must close it).
4. `cap-runner.py` + `hub-run` + plist + `services.tsv` + a heartbeat-check job + tests (requests in another project's
   folder, `.hub-run` as a symlink, `cwd` escaping, undeclared capability, timeout: all rejected or killed).
5. `create-idea-sandbox.py` writes capabilities per `default_for`; one line added to `idea-1`'s registry; one line added
   to work mode's system prompt.
6. A real run: in `idea-1`, in a session opened with its real settings, run
   `hub-run --cap gpu -- blender -b … -P tools/blender_render.py …` and get images. Add "Sandboxes cannot be stacked"
   and "`excludedCommands` only works for a command on its own" to `known-issues.md`.
7. Reply to `muninn` (`m10251437-1f92`): what was done, how to use the command, how it differs from the proposal (the
   platform runs it instead of excluding it), then `hubbus done`.

## A separate letter: m10251446 (contract independent of the runtime)

On 10-25 at 14:46 the assistant separately asked that `contracts/lease-runner.v1.schema.json` stop using Claude Code's
vocabulary: tool names in events become generic categories
(`kind: ask_user|read|search|write_file|run_command|open_web|other` + `summary`), questions use a generic structure,
rate limit becomes `windows: [{id, label, used, resets_at}]`, `/terminal` returns `{command, hint}`, error code `auth`
becomes `agent_login`.

It is the same principle as this document, and its translation table goes into the same `runtime_claude.py` (tool
names → kind, stream-json → neutral events, AskUserQuestion ↔ generic questions, rate limit → windows, the resume
command, error classification; `lease-relay.py`'s rules decide by kind). But it needs a parallel-running deadline agreed
with the assistant and involves the `permission-requests` contract the assistant owns, so **it gets a separate
design**, not confirmed together with this one; it starts after this one is done.

## 10-25 implementation and verification

Commits: `2568776` (policy.py, Claude settings translation moved into runtime_claude) → `c3583ea` (registry
capabilities, locking security fields) → `61ff364` (seatbelt.py) → `92b2898` (cap-runner, hub-run) → `c6eb20f`
(default gpu, idea-1, prompt) → `fa95f4a` (start failures must answer, known-issues).

- **Behaviour unchanged**: before and after the move, the Claude settings computed for 24 projects and the lease's
  `--settings` are byte for byte the same; `render-project-settings.py --report` shows no drift.
- **Where it differs from the design**: the service became a registry service (the "Service" section above), with no
  hand-written plist and no separate heartbeat job; the capability-list lookup is in `hubconf.capabilities()`
  (standard library, so the sandbox-creation script can use it too); the git exclude for `.hub-run/` is written by
  cap-runner the first time it sees a request.
- **Found in live tests, fixed**: `(deny signal (target others))` did not block sending signals to other processes;
  changed to "block everything, then allow same-sandbox"; the resident cap-runner was not restarted after
  `hubconf.py` changed, and mixing old and new modules left one job stuck in queued: now every start failure answers
  rejected, and known-issues.md records it ("A long-running service runs the code loaded when it started").
- **Tests**: `test_cap_profile.py` (real sandbox-exec, under workspace_root; files, Keychain, network including local
  and unix sockets, opening apps, Apple Events, launchctl, crontab, signals), `test_cap_runner.py` (14: the various
  rejections, symlinked .hub-run/jobs/requests, timeout killing the whole process group, queueing, restart, the
  client), `test_registry_capabilities.py`, `test_runtime_boundary.py`. All 114 tests pass.
- **A real run**: 10-25 15:26 in idea-1, in a Claude Code session opened with its real sandbox settings (`claude -p`,
  only hub-run allowed), ran
  `hub-run --cap gpu -- blender -b --factory-startup -P tools/blender_render.py -- results/scene-v2.json <output>`:
  2.1 seconds, exit 0, two pngs, .blend, .glb (only Blender writing thumbnails to `~/.thumbnails` was blocked, no
  effect).
- **Not verified yet**: no real lease has run since lease-runner restarted; the extra line work mode adds to the prompt
  has only been seen in `preamble()`'s output, never walked through from the phone.
- Reply to `muninn` `m10251527-a8e0`, `m10251437-1f92` closed.

## 10-31: a sandboxed project's own services and jobs run sandboxed too

The hole: a project's `manifest.update` adds `services:` and `jobs:` without the owner's approval (they are not security
fields), and both ran outside any sandbox (launchd, the scheduler's shell). A sandboxed project could declare a service and
run anything with the owner's full rights. The owner chose to run them sandboxed (instead of making them owner-approved
fields), so projects can still set up their own services.

- `bin/sandboxed-run --project <p> --cwd <dir> [--port <n>] -- <argv…>`: computes `seatbelt.service_profile` fresh on every
  start and `exec`s `sandbox-exec -p`. render-services puts it in front of a sandboxed project's service command, the
  scheduler (`registry.scheduler_jobs`) in front of its command jobs. Agent jobs already go through `llm.py`.
- The profile is closer to a session's than to cap-run's: its runtime and workspace (and `policy.write`) are
  writable, the bus directory neither readable nor writable (11-01: its letters go through its outbox,
  `bus-sender.md`), and its tool caches are its own, in its repo's git dir (`policy.env`; 11-01: the shared uv/pip/npm
  caches were writable from every sandbox, so one project could change a package another installs next); other projects, platform state, credentials (`~/.ssh`, `~/.claude`'s token…) and the
  Keychain are not readable. Outbound network is open: Seatbelt can't filter by domain, so the registry's `network` list
  is not enforced at this layer. Listening: only `expose.local_port` can be bound, and only connections arriving through
  localhost are accepted (tested on the mini: LAN and tailnet addresses time out even when listening on 0.0.0.0;
  inbound rules per port were unreliable, so inbound is `localhost:*` and the port limit sits on bind).
- Fails closed: where no adapter works (see the Linux section below for the adapters) render-services doesn't render the
  service (reported like a command that can't be found) and `sandboxed-run` exits 78 without running anything.
- `hub.json` `service_sandbox_exempt`: projects held back temporarily, owner's decision. The first one on it
  (its job reads `~/.claude/headless-oauth-token` itself, its service's `git pull` needs `~/.ssh`) until it moves its model
  calls to the platform and stops reading credentials.
- Sessions of sandboxed projects may bind 127.0.0.1 (`policy.local_bind` → `allowLocalBinding`; owner confirmed 10-31 in
  the agent-hub session), so a server can be tested before it's declared.

## 10-31 (Linux): one adapter per OS behind the same policy

`bin/isolation.py` is the neutral front: `sandboxed(doc)`, `wrap()`, `can_sandbox()`, `exec_service()`; it picks the
adapter for this machine (`darwin` → `seatbelt.py`, `linux` → `landlock.py`, anything else → none). `sandboxed-run`,
render-services and `registry.scheduler_jobs` only talk to it. `policy.py` stays OS-neutral. No adapter, or the adapter
can't work on this machine → refused with the reason, never unsandboxed; `agenthub doctor` has a row for it (red when a
registered project is sandboxed and nothing can isolate it).

### What was measured (colima VM, Ubuntu kernel 6.8.0, `kernel.apparmor_restrict_unprivileged_userns=1`, as on Ubuntu 24.04)

- **Unprivileged user namespaces: refused.** `unshare -Ur` → `write failed /proc/self/uid_map: Operation not permitted`;
  `bwrap` → `setting up uid map: Permission denied` (Debian 13 container with systemd 257). In the plain Docker container
  `bin/linux-test` uses, Docker's seccomp profile refuses `unshare(CLONE_NEWUSER)` too. So bubblewrap is out.
- **A systemd user unit's sandboxing, one directive at a time** (a unit file under `~/.config/systemd/user`, systemd 257,
  probing writes, reads of another folder and `~/.ssh`, binds, and the user's D-Bus socket):

  | directive | in a user unit here |
  |---|---|
  | `NoNewPrivileges=yes` | applied (just a prctl) |
  | `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `ReadWritePaths`, `InaccessiblePaths`, `PrivateUsers` | **silently dropped**: "Failed to unshare the mount namespace: Operation not permitted … Failed to set up namespace, assuming containerized execution and ignoring"; the service starts with full access |
  | `SocketBindAllow`/`SocketBindDeny` | silently dropped (cgroup BPF, which a user manager can't attach); every port binds |
  | `IPAddressAllow`/`IPAddressDeny` | silently dropped (same reason) |
  | `RestrictAddressFamilies=AF_INET AF_INET6` | applied (seccomp); but it would break DNS (`AF_NETLINK`) and socketpair-based child processes (node) |

  A user unit therefore can't be the guarantee: wherever user namespaces are restricted, systemd starts the service
  unsandboxed without failing. The units render-services writes carry no sandboxing directives; the enforcement is
  `sandboxed-run` in `ExecStart`, the same as on macOS.
- **Landlock: ABI 4** (TCP bind/connect rules, kernel ≥ 6.7). Its syscalls and a nested seccomp filter are allowed in
  the plain Docker container, so `bin/linux-test` runs the real check (`tests.test_sandboxed_services.Conformance`,
  with the Landlock adapter).

### The Linux adapter: `bin/landlock.py`

`sandboxed-run` restricts its own process and then execs the command (children inherit it, it can't be undone).
Standard library only (ctypes), unprivileged; the same for services and command jobs.

- **Writes**: own runtime/workspace (its tool caches included: `policy.env`), `policy.write` (`allow_write`; not the bus since 11-01), `/tmp`, `/var/tmp`, `/dev/shm`,
  a few devices. A missing writable folder is created first when its parent exists (a rule needs an fd).
- **Reads**: Landlock can only allow, so the deny-list is inverted: walk from `/`; a path with nothing denied inside is
  allowed whole, a denied one is skipped, one with something denied inside is walked into. Denied: `workspace_root`,
  `services_root`, other projects, platform state, the bus directory,
  `policy.deny_read` (globs expanded now; the folder holding the matches is walked, so later matches stay unreadable),
  credential directories and files. Paths that don't exist yet still count, so a credentials file created later is not
  readable either. Symlinks are allowed only when their target would be. Own folders are allowed explicitly.
- **Network**: bind only `expose.local_port` (TCP); outbound open (only `BIND_TCP` is handled), as on macOS.
- **Other**: `no_new_privs`; seccomp turns `socket(AF_UNIX)` and `io_uring_setup` into `EACCES` (socketpair still
  works). Without this a service could reach the user's D-Bus or systemd's private socket and run
  `systemd-run --user …` outside the sandbox: Landlock on 6.8 doesn't cover connecting to a pathname socket. Checked
  in the systemd container: from inside, `systemd-run --user` fails ("Failed to connect to user scope bus … Operation
  not permitted") and `~/.ssh` is unreadable. Landlock itself refuses ptrace and `/proc/<pid>/environ` of processes
  outside the sandbox (measured: a child started before restricting, `cat /proc/<pid>/environ` → Permission denied;
  `/proc/self/environ` readable). With ABI ≥ 6 (kernel 6.12) signals and abstract unix sockets are scoped as well.
- **Refuses** (`can_sandbox()`): not Linux, an architecture without a seccomp entry (only x86_64 and aarch64), Landlock
  ABI < 4 (kernel < 6.7, or `landlock` not in the `lsm=` list).

### What each OS enforces and what it can't

Each adapter declares the rows it guarantees in code (`guarantees()`, names in `isolation.GUARANTEES`), and
`isolation.REQUIRED` (own writes only, isolated reads, declared port only, no mounts) must be among them or the adapter is refused.
`tests/test_sandboxed_services.py` `Conformance` runs one probe through `bin/sandboxed-run` with whichever adapter the
machine has and checks every guarantee it declares, so a second backend (docs/design/competitive-gaps.md) is added by
passing the same run, not by reading its docs. The table below is the prose form of the two adapters' declarations.

| | macOS (Seatbelt) | Linux (Landlock + seccomp) |
|---|---|---|
| writes limited to own folders (own tool caches included), temp; the bus neither written nor read | yes | yes |
| other projects, platform state, credentials unreadable | yes | yes (complement walk; new files next to denied paths unreadable too) |
| Keychain / D-Bus, launchd / systemd user manager | Keychain services and launchctl blocked | unix-domain sockets blocked (no D-Bus, no `systemctl --user`) |
| bind only the declared port | yes | yes (TCP only; UDP binds are not covered) |
| inbound only through localhost | yes | **no**: a service can bind `0.0.0.0:<its port>`; Landlock can't see the address and the unit's `IPAddressDeny` is dropped in a user unit. A host firewall is the place for this |
| outbound domain allowlist (`policy.network`) | no (Seatbelt can't filter by domain) | **no**: needs a network namespace + proxy, i.e. user namespaces, refused above; not cheap, not implemented |
| signals to the owner's other processes | blocked | only with ABI ≥ 6 (kernel 6.12); on 6.8 a service can signal the owner's processes |
| what its sessions load settings, hooks and tools from (`policy.protect`, any depth, worktrees included) can't be written | yes (11-01) | **no**: Landlock only allows, and a write right on a folder covers everything under it; the renderer removes hooks the platform didn't write every 15 minutes |
| programs that need unix sockets of their own (a local database socket, syslog) | allowed | refused (`EACCES`) |
| nothing mounted, attached as a disk or unmounted (`no-mounts`, required; "11-15: no mounts") | yes (11-15) | yes (Landlock refuses every topology change) |

## 10-31: projects' own model calls (`agenthub complete`)

A sandboxed service or job can't read the platform's credentials, so a project's model calls go through the platform.
Owner's decisions (10-31): tier only, never model names; per-project usage on the status page; a metered provider only
with the owner's per-project opt-in; fail visibly, no fallback; one platform-wide daily budget for now (per-project
budgets later, if a project spends a lot).

- Registry: `models: {<point>: {tier}}` (the project declares it, through manifest.update); `metered_models: true`
  (owner-only, in PLATFORM_OWNED).
- Path: `agenthub complete <point>` = `hub-run --complete` writes `{"kind": "complete", point, prompt, schema}` into its
  own folder's `.hub-run/requests/` (workspace or runtime: services and jobs run in the runtime folder). cap-runner finds
  it; the folder decides the project (no self-declared identity), the point must be declared, and the prompt is copied
  into `.state/cap-runner/<project>/<id>.in.json`, never read back from the project's folder. It runs `llm.py project
  <project> <point>` outside any sandbox.
- `llm.py project` resolves the actual target (after `llm.points` pins) and refuses before sending if it is metered and
  the project has no `metered_models`. Metered = `completion.metered`: a cloud provider reached with an API key; a
  runtime's own login (subscription / headless token) and local providers are not. Usage rows get `project`; the point
  is named `<project>/<point>` (pins in `llm.points` use that name; since 11-15 the owner can pin one to a provider, e.g.
  a local model, `llm-providers.md` "Projects' calls on a provider").
- Services' profile now also reads `policy.read` (the registry index): without it `agenthub send`/`complete` inside a
  sandboxed service couldn't tell which project it was in.
- Not guarded: a project that brings its own API key and calls a provider directly. That is the project's own credential
  and spending (declared under `secrets:`), not the platform's (owner, 10-31).
- Checked 10-31 in a throwaway instance with a fake provider: a client under the service profile → the real cap-runner →
  `llm.py project`; the declared call answered, an undeclared one was rejected (126), the usage row carries the project.

## 11-08: a sandboxed project's sessions are denied what its services are

### What was wrong (checked 11-08)

The policy (`bin/policy.py`) names what a sandboxed project may not read, but its three renderers took different parts of it:

| | Seatbelt (services, jobs, cap-runner) | Landlock (the same, Linux) | Claude session: Bash sandbox | Claude session: file tools |
|---|---|---|---|---|
| other projects, `deny_read` (raw notes, secrets, the bus for an outbox project) | denied | denied | denied | `Read` denied |
| `credentials` (`~/.ssh`, `~/.claude`, `~/.config/gh`, `~/.aws`) and `credential_files` (`~/.claude.json`) | denied | denied | **readable** | **readable** |
| the hub's `.state` | denied | denied | **readable** | **readable** |
| `.logs`, the data dir (digests, `todo.md`: every project's open work), the evals dir (judgment-point inputs) | **readable** | **readable** | **readable** | **readable** |
| secrets, writing | denied | denied | denied (no `allowWrite`) | **`Edit` allowed** |
| the runtime's user-level control files (`~/.claude/settings.json`, its hooks, commands, skills, agents) | denied (write) | denied (write) | denied (no `allowWrite`) | **`Edit` allowed** |

A session's Bash in a sandboxed project could `cat ~/.ssh/id_*`, read every project's `todo.md`, or read the hub's job reports. Its
file tools could write a hook into the user's own settings, which the next session anywhere runs as the user. In a session the owner
opens, the default mode asks before a file tool goes outside the folder, so the file-tool rows are reinforcement there; for a
session a mode or the platform starts with fewer prompts, they aren't.

### Measured 11-08 (Claude Code 2.x, macOS, `claude -p --setting-sources project` in a scratch repo)

- With `sandbox.filesystem.denyRead: ["~/.claude", "~/.ssh"]`, Bash still runs (`echo`, `which git`); reading either is
  "Operation not permitted". So a session's Bash doesn't need `~/.claude` (`-p`; a Desktop session is checked live after merge).
- `denyRead: [<dir>]` with `allowRead: [<dir>/registry]`: the narrower path wins, `<dir>/registry/f` reads, `<dir>/jobs/g` doesn't.
- A permission rule `Read(/<dir>/**)` denies the Read tool on everything in it, `<dir>/registry` included, while Bash still reads
  `<dir>/registry` through `allowRead`. So the file tools can be denied a folder whole and Bash keep the carve-outs: no list of a
  folder's entries is needed for `.state`.

### The design (after two blind reviews): A1 now, A2 after a measurement

The second review found that the finer rules inside the runtime's home (`~/.claude`: which entries the file tools may read or
write) each depend on how Claude Code names and uses them: its project folders' names (it replaces every character that isn't a
letter or digit, cuts at 200 and adds a hash, so `foo_bar` and `foo-bar` share a folder, and a prefix rule lets `foo` read
`foo-bar`), plan files (`~/.claude/plans`, written with the Write tool), skills' supporting files, and the shell snapshot every Bash
command runs first. Those need a measurement in a session the Desktop app opens. So the work is split: **A1** closes what lets
sandboxed code act as the user or read credentials, and is built now; **A2** (other projects' transcripts and memory in
`~/.claude`) waits for that measurement and a port of Claude's own folder naming. Until A2, a sandboxed session's Bash and file
tools can read other projects' transcripts there, as today.

**A1**

1. **`platform_state`, for projects other than the hub**: the hub's `.state` and `.logs`, the data dir and the evals dir (its
   configured path, read without making it), minus any that is or contains the hub root (the old layout's data dir is the hub root).
   Empty for the hub (its sessions and leases read its own state; its confined runs already hide `.state` and `.logs`).
   `.state` is denied whole: the three things sandboxed code reads moved out of it into `hubconf.shared()` (`<hub>/.shared`, the
   main checkout's, gitignored, written only by the platform, nothing private), which is `read`: the registry cache and index
   (`registry.CACHE`, `REGISTRY_INDEX`), brunna test's worker slots (`test_slots()`) and the renames record (`renames()`).
   `brunna land` moves all three at once after the merge (`move_shared`; cap-runner and lease-runner restart themselves on the
   new code once idle, `code_stamp`); a later first look moves what is left. Only the main checkout's own code in its own state
   moves (`ROOT == MAIN`, no STATE override), never read in both places; a copy that differs or a move that fails stays, is
   said, and doctor shows it red (`shared_leftovers`); two renames records are merged by key unless a key differs. Until they
   restart, services still on the old code fail closed (a sandboxed cap-run or complete is refused, not run unconfined). Only a confined job's run settings list `.state` beside its own
   folder (`keep`, or its `cwd: state` folder with no rule writing it), the way `write_except` lists the hub (`beside`). No `allowRead` is relied on. The Seatbelt
   and Landlock renderers keep `.state` in their own fixed list as well, so a profile built from any policy still denies it.
2. **Credentials**: the settings' `credentials`, plus a code default, plus what each installed runtime declares
   (`runtime_<name>.CREDENTIALS`: dirs and files; Claude's: its credential file `.credentials.json` in its home, not the home itself,
   whose finer rules are A2): `~/.ssh`, `~/.config/gh`, `~/.aws`, `~/.config/gcloud`, `~/.kube`, `~/.docker/config.json`,
   `~/.netrc`, `~/.npmrc`, `~/.git-credentials`, `~/.codex` (no Codex adapter yet to declare its `auth.json`).
3. **`deny_write`**, added: the instance dir (config, data, secrets), the bus dir for a project whose mail goes through its outbox,
   the platform's Python (its venv under `~/.local/share/<share_dir>` and the interpreter the venv's `pyvenv.cfg` names, whose
   `lib` is on every platform script's path), uv, the folder `install-cli` writes the wrapper to (by default `~/.local/bin`, whole: a
   new `git` there shadows the user's), the nvm dir, the shell's startup files (`~/.zshrc`, `~/.zprofile`, `~/.bashrc`,
   `~/.bash_profile`, `~/.profile`), git's user config (`~/.gitconfig`, `~/.config/git`: `core.hooksPath` runs in every repo), and
   the service manager's folder (LaunchAgents; `~/.config/systemd/user`). Seatbelt and Landlock already deny every write outside a
   project's own folders; this is for the file tools (and anything else that renders `deny_write`).
4. **The renderers take it from the policy**: `seatbelt.read_denies` and `landlock.denied` add `platform_state` (and keep their own
   fixed list: workspace root, services root, `.state`, the bus, the Keychain files); Seatbelt emits both spellings of each (fix B,
   merged). The parity test probes paths against each renderer's matcher on a tree with a symlinked instance dir.
5. **The session's Bash sandbox**: `denyRead` += credentials and credential files, `platform_state`. Not `workspace_root`: git 2.56
   dies when a folder above its `-C` can't be stat'ed, and Claude's settings have no stat-only allow (`other_projects` already names
   each project). No `allowRead` is used.
6. **The session's file tools**: `Read` + `Edit` denied on secrets, `platform_state` and credential dirs; `Read` on credential files;
   `Edit` on the runtime's user-level control files, named by the adapter (`runtime_claude.USER_CONTROL`, under its home honouring a
   configured `CLAUDE_CONFIG_DIR`): `settings.json`, `settings.local.json`, `CLAUDE.md`, `hooks/`, `commands/`, `agents/`,
   `plugins/`, `output-styles/`, `rules/`, `skills/`, and `~/.claude.json`. Reading them stays allowed (skills' files, plans);
   `plans/` and `projects/` are untouched. `Bash(security *)` denied, as for leases (reinforcement; the Keychain residual is stated in
   doctor's sandbox row).
7. **Where `settings()` is also used** (`platform_settings`, `lease_settings`): the hub's own get no `platform_state`; a confined
   job's `keep` and `notes_dir` are left out of `platform_state` (1).
8. **backup-verify** restores into `hubconf.private_tmp()` (excluded from the backup, denied to every sandbox both ways).
9. **Pushing from a sandboxed session**: as before — on macOS an ssh push already fails inside the sandbox; on Linux it stops, and an
   HTTPS remote through gh's helper stops fetching. The owner pushes from a terminal, or the project uses a deploy key of its own.

Built 11-08 as above, with three small additions: a settings `credentials.dirs` entry that is the runtime's own home
(`~/.claude`) stays whole for Seatbelt, Landlock and leases while sessions get only its credential file (6); `Edit` is denied on
credential files too (`~/.npmrc` can name a shell npm runs); `git bundle verify` runs in an empty repo of its own (8).

**A2 (later)**: port Claude's folder naming into the adapter (exact own slug and `<slug>--claude-worktrees-*`, the computed slugs of
every registered project denied even before their first session), then measure in a Desktop-opened sandboxed session what Bash and
the file tools need from the home (shell snapshot, plans, skills), and deny the rest.

### Checks

- Unit: the parity test (4); the hub's policy has no `platform_state`; the old layout's data dir is left out; `evals_dir()` isn't
  called (nothing is created); `.state` is denied whole and `read` is beside it (only `keep` is listed around); the move out of
  `.state` happens once and only in the live layout; `deny_write` holds the instance
  dir, the venv and its interpreter, uv, the wrapper folder, nvm, the shell files, git's config; `Edit` denies match
  `~/.claude/settings.json`, `hooks/x`, `plugins/x` and not `~/.claude/projects/x/memory/a.md` or `~/.claude/plans/p.md`.
- Before merge: `render-project-settings.py --report` before and after for a sandboxed project, the diff only these rules.
- After merge, live, in a sandboxed project's session opened from the Desktop app: `command -v brunna node git` still answers; Bash
  `ls ~/.ssh`, `cat <data>/todo.md`, `ls <hub>/.state/agent-jobs` fail; `brunna inbox`, `brunna send`, `brunna test` and a send to a
  renamed project's old name still work; a memory note and a plan file can still be written.

## 11-15: no mounts

### The hole (cold review 11-15, reproduced on macOS 27.0.1)

Under the Seatbelt service profile (services, command jobs, the test runner's wrap, platform git work as the project)
and the cap profile, a process could write a disk image and attach it: `hdiutil attach -mountpoint <dir>`, `diskutil
image attach --mountPoint <dir>`, or either at `/Volumes`. The mount point could be a folder of its workspace or any
folder the owner owns, including one outside the profile's writable set. The image's files then appear there although
the process never wrote them through the file system. The review planted a `.git` with a hostile `core.fsmonitor`, and
the owner's git ran it. This defeats `own-writes-only` and `runtime-files-locked`, not only the git fence. The same
profile also let it `mount_devfs` over its own folders, mount an image device the owner had attached, and unmount
or eject the owner's volumes.

The review reported that denying the DiskArbitration lookup didn't stop the attach. That was an artifact of judging by
exit code: `hdiutil attach` exits 0 for an attach that made nothing. Every measurement below is taken from outside
(the mount table, `hdiutil info`, `diskutil list`, polled), never from the route's exit code.

### Measured (each rule alone under `(allow default)`, then the union in the real profiles)

- **`(deny file-mount)`** (with `file-mount-update` and `file-unmount`): refuses a mount(2) the process makes itself
  (`mount_devfs` mounted under a no-op profile and is refused with this rule), and a mount a daemon makes at a mount point the caller
  names (`hdiutil attach -mountpoint`, `diskutil image attach --mountPoint`, `diskutil mount -mountPoint`:
  DiskArbitration checks the caller's sandbox). It does **not** refuse an attach that DiskArbitration mounts at
  `/Volumes`, nor the attach itself (a device is still made).
- **The services, by name** (`global-name-prefix`, and `global-name` for one): `com.apple.DiskArbitration.`
  (diskarbitrationd: hdiutil and diskutil can't attach, mount, unmount or eject at all), `com.apple.diskimagescontroller`
  (named on its own, and also under the `com.apple.diskimage` prefix), `com.apple.diskimage` (diskimagesiod,
  diskimagespawner), `com.apple.system.hdiejectd` (with only this denied, hdiutil's attach fails), and with no single
  route of their own measured, `com.apple.storagekitd` (erase and partition), `com.apple.filesystems.` and
  `com.apple.fskit.` (FSKit, UserFS), `com.apple.netauth.` (NetFS), `com.apple.automountd`.
  diskutil's attach goes through **diskimagescontroller**, the XPC service bundled in DiskImages2.framework. It runs in
  a sandbox of its own, not the caller's, and reaches diskimagesiod from there. With diskimagesiod and diskimagespawner
  denied and diskimagescontroller allowed, `diskutil image attach --noMount` still makes the device. Denying
  diskimagescontroller alone, as `global-name` or as `xpc-service-name`, stops it with "Couldn't communicate with a
  helper application". (A first reading credited this to diskimagesiod; the prefix only caught the controller because
  the names share it.)
- **The disk-image driver's door:** hdiutil opens `IOHDIXControllerUserClient` directly. Denying `iokit-open-user-client`
  for that class, or `iokit-open-service` for `IOHDIXController`/`AppleDiskImagesController`, alone stops its attach.
  `AppleDiskImagesController` also serves `DIDeviceCreatorUserClient`, the client diskimagescontroller opens; an
  `IOServiceOpen` from the sandboxed process on either service gets `kIOReturnNotPermitted`.
- **LaunchServices** (`(deny lsopen)`, in `_isolation`): opening a .dmg starts DiskImageMounter outside the
  sandbox, which attaches it and mounts it at `/Volumes`. Under the `mount_denies()` lines alone, a process's own
  `LSOpenCFURLRef` (or `NSWorkspace.open`) on the image mounts it; under the real profiles, `lsopen` refuses it. The
  `/usr/bin/open` binary is stopped earlier by the exec deny, so a route through the binary alone never reaches this
  layer.
- **Not an exec deny:** denying `/usr/bin/hdiutil` stops only that path. (A copy of hdiutil or `open` in a writable
  folder is killed at exec on this system, but that is code signing, not this profile.)

The first three layers are `seatbelt.mount_denies()`, and the fourth is `(deny lsopen)`. Both are in `_isolation`, so
both profile kinds carry them. Routes under the real `service_profile` and `profile(pol, "gpu")`, observed from
outside ("before" is the same profile without `mount_denies()`):

| route | before | after |
|---|---|---|
| `hdiutil attach -mountpoint <ws>/mnt` / at `/Volumes` | mounted | nothing |
| `hdiutil attach -nomount`, `hdiutil attach ram://` | device made | nothing |
| `diskutil image attach --mountPoint` / at `/Volumes` | mounted | nothing |
| `diskutil image attach --noMount`, `ram://` | device made | nothing |
| `mount_devfs`, `mount -t devfs` over its folder | mounted | refused |
| `hdiutil attach -shadow`, `hdiutil convert` to UDRW then attach, `hdiutil mount` | mounted | nothing |
| a mount point that is a symbolic link to an owner folder (hdiutil, diskutil) | mounted there | nothing |
| `/usr/libexec/attach_automation_image` | device made | nothing |
| `diskutil mount [-mountPoint]` of a device attached outside | mounted | refused |
| `mount -t hfs -o rdonly` of that device | mounted (no-op profile) | refused |
| `umount`, `diskutil unmount`, `hdiutil detach` of the owner's volume | unmounted | refused, still mounted |
| `mount_nfs 127.0.0.1:/` | no server (connection refused) | refused before connecting |
| `open x.dmg` | refused already (`/usr/bin/open` exec deny) | refused |
| `LSOpenCFURLRef` / `NSWorkspace.open` on x.dmg from its own code | refused already (`lsopen`); mounted at `/Volumes` with only `mount_denies()` | refused |
| `mount_ftp` (FSKit) to a local FTP server | refused already: FSKit refuses a sandboxed caller even under a no-op profile | refused |
| `mount_webdav`, `mount_url http://` to a local WebDAV server | mounts unsandboxed; under any Seatbelt profile, a no-op one included, nothing is mounted although the server is asked | nothing |
| reading `/dev/rdiskN` of the owner's image mounted over a folder in `deny_read` | the file refused, its blocks read | both refused |

Not reached: SMB (no server on this machine). Its mount tool calls mount(2) in-process, which the `mount_devfs` row
shows refused. The automounter's maps here are `/home` and `/Network/Servers`; `/net` is off.

Ordinary work under the new profiles, A/B with and without the block: python (json, sqlite3, ssl), the platform's
Python with PyYAML, node, git (init, commit, log, `clone` over https), `uv venv` + `uv pip install` and `npm install`
over the network, `curl https://pypi.org`, node `fetch`, `swiftc` and `clang` builds, ad-hoc `codesign -s -`, `df`,
`stat -f`, `FileManager.mountedVolumeURLs`, a Metal shader compiled at run time and dispatched, and MLX on the GPU
(Metal) under the cap profile all ran the same. What no longer works: read-only disk questions (`diskutil info`,
`diskutil list`, `hdiutil imageinfo`: they need DiskArbitration or the disk-image services) and `stat` of a
`/dev/disk*` node. Building a .dmg (`hdiutil create -srcfolder`, `-attach`) failed before this block too, under the
write rules. Claude Code's
own sandbox is deny-by-default, and its mach-lookup and IOKit allowlists name none of these services, classes or
`file-mount`. That is read from the profile its binary generates, not measured. A canary in `conformance_claude`
would run `hdiutil attach -mountpoint` and `diskutil image attach` through the session's Bash and look from outside.

Linux: a landlocked process can't change the file-system topology (mount, umount, remount, pivot_root, move_mount:
Landlock refuses them whatever the ABI). `no_new_privs` leaves a setuid helper (fusermount3, mount's `user` entries)
powerless, and udisks, gvfs and systemd-mount are behind D-Bus, a unix socket the seccomp filter refuses. A mount in a
user namespace of its own is seen only inside it. So `landlock.guarantees()` declares `no-mounts` too, and it is in
`isolation.REQUIRED`: without it, `own-writes-only` means nothing. The Conformance probe tries `mount -t tmpfs` there.
In the test container that fails as a non-root user with or without Landlock, so it has no positive control.

Tests: `tests/test_no_mounts.py` (the routes of the first table above under both kinds, LaunchServices through the
platform's Python and ctypes, an existing device, the owner's volume, the raw device, ordinary work), and the
Conformance probe (`no-mounts`: `mount-blocked`, seen from outside). Every route first runs under `(allow default)` and
must be seen there, or the test fails: a flag gone or a tool moved can't pass as "refused". The sandboxed run is then
watched twice as long as the control took to show. The tools are named by absolute path, never through PATH. Switching
`mount_denies` off made 30 of its subtests fail; dropping `(deny lsopen)` fails the LaunchServices route under both
kinds, and dropping the raw-device deny fails the raw read under both.

### Residuals

- **By name, not by kind.** The service list is an enumeration of this macOS's services. A later macOS that mounts
  through a new service is caught only if a route test covers it. `test_no_mounts` runs on every gate, so a new route
  added there shows up; a new service nobody tries does not.
- **Framework XPC services run outside the caller's profile.** An XPC service bundled in a framework is started by
  launchd in a sandbox of its own (or none), so what it does for the caller is limited only by whether the caller may
  look it up (Seatbelt matches the lookup by `global-name` as well as `xpc-service-name`, measured) and by the
  service's own checks. Of the system's 471 `.xpc` bundles, those whose binaries reference mount or attach calls:
  `com.apple.diskimagescontroller` (denied by name), `com.apple.filesystems.netfs.PlugInLibraryService` and
  `com.apple.filesystems.lifs.userfsd.UVFSService` (under the `com.apple.filesystems.` prefix),
  `com.apple.CSCMountService` and `com.apple.WorkflowKit.MacHelper` (their binaries reject a client without a private
  entitlement), and two MobileSoftwareUpdate services (`CleanupPreparePathService`, `CryptegraftService`; not
  inspected). None of the last four was exercised. `com.apple.mobile.storage_mounter` (MobileStorageMounter) and
  cryptexd also mount, but behind private entitlements.
- **Later rules win.** A capability fragment that allows `default`, `file-mount`, `file-unmount`, `file*`, `mach…`,
  `iokit…` or `lsopen` after the base would reopen this. `test_no_mounts` checks that nothing after the block allows
  them back, in any of those spellings, for every fragment in `capabilities/`. (`(allow file* (subpath <dir>))` after
  the block was measured not to reopen a mount at `<dir>`.)
- **Closed (11-15): raw disk devices.** An image the owner attaches gets `/dev/diskN` and `/dev/rdiskN` owned by the
  owner, so a sandboxed process could read its blocks: an encrypted image's decrypted, and the files of a folder in
  `deny_read` that the image is mounted over (measured: the file refused, the same bytes read from `/dev/rdiskNs1`).
  `_isolation` now denies reading and writing `^/dev/r?disk[0-9]`; the test reads the device of an image it mounts
  over a denied folder.
- **Not measured here:** a hand-written client of the disk-image services other than diskimagescontroller (the
  services it reaches from its own sandbox, diskimagesiod and diskimagespawner, are denied by name too). Also not
  measured: whether a process under these profiles can get launchd to run a job outside the sandbox through its API
  rather than the `launchctl` binary. The probe was not run in this session. If launchd would run such a job, every
  guarantee here is moot, not only this one.

## `pdf`: print a project's HTML to PDF (11-23)

**Status:** built 11-23 (r1 below holds where it differs). The owner asked for it on 11-23 ("Yes, build it"): only printing a page to a
PDF, not a general browser capability. Granting it to a project is a separate yes (`enforce.capabilities` is a
security field).

### Why

A sandboxed project builds a document as HTML and prints it with headless Chrome, whose layout another renderer does
not match. In the session's sandbox Chrome does not start (it binds a socket for its process singleton, and its own
sandbox can't start inside another), so today the owner runs it in a terminal each time.

### Measured (11-22 and 11-23, scratch folders, removed)

1. **Chrome itself can't run in the platform's runner, and we don't make it.** Under the cap-run profile it stops at
   `ProcessSingleton`: `bind()` of a Unix socket in the user temp area (`/var/folders/…/T/`, from the system's temp
   directory call, not `TMPDIR`), denied by `network-bind`. Opening that is not an option: the same kind of socket in
   the same area is the owner's running Chrome's singleton, and connecting to it hands that Chrome a command line
   (open a URL in the owner's signed-in browser).
2. **`chrome-headless-shell`** (Chrome for Testing's headless build, the same Blink) has no process singleton. Under
   the cap-run profile it fails only on its own sandbox (`sandbox initialization failed`); with `--no-sandbox
   --disable-gpu` it prints the PDF.
3. **A PDF-only profile** (below), in the real location (a scratch folder under `workspace_root`, the deny/allow
   overlap of test 4 above), with `HOME` and `TMPDIR` in the job's folder: reading the folder's file works; reading
   `~/.zshrc`, the owner's Chrome profile and `/private/tmp` is refused; `curl` to a font server fails; a write next to
   the folder is refused; the headless shell prints the PDF and leaves no process.
4. **JavaScript stays on.** `--blink-settings=scriptEnabled=false` makes `--print-to-pdf` exit 0 with no file. Nothing
   in the boundary depends on scripts being off: a script reaches no more than the profile lets the process reach.
5. **No publisher signature.** The macOS build is signed ad hoc (`TeamIdentifier=not set`), and Chrome for Testing
   publishes no digests. The trust anchor is HTTPS from Google's download host.

### Design

1. **A request kind, not a command.** `brunna pdf <in.html> <out.pdf>` (client `bin/hub-run --pdf`) writes
   `{"v": 1, "id", "kind": "pdf", "html": <relative>, "pdf": <relative>}` into the project's `.hub-run/requests/`, like
   `complete` and `agent`. The runner builds the whole command line; nothing from the request reaches it but the two
   paths, as a `file://` URI (`Path.as_uri`, which escapes `#`, `?`, `%`) and a `--print-to-pdf=` path.
2. **Who may.** The project declares `pdf` in `enforce.capabilities` (the owner's yes; `brunna.json` `capabilities.pdf`,
   risk low, no `default_for`). `pdf` admits only this kind: a cap-run request naming `pdf` is refused, so declaring it
   never grants running other programs.
3. **The request's checks**, on top of the runner's existing ones (folder not a link, file size, age, id): both paths
   relative, no NUL, at most 4096 characters; the input resolves inside the project folder, is a regular file and ends
   in `.html` or `.htm`; the output's folder resolves inside the project folder and the name ends in `.pdf`. The runner
   writes nothing itself: the renderer, inside the profile, writes the output, so a path swapped to a link after the
   check still lands only where the profile allows.
4. **The profile** (`seatbelt.pdf_profile`): `(allow default)`; `(deny network*)`; the shared isolation with
   `temp=False` (writes only to the project folder; reads of other projects, credentials, platform state, Keychain,
   raw disks; mounts, `lsopen`, Apple Events, `NO_EXEC`, signals and process info outside the sandbox); then reads of
   the owner's home, `/Users`, `/Volumes`, `/private/tmp` and `/private/var/folders` refused; then reads allowed again
   for the project folder and the renderer's own folder, and metadata for the folders above them. No capability
   fragment is appended.
5. **The run.** `HOME` and `TMPDIR` are the job's folder (`.hub-run/jobs/<id>/`, made by the runner with `O_NOFOLLOW`
   fds, which only this project and the platform write); `--user-data-dir` under it. The command: the renderer,
   `--no-sandbox --disable-gpu --no-first-run --no-pdf-header-footer --user-data-dir=… --print-to-pdf=<out>
   <file URI>`. Timeout 120 s, process group killed, as for every job. The job folder goes with the runner's 7-day
   cleanup.
6. **The renderer** lives in `<instance>/tools/chrome-headless-shell/<version>/` (in no sandbox's writes; readable only
   through the profile's allow). `bin/pdf_renderer.py install` downloads the newest Stable for the machine's platform
   from Chrome for Testing's index over HTTPS, unpacks it with `ditto` into a new version folder, records its sha256,
   and makes it current only after a fixture page prints under `pdf_profile`; a version that doesn't print is kept
   aside and said. A weekly platform job `brunna/pdf-renderer` runs it, and only when some project declares `pdf`;
   with none, nothing is downloaded. A `pdf` request with no current renderer is refused with the reason, never
   served by another browser.
7. **Linux and the rest:** the runner is macOS-only (Seatbelt), so `pdf` is too; elsewhere `brunna pdf` says it is not
   supported. No network, so a page's remote fonts and images don't load: the project vendors them (the asking project
   is moving its font into its folder).
8. **The owner's surface:** no new command for the owner. The project's agent runs `brunna pdf`; the rules file names
   it beside `cap-run`; `SESSION_SUBCOMMANDS` gains `pdf`.

### r1 after a blind review (11-23)

Two blocking findings, both taken; the should-fix ones too. Where r1 differs from the design above, r1 holds.

- **B1, a capability with no fragment.** `hubconf.capabilities()` listed only names with a `.sb`, and a `pdf.sb` would
  have let `cap: pdf` run any argv. Now a catalog entry with `kind` (`capabilities.pdf: {kind: pdf, …}`) is a
  capability with no fragment, admitting only that request kind: the cap branch refuses it by name, and the runner
  dispatches on `kind` explicitly (`complete`, `agent`, `pdf`, or none for cap-run) and refuses any other. The
  timeout is the runner's (120 s); nothing in the request sets it.
- **B2, a renderer bug with Chrome's sandbox off.** The profile now ends with `exec_guard` naming only the renderer's
  version folder (measured: `cat` and `sh` refused, the renderer prints in 0.4 s), denies `user-preference-write`,
  and denies the pasteboard (`com.apple.pasteboard*`) and the DNS services (`com.apple.dnssd.service`,
  `com.apple.mDNSResponder`) by name. A compromised renderer keeps the rest of `(allow default)`: Mach and IOKit are
  not denied by default (not measured against the headless shell; a later step).
- **The renderer's age.** The job runs daily (not weekly). A `pdf` request is refused, with the reason, once the index
  has had a newer Stable for more than 14 days that the platform hasn't made current (`behind_since` in the
  renderer's record).
- **The download.** The index is fetched with verified TLS (the system's CA file, refused when it loads none); a
  version must match `^\d+(\.\d+){3}$` and a URL be `https://storage.googleapis.com/chrome-for-testing-public/…`
  for that version and platform. Unpacked with `ditto` into a fresh folder; any link in it that is absolute or leaves
  the folder refuses the version. The recorded sha256 is trust on first use, said as such. The fixture print uses a
  scratch folder and a synthetic policy (no project's).
- **The git fence stays** in `pdf_profile` (it isn't stripped as the git writer's profiles do), and a test says so.
- **Processes.** After each `pdf` job and in `recover()`, any process whose executable is in the renderer's folder and
  isn't in a running job's group is killed (a renderer that called `setsid`). Measured: a page that never finishes
  leaves 5 processes; killing the group ends all of them.
- **The job's scratch is the platform's.** `HOME`, `TMPDIR` and `--user-data-dir` are a new folder per job under
  `.state/cap-runner/<project>/<id>.d/` (no project writes `.state`), allowed in the profile for reading and writing,
  removed after the job (`shutil.rmtree`, which on macOS walks with fds and doesn't follow links). The project's job
  folder keeps only state, stdout and stderr.
- **Paths.** The runner passes the paths as resolved at the check and touches neither again by path.
- **Open, not blocking:** whether macOS resolves names through another service (no packet capture here; what could
  leave is no more than the session already reads).
