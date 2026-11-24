# Long-running services follow the platform's code

> brunna session · 2026-11-24 · r2 (after a blind review: work in flight across the exit, flags a dead process leaves,
> land's steps after the move, the start's code rather than a later import, dynamic loads, the manager's pid). Follows the
> 11-24 incident below; replaces the `code_stamp` restart that lease-runner and cap-runner have had since 10-25
> (`known-issues.md`, "A long-running service runs the code loaded when it started").

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

Why the commit and not the files' times: git writes the new tree first and moves the ref last, for `merge --ff-only`
(merge-branch, upgrade on main), `checkout --detach` (upgrade to a tag) and `checkout -B`. Once HEAD names the new commit
every file is in place, and the commit covers everything it holds (`modules/`, `contracts/`, templates, the platform's own
registry entry), on macOS and Linux alike.

**The services move when the commit is landed, not when HEAD moves.** What follows the move must be done before new code
starts: `brunna land` moves what sandboxed code reads out of `.state`, rebuilds the index and applies the config; upgrade
runs `setup-python` (new packages) and `config migrate` first. So `land` ends by writing the commit to
`<state>/code-landed` (`hubconf.mark_landed`) when every step passed, and a service follows a commit only once it is
marked there (merge-branch runs land after its fast-forward; upgrade runs land after setup-python and the config steps).
The mark is the land process's own commit (`LOADED_REV`), so a checkout moved again during a land isn't marked by it, and
a linked worktree never marks. A checkout moved some other way (a manual `git
pull`, a land that failed) is followed after `LAND_GRACE_S` (10 minutes) with no land. HEAD itself stays the test for D2:
old code must not write even before the land is done.

Not a git checkout (an exported test tree) or a HEAD it can't read (a reftable repo): `code_rev()` is None; the services
fall back to `code_stamp()` with the 60-second settle as before, and the index guard (D2) is off.

### D2. Old code never writes the live index

`hubconf` notes `code_rev()` when it is imported (`LOADED_REV`): every platform process imports it first, so it is the
code the process started with, even for a process that imports `registry` lazily after a merge. `refresh()` writes the
cache and `index.json` only while the checkout still has that commit, asked again just before each write; otherwise it
validates and returns the index as a linked worktree's refresh does, writes nothing, and says once on stderr that newer
code is in place. The writer is held to it, whoever it is: a service, a job
that started before a merge, a command caught mid-merge. The index is then rewritten by the next process on the current
code: `brunna land` right after the merge, the scheduler's tick, any command.

The `format` guard stays (it also covers a checkout moved back to older code).

The cost for an old process's caller is the index it computes itself, the one it would have written; lease-runner uses it
only for the projects a lease may attach to, and a process on old code is about to exit (D3).

### D3. Each service exits as soon as nothing is in flight

A small shared helper, `bin/code_watch.py`:

- `Watch(name)` takes the process's own `LOADED_REV` (or `code_stamp()`) and writes the service's record,
  `<state>/services/<name>.json` = `{pid, rev, started}`;
- `stale()` says whether the checkout has moved on to a commit that is landed or has waited `LAND_GRACE_S` (D1; the
  settle only on the `code_stamp` fallback);
- `draining(why)` adds to the record when the service first found itself held and what holds it now;
- every check is wrapped: an error while reading git's files is "not stale this time" and is logged, never the end of
  the thread or the service.

**lease-runner.** A watcher thread checks every 2 seconds. When the code has moved on (and is landed), under the
runner's lock, it stays while anything is in flight, judged by what is alive, never by a flag that could outlive it:

- a POST being handled, counted from its first line to its reply (`in_flight`). Without it a message could be taken
  (its `client_msg_id` recorded and saved) and the runner exit before writing it to the session: the resend would be
  answered "duplicate" and the message lost. It also covers a rename or an upgrade's subprocesses, which the service
  manager would kill with the runner;
- a handover, counted from before its thread starts (`spawn_detach`; the handler that starts it returns at once), and
  the `hubbus bridge` it starts until that ends (systemd ends what is left in the unit's group when it restarts);
- a lease process that is alive and in a turn or waiting for the owner's answer. `waiting` is cleared whenever its
  process stops or dies; a lease left `resuming` by a start that raised, or `detaching` by a runner that died mid-handover,
  holds nothing (those states are what they were before; how a cut handover is finished is not this change's).

When nothing is, the process of every lease between turns is stopped as an idle stop does it (state `idle_stopped`; the
next message resumes it with `--resume`, which the idle stop and the mode switch already exercise), the leases are
saved, and the runner exits; the service manager starts it on the new code (`throttle_s: 10`; systemd's `RestartSec` makes
it 10 s every time, launchd only when the job ran less than that). While held, the record says what holds it (D5).

`stop_proc` runs under the lock and may hold requests for up to 25 s per lease (15 s for the process to end on its stdin
closing, 10 more after a terminate), as the mode switch and quiesce already do; in practice the agent exits at once.

A message that reaches a draining runner is handled as now: it may start a turn, which the drain then waits for. With one
owner talking to one lease at a time the drain is the turn in progress; turns overlapping across leases without a gap
would hold it longer, and D5 says so if it ever lasts. Refusing new turns while draining (503) would change the
contract the assistant reads, for a case that doesn't happen. A request in the ten seconds the runner is down fails to
connect, as during every restart today.

**cap-runner.** Its loop checks every second (as now) and exits once nothing is running or queued (as now). It keeps
taking and starting requests while it drains: the client (`hub-run`) withdraws a request nobody picks up within 30
seconds and reports the service as down, so a runner that stopped taking them would fail work rather than delay it; and a
job taken but not started is marked lost by the next runner. Its drain is therefore bounded by the load, and jobs started
during it run with the old code's sandbox profile, as they did during the old settle. Re-adopting jobs taken but not
started (so a draining runner could take without starting) is a follow-up.

**status-page** runs `python -m http.server`: no platform code; nothing to follow.

### D4. A service on old code imports nothing new

Each service imports at start every platform module it otherwise imports lazily (`code_watch.preload(PRELOAD)`), so a
process that is draining is one consistent snapshot of the code it started with: a handover that runs after a merge runs
the old handover code with the old modules, not a mix. lease-runner also loads its runtime adapter (`rt()`, whichever the
configured runtime is) and `registrar.py`, which it used to load from its file at every handover, once at start. A test
reads each service's source and fails when a function-level import of a platform module is missing from its list.

What this doesn't cover: imports made lazily inside those modules, files read at call time (templates, locales,
modules' YAML), and the platform's programs a service starts as subprocesses (`llm.py`, `hubbus`), which are new code
from the moment the checkout moves (each is a process of its own, consistent in itself). The drain keeps that window to
the work in flight.

### D5. The platform checks that every service follows

- `render-services.py` holds the list of the platform's long-running services and how each follows the code
  (`FOLLOWS_CODE`: lease-runner and cap-runner by their record, status-page `None`), hard-coded like `WAIT_FOR_IDLE`:
  a registry field would be read by the running code during the very merge that adds it (the 11-24 problem). A test
  fails when a `keepalive` service of the platform's entry or a module is missing from it, or when a service listed as
  following doesn't use `code_watch`.
- On each run (every 15 minutes), render-services reads each listed service's record: a record whose pid is the one the
  service manager runs the service as (`launchctl print`'s pid, systemd's `MainPID`; not a bare `kill 0`, which a pid
  reused after a reboot would pass) and whose commit isn't the checkout's is "on old code since" the first run that saw
  it. Past `wait_limit_s()` (two hours beyond the longest agent call, as for a unit change that waits) it is a problem,
  alerted like the others and shown red on the status page, naming what the record says holds it (a lease waiting hours
  for the owner's answer reads as that, not as a broken service). The words stay the same on every run while it lasts
  (when it was first seen, never a count of hours, and what holds it by kind, never lease ids): `alert.once` takes a
  changed wording for a new alert. A missing record is not a problem by itself: a
  service started before this change writes none until its own restart. A module turned off has no services in the
  index, so its old record is never read.
- render-services does not restart a service for its code. Only the service knows what is in flight at the moment it
  decides (a message can start a turn between an outside check and an outside kill); the outside's part is to say it
  when that doesn't happen.

## Landing this change

The runners that are running when this merges still have the old mechanism: they move to the new code by it (bin/'s
times, the 60-second settle), and in that minute they can still write the index once more without the guard. A
`registry` they import for the first time in that minute finds no `LOADED_REV` in their old `hubconf` and treats it as
unknown (no guard), rather than failing.

## Out of scope

- **Processes that live as long as a session**, not services: the Stop hook's waiter (`hub-event wait`, up to 14 days),
  `brunna mcp`, lease-relay. The agent tool starts and ends them; they read and write only that session's files. A format
  change in what they read is the change's own compatibility question (as for any reader of a file).
- **Projects' services** (`~/services/<project>`, `bin/deploy.sh`): each project deploys its own.
- The scheduler and the watchdog run once per tick.

## Steps

1. `hubconf.code_rev()`, with tests for a main checkout, a linked worktree, a packed ref and a detached HEAD;
   `LOADED_REV`, `mark_landed`/`landed_rev`, land marking it.
2. The 11-24 reproduction as a test first: a process holding the old validation, a module using a field it doesn't
   know, the checkout moved on; `refresh()` must leave `index.json` as it was. Then D2.
3. `bin/code_watch.py`; lease-runner (D3, D4) with tests: a stale runner with a lease between turns stops it and exits;
   with a lease in a turn or waiting it waits, and exits once the turn ends; an error in the check doesn't end the thread.
4. cap-runner on `code_watch` (D3, D4).
5. render-services' `FOLLOWS_CODE`, the record check, the coverage tests (D5).
6. known-issues.md, `brunna land`'s header, AGENTS.md if it names the mechanism; direction.md.

## Follow-up (not in this change)

- cap-runner taking without starting while it drains (re-adopting queued jobs and their agent-minute reservations).
- A lease left `detaching` by a runner that died mid-handover stays so (`load()` resets only running, resuming,
  attaching and crashed), and a new detach is refused.
- lease-runner's stderr has a `FileNotFoundError` from `index.json.tmp` → `index.json`: two refreshes in one live index
  share the temporary name, and the second rename finds it gone. Fewer old writers (D2) makes it rarer, not impossible.
