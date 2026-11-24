# Long-running services follow the platform's code

> brunna session · 2026-11-24 · r1. Follows the 11-24 incident below; replaces the `code_stamp` restart that lease-runner
> and cap-runner have had since 10-25 (`known-issues.md`, "A long-running service runs the code loaded when it
> started").

## Why

The main checkout is the running platform: `brunna merge-branch` fast-forwards it, `brunna upgrade` moves it to a
release. Short-lived programs (the scheduler's tick, jobs, the CLI) start on the new code by themselves. A long-running
service does not: it keeps the modules it loaded when it started, and imports others lazily from whatever is on disk
then.

What exists (since 10-25): lease-runner and cap-runner note `hubconf.code_stamp()` (the newest modification time of the
files directly under `bin/`) when they start; once it has changed and stayed the same for 60 seconds, each exits at a
moment it has nothing in flight, and launchd (`KeepAlive`) or systemd (`Restart=always`) starts it again on the new code.
`runner.log` shows it working: the 16:14:13 merge on 11-24 restarted lease-runner at 16:15:15.

What happened on 11-24 in those 62 seconds: the merge brought code that knows a new job field (`found_exit`) together
with three modules using it. `brunna land` rebuilt the registry index with the new code. Then the assistant's request to
the old lease-runner (`GET /v1/policy`) ran `registry.refresh()` with the old validation, which rejected the three
modules and wrote the live index without them, until the scheduler's next tick rewrote it.

The gaps, from that and from reading the code:

1. **A window after every change.** The settle (60 s) plus the idle tick (20 s): old code serves requests for up to 80
   seconds after each merge, and every request may run platform code.
2. **Old code writes live shared state.** The registry index is a projection that every reader trusts; an old process
   that refreshes it overwrites the new code's result. `refresh()` already refuses to overwrite an index of a newer
   *format*, but a new field doesn't change the format. The same is true of any process that started before a merge
   and refreshes after it: a long job, a CLI command caught mid-merge, not only the services.
3. **No bound while a lease is open.** lease-runner counts a lease whose process is alive (`running`) as busy, even between
   turns; it waits until the process has idled 10 minutes, or for as long as the conversation goes on.
4. **The signal misses code.** `code_stamp` sees only the files directly in `bin/`: not `modules/`, `contracts/`,
   `templates/`, `projects/brunna.yaml`, or anything in a subfolder of `bin/`.
5. **Lazy imports mix versions.** A service that imports a module for the first time after a change gets the new
   module next to its old ones (lease 9af483fb's handover failed that way on 10-25).
6. **The check itself is fragile and unwatched.** lease-runner's check runs outside the idle loop's `try`, so one
   failed `stat` mid-checkout ends that thread for good (no more idle stops, no more restarts); cap-runner's is outside its
   `try` too, so it ends the service and its running jobs are marked lost. Nothing says when a service has stayed on old
   code: "new long-running services do the same" is a sentence in known-issues.md, nothing checks it.

## Design

### D1. The code's identity is the checkout's commit

`hubconf.code_rev()`: the commit the hub checkout's HEAD points at, read from git's files (`HEAD`, the ref it names as a
loose ref or in `packed-refs`, through the `.git` file of a linked worktree and its `commondir`), never by running git: it
is cheap enough to read every couple of seconds and on every index write.

Why the commit and not the files' times: git writes the new tree first and moves the ref last, both for `merge
--ff-only` (merge-branch) and for the detached checkout of a tag (upgrade). Once HEAD names the new commit, every file
is in place, so no settle is needed and nothing is restarted onto a half-written tree. It covers everything the commit
holds (`modules/`, `contracts/`, templates, the platform's own registry entry), on macOS and Linux alike.

Not a git checkout (an exported test tree): `code_rev()` is None; the services fall back to `code_stamp()` with the
60-second settle as before, and the index guard (D2) is off (there is nothing to compare).

### D2. Old code never writes the live index

`registry` notes `code_rev()` when it is imported. `refresh()` writes the cache and `index.json` only when the checkout
still has that commit; otherwise it validates and returns the index as a linked worktree's refresh does, writes
nothing, and says once on stderr that newer code is in place. The writer is held to it, whoever it is: a service, a job
that started before a merge, a command caught mid-merge. The index is then rewritten by the next process on the current
code: `brunna land` right after the merge, the scheduler's tick, any command.

The `format` guard stays (it also covers a checkout moved back to older code).

The cost for an old process's caller is the index it computes itself, the one it would have written; lease-runner uses it
only for the projects a lease may attach to, and a process on old code is about to exit (D3).

### D3. Each service exits as soon as nothing is in flight

A small shared helper, `bin/code_watch.py`:

- `Watch(name)` notes `code_rev()` (or `code_stamp()`) at start and writes the service's record,
  `<state>/services/<name>.json` = `{pid, rev, started}`;
- `stale()` says whether the checkout has moved on (with the settle only on the `code_stamp` fallback);
- every check is wrapped: an error while reading git's files is "not stale this time" and is logged, never the end of
  the thread or the service.

**lease-runner.** A watcher thread checks every 2 seconds. When the code has moved on, under the runner's lock:

- a lease that is mid-work keeps the runner alive: in a turn, waiting for the owner's answer to a permission or a
  question, or attaching, resuming or detaching (handing over);
- the process of every other lease (`running` between turns) is stopped as an idle stop does it: state `idle_stopped`,
  and the next message resumes it with `--resume`, which the idle stop and the mode switch already exercise;
- once no lease is mid-work, the runner saves its leases and exits; the service manager starts it on the new code
  (`throttle_s: 10`).

So the drain is bounded by the turn in progress, not by the conversation. A message that reaches the runner while it
drains is handled as now (it may start a turn, which the drain then waits for); a request that arrives in the ten
seconds the runner is down fails to connect, as during today's restarts, and the assistant sends it again
(`client_msg_id` makes a resend safe).

**cap-runner.** Its loop checks every second (as now) and exits once nothing is running or queued (as now). It keeps
taking requests while it drains: a request left in `requests/` is refused as too old after 10 minutes, and a running
agent call may last an hour, so not taking them would refuse work rather than delay it. Its drain is bounded by the load,
which the visibility check below watches.

**status-page** runs `python -m http.server`: no platform code; nothing to follow.

### D4. A service on old code imports nothing new

Each service imports at start every platform module it otherwise imports lazily (`code_watch.preload([...])`), so a
process that is draining is one consistent snapshot of the code it started with: a handover that runs after a merge runs
the old handover code with the old modules, not a mix. A test reads each service's source and fails when a function-level
import of a platform module is missing from its preload list. (Imports made lazily inside those modules are not covered;
the drain keeps that window to the work in flight.)

### D5. The platform checks that every service follows

- `render-services.py` holds the list of the platform's long-running services and how each follows the code
  (`FOLLOWS_CODE`: lease-runner and cap-runner by their record, status-page `None`), hard-coded like `WAIT_FOR_IDLE`:
  a registry field would be read by the running code during the very merge that adds it (the 11-24 problem). A test
  fails when a `keepalive` service of the platform's entry or a module is missing from it, or when a service listed as
  following doesn't use `code_watch`.
- On each run (every 15 minutes, and from `brunna land` through `config apply`), render-services reads each listed
  service's record: a live pid on a commit other than the checkout's is "on old code since" the first run that saw it.
  Past `wait_limit_s()` (two hours beyond the longest agent call, as for a unit change that waits) it is a problem:
  alerted like the others (to brunna first, `alert.route`), shown red on the status page. A missing record is not a
  problem by itself: a service started before this change writes none until its own restart.
- render-services does not restart a service for its code. Only the service knows what is in flight at the moment it
  decides (a message can start a turn between an outside check and an outside kill); the outside's part is to say it
  when that doesn't happen.

## Out of scope

- **Processes that live as long as a session**, not services: the Stop hook's waiter (`hub-event wait`, up to 14 days),
  `brunna mcp`, lease-relay. The agent tool starts and ends them; they read and write only that session's files. A format
  change in what they read is the change's own compatibility question (as for any reader of a file).
- **Projects' services** (`~/services/<project>`, `bin/deploy.sh`): each project deploys its own.
- The scheduler and the watchdog run once per tick.

## Steps

1. `hubconf.code_rev()`, with tests for a main checkout, a linked worktree, a packed ref and a detached HEAD.
2. The 11-24 reproduction as a test first: a process holding the old validation, a module using a field it doesn't
   know, the checkout moved on; `refresh()` must leave `index.json` as it was. Then D2.
3. `bin/code_watch.py`; lease-runner (D3, D4) with tests: a stale runner with a lease between turns stops it and exits;
   with a lease in a turn or waiting it waits, and exits once the turn ends; an error in the check doesn't end the thread.
4. cap-runner on `code_watch` (D3, D4).
5. render-services' `FOLLOWS_CODE`, the record check, the coverage tests (D5).
6. known-issues.md, `brunna land`'s header, AGENTS.md if it names the mechanism; direction.md.

## Follow-up (not in this change)

- lease-runner's stderr has a `FileNotFoundError` from `index.json.tmp` → `index.json`: two refreshes in one live index
  share the temporary name, and the second rename finds it gone. Fewer old writers (D2) makes it rarer, not impossible.
